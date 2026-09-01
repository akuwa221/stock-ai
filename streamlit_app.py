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

# =========================================================
# 上部の手動更新
# =========================================================
if st.button("🔄 最新情報に更新", key="top_refresh", use_container_width=True):
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


def to_jst_timestamp(value):
    """Supabaseの日時をJSTのTimestampへ安全に変換する。"""
    try:
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(ts):
            return pd.NaT
        return ts.tz_convert("Asia/Tokyo")
    except Exception:
        return pd.NaT


def load_account_data():
    """運用成績管理用テーブルを取得する。SQL未適用時はready=False。"""
    try:
        settings_rows = (
            supabase.table("account_settings")
            .select("*")
            .eq("id", 1)
            .execute()
            .data
            or []
        )
        cash_flows = (
            supabase.table("cash_flows")
            .select("*")
            .order("occurred_at", desc=True)
            .execute()
            .data
            or []
        )
        trade_rows = (
            supabase.table("trade_history")
            .select("*")
            .order("sold_at", desc=True)
            .execute()
            .data
            or []
        )
        return {
            "ready": True,
            "settings": settings_rows[0] if settings_rows else {},
            "cash_flows": cash_flows,
            "trades": trade_rows,
            "error": None,
        }
    except Exception as e:
        return {
            "ready": False,
            "settings": {},
            "cash_flows": [],
            "trades": [],
            "error": str(e),
        }


def calculate_account_metrics(holdings, total_market_value, total_cost_known, account_data):
    """
    売買履歴・入金履歴・現在保有から運用成績を計算する。

    現金余力 = 累計入金額 + 累計実現損益 - 現在保有の取得原価
    月間3%目標の基準額 = 初期元本 + 月初より前の追加資金 + 月初より前の実現損益
    （月途中の追加入金は翌月の目標から反映）
    """
    settings = account_data.get("settings") or {}
    initial_capital = clean_float(settings.get("initial_capital")) or 0.0

    cash_flows = account_data.get("cash_flows") or []
    trades = account_data.get("trades") or []

    deposits_total = 0.0
    withdrawals_total = 0.0
    for flow in cash_flows:
        amount = clean_float(flow.get("amount")) or 0.0
        flow_type = str(flow.get("flow_type", "deposit"))
        if flow_type == "withdrawal":
            withdrawals_total += amount
        else:
            deposits_total += amount

    total_realized = sum((clean_float(t.get("realized_profit")) or 0.0) for t in trades)
    current_cost_basis = sum(
        (clean_float(row.get("buy_price")) or 0.0) * clean_int(row.get("shares"), 0)
        for row in holdings
    )

    contributed_capital = initial_capital + deposits_total - withdrawals_total
    cash_available = contributed_capital + total_realized - current_cost_basis
    total_assets = cash_available + float(total_market_value or 0.0)
    unrealized_profit = float(total_market_value or 0.0) - float(total_cost_known or 0.0)

    now = datetime.now(JST)
    month_start = pd.Timestamp(
        datetime(now.year, now.month, 1, tzinfo=JST)
    )

    realized_before_month = 0.0
    realized_this_month = 0.0
    wins_this_month = 0
    losses_this_month = 0
    profit_values = []
    loss_values = []

    for trade in trades:
        profit = clean_float(trade.get("realized_profit")) or 0.0
        sold_at = to_jst_timestamp(trade.get("sold_at"))
        if pd.isna(sold_at):
            continue
        if sold_at < month_start:
            realized_before_month += profit
        else:
            realized_this_month += profit
            if profit > 0:
                wins_this_month += 1
            elif profit < 0:
                losses_this_month += 1

        if profit > 0:
            profit_values.append(profit)
        elif profit < 0:
            loss_values.append(profit)

    deposits_before_month = 0.0
    withdrawals_before_month = 0.0
    for flow in cash_flows:
        amount = clean_float(flow.get("amount")) or 0.0
        occurred_at = to_jst_timestamp(flow.get("occurred_at"))
        if pd.isna(occurred_at) or occurred_at >= month_start:
            continue
        if str(flow.get("flow_type", "deposit")) == "withdrawal":
            withdrawals_before_month += amount
        else:
            deposits_before_month += amount

    monthly_target_base = (
        initial_capital
        + deposits_before_month
        - withdrawals_before_month
        + realized_before_month
    )
    monthly_target_base = max(monthly_target_base, 0.0)
    monthly_target = monthly_target_base * 0.03
    monthly_target_progress = (
        realized_this_month / monthly_target * 100
        if monthly_target > 0
        else 0.0
    )
    monthly_profit_rate = (
        realized_this_month / monthly_target_base * 100
        if monthly_target_base > 0
        else 0.0
    )
    target_remaining = max(monthly_target - realized_this_month, 0.0)

    total_closed = wins_this_month + losses_this_month
    month_win_rate = wins_this_month / total_closed * 100 if total_closed else None

    all_nonzero = [
        clean_float(t.get("realized_profit")) or 0.0
        for t in trades
        if (clean_float(t.get("realized_profit")) or 0.0) != 0
    ]
    all_wins = [v for v in all_nonzero if v > 0]
    all_losses = [v for v in all_nonzero if v < 0]
    all_win_rate = len(all_wins) / len(all_nonzero) * 100 if all_nonzero else None

    return {
        "initial_capital": initial_capital,
        "deposits_total": deposits_total,
        "withdrawals_total": withdrawals_total,
        "contributed_capital": contributed_capital,
        "total_realized": total_realized,
        "current_cost_basis": current_cost_basis,
        "cash_available": cash_available,
        "total_assets": total_assets,
        "unrealized_profit": unrealized_profit,
        "realized_this_month": realized_this_month,
        "monthly_target_base": monthly_target_base,
        "monthly_target": monthly_target,
        "monthly_target_progress": monthly_target_progress,
        "monthly_profit_rate": monthly_profit_rate,
        "target_remaining": target_remaining,
        "month_win_rate": month_win_rate,
        "all_win_rate": all_win_rate,
        "avg_profit": sum(all_wins) / len(all_wins) if all_wins else None,
        "avg_loss": sum(all_losses) / len(all_losses) if all_losses else None,
    }


