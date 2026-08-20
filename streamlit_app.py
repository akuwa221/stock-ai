import math
import time as time_module
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta
from io import BytesIO
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from supabase import create_client


# =========================================================
# 基本設定
# =========================================================

st.set_page_config(page_title="株AI", page_icon="📈", layout="centered")
st.title("📈 株AI")

# ページ上部の手動更新ボタン
if st.button("🔄 最新情報に更新", use_container_width=True, key="refresh_top"):
    st.cache_data.clear()
    st.rerun()

JST = ZoneInfo("Asia/Tokyo")
JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "misc/tvdivq0000001vg2-att/data_j.xls"
)
CHECK_TIMES = [time(10, 0), time(11, 0), time(13, 0), time(14, 30), time(15, 45)]


# =========================================================
# 次回自動チェック
# =========================================================

def get_next_check():
    now = datetime.now(JST)
    for day_offset in range(8):
        target_date = now.date() + timedelta(days=day_offset)
        if target_date.weekday() >= 5:
            continue
        for check_time in CHECK_TIMES:
            candidate = datetime.combine(target_date, check_time, tzinfo=JST)
            if candidate > now:
                return candidate
    return None


next_check = get_next_check()
st.success("🔔 自動通知を監視中")

if next_check is not None:
    now_jst = datetime.now(JST)
    if next_check.date() == now_jst.date():
        next_text = "今日 " + next_check.strftime("%H:%M")
    elif next_check.date() == now_jst.date() + timedelta(days=1):
        next_text = "明日 " + next_check.strftime("%H:%M")
    else:
        next_text = next_check.strftime("%m/%d %H:%M")
    st.write(f"次回チェック予定： **{next_text}**")

st.caption("平日 10:00 / 11:00 / 13:00 / 14:30 / 15:45")
st.divider()


# =========================================================
# Supabase
# =========================================================

@st.cache_resource
def get_supabase():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


try:
    supabase = get_supabase()
except Exception as e:
    st.error(f"Supabase接続エラー：{e}")
    st.stop()


# =========================================================
# 共通
# =========================================================

def normalize_ticker(code):
    code = str(code).strip()
    if code.isdigit() and len(code) == 4:
        return f"{code}.T"
    return code


def simple_ticker(code):
    code = str(code).strip()
    if code.endswith(".T"):
        return code[:-2]
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
# Yahoo株価取得
# =========================================================

def retry_history(ticker_code, period, interval, attempts=3, allow_empty=False):
    last_error = None
    for attempt in range(attempts):
        try:
            data = yf.Ticker(ticker_code).history(
                period=period,
                interval=interval,
                auto_adjust=False,
                prepost=False,
                actions=False,
            )
            if not data.empty:
                return data
            last_error = RuntimeError("Yahooから空データが返されました")
        except Exception as e:
            last_error = e

        if attempt < attempts - 1:
            time_module.sleep(1.0 * (attempt + 1))

    if allow_empty:
        return pd.DataFrame()
    raise RuntimeError(f"株価取得失敗：{last_error}")


def retry_fast_price(ticker_code, attempts=3):
    for attempt in range(attempts):
        try:
            price = float(yf.Ticker(ticker_code).fast_info["last_price"])
            if price > 0:
                return price
        except Exception:
            pass
        if attempt < attempts - 1:
            time_module.sleep(0.8 * (attempt + 1))
    return None


def download_daily_fallback(ticker_code):
    try:
        data = yf.download(
            ticker_code,
            period="1y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if data.empty:
            return pd.DataFrame()

        if isinstance(data.columns, pd.MultiIndex):
            try:
                data = data.xs(ticker_code, axis=1, level=-1, drop_level=True)
            except Exception:
                data.columns = data.columns.get_level_values(0)
        return data
    except Exception:
        return pd.DataFrame()


# =========================================================
# 銘柄名
# =========================================================

@st.cache_data(ttl=86400, show_spinner=False)
def get_jpx_name_map():
    try:
        response = requests.get(JPX_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        df = pd.read_excel(BytesIO(response.content), engine="xlrd")
        result = {}
        for _, row in df.iterrows():
            raw_code = str(row.get("コード", "")).strip()
            if raw_code.endswith(".0"):
                raw_code = raw_code[:-2]
            if raw_code.isdigit() and len(raw_code) < 4:
                raw_code = raw_code.zfill(4)
            name = str(row.get("銘柄名", "")).strip()
            if raw_code and name:
                result[raw_code] = name
        return result
    except Exception:
        return {}


@st.cache_data(ttl=86400, show_spinner=False)
def get_company_name(code):
    code = simple_ticker(normalize_ticker(code))
    jpx_names = get_jpx_name_map()
    if code in jpx_names:
        return jpx_names[code]

    try:
        info = yf.Ticker(normalize_ticker(code)).get_info()
        name = info.get("shortName") or info.get("longName")
        if name and str(name).strip() != code:
            return str(name).strip()
    except Exception:
        pass
    return code


# =========================================================
# 日本語ニュース
# =========================================================

@st.cache_data(ttl=900, show_spinner=False)
def get_japanese_news(company_name, code):
    query = quote_plus(f"{company_name} {code} 株")
    url = (
        "https://news.google.com/rss/search?"
        f"q={query}&hl=ja&gl=JP&ceid=JP:ja"
    )
    try:
        response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        news = []
        for node in root.findall(".//item"):
            title = node.findtext("title", default="").strip()
            link = node.findtext("link", default="").strip()
            pub_date = node.findtext("pubDate", default="").strip()
            source_node = node.find("source")
            source = (source_node.text or "").strip() if source_node is not None else ""
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)]
            if title and link:
                news.append({"title": title, "source": source, "date": pub_date, "url": link})
            if len(news) >= 5:
                break
        return news
    except Exception:
        return []


