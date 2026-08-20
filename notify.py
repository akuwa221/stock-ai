import math
import os
from datetime import datetime, time, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from supabase import create_client


JST = ZoneInfo("Asia/Tokyo")
JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "misc/tvdivq0000001vg2-att/data_j.xls"
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================================================
# 共通
# =========================================================

def normalize_ticker(code):
    code = str(code).strip()
    if code.isdigit() and len(code) == 4:
        return f"{code}.T"
    return code


def to_jst_naive(index):
    index = pd.DatetimeIndex(index)
    if index.tz is None:
        return index
    return index.tz_convert("Asia/Tokyo").tz_localize(None)


def clean_float(value):
    try:
        value = float(value)
        if pd.isna(value):
            return None
        return value
    except Exception:
        return None


def clean_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# =========================================================
# JPX銘柄名
# =========================================================

def get_jpx_names():
    try:
        response = requests.get(JPX_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        df = pd.read_excel(BytesIO(response.content), engine="xlrd")
        result = {}
        for _, row in df.iterrows():
            code = str(row.get("コード", "")).strip()
            if code.endswith(".0"):
                code = code[:-2]
            if code.isdigit() and len(code) < 4:
                code = code.zfill(4)
            name = str(row.get("銘柄名", "")).strip()
            if code and name:
                result[code] = name
        return result
    except Exception:
        return {}


JPX_NAMES = get_jpx_names()


def company_name(code):
    code = str(code).strip()
    if code in JPX_NAMES:
        return JPX_NAMES[code]

    try:
        info = yf.Ticker(normalize_ticker(code)).get_info()
        return info.get("shortName") or info.get("longName") or code
    except Exception:
        return code


# =========================================================
# 心理的節目
# =========================================================

def get_price_step(price):
    if price < 500:
        return 10
    if price < 1000:
        return 25
    if price < 5000:
        return 100
    if price < 10000:
        return 250
    if price < 30000:
        return 500
    return 1000


def previous_session_close(daily):
    if len(daily) < 2:
        return None
    return float(daily["Close"].iloc[-2])


# =========================================================
# 節目判定
# 本当に下側から来た時だけ正式突破
# =========================================================

def inspect_price_level(level, price, daily, intraday, market_open, step):
    touch_margin = max(step * 0.03, level * 0.001)

    recent_daily = daily.tail(5).copy()
    rejection_mask = (
        (recent_daily["High"] >= level - touch_margin)
        & (recent_daily["Close"] < level)
    )
    rejection_count = int(rejection_mask.sum())

    prev_session_close = previous_session_close(daily)

    hold_15min = False
    volume_boost = False
    crossed_from_below = False
    intraday_confirmed = False

    if not intraday.empty:
        latest_date = intraday.index[-1].date()
        today_intraday = intraday[intraday.index.date == latest_date]

        if len(today_intraday) >= 15:
            recent15 = today_intraday.tail(15)
            older_today = today_intraday.iloc[:-15]

            hold_15min = bool((recent15["Close"] > level).all())

            crossed_today = bool((older_today["Close"] <= level).any()) if not older_today.empty else False
            gap_from_below = bool(prev_session_close is not None and prev_session_close <= level)
            crossed_from_below = crossed_today or gap_from_below

            older_volume = older_today.tail(60)
            if len(older_volume) >= 10:
                old_volume = float(older_volume["Volume"].mean())
                new_volume = float(recent15["Volume"].mean())
                if old_volume > 0:
                    volume_boost = new_volume >= old_volume * 1.15

            intraday_confirmed = (
                crossed_from_below
                and price >= level * 1.001
                and hold_15min
                and volume_boost
            )

    close_confirmed = False
    if not market_open and prev_session_close is not None:
        latest = daily.iloc[-1]
        avg_volume = float(daily["Volume"].iloc[:-1].tail(20).mean())
        close_confirmed = (
            prev_session_close <= level
            and float(latest["Close"]) >= level * 1.002
            and avg_volume > 0
            and float(latest["Volume"]) >= avg_volume * 1.10
        )
        if close_confirmed:
            crossed_from_below = True

    confirmed = intraday_confirmed or close_confirmed
    distance_pct = (level - price) / price * 100

    established_above = bool(
        price > level
        and prev_session_close is not None
        and prev_session_close > level
    )

    if confirmed:
        state = "confirmed"
    elif established_above:
        state = "established_above"
    elif price > level:
        state = "testing_above"
    elif rejection_count >= 3:
        state = "strong_resistance"
    elif rejection_count >= 2:
        state = "resistance"
    elif 0 <= distance_pct <= 1:
        state = "approaching"
    else:
        state = "below"

    return {
        "level": float(level),
        "state": state,
        "confirmed": confirmed,
        "rejection_count": rejection_count,
        "crossed_from_below": crossed_from_below,
    }


def get_round_level_state(price, daily, intraday, market_open, saved_breakout_level=None):
    step = get_price_step(price)
    upper_level = math.ceil(price / step) * step
    if upper_level <= 0:
        upper_level = step

    previous_level = upper_level - step
    recent_confirmed = None
    saved_breakout_level = clean_float(saved_breakout_level)

    if previous_level > 0 and price > previous_level:
        previous_state = inspect_price_level(
            previous_level,
            price,
            daily,
            intraday,
            market_open,
            step,
        )

        already_saved = bool(
            saved_breakout_level is not None
            and saved_breakout_level >= previous_level
        )

        if previous_state["confirmed"]:
            recent_confirmed = float(previous_level)
        elif already_saved or previous_state["state"] == "established_above":
            pass
        else:
            return {
                "step": step,
                "active_level": float(previous_level),
                "level_state": previous_state,
                "recent_confirmed_level": None,
            }

    active_state = inspect_price_level(
        upper_level,
        price,
        daily,
        intraday,
        market_open,
        step,
    )

    return {
        "step": step,
        "active_level": float(upper_level),
        "level_state": active_state,
        "recent_confirmed_level": recent_confirmed,
    }


# =========================================================
# 逆指値プロファイル
# =========================================================

def get_stop_profile(risk_rank, danger_streak):
    if risk_rank == 4:
        return 1.50, 0.030, "強い上昇"
    if risk_rank == 3:
        return 1.35, 0.028, "上昇"
    if risk_rank == 2:
        return 1.20, 0.025, "様子見"
    if risk_rank == 1:
        return 1.00, 0.020, "注意"
    if danger_streak < 2:
        return 1.00, 0.020, "危険1回目・強い引き締め保留"
    return 0.80, 0.016, "危険2回連続以上"


# =========================================================
# 利確
# =========================================================

def get_take_profit_lines(buy_price, initial_stop, atr14):
    if initial_stop < buy_price:
        risk = buy_price - initial_stop
    else:
        risk = max(atr14 * 1.5, buy_price * 0.03)

    tp1 = round(buy_price + risk * 1.5)
    tp2 = round(buy_price + risk * 2.0)
    return risk, float(tp1), float(tp2)


# =========================================================
# 株チェック
# =========================================================

def check_stock(row, previous_state=None):
    code = str(row["ticker"]).strip()
    ticker_code = normalize_ticker(code)
    stock = yf.Ticker(ticker_code)

    daily = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
        actions=False,
    )
    daily = daily.dropna(subset=["Open", "High", "Low", "Close"])
    if len(daily) < 80:
        return None

    daily = daily.copy()
    daily.index = to_jst_naive(daily.index)
    if "Volume" not in daily.columns:
        daily["Volume"] = 0

    try:
        intraday = stock.history(
            period="5d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
            actions=False,
        )
    except Exception:
        intraday = pd.DataFrame()

    if not intraday.empty:
        intraday = intraday.dropna(subset=["Open", "High", "Low", "Close"])
        intraday.index = to_jst_naive(intraday.index)

    try:
        fast_price = float(stock.fast_info["last_price"])
    except Exception:
        fast_price = None

    now = datetime.now(JST)
    market_open = (
        now.weekday() < 5
        and (
            time(9, 0) <= now.time() < time(11, 30)
            or time(12, 30) <= now.time() < time(15, 30)
        )
    )

    if market_open and not intraday.empty:
        price = float(intraday["Close"].iloc[-1])
    elif fast_price is not None and fast_price > 0:
        price = fast_price
    else:
        price = float(daily["Close"].iloc[-1])

    daily.loc[daily.index[-1], "Close"] = price

    previous_close = float(daily["Close"].iloc[-2])
    change_pct = (price - previous_close) / previous_close * 100

    closes = daily["Close"]
    ma5 = float(closes.rolling(5).mean().iloc[-1])
    ma25 = float(closes.rolling(25).mean().iloc[-1])
    ma75 = float(closes.rolling(75).mean().iloc[-1])

    recent_low = float(daily["Low"].tail(20).min())
    prior_20_low = float(daily["Low"].iloc[:-1].tail(20).min())

    previous_series = daily["Close"].shift(1)
    tr1 = daily["High"] - daily["Low"]
    tr2 = (daily["High"] - previous_series).abs()
    tr3 = (daily["Low"] - previous_series).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = float(true_range.rolling(14).mean().iloc[-1])

    current_volume = float(daily["Volume"].iloc[-1])
    volume_ma20 = float(daily["Volume"].tail(20).mean())
    volume_ratio = current_volume / volume_ma20 if volume_ma20 > 0 else 1

    score = 0
    score += 1 if price > ma5 else -1
    score += 1 if ma5 > ma25 else -1
    score += 1 if price > ma25 else -1
    score += 1 if ma25 > ma75 else -1

    low_distance = (price - recent_low) / price * 100
    if low_distance <= 2:
        score -= 2
    if volume_ratio >= 1.5:
        score += 1

    previous_state = previous_state or {}
    saved_breakout_level = clean_float(previous_state.get("breakout_level"))

    round_state = get_round_level_state(
        price,
        daily,
        intraday,
        market_open,
        saved_breakout_level=saved_breakout_level,
    )
    active_info = round_state["level_state"]

    if active_info["state"] == "strong_resistance":
        score -= 1
    if round_state["recent_confirmed_level"] is not None:
        score += 1

    if score >= 4:
        risk_rank = 4
        status = "🟢 強い上昇"
    elif score >= 2:
        risk_rank = 3
        status = "🟢 上昇"
    elif score >= 0:
        risk_rank = 2
        status = "🟡 様子見"
    elif score >= -2:
        risk_rank = 1
        status = "🟠 注意"
    else:
        risk_rank = 0
        status = "🔴 危険"

    previous_danger_streak = max(clean_int(previous_state.get("danger_streak", 0), 0), 0)
    danger_streak = previous_danger_streak + 1 if risk_rank == 0 else 0

    stop_support = prior_20_low * 0.995
    base_stop = min(stop_support, price - atr14 * 1.5)

    atr_multiplier, minimum_gap_pct, stop_profile_text = get_stop_profile(
        risk_rank,
        danger_streak,
    )

    defensive_atr_stop = price - atr14 * atr_multiplier
    minimum_gap_stop = price * (1 - minimum_gap_pct)
    defensive_stop = min(defensive_atr_stop, minimum_gap_stop)

    stop_candidate = base_stop if risk_rank == 4 else max(base_stop, defensive_stop)
    stop_candidate = min(stop_candidate, price * 0.995)
    stop_candidate = float(round(stop_candidate))

    saved_stop = clean_float(row.get("stop_price"))
    initial_stop = clean_float(row.get("initial_stop_price"))

    if initial_stop is None:
        initial_stop = float(saved_stop) if saved_stop is not None else float(stop_candidate)

    active_stop = float(stop_candidate) if saved_stop is None else max(saved_stop, float(stop_candidate))

    portfolio_update = {}
    if clean_float(row.get("initial_stop_price")) is None:
        portfolio_update["initial_stop_price"] = initial_stop
    if saved_stop is None or active_stop > saved_stop:
        portfolio_update["stop_price"] = active_stop

    if portfolio_update:
        supabase.table("portfolio").update(portfolio_update).eq("id", row["id"]).execute()

    stop_distance = (price - active_stop) / price * 100

    buy_price = float(row["buy_price"])
    risk, tp1, tp2 = get_take_profit_lines(buy_price, initial_stop, atr14)

    sudden_drop = change_pct <= -3
    stop_near = stop_distance <= 2
    stop_breached = price <= active_stop
    breakdown = price < prior_20_low

    confirmed_level = round_state["recent_confirmed_level"]

    resistance_level = None
    resistance_count = 0
    if active_info["state"] in ["resistance", "strong_resistance"]:
        resistance_level = float(active_info["level"])
        resistance_count = int(active_info["rejection_count"])

    return {
        "ticker": code,
        "name": company_name(code),
        "price": price,
        "change_pct": change_pct,
        "score": score,
        "risk_rank": risk_rank,
        "status": status,
        "danger_streak": danger_streak,
        "stop_profile_text": stop_profile_text,
        "prior_20_low": prior_20_low,
        "stop_price": active_stop,
        "stop_distance": stop_distance,
        "stop_near": stop_near,
        "stop_breached": stop_breached,
        "sudden_drop": sudden_drop,
        "breakdown": breakdown,
        "risk": risk,
        "tp1": tp1,
        "tp2": tp2,
        "confirmed_level": confirmed_level,
        "resistance_level": resistance_level,
        "resistance_count": resistance_count,
    }