def build_monthly_trade_summary(trades):
    rows = []
    for trade in trades:
        sold_at = to_jst_timestamp(trade.get("sold_at"))
        if pd.isna(sold_at):
            continue
        rows.append(
            {
                "月": sold_at.strftime("%Y-%m"),
                "実現損益": clean_float(trade.get("realized_profit")) or 0.0,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    result = (
        df.groupby("月", as_index=False)
        .agg(売却回数=("実現損益", "size"), 実現損益=("実現損益", "sum"))
        .sort_values("月", ascending=False)
    )
    return result


def build_ticker_trade_summary(trades):
    rows = []
    for trade in trades:
        profit = clean_float(trade.get("realized_profit")) or 0.0
        code = simple_ticker(str(trade.get("ticker", "")))
        name = str(trade.get("company_name") or "").strip()
        label = f"{code} {name}".strip()
        rows.append({"銘柄": label, "実現損益": profit})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    grouped = df.groupby("銘柄", as_index=False).agg(
        売却回数=("実現損益", "size"),
        実現損益=("実現損益", "sum"),
    )
    return grouped.sort_values("実現損益", ascending=False)


def render_account_management(account_data, metrics):
    st.subheader("📒 運用成績")

    if not account_data.get("ready"):
        st.warning(
            "運用成績管理用のSupabaseテーブルがまだありません。"
            "先に付属の `supabase_v12.sql` をSQL Editorで1回実行してください。"
        )
        with st.expander("エラー詳細"):
            st.code(account_data.get("error") or "不明なエラー")
        return

    initial_capital = metrics["initial_capital"]
    if initial_capital <= 0:
        st.warning(
            "最初に現在の運用元本を1回だけ登録してください。"
            "今持っている株の取得額だけでなく、証券口座の現金余力も含めた運用資金総額です。"
        )
        setup_capital = st.number_input(
            "現在の運用元本",
            min_value=0.0,
            step=10000.0,
            value=0.0,
            key="setup_initial_capital",
        )
        if st.button("運用元本を登録", type="primary", key="save_initial_capital"):
            if setup_capital <= 0:
                st.warning("運用元本を入力してください。")
            else:
                supabase.table("account_settings").upsert(
                    {"id": 1, "initial_capital": float(setup_capital)}
                ).execute()
                st.cache_data.clear()
                st.rerun()
        return

    c1, c2 = st.columns(2)
    with c1:
        st.metric("現在の現金余力", f"{metrics['cash_available']:,.0f}円")
    with c2:
        st.metric("現在の総資産", f"{metrics['total_assets']:,.0f}円")

    c3, c4 = st.columns(2)
    with c3:
        st.metric("今月の確定利益", f"{metrics['realized_this_month']:+,.0f}円")
    with c4:
        st.metric("現在の含み損益", f"{metrics['unrealized_profit']:+,.0f}円")

    target = metrics["monthly_target"]
    progress = metrics["monthly_target_progress"]
    if target > 0:
        st.write(
            f"今月の3%目標： **{target:,.0f}円** "
            f"（基準額 {metrics['monthly_target_base']:,.0f}円）"
        )
        st.progress(min(max(progress / 100, 0.0), 1.0))
        if metrics["target_remaining"] > 0:
            st.caption(
                f"達成率 {progress:.1f}% / あと {metrics['target_remaining']:,.0f}円"
            )
        else:
            st.success(f"🎉 月間3%目標を達成しています（達成率 {progress:.1f}%）")
        st.caption(
            f"今月の確定利益率：{metrics['monthly_profit_rate']:+.2f}%"
            "　※月途中の追加入金は翌月の3%目標から反映"
        )

    with st.expander("💰 資金追加・成績詳細"):
        st.markdown("#### 資金を追加")
        add_amount = st.number_input(
            "追加する金額",
            min_value=0.0,
            step=10000.0,
            value=0.0,
            key="capital_add_amount",
        )
        if st.button("資金を追加", type="primary", key="add_capital_button"):
            if add_amount <= 0:
                st.warning("追加する金額を入力してください。")
            else:
                supabase.table("cash_flows").insert(
                    {"flow_type": "deposit", "amount": float(add_amount)}
                ).execute()
                st.success(f"{add_amount:,.0f}円を運用資金に追加しました。")
                st.cache_data.clear()
                st.rerun()

        st.caption(
            "追加資金は利益には含めません。現金余力と総資産には即反映し、"
            "月間3%目標の基準額には翌月から反映します。"
        )

        with st.expander("初期運用元本を修正"):
            corrected_capital = st.number_input(
                "初期運用元本",
                min_value=0.0,
                step=10000.0,
                value=float(initial_capital),
                key="correct_initial_capital",
            )
            if st.button("元本設定を修正", key="correct_initial_capital_button"):
                supabase.table("account_settings").upsert(
                    {"id": 1, "initial_capital": float(corrected_capital)}
                ).execute()
                st.cache_data.clear()
                st.rerun()

        st.markdown("#### 集計")
        s1, s2 = st.columns(2)
        with s1:
            win_rate = metrics["all_win_rate"]
            st.metric("累計勝率", "-" if win_rate is None else f"{win_rate:.1f}%")
            avg_profit = metrics["avg_profit"]
            st.metric("平均利益", "-" if avg_profit is None else f"{avg_profit:+,.0f}円")
        with s2:
            st.metric("累計実現損益", f"{metrics['total_realized']:+,.0f}円")
            avg_loss = metrics["avg_loss"]
            st.metric("平均損失", "-" if avg_loss is None else f"{avg_loss:+,.0f}円")

        trades = account_data.get("trades") or []
        monthly = build_monthly_trade_summary(trades)
        if not monthly.empty:
            st.markdown("#### 月別利益")
            monthly_display = monthly.copy()
            monthly_display["実現損益"] = monthly_display["実現損益"].map(lambda v: f"{v:+,.0f}円")
            st.dataframe(monthly_display, use_container_width=True, hide_index=True)

        by_ticker = build_ticker_trade_summary(trades)
        if not by_ticker.empty:
            st.markdown("#### 銘柄ごとの利益")
            ticker_display = by_ticker.copy()
            ticker_display["実現損益"] = ticker_display["実現損益"].map(lambda v: f"{v:+,.0f}円")
            st.dataframe(ticker_display, use_container_width=True, hide_index=True)

        if trades:
            st.markdown("#### 最近の売買履歴")
            recent_rows = []
            for trade in trades[:20]:
                sold_at = to_jst_timestamp(trade.get("sold_at"))
                recent_rows.append(
                    {
                        "売却日": "" if pd.isna(sold_at) else sold_at.strftime("%Y/%m/%d"),
                        "銘柄": f"{simple_ticker(str(trade.get('ticker', '')))} {str(trade.get('company_name') or '').strip()}".strip(),
                        "買値": clean_float(trade.get("buy_price")) or 0.0,
                        "売値": clean_float(trade.get("sell_price")) or 0.0,
                        "株数": clean_int(trade.get("sold_shares"), 0),
                        "実現損益": clean_float(trade.get("realized_profit")) or 0.0,
                    }
                )
            history_df = pd.DataFrame(recent_rows)
            history_df["買値"] = history_df["買値"].map(lambda v: f"{v:,.0f}円")
            history_df["売値"] = history_df["売値"].map(lambda v: f"{v:,.0f}円")
            history_df["実現損益"] = history_df["実現損益"].map(lambda v: f"{v:+,.0f}円")
            st.dataframe(history_df, use_container_width=True, hide_index=True)


def render_sale_registration(row, row_id, code, name, buy_price, shares, current_price, account_ready):
    """売値と株数だけで売却登録する。DB側RPCで履歴保存と保有株更新を同時実行。"""
    with st.expander("💴 売却登録"):
        if not account_ready:
            st.warning(
                "売却履歴を保存するため、先に `supabase_v12.sql` をSupabaseで実行してください。"
            )
            return

        st.caption("基本操作は「売却価格＋売却株数 → 売却登録」だけです。")
        sell_price = st.number_input(
            "売却価格",
            min_value=0.0,
            value=float(round(current_price)),
            step=1.0,
            key=f"sell_price_{row_id}",
        )
        sell_shares = st.number_input(
            "売却株数",
            min_value=1,
            max_value=max(int(shares), 1),
            value=int(shares),
            step=1,
            key=f"sell_shares_{row_id}",
        )
        estimated_profit = (float(sell_price) - float(buy_price)) * int(sell_shares)
        proceeds = float(sell_price) * int(sell_shares)
        if estimated_profit >= 0:
            st.success(
                f"登録すると実現損益 **+{estimated_profit:,.0f}円** / 売却代金 **{proceeds:,.0f}円**"
            )
        else:
            st.error(
                f"登録すると実現損益 **{estimated_profit:,.0f}円** / 売却代金 **{proceeds:,.0f}円**"
            )

        if st.button("売却登録", type="primary", key=f"register_sale_{row_id}"):
            if sell_price <= 0:
                st.warning("売却価格を入力してください。")
                return
            try:
                response = supabase.rpc(
                    "register_sale",
                    {
                        "p_portfolio_id": int(row_id),
                        "p_sell_price": float(sell_price),
                        "p_sell_shares": int(sell_shares),
                        "p_company_name": str(name or code),
                    },
                ).execute()
                result_rows = response.data or []
                result = result_rows[0] if result_rows else {}
                actual_profit = clean_float(result.get("realized_profit"))
                if actual_profit is None:
                    actual_profit = estimated_profit
                st.success(f"売却を登録しました。実現損益 {actual_profit:+,.0f}円")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"売却登録に失敗しました：{e}")


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

def get_stop_profile(downside_risk):
    # V7: 3年バックテストで高リスク帯の最大下落が明確に大きかったため、
    # 採用配点のリスク帯に合わせて逆指値を段階的に締める。
    # ただし高リスクのサンプルはまだ少ないので締めすぎない。
    if downside_risk <= 1:
        return 1.50, 0.030, "下落継続リスク低"
    if downside_risk <= 3:
        return 1.35, 0.028, "下落継続リスク注意"
    return 1.15, 0.024, "下落継続リスク高"


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

    # 場中は前日終値、引け後は最新終値を「確定終値」として使う。
    # 警戒ラインを終値で割ったかどうかの判定に使用する。
    confirmed_close = (
        previous_close
        if market_open
        else float(daily["Close"].iloc[-1])
    )

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

    # =====================================================
    # 下落継続リスク V7（正式採用モデル）
    # 3年バックテスト結果を反映：
    # ・株価が25日線より下、だけでは0点
    # ・下落日の出来高増を2点
    # ・安値割れ＋急落/出来高、25日線下降＋出来高を追加加点
    # =====================================================
    ma25_5days_ago = float(daily["MA25"].iloc[-6]) if len(daily) >= 6 else ma25
    ma25_slope_pct = (ma25 / ma25_5days_ago - 1) * 100 if ma25_5days_ago else 0.0
    breakdown_confirmed = latest_price < prior_20_low * 0.995
    down_volume = change_pct < 0 and volume_ratio >= 1.30

    downside_risk = 0
    downside_reasons = []

    if breakdown_confirmed:
        downside_risk += 2
        downside_reasons.append("20日安値を0.5%以上割り込み：+2")

    # 25日線より下は、単独では下落予測力がほぼ無かったため0点。
    if latest_price < ma25:
        downside_reasons.append("株価が25日線より下：参考（0点）")

    if ma25_slope_pct < 0:
        downside_risk += 1
        downside_reasons.append("25日線が下降中：+1")

    if change_pct <= -2.0:
        downside_risk += 1
        downside_reasons.append("前日比-2%以上：+1")

    if down_volume:
        downside_risk += 2
        downside_reasons.append("下落日に出来高増：+2")

    # 組み合わせボーナス
    if breakdown_confirmed and change_pct <= -2.0:
        downside_risk += 2
        downside_reasons.append("安値割れ＋前日比-2%以上：追加+2")

    if breakdown_confirmed and down_volume:
        downside_risk += 2
        downside_reasons.append("安値割れ＋下落出来高増：追加+2")

    if ma25_slope_pct < 0 and down_volume:
        downside_risk += 1
        downside_reasons.append("25日線下降＋下落出来高増：追加+1")

    downside_label = downside_band_from_score(downside_risk)

    # 旧V7との互換用。候補モデルはV7で正式採用済み。
    candidate_downside_risk = downside_risk
    candidate_downside_label = downside_label

    stop_support = prior_20_low * 0.995
    base_stop = min(stop_support, latest_price - atr14 * 1.5)

    atr_multiplier, minimum_gap_pct, stop_profile_text = get_stop_profile(
        downside_risk
    )

    defensive_atr_stop = latest_price - atr14 * atr_multiplier
    minimum_gap_stop = latest_price * (1 - minimum_gap_pct)
    defensive_stop = min(defensive_atr_stop, minimum_gap_stop)

    if downside_risk <= 1:
        stop_candidate = base_stop
    else:
        stop_candidate = max(base_stop, defensive_stop)

    stop_candidate = min(stop_candidate, latest_price * 0.995)

    return {
        "ticker": ticker_code,
        "price": latest_price,
        "previous_close": previous_close,
        "confirmed_close": confirmed_close,
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
        "downside_risk": downside_risk,
        "downside_label": downside_label,
        "downside_reasons": downside_reasons,
        "candidate_downside_risk": candidate_downside_risk,
        "candidate_downside_label": candidate_downside_label,
        "breakdown_confirmed": breakdown_confirmed,
        "ma25_slope_pct": ma25_slope_pct,
        "score": score,
        "status": status,
        "level": level,
        "risk_rank": risk_rank,
        "reasons": reasons,
        "round_state": round_state,
        "daily": daily,
    }


# =========================================================
# 2段階防御ライン保存
#
# stop_price       = 警戒ライン
# final_exit_price = 最終撤退ライン
#
# どちらも一度上がったら自動では下げない。
# =========================================================

def get_stop_data(
    row_id,
    saved_stop,
    saved_initial_stop,
    saved_final_exit,
    warning_candidate,
    atr14,
    buy_price,
    current_price,
    confirmed_breakout_level=None,
):
    warning_candidate = round(float(warning_candidate))
    original_warning_candidate = float(warning_candidate)
    saved_warning = clean_float(saved_stop)
    initial = clean_float(saved_initial_stop)
    saved_final = clean_float(saved_final_exit)
    buy_price = float(buy_price)
    current_price = float(current_price)
    breakout_level = clean_float(confirmed_breakout_level)

    # =====================================================
    # 利益保護モード V10
    #
    # 買値より2%以上上の心理的節目を「正式突破」したら、
    # それまでの広い警戒ラインだけに頼らず利益を守る段階へ移行する。
    #
    # 警戒ライン候補：
    #   突破した節目 - max(0.75ATR, 節目の1%)
    # ただし買値より下には置かない。
    #
    # 例）買値3,005円、3,100円正式突破なら、
    # 3,100円の少し下（ATR分の押しを許容）まで警戒ラインを引き上げる。
    # =====================================================
    profit_protection_active = bool(
        breakout_level is not None
        and buy_price > 0
        and breakout_level >= buy_price * 1.02
    )
    profit_warning_candidate = None
    profit_buffer = None

    if profit_protection_active:
        profit_buffer = max(
            float(atr14) * 0.75,
            float(breakout_level) * 0.010,
        )
        raw_profit_warning = max(
            buy_price,
            float(breakout_level) - profit_buffer,
        )
        # 現在値より上に新しいラインを作らない。
        profit_warning_candidate = min(
            raw_profit_warning,
            current_price * 0.995,
        )
        warning_candidate = max(
            warning_candidate,
            round(profit_warning_candidate),
        )

    # 利確計算用の初期ラインは従来通り固定。
    if initial is None:
        initial = (
            float(saved_warning)
            if saved_warning is not None
            else float(original_warning_candidate)
        )

    # 警戒ラインは上方向にだけ追従。
    active_warning = (
        float(warning_candidate)
        if saved_warning is None
        else max(saved_warning, float(warning_candidate))
    )

    # 最終撤退ラインは警戒ラインよりさらに下。
    # 「0.5ATR」か「警戒ラインの1.5%」の大きい方だけ余裕を持たせる。
    final_gap = max(
        float(atr14) * 0.50,
        active_warning * 0.015,
    )
    final_candidate = round(active_warning - final_gap)

    # 利益保護モードでは、最終撤退ラインで損失に戻さない。
    # 警戒ラインから通常は0.5ATR/1.5%の余裕を残すが、
    # その計算値が買値を下回る場合は「買値」を下限にする。
    if profit_protection_active and buy_price > 0:
        final_candidate = max(
            final_candidate,
            round(buy_price),
        )
        # 最終撤退ラインは警戒ラインそのものには重ねない。
        final_candidate = min(
            final_candidate,
            round(active_warning - 1),
        )
    else:
        # 通常モードでは従来どおり、警戒ラインより最低1.5%下に置く。
        final_candidate = min(
            final_candidate,
            round(active_warning * 0.985),
        )

    # 最終撤退ラインも一度上げたら自動では下げない。
    active_final = (
        float(final_candidate)
        if saved_final is None
        else max(saved_final, float(final_candidate))
    )

    # 念のため警戒ライン以上にならないようにする。
    active_final = min(
        active_final,
        active_warning - 1,
    )

    update_data = {}

    if clean_float(saved_initial_stop) is None:
        update_data["initial_stop_price"] = initial

    if saved_warning is None or active_warning > saved_warning:
        update_data["stop_price"] = active_warning

    if saved_final is None or active_final > saved_final:
        update_data["final_exit_price"] = active_final

    if update_data:
        try:
            (
                supabase
                .table("portfolio")
                .update(update_data)
                .eq("id", row_id)
                .execute()
            )
        except Exception:
            pass

    profit_info = {
        "active": profit_protection_active,
        "breakout_level": breakout_level,
        "warning_candidate": profit_warning_candidate,
        "buffer": profit_buffer,
    }

    return active_warning, initial, active_final, profit_info


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

def get_position_judgment(
    price,
    buy_price,
    score,
    warning_line,
    warning_gap_pct,
    final_exit_line,
    tp1,
    tp2,
    downside_risk,
    breakdown_confirmed,
    confirmed_close,
):
    final_breached = price <= final_exit_line
    warning_breached = price <= warning_line
    warning_close_breached = confirmed_close <= warning_line

    # 最終撤退ラインは最優先。
    if final_breached:
        return (
            "🔴 最終撤退ライン到達・売却を強く検討",
            "通常の値動きより深い下落です。最終撤退ラインに到達しているため、保有継続より損失限定を優先する局面です。",
        )

    if price >= tp2:
        return (
            "🎯 利確②到達",
            "2Rの利確目安に到達。残りの利確や防御ライン引き上げを検討する水準です。",
        )

    if price >= tp1:
        return (
            "🟢 一部利確を検討",
            "1.5Rの利確目安に到達しています。",
        )

    # 警戒ラインを終値で割り、下落継続リスクも高い。
    if warning_close_breached and downside_risk >= 4:
        return (
            "🔴 警戒ライン終値割れ・一部撤退を強く検討",
            "警戒ラインを確定終値で下回り、下落継続リスクも高い状態です。最終撤退ラインまで待たず、一部撤退を強く検討する局面です。",
        )

    # 警戒ラインを終値で割り、注意帯以上。
    if warning_close_breached and downside_risk >= 2:
        return (
            "🟠 警戒ライン終値割れ・一部撤退を検討",
            "警戒ラインを確定終値で下回り、下落継続リスクも注意帯です。反発が弱ければ一部撤退を検討します。",
        )

    # 場中に警戒ラインを割っただけなら即売却しない。
    if warning_breached:
        if downside_risk >= 4:
            return (
                "🟠 警戒ライン割れ・強く警戒",
                "場中に警戒ラインを割れています。ただし一瞬の割れだけでは即売却せず、終値と下落継続リスクを確認します。",
            )
        return (
            "🟡 警戒ライン割れ・反発確認",
            "警戒ラインを下回っていますが、下落継続リスクが高くなければ即売却せず、反発または終値での割れを確認します。",
        )

    # ライン未到達でも高リスクなら警戒。
    if downside_risk >= 4 and breakdown_confirmed:
        return (
            "🔴 強く警戒・一部撤退を検討",
            "下落継続リスクが高く、20日安値も明確に割っています。警戒ライン到達前でも一部撤退を検討する局面です。",
        )

    if downside_risk >= 4:
        return (
            "🟠 強く警戒",
            "バックテストで高リスク帯は大きな下落が増えました。警戒ラインと最終撤退ラインを確認しながら保有します。",
        )

    if downside_risk >= 2 or warning_gap_pct <= 2:
        return (
            "🟠 保有継続・警戒",
            "下落継続リスクは注意帯、または警戒ラインが近い状態です。安値割れ・出来高・終値を確認します。",
        )

    if score <= -3:
        return (
            "🟡 トレンド弱い・反発確認",
            "現在のトレンドは弱いですが、過去検証ではこの状態だけでは売却根拠になりにくいため、反発か安値割れを確認します。",
        )

    if price < buy_price and score <= 0:
        return (
            "🟡 保有継続・警戒",
            "買値を下回っているため、上昇確認までは警戒が必要です。",
        )

    return (
        "🟢 保有継続",
        "現時点では保有継続を優先できる状態です。",
    )


# =========================================================
# 3年バックテスト V7
# ・旧トレンド判定だけでなく「下落継続リスク」そのものを検証
# ・同じ状態が続く日を重複カウントせず、状態が変わった初日を1回とする
# ・5日/20日後の終値、途中の最大下落/上昇、逆指値到達まで確認
# =========================================================


def downside_band_from_score(value):
    value = int(value)
    if value >= 4:
        return "🔴 高い"
    if value >= 2:
        return "🟠 注意"
    return "🟢 低め"


@st.cache_data(ttl=3600, show_spinner=False)
def run_signal_backtest(code):
    ticker_code = normalize_ticker(code)

    try:
        daily = retry_history(
            ticker_code,
            period="3y",
            interval="1d",
            attempts=3,
        )
    except Exception:
        daily = yf.download(
            ticker_code,
            period="3y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

    if daily.empty:
        raise ValueError("バックテスト用の株価データを取得できませんでした。")

    if isinstance(daily.columns, pd.MultiIndex):
        try:
            daily = daily.xs(ticker_code, axis=1, level=-1, drop_level=True)
        except Exception:
            daily.columns = daily.columns.get_level_values(0)

    daily = daily.copy()
    daily.index = to_jst_naive(daily.index)
    daily = daily.dropna(subset=["High", "Low", "Close"])

    if "Volume" not in daily.columns:
        daily["Volume"] = 0

    if len(daily) < 140:
        raise ValueError("バックテスト用データが不足しています。")

    # -----------------------------------------------------
    # 指標
    # -----------------------------------------------------
    daily["MA5"] = daily["Close"].rolling(5).mean()
    daily["MA25"] = daily["Close"].rolling(25).mean()
    daily["MA75"] = daily["Close"].rolling(75).mean()
    daily["LOW20"] = daily["Low"].rolling(20).min()
    daily["PRIOR_LOW20"] = daily["Low"].shift(1).rolling(20).min()
    daily["VOL20"] = daily["Volume"].rolling(20).mean()

    prev_close = daily["Close"].shift(1)
    tr1 = daily["High"] - daily["Low"]
    tr2 = (daily["High"] - prev_close).abs()
    tr3 = (daily["Low"] - prev_close).abs()
    daily["ATR14"] = (
        pd.concat([tr1, tr2, tr3], axis=1)
        .max(axis=1)
        .rolling(14)
        .mean()
    )

    # -----------------------------------------------------
    # 旧トレンド判定
    # -----------------------------------------------------
    score = pd.Series(0.0, index=daily.index)
    score += daily["Close"].gt(daily["MA5"]).map({True: 1, False: -1})
    score += daily["MA5"].gt(daily["MA25"]).map({True: 1, False: -1})
    score += daily["Close"].gt(daily["MA25"]).map({True: 1, False: -1})
    score += daily["MA25"].gt(daily["MA75"]).map({True: 1, False: -1})

    low_distance = (daily["Close"] - daily["LOW20"]) / daily["Close"] * 100
    score += low_distance.le(2).astype(int) * -2

    volume_ratio = daily["Volume"] / daily["VOL20"].replace(0, pd.NA)
    score += volume_ratio.ge(1.5).fillna(False).astype(int)
    daily["score"] = score

    def classify(value):
        if value >= 4:
            return "🟢 強い上昇"
        if value >= 2:
            return "🟢 上昇"
        if value >= 0:
            return "🟡 様子見"
        if value >= -2:
            return "🟠 注意"
        return "🔴 危険"

    daily["signal"] = daily["score"].apply(classify)

    # -----------------------------------------------------
    # 新しい下落継続リスク
    # ライブ判定と同じ条件
    # -----------------------------------------------------
    daily["MA25_SLOPE"] = (daily["MA25"] / daily["MA25"].shift(5) - 1) * 100
    daily["CHANGE_PCT"] = daily["Close"].pct_change() * 100
    daily["VOL_RATIO"] = daily["Volume"] / daily["VOL20"].replace(0, pd.NA)
    daily["BREAKDOWN"] = daily["Close"] < daily["PRIOR_LOW20"] * 0.995

    daily["downside_risk"] = 0
    daily.loc[daily["BREAKDOWN"], "downside_risk"] += 2
    daily.loc[daily["Close"] < daily["MA25"], "downside_risk"] += 1
    daily.loc[daily["MA25_SLOPE"] < 0, "downside_risk"] += 1
    daily.loc[daily["CHANGE_PCT"] <= -2.0, "downside_risk"] += 1
    daily.loc[
        (daily["CHANGE_PCT"] < 0) & (daily["VOL_RATIO"] >= 1.30),
        "downside_risk",
    ] += 1

    daily["downside_band"] = daily["downside_risk"].apply(downside_band_from_score)

    # -----------------------------------------------------
    # 候補配点モデル（V7検証用）
    # 25日線より下は0点、出来高増を重くし、
    # 安値割れ＋急落/出来高などの組み合わせを加点
    # -----------------------------------------------------
    candidate_risk = pd.Series(0, index=daily.index, dtype="int64")
    candidate_risk += daily["BREAKDOWN"].fillna(False).astype(int) * 2
    candidate_risk += (daily["MA25_SLOPE"] < 0).fillna(False).astype(int) * 1
    candidate_risk += (daily["CHANGE_PCT"] <= -2.0).fillna(False).astype(int) * 1

    candidate_down_volume = (
        (daily["CHANGE_PCT"] < 0)
        & (daily["VOL_RATIO"] >= 1.30)
    ).fillna(False)
    candidate_risk += candidate_down_volume.astype(int) * 2

    candidate_risk += (
        daily["BREAKDOWN"].fillna(False)
        & (daily["CHANGE_PCT"] <= -2.0).fillna(False)
    ).astype(int) * 2

    candidate_risk += (
        daily["BREAKDOWN"].fillna(False)
        & candidate_down_volume
    ).astype(int) * 2

    candidate_risk += (
        (daily["MA25_SLOPE"] < 0).fillna(False)
        & candidate_down_volume
    ).astype(int) * 1

    daily["candidate_downside_risk"] = candidate_risk
    daily["candidate_downside_band"] = daily["candidate_downside_risk"].apply(
        downside_band_from_score
    )

    # V7では候補配点を正式採用。以下の主バックテストも採用モデルで集計する。
    daily["downside_risk"] = daily["candidate_downside_risk"]
    daily["downside_band"] = daily["candidate_downside_band"]

    # -----------------------------------------------------
    # 下落継続リスクを構成する各条件
    # 後で「どの条件が本当に効いているか」を個別検証する
    # -----------------------------------------------------
    daily["COND_BREAKDOWN"] = daily["BREAKDOWN"].fillna(False).astype(bool)
    daily["COND_BELOW_MA25"] = (daily["Close"] < daily["MA25"]).fillna(False).astype(bool)
    daily["COND_MA25_DOWN"] = (daily["MA25_SLOPE"] < 0).fillna(False).astype(bool)
    daily["COND_DROP_2"] = (daily["CHANGE_PCT"] <= -2.0).fillna(False).astype(bool)
    daily["COND_DOWN_VOLUME"] = (
        (daily["CHANGE_PCT"] < 0) & (daily["VOL_RATIO"] >= 1.30)
    ).fillna(False).astype(bool)

    indicator_ready = daily[
        ["MA75", "LOW20", "PRIOR_LOW20", "ATR14", "MA25_SLOPE"]
    ].notna().all(axis=1)

    # -----------------------------------------------------
    # 全営業日の基準値
    # -----------------------------------------------------
    daily["ret_5"] = (daily["Close"].shift(-5) / daily["Close"] - 1) * 100
    daily["ret_20"] = (daily["Close"].shift(-20) / daily["Close"] - 1) * 100

    # 全営業日ベースの「20日以内にどれだけ下げたか」も計算
    future20_lows = pd.concat(
        [daily["Low"].shift(-i) for i in range(1, 21)],
        axis=1,
    )
    daily["BASE_MAX_DROP_20"] = (
        1 - future20_lows.min(axis=1) / daily["Close"]
    ).clip(lower=0) * 100
    daily["BASE_DROP_5PCT_20"] = daily["BASE_MAX_DROP_20"] >= 5.0

    baseline = daily[
        indicator_ready
        & daily["ret_5"].notna()
        & daily["ret_20"].notna()
    ].copy()

    if baseline.empty:
        raise ValueError("バックテスト可能な期間がありません。")

    # -----------------------------------------------------
    # イベント評価の共通処理
    # -----------------------------------------------------
    def evaluate_event(pos, event_type):
        if pos + 20 >= len(daily):
            return None

        row = daily.iloc[pos]
        entry_price = float(row["Close"])
        future5 = daily.iloc[pos + 1 : pos + 6]
        future20 = daily.iloc[pos + 1 : pos + 21]

        if len(future5) < 5 or len(future20) < 20:
            return None

        ret5 = (float(daily["Close"].iloc[pos + 5]) / entry_price - 1) * 100
        ret20 = (float(daily["Close"].iloc[pos + 20]) / entry_price - 1) * 100

        max_drop_5 = max(
            0.0,
            (1 - float(future5["Low"].min()) / entry_price) * 100,
        )
        max_gain_5 = max(
            0.0,
            (float(future5["High"].max()) / entry_price - 1) * 100,
        )
        max_drop_20 = max(
            0.0,
            (1 - float(future20["Low"].min()) / entry_price) * 100,
        )
        max_gain_20 = max(
            0.0,
            (float(future20["High"].max()) / entry_price - 1) * 100,
        )

        downside_risk = int(row["downside_risk"])
        atr14 = float(row["ATR14"])
        prior_20_low = float(row["PRIOR_LOW20"])

        stop_support = prior_20_low * 0.995
        base_stop = min(stop_support, entry_price - atr14 * 1.5)

        atr_multiplier, minimum_gap_pct, _ = get_stop_profile(downside_risk)
        defensive_atr_stop = entry_price - atr14 * atr_multiplier
        minimum_gap_stop = entry_price * (1 - minimum_gap_pct)
        defensive_stop = min(defensive_atr_stop, minimum_gap_stop)

        if downside_risk <= 1:
            stop_price = base_stop
        else:
            stop_price = max(base_stop, defensive_stop)

        stop_price = min(stop_price, entry_price * 0.995)
        stop_return = (stop_price / entry_price - 1) * 100

        stop_hit_5 = bool((future5["Low"] <= stop_price).any())
        stop_hit_20 = bool((future20["Low"] <= stop_price).any())

        rebound_to_entry = False
        stop_helped_vs_20d_close = False

        if stop_hit_20:
            hit_position = None
            for j in range(len(future20)):
                if float(future20["Low"].iloc[j]) <= stop_price:
                    hit_position = j
                    break

            if hit_position is not None:
                after_hit = future20.iloc[hit_position + 1 :]
                if not after_hit.empty:
                    rebound_to_entry = bool((after_hit["High"] >= entry_price).any())

            stop_helped_vs_20d_close = ret20 < stop_return

        return {
            "date": daily.index[pos],
            "event_type": event_type,
            "signal": row["signal"],
            "downside_risk": downside_risk,
            "downside_band": row["downside_band"],
            "candidate_downside_risk": int(row["candidate_downside_risk"]),
            "candidate_downside_band": row["candidate_downside_band"],
            "entry": entry_price,
            "ret_5": ret5,
            "ret_20": ret20,
            "max_drop_5": max_drop_5,
            "max_gain_5": max_gain_5,
            "max_drop_20": max_drop_20,
            "max_gain_20": max_gain_20,
            "drop_3pct_5": max_drop_5 >= 3.0,
            "drop_5pct_20": max_drop_20 >= 5.0,
            "stop_price": stop_price,
            "stop_hit_5": stop_hit_5,
            "stop_hit_20": stop_hit_20,
            "rebound_to_entry": rebound_to_entry,
            "stop_helped_vs_20d_close": stop_helped_vs_20d_close,
        }

    # -----------------------------------------------------
    # 旧トレンド判定イベント
    # -----------------------------------------------------
    daily["signal_event"] = (
        indicator_ready
        & indicator_ready.shift(1).fillna(False).astype(bool)
        & daily["signal"].ne(daily["signal"].shift(1))
    )

    trend_events = []
    for pos in range(len(daily)):
        if bool(daily["signal_event"].iloc[pos]):
            event = evaluate_event(pos, "trend")
            if event is not None:
                trend_events.append(event)

    trend_df = pd.DataFrame(trend_events)

    # -----------------------------------------------------
    # 新しい下落継続リスクイベント
    # 「低め→注意」「注意→高い」などバンドが変わった初日だけ
    # -----------------------------------------------------
    daily["downside_event"] = (
        indicator_ready
        & indicator_ready.shift(1).fillna(False).astype(bool)
        & daily["downside_band"].ne(daily["downside_band"].shift(1))
    )

    downside_events = []
    for pos in range(len(daily)):
        if bool(daily["downside_event"].iloc[pos]):
            event = evaluate_event(pos, "downside")
            if event is not None:
                downside_events.append(event)

    downside_df = pd.DataFrame(downside_events)

    # -----------------------------------------------------
    # 候補配点モデルのイベント
    # 低め→注意→高いなど、候補リスク帯が変わった初日だけ
    # -----------------------------------------------------
    daily["candidate_downside_event"] = (
        indicator_ready
        & indicator_ready.shift(1).fillna(False).astype(bool)
        & daily["candidate_downside_band"].ne(
            daily["candidate_downside_band"].shift(1)
        )
    )

    candidate_events = []
    for pos in range(len(daily)):
        if bool(daily["candidate_downside_event"].iloc[pos]):
            event = evaluate_event(pos, "candidate_downside")
            if event is not None:
                candidate_events.append(event)

    candidate_df = pd.DataFrame(candidate_events)

    # -----------------------------------------------------
    # 各条件が「新しく成立した日」のその後を検証
    # 同じ条件が何日も続いても1回として扱う
    # -----------------------------------------------------
    condition_specs = [
        ("20日安値を0.5%以上割り込み", 2, "COND_BREAKDOWN"),
        ("株価が25日線より下", 1, "COND_BELOW_MA25"),
        ("25日線が下降中", 1, "COND_MA25_DOWN"),
        ("前日比-2%以上", 1, "COND_DROP_2"),
        ("下落日に出来高増", 1, "COND_DOWN_VOLUME"),
    ]

    condition_events = []
    for condition_name, current_weight, col in condition_specs:
        condition_event_mask = (
            indicator_ready
            & daily[col]
            & ~daily[col].shift(1).fillna(False).astype(bool)
        )

        for pos in range(len(daily)):
            if bool(condition_event_mask.iloc[pos]):
                event = evaluate_event(pos, "condition")
                if event is not None:
                    event["condition_name"] = condition_name
                    event["current_weight"] = current_weight
                    condition_events.append(event)

    condition_df = pd.DataFrame(condition_events)

    # -----------------------------------------------------
    # 条件2つの組み合わせも検証
    # 例：25日線下降 ＋ 20日安値割れ
    # -----------------------------------------------------
    combination_events = []
    for i in range(len(condition_specs)):
        name1, weight1, col1 = condition_specs[i]
        for j in range(i + 1, len(condition_specs)):
            name2, weight2, col2 = condition_specs[j]
            combo_active = daily[col1] & daily[col2]
            combo_event_mask = (
                indicator_ready
                & combo_active
                & ~combo_active.shift(1).fillna(False).astype(bool)
            )

            combo_name = f"{name1} ＋ {name2}"
            for pos in range(len(daily)):
                if bool(combo_event_mask.iloc[pos]):
                    event = evaluate_event(pos, "combination")
                    if event is not None:
                        event["combination_name"] = combo_name
                        event["current_weight_sum"] = weight1 + weight2
                        combination_events.append(event)

    combination_df = pd.DataFrame(combination_events)

    if trend_df.empty and downside_df.empty:
        raise ValueError("判定変化のシグナルを十分に取得できませんでした。")

    # -----------------------------------------------------
    # 旧トレンド判定集計
    # -----------------------------------------------------
    trend_order = [
        "🟢 強い上昇",
        "🟢 上昇",
        "🟡 様子見",
        "🟠 注意",
        "🔴 危険",
    ]

    performance_rows = []
    trend_risk_rows = []

    if not trend_df.empty:
        for signal in trend_order:
            part = trend_df[trend_df["signal"] == signal]
            if part.empty:
                continue

            stop_hits = part[part["stop_hit_20"]]
            rebound_rate = (
                stop_hits["rebound_to_entry"].mean() * 100
                if not stop_hits.empty
                else 0.0
            )
            stop_help_rate = (
                stop_hits["stop_helped_vs_20d_close"].mean() * 100
                if not stop_hits.empty
                else 0.0
            )

            performance_rows.append(
                {
                    "判定": signal,
                    "発生回数": len(part),
                    "5日後上昇率": (part["ret_5"] > 0).mean() * 100,
                    "5日後平均": part["ret_5"].mean(),
                    "20日後上昇率": (part["ret_20"] > 0).mean() * 100,
                    "20日後平均": part["ret_20"].mean(),
                }
            )

            trend_risk_rows.append(
                {
                    "判定": signal,
                    "5日最大下落": part["max_drop_5"].mean(),
                    "5日最大上昇": part["max_gain_5"].mean(),
                    "20日最大下落": part["max_drop_20"].mean(),
                    "20日最大上昇": part["max_gain_20"].mean(),
                    "20日逆指値到達": part["stop_hit_20"].mean() * 100,
                    "到達後買値回復": rebound_rate,
                    "逆指値有効": stop_help_rate,
                }
            )

    # -----------------------------------------------------
    # 下落継続リスク集計
    # -----------------------------------------------------
    downside_order = ["🟢 低め", "🟠 注意", "🔴 高い"]
    downside_rows = []

    if not downside_df.empty:
        for band in downside_order:
            part = downside_df[downside_df["downside_band"] == band]
            if part.empty:
                continue

            stop_hits = part[part["stop_hit_20"]]
            rebound_rate = (
                stop_hits["rebound_to_entry"].mean() * 100
                if not stop_hits.empty
                else 0.0
            )

            downside_rows.append(
                {
                    "下落継続リスク": band,
                    "発生回数": len(part),
                    "5日後下落率": (part["ret_5"] < 0).mean() * 100,
                    "20日後下落率": (part["ret_20"] < 0).mean() * 100,
                    "5日以内-3%": part["drop_3pct_5"].mean() * 100,
                    "20日以内-5%": part["drop_5pct_20"].mean() * 100,
                    "5日最大下落": part["max_drop_5"].mean(),
                    "20日最大下落": part["max_drop_20"].mean(),
                    "20日最大上昇": part["max_gain_20"].mean(),
                    "20日逆指値到達": part["stop_hit_20"].mean() * 100,
                    "到達後買値回復": rebound_rate,
                }
            )

    # -----------------------------------------------------
    # 候補配点モデル集計
    # -----------------------------------------------------
    candidate_rows = []

    if not candidate_df.empty:
        for band in downside_order:
            part = candidate_df[
                candidate_df["candidate_downside_band"] == band
            ]
            if part.empty:
                continue

            candidate_rows.append(
                {
                    "候補リスク": band,
                    "発生回数": len(part),
                    "5日後下落率": (part["ret_5"] < 0).mean() * 100,
                    "20日後下落率": (part["ret_20"] < 0).mean() * 100,
                    "5日以内-3%": part["drop_3pct_5"].mean() * 100,
                    "20日以内-5%": part["drop_5pct_20"].mean() * 100,
                    "5日最大下落": part["max_drop_5"].mean(),
                    "20日最大下落": part["max_drop_20"].mean(),
                    "20日最大上昇": part["max_gain_20"].mean(),
                }
            )

    # -----------------------------------------------------
    # リスク点数そのものも確認
    # バンド内で2点と3点に差があるか等を見る
    # -----------------------------------------------------
    score_rows = []
    if not downside_df.empty:
        for risk_score in sorted(downside_df["downside_risk"].unique()):
            part = downside_df[downside_df["downside_risk"] == risk_score]
            if part.empty:
                continue

            score_rows.append(
                {
                    "リスク点": int(risk_score),
                    "発生回数": len(part),
                    "5日後下落率": (part["ret_5"] < 0).mean() * 100,
                    "20日後下落率": (part["ret_20"] < 0).mean() * 100,
                    "20日以内-5%": part["drop_5pct_20"].mean() * 100,
                    "20日最大下落": part["max_drop_20"].mean(),
                    "20日逆指値到達": part["stop_hit_20"].mean() * 100,
                }
            )

    # -----------------------------------------------------
    # 条件ごとの実績
    # -----------------------------------------------------
    baseline_drop5_20 = baseline["BASE_DROP_5PCT_20"].mean() * 100
    baseline_max_drop_20 = baseline["BASE_MAX_DROP_20"].mean()

    condition_rows = []
    if not condition_df.empty:
        for condition_name, current_weight, _ in condition_specs:
            part = condition_df[condition_df["condition_name"] == condition_name]
            if part.empty:
                continue

            crash_rate = part["drop_5pct_20"].mean() * 100
            avg_max_drop = part["max_drop_20"].mean()
            crash_gap = crash_rate - baseline_drop5_20
            draw_gap = avg_max_drop - baseline_max_drop_20
            n = len(part)

            # 配点候補は自動適用せず、検討材料としてだけ表示する
            if n < 5:
                suggested = "保留"
                strength = "サンプル少"
            elif crash_gap >= 20 or draw_gap >= 2.0:
                suggested = 2
                strength = "強い"
            elif crash_gap >= 8 or draw_gap >= 1.0:
                suggested = 1
                strength = "有効"
            elif crash_gap <= -8 and draw_gap <= 0:
                suggested = 0
                strength = "弱い/逆効果"
            else:
                suggested = 1
                strength = "差小"

            condition_rows.append(
                {
                    "条件": condition_name,
                    "現在配点": current_weight,
                    "発生回数": n,
                    "5日後下落率": (part["ret_5"] < 0).mean() * 100,
                    "20日後下落率": (part["ret_20"] < 0).mean() * 100,
                    "20日以内-5%": crash_rate,
                    "基準との差": crash_gap,
                    "20日最大下落": avg_max_drop,
                    "20日最大上昇": part["max_gain_20"].mean(),
                    "効き方": strength,
                    "配点候補": suggested,
                }
            )

    combination_rows = []
    if not combination_df.empty:
        for combo_name in combination_df["combination_name"].unique():
            part = combination_df[combination_df["combination_name"] == combo_name]
            if len(part) < 3:
                continue

            crash_rate = part["drop_5pct_20"].mean() * 100
            combination_rows.append(
                {
                    "条件の組み合わせ": combo_name,
                    "発生回数": len(part),
                    "5日後下落率": (part["ret_5"] < 0).mean() * 100,
                    "20日以内-5%": crash_rate,
                    "基準との差": crash_rate - baseline_drop5_20,
                    "20日最大下落": part["max_drop_20"].mean(),
                    "20日最大上昇": part["max_gain_20"].mean(),
                }
            )

    combination_rows.sort(
        key=lambda x: (x["20日以内-5%"], x["発生回数"]),
        reverse=True,
    )

    return {
        "performance": pd.DataFrame(performance_rows),
        "risk": pd.DataFrame(trend_risk_rows),
        "downside": pd.DataFrame(downside_rows),
        "candidate_downside": pd.DataFrame(candidate_rows),
        "candidate_events": candidate_df,
        "downside_scores": pd.DataFrame(score_rows),
        "condition_effects": pd.DataFrame(condition_rows),
        "condition_combinations": pd.DataFrame(combination_rows),
        "baseline_drop5_20": baseline_drop5_20,
        "baseline_max_drop_20": baseline_max_drop_20,
        "trend_events": trend_df,
        "downside_events": downside_df,
        "baseline_down5": (baseline["ret_5"] < 0).mean() * 100,
        "baseline_down20": (baseline["ret_20"] < 0).mean() * 100,
        "baseline_up5": (baseline["ret_5"] > 0).mean() * 100,
        "baseline_up20": (baseline["ret_20"] > 0).mean() * 100,
        "baseline_samples": len(baseline),
        "trend_event_samples": len(trend_df),
        "downside_event_samples": len(downside_df),
        "start": baseline.index.min(),
        "end": baseline.index.max(),
    }


def render_backtest(code, row_id, current_status, current_downside_risk, current_candidate_risk):
    session_key = f"backtest_open_{row_id}"

    with st.expander("🧪 この判定を過去3年で検証"):
        st.caption(
            "現在の『下落継続リスク』と旧トレンド判定を、"
            "過去約3年の日足で別々に検証します。"
        )

        if st.button(
            "▶ 下落継続リスクをバックテスト",
            key=f"run_backtest_{row_id}",
            use_container_width=True,
        ):
            st.session_state[session_key] = True

        if not st.session_state.get(session_key, False):
            st.info(
                "実行すると、リスクが高いほど本当にその後の下落率・最大下落・"
                "逆指値到達率が増えているかを確認できます。"
            )
            return

        try:
            with st.spinner("過去3年の下落継続リスクを検証しています..."):
                result = run_signal_backtest(code)

            downside = result["downside"]
            candidate_downside = result["candidate_downside"]
            downside_scores = result["downside_scores"]
            performance = result["performance"]
            trend_risk = result["risk"]
            condition_effects = result["condition_effects"]
            condition_combinations = result["condition_combinations"]

            st.write(
                f"検証期間： **{result['start'].date()} ～ {result['end'].date()}**  "
                f"下落リスク変化： **{result['downside_event_samples']:,}回**"
            )

            st.caption(
                f"全営業日の基準：5日後下落 {result['baseline_down5']:.1f}% / "
                f"20日後下落 {result['baseline_down20']:.1f}%"
            )

            # =================================================
            # 新しい下落継続リスクの検証
            # =================================================
            st.markdown("### 🚨 新しい下落継続リスクの過去実績")

            current_band = downside_band_from_score(current_downside_risk)
            st.markdown(
                f"#### 今：{current_band}（スコア {int(current_downside_risk)}）"
            )

            current_band_row = downside[
                downside["下落継続リスク"] == current_band
            ]

            if not current_band_row.empty:
                r = current_band_row.iloc[0]
                n = int(r["発生回数"])

                c1, c2 = st.columns(2)
                with c1:
                    st.metric("5日後に下落", f"{float(r['5日後下落率']):.1f}%")
                with c2:
                    st.metric("20日後に下落", f"{float(r['20日後下落率']):.1f}%")

                c3, c4 = st.columns(2)
                with c3:
                    st.metric("5日以内に-3%", f"{float(r['5日以内-3%']):.1f}%")
                with c4:
                    st.metric("20日以内に-5%", f"{float(r['20日以内-5%']):.1f}%")

                c5, c6 = st.columns(2)
                with c5:
                    st.metric("20日平均最大下落", f"{float(r['20日最大下落']):.1f}%")
                with c6:
                    st.metric("20日平均最大上昇", f"{float(r['20日最大上昇']):.1f}%")

                c7, c8 = st.columns(2)
                with c7:
                    st.metric("逆指値に到達", f"{float(r['20日逆指値到達']):.1f}%")
                with c8:
                    st.metric("到達後に買値回復", f"{float(r['到達後買値回復']):.1f}%")

                if n < 10:
                    st.warning(
                        f"このリスク帯の発生回数は {n}回です。"
                        "サンプルが少ないため、割合は大きくブレる可能性があります。"
                    )

            # -------------------------------------------------
            # リスク判定が本当に段階的に悪化しているか自動診断
            # -------------------------------------------------
            low_row = downside[downside["下落継続リスク"] == "🟢 低め"]
            caution_row = downside[downside["下落継続リスク"] == "🟠 注意"]
            high_row = downside[downside["下落継続リスク"] == "🔴 高い"]

            if not low_row.empty and not high_row.empty:
                low = low_row.iloc[0]
                high = high_row.iloc[0]

                down_gap = float(high["20日後下落率"]) - float(low["20日後下落率"])
                draw_gap = float(high["20日最大下落"]) - float(low["20日最大下落"])
                crash_gap = float(high["20日以内-5%"]) - float(low["20日以内-5%"])

                if down_gap >= 10 and (draw_gap >= 1.0 or crash_gap >= 10):
                    st.success(
                        "✅ 新しい下落継続リスクは、低リスク時より高リスク時の方が"
                        "実際の下落・大きな下振れを強く捉えています。"
                    )
                elif down_gap > 0 or draw_gap > 0 or crash_gap > 0:
                    st.warning(
                        "⚠️ リスクが高いほど悪化する傾向は一部ありますが、"
                        "差はまだ小さめです。配点をさらに調整する余地があります。"
                    )
                else:
                    st.error(
                        "❌ 高リスク判定の方が実際に下がりやすい形になっていません。"
                        "このまま売却判断や逆指値の主軸に使うのは避けた方がよい結果です。"
                    )

            st.markdown("#### 📊 リスク帯が変わった日のその後")
            show_downside = downside.copy()
            for col in [
                "5日後下落率",
                "20日後下落率",
                "5日以内-3%",
                "20日以内-5%",
                "5日最大下落",
                "20日最大下落",
                "20日最大上昇",
                "20日逆指値到達",
                "到達後買値回復",
            ]:
                if col in show_downside.columns:
                    show_downside[col] = show_downside[col].map(lambda x: f"{x:.1f}%")

            st.dataframe(show_downside, use_container_width=True, hide_index=True)

            st.markdown("#### 🔢 リスク点数ごとの実績")
            show_scores = downside_scores.copy()
            for col in [
                "5日後下落率",
                "20日後下落率",
                "20日以内-5%",
                "20日最大下落",
                "20日逆指値到達",
            ]:
                if col in show_scores.columns:
                    show_scores[col] = show_scores[col].map(lambda x: f"{x:.1f}%")

            st.dataframe(show_scores, use_container_width=True, hide_index=True)

            # =================================================
            # 候補配点モデルを再検証
            # =================================================
            st.markdown("### ✅ 採用中の下落継続リスクモデル")
            st.caption(
                "現在の本番判定に採用している配点です。"
                "25日線より下は0点、出来高増を2点にし、"
                "安値割れ＋急落/出来高などの組み合わせを重く見ます。"
            )

            candidate_band = downside_band_from_score(current_candidate_risk)
            st.markdown(
                f"#### 現在の採用配点では：{candidate_band} "
                f"（スコア {int(current_candidate_risk)}）"
            )

            if candidate_downside.empty:
                st.info("候補配点モデルのイベントを十分に取得できませんでした。")
            else:
                show_candidate = candidate_downside.copy()
                for col in [
                    "5日後下落率",
                    "20日後下落率",
                    "5日以内-3%",
                    "20日以内-5%",
                    "5日最大下落",
                    "20日最大下落",
                    "20日最大上昇",
                ]:
                    if col in show_candidate.columns:
                        show_candidate[col] = show_candidate[col].map(
                            lambda x: f"{x:.1f}%"
                        )

                st.dataframe(
                    show_candidate,
                    use_container_width=True,
                    hide_index=True,
                )

                low_c = candidate_downside[
                    candidate_downside["候補リスク"] == "🟢 低め"
                ]
                high_c = candidate_downside[
                    candidate_downside["候補リスク"] == "🔴 高い"
                ]

                if not low_c.empty and not high_c.empty:
                    low = low_c.iloc[0]
                    high = high_c.iloc[0]
                    crash_gap = (
                        float(high["20日以内-5%"])
                        - float(low["20日以内-5%"])
                    )
                    draw_gap = (
                        float(high["20日最大下落"])
                        - float(low["20日最大下落"])
                    )
                    high_n = int(high["発生回数"])

                    if high_n >= 10 and crash_gap >= 20 and draw_gap >= 1.0:
                        st.success(
                            "✅ 採用配点は良好に分離できています。"
                            f"高リスクは低リスクより20日以内-5%が "
                            f"{crash_gap:+.1f}pt高く、最大下落も "
                            f"{draw_gap:+.1f}pt大きくなっています。"
                        )
                    elif crash_gap > 0 or draw_gap > 0:
                        st.warning(
                            "⚠️ 採用配点は改善傾向がありますが、"
                            f"高リスクの発生回数は {high_n}回です。"
                            "サンプル数と差を見てから本番反映を判断します。"
                        )
                    else:
                        st.error(
                            "❌ 候補配点では高リスクほど悪化する形が出ていません。"
                            "この配点は今後見直す必要があります。"
                        )

            # =================================================
            # 条件ごとの効き方
            # =================================================
            st.markdown("### 🔬 どの条件が実際に効いているか")
            st.caption(
                f"全営業日の20日以内-5%到達率は "
                f"{result['baseline_drop5_20']:.1f}%、平均最大下落は "
                f"{result['baseline_max_drop_20']:.1f}%です。"
                "各条件が新しく成立した日の実績と比較します。"
            )

            if condition_effects.empty:
                st.info("条件別のバックテスト結果を十分に取得できませんでした。")
            else:
                reliable = condition_effects[condition_effects["発生回数"] >= 5].copy()

                if not reliable.empty:
                    strongest = reliable.sort_values(
                        ["基準との差", "20日最大下落"],
                        ascending=False,
                    ).iloc[0]

                    st.success(
                        f"最も下振れとの関連が強かった条件は "
                        f"**{strongest['条件']}**。"
                        f"20日以内-5%は {float(strongest['20日以内-5%']):.1f}% "
                        f"（全営業日より {float(strongest['基準との差']):+.1f}pt）でした。"
                    )

                    weak = reliable[
                        (reliable["基準との差"] <= 0)
                        & (reliable["20日最大下落"] <= result["baseline_max_drop_20"])
                    ]
                    if not weak.empty:
                        names = "、".join(weak["条件"].astype(str).tolist())
                        st.warning(
                            f"**{names}** は単独では下落予測への寄与が弱い可能性があります。"
                            "配点を下げる候補です。"
                        )

                show_conditions = condition_effects.copy()
                for col in [
                    "5日後下落率",
                    "20日後下落率",
                    "20日以内-5%",
                    "基準との差",
                    "20日最大下落",
                    "20日最大上昇",
                ]:
                    if col in show_conditions.columns:
                        if col == "基準との差":
                            show_conditions[col] = show_conditions[col].map(lambda x: f"{x:+.1f}pt")
                        else:
                            show_conditions[col] = show_conditions[col].map(lambda x: f"{x:.1f}%")

                st.dataframe(show_conditions, use_container_width=True, hide_index=True)
                st.caption(
                    "『配点候補』はバックテストからの参考値で、現在の本番判定には自動反映していません。"
                    "サンプル数と条件の組み合わせを確認してから変更します。"
                )

            # -------------------------------------------------
            # 条件2つの組み合わせ
            # -------------------------------------------------
            st.markdown("### 🧩 条件の組み合わせ")
            if condition_combinations.empty:
                st.info("3回以上発生した条件の組み合わせがありませんでした。")
            else:
                st.caption(
                    "2条件が同時に成立し始めた日の実績です。"
                    "単独では弱い条件でも、組み合わせると有効になる場合があります。"
                )

                show_combos = condition_combinations.head(10).copy()
                for col in [
                    "5日後下落率",
                    "20日以内-5%",
                    "基準との差",
                    "20日最大下落",
                    "20日最大上昇",
                ]:
                    if col in show_combos.columns:
                        if col == "基準との差":
                            show_combos[col] = show_combos[col].map(lambda x: f"{x:+.1f}pt")
                        else:
                            show_combos[col] = show_combos[col].map(lambda x: f"{x:.1f}%")

                st.dataframe(show_combos, use_container_width=True, hide_index=True)

            # =================================================
            # 旧トレンド判定は参考として残す
            # =================================================
            st.markdown("### 📉 参考：旧トレンド判定の過去実績")

            current_perf = performance[performance["判定"] == current_status]
            current_risk_row = trend_risk[trend_risk["判定"] == current_status]

            if not current_perf.empty and not current_risk_row.empty:
                p = current_perf.iloc[0]
                r = current_risk_row.iloc[0]

                if current_status in ["🔴 危険", "🟠 注意"]:
                    down5 = 100 - float(p["5日後上昇率"])
                    down20 = 100 - float(p["20日後上昇率"])
                    c1, c2 = st.columns(2)
                    with c1:
                        st.metric("5日後に下落", f"{down5:.1f}%")
                    with c2:
                        st.metric("20日後に下落", f"{down20:.1f}%")
                else:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.metric("5日後に上昇", f"{float(p['5日後上昇率']):.1f}%")
                    with c2:
                        st.metric("20日後に上昇", f"{float(p['20日後上昇率']):.1f}%")

                st.caption(
                    f"旧判定の20日平均最大下落 {float(r['20日最大下落']):.1f}% / "
                    f"最大上昇 {float(r['20日最大上昇']):.1f}%"
                )

            with st.expander("旧トレンド判定の一覧も見る"):
                show_perf = performance.copy()
                for col in ["5日後上昇率", "5日後平均", "20日後上昇率", "20日後平均"]:
                    if col in show_perf.columns:
                        show_perf[col] = show_perf[col].map(lambda x: f"{x:.1f}%")
                st.dataframe(show_perf, use_container_width=True, hide_index=True)

                show_risk = trend_risk.copy()
                for col in [
                    "5日最大下落",
                    "5日最大上昇",
                    "20日最大下落",
                    "20日最大上昇",
                    "20日逆指値到達",
                    "到達後買値回復",
                    "逆指値有効",
                ]:
                    if col in show_risk.columns:
                        show_risk[col] = show_risk[col].map(lambda x: f"{x:.1f}%")
                st.dataframe(show_risk, use_container_width=True, hide_index=True)

            st.caption(
                "※日足バックテストです。同日の高値と安値の順序、場中の節目突破、"
                "ニュース、決算、地合い、約定スリッページは含みません。"
            )

        except Exception as e:
            st.error(f"バックテストエラー：{e}")


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
                        "final_exit_price": None,
                        "tp1_done": False,
                        "tp2_done": False,
                    }
                )

            supabase.table("portfolio").update(update_data).eq("id", row_id).execute()
            st.cache_data.clear()
            st.rerun()

        if st.button("🔄 防御ライン・利確ラインを再計算", key=f"{key_prefix}reset_{row_id}"):
            supabase.table("portfolio").update(
                {
                    "stop_price": None,
                    "initial_stop_price": None,
                    "final_exit_price": None,
                    "tp1_done": False,
                    "tp2_done": False,
                }
            ).eq("id", row_id).execute()
            st.cache_data.clear()
            st.rerun()

        if st.button("🗑️ 登録ミスとして削除（売却ではない）", key=f"{key_prefix}delete_{row_id}"):
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