# =========================================================
# 1分足 → 日足
# =========================================================

def make_intraday_daily(intraday):
    if intraday.empty:
        return pd.DataFrame()

    temp = intraday.copy()
    temp.index = to_jst_naive(temp.index)
    temp["TradeDate"] = temp.index.date

    result = temp.groupby("TradeDate").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index))
    return result


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
# 節目の突破・跳ね返され判定
# 修正版：本当に下側から来た時だけ「突破確認」
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
        latest_intraday_date = intraday.index[-1].date()
        today_intraday = intraday[intraday.index.date == latest_intraday_date]

        if len(today_intraday) >= 15:
            recent15 = today_intraday.tail(15)
            older_today = today_intraday.iloc[:-15]

            hold_15min = bool((recent15["Close"] > level).all())

            # 今日の途中で実際に下側にいた、または前日終値が下側だった
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

    # 引け後は「前日終値が下」「当日終値が上」を必須にする
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

    # 前日からすでに上にいるなら「今突破した」とは扱わない
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
        "hold_15min": hold_15min,
        "volume_boost": volume_boost,
        "crossed_from_below": crossed_from_below,
        "distance_pct": distance_pct,
    }


# =========================================================
# 現在チェックすべきキリ番
# saved_breakout_level は過去に正式突破を確認済みの最大節目
# =========================================================

def get_round_level_state(price, daily, intraday, market_open, saved_breakout_level=None):
    step = get_price_step(price)
    upper_level = math.ceil(price / step) * step
    if upper_level <= 0:
        upper_level = step

    previous_level = upper_level - step
    recent_confirmed_level = None

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
            recent_confirmed_level = float(previous_level)
        elif already_saved or previous_state["state"] == "established_above":
            # 以前から上で定着済み。新しい突破通知は出さず次の節目を見る。
            pass
        else:
            # 一瞬上に出ただけならまだ前の節目を監視
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
        "recent_confirmed_level": recent_confirmed_level,
    }


# =========================================================
# 逆指値プロファイル
# 修正版：締めすぎを緩和
# 危険1回目は「注意」相当で様子見、2回連続で危険なら危険用へ
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

    # 危険1回目は急激に締めない
    if danger_streak < 2:
        return 1.00, 0.020, "危険1回目・強い引き締め保留"

    return 0.80, 0.016, "危険2回連続以上"


# =========================================================
# 株価分析
# =========================================================