# =========================================================
# DB取得
# =========================================================

holdings = supabase.table("portfolio").select("*").execute().data or []
state_rows = supabase.table("alert_state").select("*").execute().data or []
states = {str(s["ticker"]): s for s in state_rows}

alerts = []


# =========================================================
# 全銘柄チェック
# =========================================================

for row in holdings:
    code = str(row.get("ticker", "")).strip()
    if not code:
        continue

    previous = states.get(code) or states.get(normalize_ticker(code)) or {}

    try:
        current = check_stock(row, previous_state=previous)
    except Exception as e:
        print(f"{code}: {e}")
        continue

    if current is None:
        continue

    reasons = []

    # 判定悪化
    if previous:
        previous_rank = clean_int(previous.get("risk_rank", 4), 4)
        if current["risk_rank"] < previous_rank:
            reasons.append(f"判定悪化 → {current['status']}")

    # 危険が2回連続になった瞬間
    previous_danger_streak = clean_int(previous.get("danger_streak", 0), 0)
    if current["danger_streak"] >= 2 and previous_danger_streak < 2:
        reasons.append("🔴 危険判定が2回連続 → 逆指値を本格的に引き締め")

    # 急落
    previous_drop = bool(previous.get("sudden_drop", False)) if previous else False
    if current["sudden_drop"] and not previous_drop:
        reasons.append(f"前日比 {current['change_pct']:.2f}%")

    # 逆指値接近
    previous_stop_near = bool(previous.get("stop_near", False)) if previous else False
    if current["stop_near"] and not previous_stop_near:
        reasons.append("逆指値まで2%以内")

    # 逆指値到達
    previous_breached = bool(previous.get("stop_breached", False)) if previous else False
    if current["stop_breached"] and not previous_breached:
        reasons.append("🚨 逆指値ライン到達・割れ")

    # 20日安値割れ
    previous_breakdown = bool(previous.get("breakdown", False)) if previous else False
    if current["breakdown"] and not previous_breakdown:
        reasons.append("20日安値を割りました")

    # 正式突破
    previous_breakout_level = clean_float(previous.get("breakout_level")) if previous else None
    confirmed_level = current["confirmed_level"]

    if confirmed_level is not None:
        if previous_breakout_level is None or confirmed_level > previous_breakout_level:
            reasons.append(f"✅ {confirmed_level:,.0f}円を下側から正式突破")

    # 強い抵抗線
    previous_resistance_level = clean_float(previous.get("resistance_level")) if previous else None
    previous_resistance_count = clean_int(previous.get("resistance_count", 0), 0) if previous else 0

    if current["resistance_level"] is not None and current["resistance_count"] >= 3:
        if (
            previous_resistance_level != current["resistance_level"]
            or previous_resistance_count < 3
        ):
            reasons.append(
                f"🧱 {current['resistance_level']:,.0f}円で"
                f"{current['resistance_count']}回跳ね返り"
            )

    # 利確
    tp1_done = bool(row.get("tp1_done", False))
    tp2_done = bool(row.get("tp2_done", False))
    target_update = {}

    if current["price"] >= current["tp2"] and not tp2_done:
        reasons.append(f"🎯 利確②到達 {current['tp2']:,.0f}円")
        target_update["tp2_done"] = True
        target_update["tp1_done"] = True
    elif current["price"] >= current["tp1"] and not tp1_done:
        reasons.append(f"🎯 利確①到達 {current['tp1']:,.0f}円")
        target_update["tp1_done"] = True

    if target_update:
        supabase.table("portfolio").update(target_update).eq("id", row["id"]).execute()

    if reasons:
        current["alert_reasons"] = reasons
        alerts.append(current)

    # 状態保存
    stored_breakout_level = previous_breakout_level
    if confirmed_level is not None:
        if stored_breakout_level is None or confirmed_level > stored_breakout_level:
            stored_breakout_level = confirmed_level

    state_data = {
        "ticker": code,
        "risk_rank": current["risk_rank"],
        "score": current["score"],
        "danger_streak": current["danger_streak"],
        "sudden_drop": current["sudden_drop"],
        "stop_near": current["stop_near"],
        "stop_breached": current["stop_breached"],
        "breakdown": current["breakdown"],
        "last_price": current["price"],
        "breakout_level": stored_breakout_level,
        "breakout_confirmed": stored_breakout_level is not None,
        "resistance_level": current["resistance_level"],
        "resistance_count": current["resistance_count"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase.table("alert_state").upsert(state_data, on_conflict="ticker").execute()


# =========================================================
# iPhone通知
# =========================================================

if alerts:
    lines = ["保有株に重要な変化があります。", ""]

    for stock in alerts:
        lines.append(f"{stock['status']} {stock['ticker']} {stock['name']}")
        lines.append(f"現在値 {stock['price']:,.0f}円 ({stock['change_pct']:+.2f}%)")

        for reason in stock["alert_reasons"]:
            lines.append(f"・{reason}")

        lines.append(f"逆指値 {stock['stop_price']:,.0f}円")
        lines.append(f"利確① {stock['tp1']:,.0f}円 / 利確② {stock['tp2']:,.0f}円")
        lines.append("")

    message = "\n".join(lines)

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": "株AI 通知",
            "Priority": "high",
            "Tags": "warning,chart_with_upwards_trend",
        },
        timeout=30,
    )
    response.raise_for_status()
    print("notification sent")
else:
    print("no new alert")