# 売買・資金・運用成績管理
account_data = load_account_data()


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

            recent_confirmed_level = clean_float(
                analysis["round_state"].get("recent_confirmed_level")
            )
            confirmed_breakout_level = saved_breakout_level
            if recent_confirmed_level is not None:
                confirmed_breakout_level = (
                    recent_confirmed_level
                    if confirmed_breakout_level is None
                    else max(confirmed_breakout_level, recent_confirmed_level)
                )

            # 新規登録直後は、登録前に記録されていた古い突破履歴で
            # いきなり利益保護モードをONにしない。
            if (
                clean_float(row.get("initial_stop_price")) is None
                and clean_float(row.get("stop_price")) is None
            ):
                confirmed_breakout_level = recent_confirmed_level

            active_stop, initial_stop, final_exit, profit_protection = get_stop_data(
                row["id"],
                row.get("stop_price"),
                row.get("initial_stop_price"),
                row.get("final_exit_price"),
                analysis["stop_candidate"],
                analysis["atr14"],
                buy_price,
                current_price,
                confirmed_breakout_level=confirmed_breakout_level,
            )

            stop_gap_yen = current_price - active_stop
            stop_gap_pct = stop_gap_yen / current_price * 100

            final_exit_gap_yen = current_price - final_exit
            final_exit_gap_pct = final_exit_gap_yen / current_price * 100

            warning_breached = current_price <= active_stop
            warning_close_breached = analysis["confirmed_close"] <= active_stop
            final_exit_breached = current_price <= final_exit

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
                final_exit,
                tp1,
                tp2,
                analysis["downside_risk"],
                analysis["breakdown_confirmed"],
                analysis["confirmed_close"],
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
                    "final_exit": final_exit,
                    "stop_gap_yen": stop_gap_yen,
                    "stop_gap_pct": stop_gap_pct,
                    "final_exit_gap_yen": final_exit_gap_yen,
                    "final_exit_gap_pct": final_exit_gap_pct,
                    "warning_breached": warning_breached,
                    "warning_close_breached": warning_close_breached,
                    "final_exit_breached": final_exit_breached,
                    "profit_protection": profit_protection,
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

    st.caption("下の選択欄から、確認したい銘柄を1つ選んで表示します。")
    st.divider()