@st.cache_data(ttl=60, show_spinner=False)
def analyze_stock(code, saved_breakout_level=None, previous_danger_streak=0):
    ticker_code = normalize_ticker(code)

    try:
        daily = retry_history(ticker_code, period="1y", interval="1d", attempts=3)
    except Exception:
        daily = download_daily_fallback(ticker_code)

    if daily.empty:
        raise ValueError("日足データを取得できませんでした")

    daily = daily.copy()
    daily.index = to_jst_naive(daily.index)
    daily = daily.dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" not in daily.columns:
        daily["Volume"] = 0
    if len(daily) < 80:
        raise ValueError("分析用データが不足しています")

    original_daily_last_date = daily.index[-1].date()

    intraday = retry_history(
        ticker_code,
        period="7d",
        interval="1m",
        attempts=2,
        allow_empty=True,
    )
    if not intraday.empty:
        intraday = intraday.copy()
        intraday.index = to_jst_naive(intraday.index)
        intraday = intraday.dropna(subset=["Open", "High", "Low", "Close"])

    fast_price = retry_fast_price(ticker_code, attempts=3)
    intraday_daily = make_intraday_daily(intraday)
    intraday_last_date = intraday_daily.index[-1].date() if not intraday_daily.empty else None

    now = datetime.now(JST)
    today = now.date()
    market_open = (
        now.weekday() < 5
        and (
            time(9, 0) <= now.time() < time(11, 30)
            or time(12, 30) <= now.time() < time(15, 30)
        )
    )

    synthetic = False
    if intraday_last_date is not None and intraday_last_date > original_daily_last_date:
        d = intraday_daily.iloc[-1]
        if market_open and intraday_last_date == today and not intraday.empty:
            close_value = float(intraday["Close"].iloc[-1])
        elif fast_price is not None and fast_price > 0:
            close_value = float(fast_price)
        else:
            close_value = float(d["Close"])

        new_row = pd.DataFrame(
            {
                "Open": [float(d["Open"])],
                "High": [max(float(d["High"]), close_value)],
                "Low": [min(float(d["Low"]), close_value)],
                "Close": [close_value],
                "Volume": [float(d.get("Volume", 0))],
            },
            index=[pd.Timestamp(intraday_last_date)],
        )
        daily = pd.concat([daily, new_row])
        synthetic = True

    daily.index = pd.DatetimeIndex(daily.index)
    if daily.index.tz is not None:
        daily.index = daily.index.tz_localize(None)
    daily = daily[~daily.index.normalize().duplicated(keep="last")].sort_index()

    latest_date = daily.index[-1].date()
    latest_time = None

    if market_open and not intraday.empty and intraday.index[-1].date() == today:
        latest_price = float(intraday["Close"].iloc[-1])
        latest_time = intraday.index[-1]
        price_source = "1分足（場中）"
    elif original_daily_last_date >= latest_date:
        latest_price = float(daily["Close"].iloc[-1])
        price_source = "日足（終値）"
    elif fast_price is not None and fast_price > 0:
        latest_price = float(fast_price)
        daily.loc[daily.index[-1], "Close"] = latest_price
        price_source = "Yahoo最新値"
    else:
        latest_price = float(daily["Close"].iloc[-1])
        price_source = "日足"

    previous_close = float(daily["Close"].iloc[-2])
    change = latest_price - previous_close
    change_pct = change / previous_close * 100

    daily["MA5"] = daily["Close"].rolling(5).mean()
    daily["MA25"] = daily["Close"].rolling(25).mean()
    daily["MA75"] = daily["Close"].rolling(75).mean()

    ma5 = float(daily["MA5"].iloc[-1])
    ma25 = float(daily["MA25"].iloc[-1])
    ma75 = float(daily["MA75"].iloc[-1])

    recent_high = float(daily["High"].tail(20).max())
    recent_low = float(daily["Low"].tail(20).min())
    prior_20_high = float(daily["High"].iloc[:-1].tail(20).max())
    prior_20_low = float(daily["Low"].iloc[:-1].tail(20).min())

    current_volume = float(daily["Volume"].iloc[-1])
    volume_ma20 = float(daily["Volume"].tail(20).mean())
    volume_ratio = current_volume / volume_ma20 if volume_ma20 > 0 else 1

    prev_close_series = daily["Close"].shift(1)
    tr1 = daily["High"] - daily["Low"]
    tr2 = (daily["High"] - prev_close_series).abs()
    tr3 = (daily["Low"] - prev_close_series).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = float(true_range.rolling(14).mean().iloc[-1])

    score = 0
    reasons = []

    if latest_price > ma5:
        score += 1
        reasons.append("○ 株価が5日線より上")
    else:
        score -= 1
        reasons.append("△ 株価が5日線より下")

    if ma5 > ma25:
        score += 1
        reasons.append("○ 5日線が25日線より上")
    else:
        score -= 1
        reasons.append("△ 5日線が25日線より下")

    if latest_price > ma25:
        score += 1
        reasons.append("○ 株価が25日線より上")
    else:
        score -= 1
        reasons.append("△ 株価が25日線より下")

    if ma25 > ma75:
        score += 1
        reasons.append("○ 25日線が75日線より上")
    else:
        score -= 1
        reasons.append("△ 25日線が75日線より下")

    low_distance = (latest_price - recent_low) / latest_price * 100
    if low_distance <= 2:
        score -= 2
        reasons.append("⚠ 20日安値まで2%以内")

    if volume_ratio >= 1.5:
        score += 1
        reasons.append("○ 出来高が20日平均の1.5倍以上")

    round_state = get_round_level_state(
        latest_price,
        daily,
        intraday,
        market_open,
        saved_breakout_level=saved_breakout_level,
    )
    resistance_info = round_state["level_state"]

    if resistance_info["state"] == "strong_resistance":
        score -= 1
        reasons.append(
            f"⚠ {resistance_info['level']:,.0f}円で"
            f"{resistance_info['rejection_count']}回跳ね返され"
        )

    if round_state["recent_confirmed_level"] is not None:
        score += 1
        reasons.append(
            f"○ {round_state['recent_confirmed_level']:,.0f}円を"
            "下側から上抜けて突破確認"
        )

    if score >= 4:
        status = "🟢 強い上昇"
        level = "strong"
        risk_rank = 4
    elif score >= 2:
        status = "🟢 上昇"
        level = "up"
        risk_rank = 3
    elif score >= 0:
        status = "🟡 様子見"
        level = "neutral"
        risk_rank = 2
    elif score >= -2:
        status = "🟠 注意"
        level = "warning"
        risk_rank = 1
    else:
        status = "🔴 危険"
        level = "danger"
        risk_rank = 0

    previous_danger_streak = max(clean_int(previous_danger_streak, 0), 0)
    danger_streak = previous_danger_streak + 1 if risk_rank == 0 else 0

    stop_support = prior_20_low * 0.995
    base_stop = min(stop_support, latest_price - atr14 * 1.5)

    atr_multiplier, minimum_gap_pct, stop_profile_text = get_stop_profile(
        risk_rank,
        danger_streak,
    )

    defensive_atr_stop = latest_price - atr14 * atr_multiplier
    minimum_gap_stop = latest_price * (1 - minimum_gap_pct)
    defensive_stop = min(defensive_atr_stop, minimum_gap_stop)

    if risk_rank == 4:
        stop_candidate = base_stop
    else:
        stop_candidate = max(base_stop, defensive_stop)

    stop_candidate = min(stop_candidate, latest_price * 0.995)

    return {
        "ticker": ticker_code,
        "price": latest_price,
        "previous_close": previous_close,
        "change": change,
        "change_pct": change_pct,
        "latest_date": latest_date,
        "latest_time": latest_time,
        "price_source": price_source,
        "synthetic": synthetic,
        "ma5": ma5,
        "ma25": ma25,
        "ma75": ma75,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "prior_20_high": prior_20_high,
        "prior_20_low": prior_20_low,
        "volume_ratio": volume_ratio,
        "atr14": atr14,
        "base_stop_candidate": base_stop,
        "stop_candidate": stop_candidate,
        "stop_atr_multiplier": atr_multiplier,
        "minimum_gap_pct": minimum_gap_pct,
        "stop_profile_text": stop_profile_text,
        "danger_streak": danger_streak,
        "score": score,
        "status": status,
        "level": level,
        "risk_rank": risk_rank,
        "reasons": reasons,
        "round_state": round_state,
        "daily": daily,
    }


# =========================================================
# 逆指値保存
# =========================================================

def get_stop_data(row_id, saved_stop, saved_initial_stop, candidate):
    candidate = round(float(candidate))
    saved = clean_float(saved_stop)
    initial = clean_float(saved_initial_stop)

    if initial is None:
        initial = float(saved) if saved is not None else float(candidate)

    active = float(candidate) if saved is None else max(saved, float(candidate))

    update_data = {}
    if clean_float(saved_initial_stop) is None:
        update_data["initial_stop_price"] = initial
    if saved is None or active > saved:
        update_data["stop_price"] = active

    if update_data:
        try:
            supabase.table("portfolio").update(update_data).eq("id", row_id).execute()
        except Exception:
            pass

    return active, initial


# =========================================================
# 利確ライン
# =========================================================

def get_take_profit_lines(buy_price, initial_stop, atr14):
    if initial_stop < buy_price:
        risk_per_share = buy_price - initial_stop
    else:
        risk_per_share = max(atr14 * 1.5, buy_price * 0.03)

    tp1 = round(buy_price + risk_per_share * 1.5)
    tp2 = round(buy_price + risk_per_share * 2.0)
    return risk_per_share, float(tp1), float(tp2)


# =========================================================
# 保有・売却判断
# =========================================================

def get_position_judgment(price, buy_price, score, active_stop, stop_gap_pct, tp1, tp2):
    if price <= active_stop:
        return "🔴 売却・損切りを検討", "現在値が逆指値ラインに到達または割れています。"
    if price >= tp2:
        return "🎯 利確②到達", "2Rの利確目安に到達。残りの利確や逆指値引き上げを検討する水準です。"
    if price >= tp1:
        return "🟢 一部利確を検討", "1.5Rの利確目安に到達しています。"
    if score <= -3:
        return "🔴 売却・損切りを検討", "短中期トレンドが弱く、下落警戒を優先する状態です。"
    if score <= -1 or stop_gap_pct <= 2:
        return "🟠 保有継続・強く警戒", "トレンド悪化または逆指値接近のため、防御を優先する状態です。"
    if price < buy_price and score <= 0:
        return "🟡 保有継続・警戒", "買値を下回っているため、上昇確認までは警戒が必要です。"
    return "🟢 保有継続", "現時点では保有継続を優先できるテクニカル状態です。"


# =========================================================
# 上値ルート
# =========================================================

def build_price_route(price, prior_20_high, tp1, tp2, round_state):
    step = round_state["step"]
    active_level = float(round_state["active_level"])
    active_info = round_state["level_state"]

    levels = []

    def add_level(level, label, force=False):
        try:
            level = float(level)
        except Exception:
            return
        if not force and level <= price:
            return
        levels.append({"price": level, "label": label})

    force_active = active_info["state"] == "testing_above"
    add_level(active_level, "心理的節目", force=force_active)
    add_level(active_level + step, "次の心理的節目")
    add_level(prior_20_high, "20日高値")
    add_level(tp1, "利確①")
    add_level(tp2, "利確②")

    levels.sort(key=lambda x: x["price"])
    cluster_distance = max(5, step * 0.10)
    zones = []

    for level in levels:
        if not zones:
            zones.append({"low": level["price"], "high": level["price"], "labels": [level["label"]]})
            continue

        previous = zones[-1]
        if level["price"] - previous["high"] <= cluster_distance:
            previous["high"] = level["price"]
            if level["label"] not in previous["labels"]:
                previous["labels"].append(level["label"])
        else:
            zones.append({"low": level["price"], "high": level["price"], "labels": [level["label"]]})

    return {
        "zones": zones,
        "active_level": active_level,
        "active_info": active_info,
        "recent_confirmed_level": round_state["recent_confirmed_level"],
    }


# =========================================================
# 編集
# =========================================================