# =========================================================
# 運用成績・資金管理
# =========================================================

account_metrics = None
if account_data.get("ready"):
    account_metrics = calculate_account_metrics(
        holdings=holdings,
        total_market_value=total_value_known,
        total_cost_known=total_cost_known,
        account_data=account_data,
    )
    render_account_management(account_data, account_metrics)
    st.divider()
else:
    render_account_management(account_data, {})
    st.divider()


# =========================================================
# 保有株表示 / 銘柄切り替え
# =========================================================


def render_add_holding_form():
    st.subheader("➕ 保有株を追加")
    new_ticker = st.text_input("銘柄コード", placeholder="例：7203", key="new_ticker")
    new_buy = st.number_input("買値", min_value=0.0, step=1.0, key="new_buy")
    new_shares = st.number_input("株数", min_value=1, value=100, step=1, key="new_shares")

    if st.button("保存する", type="primary", key="save_new_holding"):
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
                    "final_exit_price": None,
                    "tp1_done": False,
                    "tp2_done": False,
                }
            ).execute()
            st.cache_data.clear()
            st.rerun()


st.subheader("📋 銘柄を選択")

if not holdings:
    st.info("まだ保有株がありません。下から最初の銘柄を追加できます。")
    render_add_holding_form()
else:
    selector_values = []
    selector_labels = {}
    item_by_selector = {}

    for item in items:
        row_id = item["row"]["id"]
        selector_key = f"holding:{row_id}"
        selector_values.append(selector_key)
        item_by_selector[selector_key] = item

        code = item["code"]
        name = item.get("name") or code
        if name and name != code:
            selector_labels[selector_key] = f"{code}  {name}"
        else:
            selector_labels[selector_key] = code

    add_selector_key = "__add_holding__"
    selector_values.append(add_selector_key)
    selector_labels[add_selector_key] = "➕ 保有株を追加"

    selected_selector = st.selectbox(
        "表示する銘柄",
        options=selector_values,
        format_func=lambda value: selector_labels[value],
        key="holding_selector",
    )

    if selected_selector == add_selector_key:
        st.divider()
        render_add_holding_form()
    else:
        selected_item = item_by_selector[selected_selector]
        st.caption("選択した1銘柄だけを表示しています。別の銘柄は上の選択欄から切り替えられます。")
        st.divider()

        # 既存の1銘柄表示ロジックをそのまま利用します。
        for item in [selected_item]:
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

                    render_sale_registration(
                        row=row,
                        row_id=row_id,
                        code=code,
                        name=name,
                        buy_price=item["buy_price"],
                        shares=item["shares"],
                        current_price=current_price,
                        account_ready=bool(account_data.get("ready")),
                    )
                else:
                    st.error("⚠️ 現在この銘柄の株価を取得できません。\n\n登録情報は消えていません。")
                    st.write(f"買値： **{item['buy_price']:,.0f}円**")
                    st.write(f"株数： **{item['shares']:,}株**")
                    st.caption("株価を取得できないため、売却登録フォームは表示していません。")

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

            render_sale_registration(
                row=row,
                row_id=row_id,
                code=code,
                name=name,
                buy_price=item["buy_price"],
                shares=item["shares"],
                current_price=a["price"],
                account_ready=bool(account_data.get("ready")),
            )

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
            # 利益保護モード
            # -----------------------------------------------------
            protection = item.get("profit_protection") or {}
            if protection.get("active"):
                breakout_level = protection.get("breakout_level")
                next_target = None
                if breakout_level is not None:
                    next_target = breakout_level + a["round_state"]["step"]

                message = (
                    "💰 **利益保護モード ON**\n\n"
                    f"**{breakout_level:,.0f}円** の正式突破を確認したため、"
                    "利益を守る段階に切り替えています。"
                )
                if next_target is not None:
                    message += f"\n\n次の心理的目標： **{next_target:,.0f}円前後**"
                st.success(message)
                st.caption(
                    "買値より2%以上上の心理的節目を正式突破すると自動でON。"
                    "突破した節目の少し下まで警戒ラインを引き上げ、"
                    "最終撤退ラインは買値を下回らないようにします。"
                )

            # -----------------------------------------------------
            # 2段階防御ライン
            # -----------------------------------------------------
            st.markdown("### 🛡️ 2段階防御ライン")

            active_stop = item["active_stop"]
            gap_yen = item["stop_gap_yen"]
            gap_pct = item["stop_gap_pct"]

            final_exit = item["final_exit"]
            final_gap_yen = item["final_exit_gap_yen"]
            final_gap_pct = item["final_exit_gap_pct"]

            if item["final_exit_breached"]:
                st.error(
                    "🚨 **最終撤退ライン到達・割れ**\n\n"
                    f"最終撤退ライン： **{final_exit:,.0f}円**\n\n"
                    f"現在値： **{a['price']:,.0f}円**"
                )
            elif item["warning_breached"]:
                if item["warning_close_breached"]:
                    st.error(
                        "⚠️ **警戒ラインを確定終値でも割れています**\n\n"
                        f"警戒ライン： **{active_stop:,.0f}円**\n\n"
                        f"確定終値： **{a['confirmed_close']:,.0f}円**"
                    )
                else:
                    st.warning(
                        "⚠️ **警戒ラインを場中に割れています**\n\n"
                        f"警戒ライン： **{active_stop:,.0f}円**\n\n"
                        "一瞬の割れだけでは即売却せず、終値と下落継続リスクを確認します。"
                    )
            elif gap_pct <= 2:
                st.warning(
                    "⚠️ **警戒ライン接近**\n\n"
                    f"警戒ライン： **{active_stop:,.0f}円**\n\n"
                    f"あと： **{gap_yen:,.0f}円 （{gap_pct:.2f}%）**"
                )
            else:
                st.info(
                    f"⚠️ 警戒ライン： **{active_stop:,.0f}円**\n\n"
                    f"現在値まで： **{gap_yen:,.0f}円 （{gap_pct:.2f}%）**"
                )

            st.error(
                f"🚨 最終撤退ライン： **{final_exit:,.0f}円**\n\n"
                f"現在値まで： **{final_gap_yen:,.0f}円 （{final_gap_pct:.2f}%）**"
            )

            if protection.get("active"):
                st.caption(
                    "警戒ラインは通常の下落継続リスク計算に加え、"
                    "正式突破した節目から0.75ATR以上（最低1%）下の利益保護候補も比較し、"
                    "高い方を採用します。"
                )
            else:
                st.caption(
                    f"警戒ラインは現在の下落継続リスクに合わせて計算 "
                    f"（ATR × {a['stop_atr_multiplier']:.2f}）。"
                )
            st.caption(
                "最終撤退ラインは、警戒ラインからさらに0.5ATR以上"
                "（最低1.5%）下に置きます。"
            )
            st.caption(
                "警戒ラインを一瞬割っただけでは即売却しません。"
                "『確定終値でも警戒ライン割れ＋下落継続リスクが注意以上』で"
                "一部撤退を検討し、最終撤退ライン到達では売却判断を強めます。"
            )

            st.markdown("#### 🚨 下落継続リスク")
            if a["downside_risk"] >= 4:
                st.error(f"{a['downside_label']}  （スコア {a['downside_risk']}）")
            elif a["downside_risk"] >= 2:
                st.warning(f"{a['downside_label']}  （スコア {a['downside_risk']}）")
            else:
                st.success(f"{a['downside_label']}  （スコア {a['downside_risk']}）")

            if a["downside_reasons"]:
                for reason in a["downside_reasons"]:
                    st.caption(f"・{reason}")
            else:
                st.caption("安値割れ・25日線下降・急落出来高などの下落継続条件は目立っていません。")

            st.caption(
                "テクニカルの『危険』は現在のトレンドの弱さを表します。"
                "売却判断は、警戒ライン・最終撤退ライン・下落継続リスクを組み合わせます。"
            )
            st.caption("一度引き上げた2本の防御ラインは、判定が改善しても自動では下がりません。")

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
            # 3年バックテスト
            # -----------------------------------------------------
            render_backtest(
                code=code,
                row_id=row_id,
                current_status=a["status"],
                current_downside_risk=a["downside_risk"],
                current_candidate_risk=a["candidate_downside_risk"],
            )

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

                st.markdown("#### 🛡️ 2段階防御ライン詳細")
                st.write(f"初期警戒ライン： **{item['initial_stop']:,.0f}円**")
                st.write(f"現在の警戒ライン： **{active_stop:,.0f}円**")
                st.write(f"最終撤退ライン： **{item['final_exit']:,.0f}円**")
                st.write(f"通常の広め候補： **{a['base_stop_candidate']:,.0f}円**")
                st.write(f"現在判定を反映した警戒候補： **{a['stop_candidate']:,.0f}円**")
                st.write(f"現在ATR倍率： **{a['stop_atr_multiplier']:.2f}倍**")
                st.write(f"下落継続リスク： **{a['downside_label']} / {a['downside_risk']}点**")
                st.write(f"25日線5日傾き： **{a['ma25_slope_pct']:+.2f}%**")
                st.write(f"20日安値明確割れ： **{'はい' if a['breakdown_confirmed'] else 'いいえ'}**")
                st.write(f"初期リスク： **{item['risk_per_share']:,.0f}円/株**")

                if a["stop_candidate"] < active_stop:
                    st.success("今回の計算候補は保存済み警戒ラインより低いため、警戒ラインを下げず維持しています。")

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
# 手動更新
# =========================================================

st.divider()
if st.button("🔄 最新情報に更新", key="bottom_refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.caption(
    "保有・売却判断、防御ライン、利確ライン、抵抗線・突破判定は"
    "テクニカル指標による参考情報です。実際の注文前には証券会社の価格も確認してください。"
)