def render_edit_controls(row, row_id, key_prefix=""):
    with st.expander("✏️ 保有情報を編集"):
        edit_ticker = st.text_input(
            "銘柄コード",
            value=str(row["ticker"]),
            key=f"{key_prefix}ticker_{row_id}",
        )
        edit_buy = st.number_input(
            "買値",
            min_value=0.0,
            value=float(row["buy_price"]),
            step=1.0,
            key=f"{key_prefix}buy_{row_id}",
        )
        edit_shares = st.number_input(
            "株数",
            min_value=1,
            value=int(row["shares"]),
            step=1,
            key=f"{key_prefix}shares_{row_id}",
        )

        if st.button("変更を保存", key=f"{key_prefix}update_{row_id}"):
            ticker_changed = edit_ticker.strip() != str(row["ticker"]).strip()
            buy_changed = float(edit_buy) != float(row["buy_price"])

            update_data = {
                "ticker": edit_ticker.strip(),
                "buy_price": float(edit_buy),
                "shares": int(edit_shares),
            }

            if ticker_changed or buy_changed:
                update_data.update(
                    {
                        "stop_price": None,
                        "initial_stop_price": None,
                        "tp1_done": False,
                        "tp2_done": False,
                    }
                )

            supabase.table("portfolio").update(update_data).eq("id", row_id).execute()
            st.cache_data.clear()
            st.rerun()

        if st.button("🔄 逆指値・利確ラインを再計算", key=f"{key_prefix}reset_{row_id}"):
            supabase.table("portfolio").update(
                {
                    "stop_price": None,
                    "initial_stop_price": None,
                    "tp1_done": False,
                    "tp2_done": False,
                }
            ).eq("id", row_id).execute()
            st.cache_data.clear()
            st.rerun()

        if st.button("🗑️ この銘柄を削除", key=f"{key_prefix}delete_{row_id}"):
            supabase.table("portfolio").delete().eq("id", row_id).execute()
            st.cache_data.clear()
            st.rerun()


# =========================================================
# DB取得
# =========================================================

try:
    holdings = supabase.table("portfolio").select("*").order("id").execute().data or []
except Exception as e:
    st.error(f"保有株取得エラー：{e}")
    holdings = []

try:
    state_rows = supabase.table("alert_state").select("*").execute().data or []
    alert_states = {str(row["ticker"]): row for row in state_rows}
except Exception:
    alert_states = {}


# =========================================================
# 全銘柄分析
# =========================================================

items = []
total_cost_known = 0.0
total_value_known = 0.0
failed_count = 0

with st.spinner("保有株を分析しています..."):
    for row in holdings:
        raw_code = str(row.get("ticker", "")).strip()
        display_code = simple_ticker(normalize_ticker(raw_code))
        name = get_company_name(raw_code)
        buy_price = float(row.get("buy_price", 0) or 0)
        shares = int(row.get("shares", 0) or 0)
        cost = buy_price * shares

        state = (
            alert_states.get(raw_code)
            or alert_states.get(display_code)
            or alert_states.get(normalize_ticker(raw_code))
            or {}
        )

        saved_breakout_level = clean_float(state.get("breakout_level"))
        previous_danger_streak = clean_int(state.get("danger_streak", 0), 0)

        try:
            analysis = analyze_stock(
                raw_code,
                saved_breakout_level=saved_breakout_level,
                previous_danger_streak=previous_danger_streak,
            )

            current_price = float(analysis["price"])
            value = current_price * shares
            profit = value - cost
            profit_pct = profit / cost * 100 if cost > 0 else 0

            active_stop, initial_stop = get_stop_data(
                row["id"],
                row.get("stop_price"),
                row.get("initial_stop_price"),
                analysis["stop_candidate"],
            )

            stop_gap_yen = current_price - active_stop
            stop_gap_pct = stop_gap_yen / current_price * 100

            risk_per_share, tp1, tp2 = get_take_profit_lines(
                buy_price,
                initial_stop,
                analysis["atr14"],
            )

            tp1_gap = tp1 - current_price
            tp2_gap = tp2 - current_price
            tp1_gap_pct = tp1_gap / current_price * 100
            tp2_gap_pct = tp2_gap / current_price * 100

            judgment, judgment_reason = get_position_judgment(
                current_price,
                buy_price,
                analysis["score"],
                active_stop,
                stop_gap_pct,
                tp1,
                tp2,
            )

            route = build_price_route(
                price=current_price,
                prior_20_high=analysis["prior_20_high"],
                tp1=tp1,
                tp2=tp2,
                round_state=analysis["round_state"],
            )

            total_cost_known += cost
            total_value_known += value

            items.append(
                {
                    "row": row,
                    "code": display_code,
                    "name": name,
                    "fresh": True,
                    "analysis": analysis,
                    "buy_price": buy_price,
                    "shares": shares,
                    "value": value,
                    "profit": profit,
                    "profit_pct": profit_pct,
                    "active_stop": active_stop,
                    "initial_stop": initial_stop,
                    "stop_gap_yen": stop_gap_yen,
                    "stop_gap_pct": stop_gap_pct,
                    "risk_per_share": risk_per_share,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp1_gap": tp1_gap,
                    "tp2_gap": tp2_gap,
                    "tp1_gap_pct": tp1_gap_pct,
                    "tp2_gap_pct": tp2_gap_pct,
                    "judgment": judgment,
                    "judgment_reason": judgment_reason,
                    "route": route,
                }
            )

        except Exception as e:
            failed_count += 1
            last_price = clean_float(state.get("last_price"))
            last_updated = state.get("updated_at")
            risk_rank = clean_int(state.get("risk_rank", -1), -1)

            if last_price is not None and last_price > 0:
                value = last_price * shares
                profit = value - cost
                profit_pct = profit / cost * 100 if cost > 0 else 0
                total_cost_known += cost
                total_value_known += value
                items.append(
                    {
                        "row": row,
                        "code": display_code,
                        "name": name,
                        "fresh": False,
                        "fallback": True,
                        "last_price": last_price,
                        "last_updated": last_updated,
                        "risk_rank": risk_rank,
                        "buy_price": buy_price,
                        "shares": shares,
                        "value": value,
                        "profit": profit,
                        "profit_pct": profit_pct,
                        "error": str(e),
                    }
                )
            else:
                items.append(
                    {
                        "row": row,
                        "code": display_code,
                        "name": name,
                        "fresh": False,
                        "fallback": False,
                        "buy_price": buy_price,
                        "shares": shares,
                        "error": str(e),
                        "risk_rank": -1,
                    }
                )


def sort_key(item):
    if item.get("fresh"):
        return item["analysis"]["risk_rank"]
    return item.get("risk_rank", -1)


items.sort(key=sort_key)

st.caption(f"登録銘柄：{len(holdings)}銘柄 / 表示中：{len(items)}銘柄")


# =========================================================
# サマリー
# =========================================================

if holdings and total_cost_known > 0:
    total_profit = total_value_known - total_cost_known
    total_pct = total_profit / total_cost_known * 100

    st.subheader("💰 保有株サマリー")
    c1, c2 = st.columns(2)
    with c1:
        st.metric("評価額", f"{total_value_known:,.0f}円")
    with c2:
        st.metric("含み損益", f"{total_profit:+,.0f}円")

    if total_profit >= 0:
        st.success(f"合計損益率：+{total_pct:.2f}%")
    else:
        st.error(f"合計損益率：{total_pct:.2f}%")

    if failed_count:
        st.caption(f"※ {failed_count}銘柄は最新取得に失敗しています。")

    st.caption("⚠️ 危険度の高い銘柄から表示")
    st.divider()


# =========================================================
# 保有株表示
# =========================================================

st.subheader("📋 保有株")

if not holdings:
    st.info("まだ保有株がありません")

for item in items:
    row = item["row"]
    row_id = row["id"]
    code = item["code"]
    name = item["name"]

    st.markdown(f"## {code}")
    if name and name != code:
        st.markdown(f"### {name}")

    if not item.get("fresh"):
        if item.get("fallback"):
            st.warning(
                "⚠️ 最新株価を一時的に取得できませんでした。\n\n"
                "前回の自動監視で取得した株価を表示しています。"
            )
            current_price = item["last_price"]
            c1, c2 = st.columns(2)
            with c1:
                st.metric("前回取得値", f"{current_price:,.0f}円")
            with c2:
                st.metric("買値", f"{item['buy_price']:,.0f}円")
            st.write(f"株数： **{item['shares']:,}株**")
            st.write(f"参考評価額： **{item['value']:,.0f}円**")
            if item["profit"] >= 0:
                st.success(f"参考損益： +{item['profit']:,.0f}円 （+{item['profit_pct']:.2f}%）")
            else:
                st.error(f"参考損益： {item['profit']:,.0f}円 （{item['profit_pct']:.2f}%）")
            if item.get("last_updated"):
                st.caption(f"前回データ：{item['last_updated']}")
        else:
            st.error("⚠️ 現在この銘柄の株価を取得できません。\n\n登録情報は消えていません。")
            st.write(f"買値： **{item['buy_price']:,.0f}円**")
            st.write(f"株数： **{item['shares']:,}株**")

        with st.expander("⚠️ 取得エラー詳細"):
            st.code(item["error"])

        render_edit_controls(row, row_id, key_prefix="error_")
        st.divider()
        continue

    a = item["analysis"]

    c1, c2 = st.columns(2)
    with c1:
        st.metric("現在値", f"{a['price']:,.0f}円", f"{a['change']:+,.0f}円")
    with c2:
        st.metric("買値", f"{item['buy_price']:,.0f}円")

    st.write(f"株数： **{item['shares']:,}株**")
    st.write(f"評価額： **{item['value']:,.0f}円**")

    if item["profit"] >= 0:
        st.success(f"含み損益： +{item['profit']:,.0f}円 （+{item['profit_pct']:.2f}%）")
    else:
        st.error(f"含み損益： {item['profit']:,.0f}円 （{item['profit_pct']:.2f}%）")

    # -----------------------------------------------------
    # 保有・売却判断
    # -----------------------------------------------------
    st.markdown("### 🧭 保有・売却判断")
    judgment = item["judgment"]
    if "🔴" in judgment or "損切り" in judgment:
        st.error(judgment)
    elif "🟠" in judgment or "警戒" in judgment:
        st.warning(judgment)
    else:
        st.success(judgment)
    st.caption(item["judgment_reason"])

    # -----------------------------------------------------
    # 逆指値
    # -----------------------------------------------------
    active_stop = item["active_stop"]
    gap_yen = item["stop_gap_yen"]
    gap_pct = item["stop_gap_pct"]

    if gap_yen <= 0:
        st.error(
            "🚨 逆指値ライン到達・割れ\n\n"
            f"逆指値： **{active_stop:,.0f}円**\n\n"
            f"現在値： **{a['price']:,.0f}円**"
        )
    elif gap_pct <= 2:
        st.warning(
            "⚠️ 逆指値ライン接近\n\n"
            f"逆指値： **{active_stop:,.0f}円**\n\n"
            f"あと： **{gap_yen:,.0f}円 （{gap_pct:.2f}%）**"
        )
    else:
        st.info(
            f"🛡️ 逆指値： **{active_stop:,.0f}円**\n\n"
            f"現在値まで： **{gap_yen:,.0f}円 （{gap_pct:.2f}%）**"
        )

    if a["risk_rank"] == 0 and a["danger_streak"] < 2:
        st.warning(
            "🔴 危険判定は現在1回目として扱っています。\n\n"
            "一時的なノイズで逆指値を締めすぎないよう、"
            "強い引き締めは次回も危険だった場合に行います。"
        )
    else:
        st.caption(
            f"現在の逆指値設定：{a['stop_profile_text']} "
            f"（ATR × {a['stop_atr_multiplier']:.2f}）"
        )

    st.caption("一度引き上げた逆指値は、判定が改善しても自動では下がりません。")

    # -----------------------------------------------------
    # 利確
    # -----------------------------------------------------
    st.markdown("### 🎯 利確ライン")
    tp1 = item["tp1"]
    tp2 = item["tp2"]

    if a["price"] >= tp2:
        st.success(f"🎯 利確②到達： **{tp2:,.0f}円**")
    elif a["price"] >= tp1:
        st.success(f"🎯 利確①到達： **{tp1:,.0f}円**")
    else:
        st.write(
            f"利確①： **{tp1:,.0f}円** "
            f"（あと {max(item['tp1_gap'], 0):,.0f}円 / {max(item['tp1_gap_pct'], 0):.2f}%）"
        )
        st.write(
            f"利確②： **{tp2:,.0f}円** "
            f"（あと {max(item['tp2_gap'], 0):,.0f}円 / {max(item['tp2_gap_pct'], 0):.2f}%）"
        )

    st.write(f"📈 20日高値目安： **{a['prior_20_high']:,.0f}円**")
    st.caption("利確①＝初期リスクの1.5倍、利確②＝初期リスクの2倍。")

    # -----------------------------------------------------
    # 上値ルート
    # -----------------------------------------------------
    st.markdown("### 📈 上値ルート")
    route = item["route"]
    active_info = route["active_info"]
    active_level = route["active_level"]
    confirmed_level = route["recent_confirmed_level"]

    if confirmed_level is not None:
        st.success(
            f"✅ **{confirmed_level:,.0f}円を正式突破確認**\n\n"
            "下側から上抜けたうえで、一定時間の維持＋出来高、"
            "または終値＋出来高の条件を満たしています。"
        )

    if active_info["state"] == "testing_above":
        st.warning(
            f"⚠️ **{active_level:,.0f}円より上ですが、まだ正式突破ではありません**\n\n"
            "下側からの上抜け確認と、15分維持＋出来高を確認中です。"
        )
    elif active_info["state"] == "strong_resistance":
        st.error(
            f"🧱 **{active_level:,.0f}円は強い抵抗線**\n\n"
            f"過去5営業日で **{active_info['rejection_count']}回** 跳ね返されています。\n\n"
            "テクニカル判定にもマイナス材料として反映しています。"
        )
    elif active_info["state"] == "resistance":
        st.warning(
            f"⚠️ **{active_level:,.0f}円で{active_info['rejection_count']}回跳ね返されています**\n\n"
            "上値抵抗として意識され始めています。"
        )
    elif active_info["state"] == "approaching":
        st.info(f"👀 **{active_level:,.0f}円に接近中**\n\nここを突破できるかを見る局面です。")

    zones = route["zones"]
    if zones:
        for index, zone in enumerate(zones[:5]):
            low = zone["low"]
            high = zone["high"]
            labels = "・".join(zone["labels"])
            price_text = f"{low:,.0f}円" if abs(high - low) < 1 else f"{low:,.0f}〜{high:,.0f}円"

            if index == 0 and low <= a["price"]:
                st.warning(f"① 突破確認中： **{price_text}** （{labels}）")
                continue

            distance = low - a["price"]
            distance_pct = distance / a["price"] * 100

            if index == 0:
                st.info(
                    "① 次に超えたいライン\n\n"
                    f"**{price_text}**\n\n"
                    f"{labels}\n\n"
                    f"あと **{distance:,.0f}円 （{distance_pct:.2f}%）**"
                )
            elif index == 1:
                st.write(f"② 突破後の目標： **{price_text}** （{labels}）")
            else:
                st.write(f"{index + 1}️⃣ **{price_text}** （{labels}）")

    st.markdown("#### 🧭 今の見方")
    if active_info["state"] == "strong_resistance":
        st.warning(
            f"{active_level:,.0f}円で何度も跳ね返されているため、"
            "遠い利確ラインよりこの抵抗線を突破できるかを優先して見ます。"
        )
    elif active_info["state"] == "testing_above":
        st.warning(
            f"{active_level:,.0f}円より上ですが、下側からの正式な突破確認がまだありません。"
            "ダマシ扱いを避けるため、次の目標へ進んだ扱いにはしていません。"
        )
    elif confirmed_level is not None:
        st.success(f"{confirmed_level:,.0f}円の正式突破を確認。次の上値目標へ進む局面です。")
    elif active_info["state"] == "approaching":
        st.info(f"まず{active_level:,.0f}円を明確に突破できるかを確認します。")
    elif a["level"] in ["strong", "up"]:
        st.success(f"上昇トレンド中。まず{active_level:,.0f}円を目標にし、突破できれば次へ進みます。")
    else:
        st.warning(f"まず{active_level:,.0f}円まで戻せるかを確認。トレンドが弱い間は逆指値管理を優先します。")

    # -----------------------------------------------------
    # テクニカル
    # -----------------------------------------------------
    st.markdown("### 📊 テクニカル判定")
    if a["level"] in ["strong", "up"]:
        st.success(a["status"])
    elif a["level"] in ["neutral", "warning"]:
        st.warning(a["status"])
    else:
        st.error(a["status"])

    # -----------------------------------------------------
    # ニュース
    # -----------------------------------------------------
    with st.expander("📰 関連ニュース"):
        news = get_japanese_news(name, code)
        if not news:
            st.info("関連する日本語ニュースを取得できませんでした。")
        for i, n in enumerate(news, start=1):
            st.markdown(f"### {i}. {n['title']}")
            caption_parts = []
            if n["source"]:
                caption_parts.append(n["source"])
            if n["date"]:
                caption_parts.append(n["date"])
            if caption_parts:
                st.caption(" / ".join(caption_parts))
            st.link_button("記事を開く", n["url"])
            st.divider()

    # -----------------------------------------------------
    # 詳細
    # -----------------------------------------------------
    with st.expander("📊 詳細分析"):
        st.write(f"最新営業日： **{a['latest_date']}**")
        st.write(f"価格データ： **{a['price_source']}**")
        if a["latest_time"] is not None:
            st.write(f"最終データ時刻： **{a['latest_time'].strftime('%H:%M')}**")
        if a["synthetic"]:
            st.warning("日足更新が遅れているため、1分足などから最新日を補完しています。")

        st.write(f"前日比： **{a['change_pct']:+.2f}%**")
        st.write(f"5日線： **{a['ma5']:,.0f}円**")
        st.write(f"25日線： **{a['ma25']:,.0f}円**")
        st.write(f"75日線： **{a['ma75']:,.0f}円**")
        st.write(f"20日高値： **{a['recent_high']:,.0f}円**")
        st.write(f"20日安値： **{a['recent_low']:,.0f}円**")
        st.write(f"出来高20日平均比： **{a['volume_ratio']:.2f}倍**")
        st.write(f"ATR： **{a['atr14']:,.0f}円**")
        st.write(f"判定スコア： **{a['score']}**")

        st.markdown("#### 🛡️ 逆指値詳細")
        st.write(f"初期逆指値： **{item['initial_stop']:,.0f}円**")
        st.write(f"現在保存中： **{active_stop:,.0f}円**")
        st.write(f"通常の広め候補： **{a['base_stop_candidate']:,.0f}円**")
        st.write(f"現在判定を反映した候補： **{a['stop_candidate']:,.0f}円**")
        st.write(f"現在ATR倍率： **{a['stop_atr_multiplier']:.2f}倍**")
        st.write(f"危険判定連続回数： **{a['danger_streak']}回**")
        st.write(f"初期リスク： **{item['risk_per_share']:,.0f}円/株**")

        if a["stop_candidate"] < active_stop:
            st.success("今回の計算候補は保存済み逆指値より低いため、逆指値を下げず維持しています。")

        st.markdown("#### 📈 チャート")
        chart = a["daily"].tail(130)[["Close", "MA5", "MA25", "MA75"]].copy()
        chart.columns = ["株価", "5日線", "25日線", "75日線"]
        st.line_chart(chart)

        st.markdown("#### 🔍 判定理由")
        for reason in a["reasons"]:
            st.write(reason)

    render_edit_controls(row, row_id)
    st.divider()


# =========================================================
# 新規追加
# =========================================================

st.subheader("➕ 保有株を追加")
new_ticker = st.text_input("銘柄コード", placeholder="例：7203", key="new_ticker")
new_buy = st.number_input("買値", min_value=0.0, step=1.0, key="new_buy")
new_shares = st.number_input("株数", min_value=1, value=100, step=1, key="new_shares")

if st.button("保存する", type="primary"):
    if not new_ticker.strip():
        st.warning("銘柄コードを入力してください")
    elif new_buy <= 0:
        st.warning("買値を入力してください")
    else:
        supabase.table("portfolio").insert(
            {
                "ticker": new_ticker.strip(),
                "buy_price": float(new_buy),
                "shares": int(new_shares),
                "stop_price": None,
                "initial_stop_price": None,
                "tp1_done": False,
                "tp2_done": False,
            }
        ).execute()
        st.cache_data.clear()
        st.rerun()


# =========================================================
# 手動更新
# =========================================================

st.divider()
if st.button("🔄 最新情報に更新", use_container_width=True, key="refresh_bottom"):
    st.cache_data.clear()
    st.rerun()

st.caption(
    "保有・売却判断、逆指値、利確ライン、抵抗線・突破判定は"
    "テクニカル指標による参考情報です。実際の注文前には証券会社の価格も確認してください。"
)
