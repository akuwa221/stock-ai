import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time as time_module

from io import BytesIO
from urllib.parse import quote_plus
from supabase import create_client
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


# =========================================================
# 基本設定
# =========================================================

st.set_page_config(
    page_title="株AI",
    page_icon="📈",
    layout="centered"
)

st.title("📈 株AI")

JST = ZoneInfo("Asia/Tokyo")

JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "misc/tvdivq0000001vg2-att/data_j.xls"
)

CHECK_TIMES = [
    time(10, 0),
    time(11, 0),
    time(13, 0),
    time(14, 30),
    time(15, 45),
]


# =========================================================
# 次回チェック
# =========================================================

def get_next_check():

    now = datetime.now(JST)

    for day_offset in range(8):

        target_date = (
            now.date()
            + timedelta(days=day_offset)
        )

        if target_date.weekday() >= 5:
            continue

        for check_time in CHECK_TIMES:

            candidate = datetime.combine(
                target_date,
                check_time,
                tzinfo=JST
            )

            if candidate > now:
                return candidate

    return None


next_check = get_next_check()

st.success("🔔 自動通知を監視中")

if next_check is not None:

    now_jst = datetime.now(JST)

    if next_check.date() == now_jst.date():

        next_text = (
            "今日 "
            + next_check.strftime("%H:%M")
        )

    elif (
        next_check.date()
        == now_jst.date() + timedelta(days=1)
    ):

        next_text = (
            "明日 "
            + next_check.strftime("%H:%M")
        )

    else:

        next_text = next_check.strftime(
            "%m/%d %H:%M"
        )

    st.write(
        f"次回チェック予定： **{next_text}**"
    )

st.caption(
    "平日 10:00 / 11:00 / 13:00 / 14:30 / 15:45"
)

st.divider()


# =========================================================
# Supabase
# =========================================================

@st.cache_resource
def get_supabase():

    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_KEY"]
    )


try:

    supabase = get_supabase()

except Exception as e:

    st.error(
        f"Supabase接続エラー：{e}"
    )

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

    return (
        index
        .tz_convert("Asia/Tokyo")
        .tz_localize(None)
    )


# =========================================================
# Yahoo取得リトライ
# =========================================================

def retry_history(
    ticker_code,
    period,
    interval,
    attempts=3,
    allow_empty=False
):

    last_error = None

    for attempt in range(attempts):

        try:

            stock = yf.Ticker(
                ticker_code
            )

            data = stock.history(
                period=period,
                interval=interval,
                auto_adjust=False,
                prepost=False,
                actions=False
            )

            if not data.empty:
                return data

            last_error = RuntimeError(
                "Yahooから空データが返されました"
            )

        except Exception as e:

            last_error = e


        if attempt < attempts - 1:

            time_module.sleep(
                1.2 * (attempt + 1)
            )


    if allow_empty:
        return pd.DataFrame()

    raise RuntimeError(
        f"株価取得失敗：{last_error}"
    )


def retry_fast_price(
    ticker_code,
    attempts=3
):

    for attempt in range(attempts):

        try:

            stock = yf.Ticker(
                ticker_code
            )

            price = float(
                stock.fast_info[
                    "last_price"
                ]
            )

            if price > 0:
                return price

        except Exception:
            pass


        if attempt < attempts - 1:

            time_module.sleep(
                0.8 * (attempt + 1)
            )


    return None


# =========================================================
# yf.downloadによる日足予備取得
# =========================================================

def download_daily_fallback(
    ticker_code
):

    try:

        data = yf.download(
            ticker_code,
            period="1y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False
        )

        if data.empty:
            return pd.DataFrame()


        if isinstance(
            data.columns,
            pd.MultiIndex
        ):

            try:

                data = data.xs(
                    ticker_code,
                    axis=1,
                    level=-1,
                    drop_level=True
                )

            except Exception:

                data.columns = (
                    data.columns
                    .get_level_values(0)
                )


        return data

    except Exception:

        return pd.DataFrame()


# =========================================================
# JPX公式銘柄名
# =========================================================

@st.cache_data(
    ttl=86400,
    show_spinner=False
)
def get_jpx_name_map():

    try:

        response = requests.get(
            JPX_URL,
            timeout=30,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            }
        )

        response.raise_for_status()

        df = pd.read_excel(
            BytesIO(
                response.content
            ),
            engine="xlrd"
        )

        result = {}

        for _, row in df.iterrows():

            raw_code = str(
                row.get(
                    "コード",
                    ""
                )
            ).strip()

            if raw_code.endswith(".0"):
                raw_code = raw_code[:-2]

            if (
                raw_code.isdigit()
                and len(raw_code) < 4
            ):
                raw_code = raw_code.zfill(4)

            name = str(
                row.get(
                    "銘柄名",
                    ""
                )
            ).strip()

            if raw_code and name:

                result[
                    raw_code
                ] = name

        return result

    except Exception:

        return {}


@st.cache_data(
    ttl=86400,
    show_spinner=False
)
def get_company_name(code):

    code = simple_ticker(
        normalize_ticker(code)
    )

    jpx_names = (
        get_jpx_name_map()
    )

    if code in jpx_names:
        return jpx_names[code]


    try:

        stock = yf.Ticker(
            normalize_ticker(code)
        )

        info = stock.get_info()

        name = (
            info.get("shortName")
            or info.get("longName")
        )

        if (
            name
            and str(name).strip() != code
        ):

            return str(
                name
            ).strip()

    except Exception:
        pass


    return code


# =========================================================
# 日本語ニュース
# =========================================================

@st.cache_data(
    ttl=900,
    show_spinner=False
)
def get_japanese_news(
    company_name,
    code
):

    query = quote_plus(
        f"{company_name} {code} 株"
    )

    url = (
        "https://news.google.com/rss/search?"
        f"q={query}"
        "&hl=ja"
        "&gl=JP"
        "&ceid=JP:ja"
    )


    try:

        response = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            }
        )

        response.raise_for_status()

        root = ET.fromstring(
            response.content
        )

        news = []


        for node in root.findall(
            ".//item"
        ):

            title = (
                node.findtext(
                    "title",
                    default=""
                )
                .strip()
            )

            link = (
                node.findtext(
                    "link",
                    default=""
                )
                .strip()
            )

            pub_date = (
                node.findtext(
                    "pubDate",
                    default=""
                )
                .strip()
            )


            source = ""

            source_node = (
                node.find(
                    "source"
                )
            )

            if source_node is not None:

                source = (
                    source_node.text
                    or ""
                ).strip()


            if (
                source
                and title.endswith(
                    f" - {source}"
                )
            ):

                title = title[
                    :-(len(source) + 3)
                ]


            if title and link:

                news.append(
                    {
                        "title": title,
                        "source": source,
                        "date": pub_date,
                        "url": link
                    }
                )


            if len(news) >= 5:
                break


        return news

    except Exception:

        return []


# =========================================================
# 1分足 → 日足
# =========================================================

def make_intraday_daily(
    intraday
):

    if intraday.empty:

        return pd.DataFrame()


    temp = intraday.copy()

    temp.index = (
        to_jst_naive(
            temp.index
        )
    )

    temp["TradeDate"] = (
        temp.index.date
    )


    result = temp.groupby(
        "TradeDate"
    ).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum"
        }
    )


    result.index = pd.DatetimeIndex(
        pd.to_datetime(
            result.index
        )
    )


    return result


# =========================================================
# 株価分析
# =========================================================

@st.cache_data(
    ttl=60,
    show_spinner=False
)
def analyze_stock(code):

    ticker_code = (
        normalize_ticker(
            code
        )
    )


    # =====================================================
    # 日足
    # 最大3回
    # =====================================================

    try:

        daily = retry_history(
            ticker_code,
            period="1y",
            interval="1d",
            attempts=3
        )

    except Exception:

        # historyが全部失敗した場合
        # yf.downloadで最後の予備取得
        daily = (
            download_daily_fallback(
                ticker_code
            )
        )


    if daily.empty:

        raise ValueError(
            "日足データを取得できませんでした"
        )


    daily = daily.copy()

    daily.index = (
        to_jst_naive(
            daily.index
        )
    )


    daily = daily.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close"
        ]
    )


    if "Volume" not in daily.columns:

        daily[
            "Volume"
        ] = 0


    if len(daily) < 80:

        raise ValueError(
            "分析用データが不足しています"
        )


    original_daily_last_date = (
        daily.index[-1].date()
    )


    # =====================================================
    # 1分足
    # 失敗しても続行
    # =====================================================

    intraday = retry_history(
        ticker_code,
        period="7d",
        interval="1m",
        attempts=2,
        allow_empty=True
    )


    if not intraday.empty:

        intraday = (
            intraday.copy()
        )

        intraday.index = (
            to_jst_naive(
                intraday.index
            )
        )

        intraday = intraday.dropna(
            subset=[
                "Open",
                "High",
                "Low",
                "Close"
            ]
        )


    # =====================================================
    # fast_info
    # =====================================================

    fast_price = (
        retry_fast_price(
            ticker_code,
            attempts=3
        )
    )


    intraday_daily = (
        make_intraday_daily(
            intraday
        )
    )


    if not intraday_daily.empty:

        intraday_last_date = (
            intraday_daily
            .index[-1]
            .date()
        )

    else:

        intraday_last_date = None


    # =====================================================
    # 日本時間
    # =====================================================

    now = datetime.now(
        JST
    )

    today = now.date()

    now_time = now.time()


    market_open = (
        now.weekday() < 5
        and
        (
            time(9, 0)
            <= now_time
            < time(11, 30)

            or

            time(12, 30)
            <= now_time
            < time(15, 30)
        )
    )


    # =====================================================
    # 日足補完
    # =====================================================

    synthetic = False


    if (
        intraday_last_date is not None
        and
        intraday_last_date
        > original_daily_last_date
    ):

        d = (
            intraday_daily
            .iloc[-1]
        )


        if (
            market_open
            and
            intraday_last_date == today
            and
            not intraday.empty
        ):

            close_value = float(
                intraday[
                    "Close"
                ].iloc[-1]
            )


        elif (
            fast_price is not None
            and
            fast_price > 0
        ):

            close_value = float(
                fast_price
            )


        else:

            close_value = float(
                d["Close"]
            )


        new_row = pd.DataFrame(
            {
                "Open": [
                    float(
                        d["Open"]
                    )
                ],

                "High": [
                    max(
                        float(
                            d["High"]
                        ),
                        close_value
                    )
                ],

                "Low": [
                    min(
                        float(
                            d["Low"]
                        ),
                        close_value
                    )
                ],

                "Close": [
                    close_value
                ],

                "Volume": [
                    float(
                        d.get(
                            "Volume",
                            0
                        )
                    )
                ]
            },

            index=[
                pd.Timestamp(
                    intraday_last_date
                )
            ]
        )


        daily = pd.concat(
            [
                daily,
                new_row
            ]
        )


        synthetic = True


    # =====================================================
    # Index整理
    # =====================================================

    daily.index = (
        pd.DatetimeIndex(
            daily.index
        )
    )


    if daily.index.tz is not None:

        daily.index = (
            daily.index
            .tz_localize(None)
        )


    daily = (
        daily[
            ~daily.index
            .normalize()
            .duplicated(
                keep="last"
            )
        ]
        .sort_index()
    )


    latest_date = (
        daily.index[-1]
        .date()
    )


    # =====================================================
    # 最新値
    # =====================================================

    latest_time = None


    if (
        market_open
        and
        not intraday.empty
        and
        intraday.index[-1].date()
        == today
    ):

        latest_price = float(
            intraday[
                "Close"
            ].iloc[-1]
        )

        latest_time = (
            intraday.index[-1]
        )

        price_source = (
            "1分足（場中）"
        )


    elif (
        original_daily_last_date
        >= latest_date
    ):

        latest_price = float(
            daily[
                "Close"
            ].iloc[-1]
        )

        price_source = (
            "日足（終値）"
        )


    elif (
        fast_price is not None
        and fast_price > 0
    ):

        latest_price = float(
            fast_price
        )

        daily.loc[
            daily.index[-1],
            "Close"
        ] = latest_price

        price_source = (
            "Yahoo最新値"
        )


    else:

        latest_price = float(
            daily[
                "Close"
            ].iloc[-1]
        )

        price_source = (
            "日足"
        )


    # =====================================================
    # 前日比
    # =====================================================

    previous_close = float(
        daily[
            "Close"
        ].iloc[-2]
    )


    change = (
        latest_price
        - previous_close
    )


    change_pct = (
        change
        / previous_close
        * 100
    )


    # =====================================================
    # 移動平均
    # =====================================================

    daily["MA5"] = (
        daily["Close"]
        .rolling(5)
        .mean()
    )

    daily["MA25"] = (
        daily["Close"]
        .rolling(25)
        .mean()
    )

    daily["MA75"] = (
        daily["Close"]
        .rolling(75)
        .mean()
    )


    ma5 = float(
        daily["MA5"]
        .iloc[-1]
    )

    ma25 = float(
        daily["MA25"]
        .iloc[-1]
    )

    ma75 = float(
        daily["MA75"]
        .iloc[-1]
    )


    # =====================================================
    # 高値安値
    # =====================================================

    recent_high = float(
        daily[
            "High"
        ]
        .tail(20)
        .max()
    )


    recent_low = float(
        daily[
            "Low"
        ]
        .tail(20)
        .min()
    )


    prior_20_low = float(
        daily[
            "Low"
        ]
        .iloc[:-1]
        .tail(20)
        .min()
    )


    # =====================================================
    # 出来高
    # =====================================================

    current_volume = float(
        daily[
            "Volume"
        ].iloc[-1]
    )


    volume_ma20 = float(
        daily[
            "Volume"
        ]
        .tail(20)
        .mean()
    )


    volume_ratio = (
        current_volume
        / volume_ma20
        if volume_ma20 > 0
        else 1
    )


    # =====================================================
    # ATR
    # =====================================================

    prev_close_series = (
        daily[
            "Close"
        ].shift(1)
    )


    tr1 = (
        daily[
            "High"
        ]
        -
        daily[
            "Low"
        ]
    )


    tr2 = (
        daily[
            "High"
        ]
        -
        prev_close_series
    ).abs()


    tr3 = (
        daily[
            "Low"
        ]
        -
        prev_close_series
    ).abs()


    true_range = pd.concat(
        [
            tr1,
            tr2,
            tr3
        ],
        axis=1
    ).max(
        axis=1
    )


    atr14 = float(
        true_range
        .rolling(14)
        .mean()
        .iloc[-1]
    )


    # =====================================================
    # 逆指値候補
    # =====================================================

    stop_support = (
        prior_20_low
        * 0.995
    )


    stop_atr = (
        latest_price
        - atr14 * 1.5
    )


    stop_candidate = min(
        stop_support,
        stop_atr
    )


    # =====================================================
    # 判定
    # =====================================================

    score = 0

    reasons = []


    if latest_price > ma5:

        score += 1

        reasons.append(
            "○ 株価が5日線より上"
        )

    else:

        score -= 1

        reasons.append(
            "△ 株価が5日線より下"
        )


    if ma5 > ma25:

        score += 1

        reasons.append(
            "○ 5日線が25日線より上"
        )

    else:

        score -= 1

        reasons.append(
            "△ 5日線が25日線より下"
        )


    if latest_price > ma25:

        score += 1

        reasons.append(
            "○ 株価が25日線より上"
        )

    else:

        score -= 1

        reasons.append(
            "△ 株価が25日線より下"
        )


    if ma25 > ma75:

        score += 1

        reasons.append(
            "○ 25日線が75日線より上"
        )

    else:

        score -= 1

        reasons.append(
            "△ 25日線が75日線より下"
        )


    low_distance = (
        latest_price
        - recent_low
    ) / latest_price * 100


    if low_distance <= 2:

        score -= 2

        reasons.append(
            "⚠ 20日安値まで2%以内"
        )


    if volume_ratio >= 1.5:

        score += 1

        reasons.append(
            "○ 出来高が20日平均の1.5倍以上"
        )


    # =====================================================
    # 危険度
    # =====================================================

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


    return {
        "ticker":
            ticker_code,

        "price":
            latest_price,

        "previous_close":
            previous_close,

        "change":
            change,

        "change_pct":
            change_pct,

        "latest_date":
            latest_date,

        "latest_time":
            latest_time,

        "price_source":
            price_source,

        "synthetic":
            synthetic,

        "ma5":
            ma5,

        "ma25":
            ma25,

        "ma75":
            ma75,

        "recent_high":
            recent_high,

        "recent_low":
            recent_low,

        "prior_20_low":
            prior_20_low,

        "volume_ratio":
            volume_ratio,

        "atr14":
            atr14,

        "stop_candidate":
            stop_candidate,

        "score":
            score,

        "status":
            status,

        "level":
            level,

        "risk_rank":
            risk_rank,

        "reasons":
            reasons,

        "daily":
            daily
    }


# =========================================================
# 逆指値
# 下げない
# =========================================================

def get_active_stop(
    row_id,
    saved_stop,
    candidate
):

    candidate = round(
        float(
            candidate
        )
    )


    try:

        saved = float(
            saved_stop
        )

        if pd.isna(
            saved
        ):

            saved = None

    except Exception:

        saved = None


    if saved is None:

        active = candidate

    else:

        active = max(
            saved,
            candidate
        )


    if (
        saved is None
        or active > saved
    ):

        try:

            (
                supabase
                .table(
                    "portfolio"
                )
                .update(
                    {
                        "stop_price":
                            float(
                                active
                            )
                    }
                )
                .eq(
                    "id",
                    row_id
                )
                .execute()
            )

        except Exception:
            pass


    return float(
        active
    )


# =========================================================
# 保有株取得
# =========================================================

try:

    result = (
        supabase
        .table(
            "portfolio"
        )
        .select("*")
        .order("id")
        .execute()
    )


    holdings = (
        result.data
        or []
    )


except Exception as e:

    st.error(
        f"保有株取得エラー：{e}"
    )

    holdings = []


# =========================================================
# 前回自動監視データ
# 株価取得失敗時の予備
# =========================================================

try:

    state_result = (
        supabase
        .table(
            "alert_state"
        )
        .select("*")
        .execute()
    )


    alert_states = {
        str(
            row["ticker"]
        ):
        row

        for row in (
            state_result.data
            or []
        )
    }


except Exception:

    alert_states = {}


# =========================================================
# 全登録銘柄
# 必ず1件につき1 item作る
# =========================================================

items = []

total_cost = 0.0

total_value = 0.0


with st.spinner(
    "保有株を分析しています..."
):

    for row in holdings:

        code = str(
            row.get(
                "ticker",
                ""
            )
        ).strip()


        # -------------------------------------------------
        # 必ず先に銘柄名取得
        # -------------------------------------------------

        name = get_company_name(
            code
        )


        buy_price = float(
            row.get(
                "buy_price",
                0
            )
            or 0
        )


        shares = int(
            row.get(
                "shares",
                0
            )
            or 0
        )


        try:

            analysis = analyze_stock(
                code
            )


            current_price = float(
                analysis[
                    "price"
                ]
            )


            value = (
                current_price
                * shares
            )


            cost = (
                buy_price
                * shares
            )


            profit = (
                value
                - cost
            )


            profit_pct = (
                profit
                / cost
                * 100
                if cost > 0
                else 0
            )


            active_stop = (
                get_active_stop(
                    row["id"],
                    row.get(
                        "stop_price"
                    ),
                    analysis[
                        "stop_candidate"
                    ]
                )
            )


            stop_gap_yen = (
                current_price
                - active_stop
            )


            stop_gap_pct = (
                stop_gap_yen
                / current_price
                * 100
            )


            total_cost += cost

            total_value += value


            items.append(
                {
                    "row":
                        row,

                    "code":
                        code,

                    "name":
                        name,

                    "fresh":
                        True,

                    "analysis":
                        analysis,

                    "buy_price":
                        buy_price,

                    "shares":
                        shares,

                    "value":
                        value,

                    "profit":
                        profit,

                    "profit_pct":
                        profit_pct,

                    "active_stop":
                        active_stop,

                    "stop_gap_yen":
                        stop_gap_yen,

                    "stop_gap_pct":
                        stop_gap_pct
                }
            )


        except Exception as e:

            # =============================================
            # Yahoo完全失敗
            # alert_stateの前回価格を探す
            # =============================================

            state = (
                alert_states.get(
                    code
                )
                or
                alert_states.get(
                    simple_ticker(
                        code
                    )
                )
            )


            last_price = None

            last_updated = None

            risk_rank = -1


            if state:

                try:

                    last_price = float(
                        state.get(
                            "last_price"
                        )
                    )

                except Exception:

                    last_price = None


                last_updated = (
                    state.get(
                        "updated_at"
                    )
                )


                try:

                    risk_rank = int(
                        state.get(
                            "risk_rank",
                            -1
                        )
                    )

                except Exception:

                    risk_rank = -1


            # =============================================
            # 前回価格がある場合
            # =============================================

            if (
                last_price is not None
                and last_price > 0
            ):

                value = (
                    last_price
                    * shares
                )


                cost = (
                    buy_price
                    * shares
                )


                profit = (
                    value
                    - cost
                )


                profit_pct = (
                    profit
                    / cost
                    * 100
                    if cost > 0
                    else 0
                )


                total_cost += cost

                total_value += value


                items.append(
                    {
                        "row":
                            row,

                        "code":
                            code,

                        "name":
                            name,

                        "fresh":
                            False,

                        "fallback":
                            True,

                        "last_price":
                            last_price,

                        "last_updated":
                            last_updated,

                        "risk_rank":
                            risk_rank,

                        "buy_price":
                            buy_price,

                        "shares":
                            shares,

                        "value":
                            value,

                        "profit":
                            profit,

                        "profit_pct":
                            profit_pct,

                        "error":
                            str(e)
                    }
                )


            # =============================================
            # 前回価格すらない
            # それでも銘柄枠は表示
            # =============================================

            else:

                total_cost += (
                    buy_price
                    * shares
                )


                items.append(
                    {
                        "row":
                            row,

                        "code":
                            code,

                        "name":
                            name,

                        "fresh":
                            False,

                        "fallback":
                            False,

                        "buy_price":
                            buy_price,

                        "shares":
                            shares,

                        "error":
                            str(e),

                        "risk_rank":
                            -1
                    }
                )


# =========================================================
# 並べ替え
# 取得失敗も消さない
# =========================================================

def sort_key(item):

    if item.get(
        "fresh"
    ):

        return item[
            "analysis"
        ][
            "risk_rank"
        ]

    return item.get(
        "risk_rank",
        -1
    )


items.sort(
    key=sort_key
)


# =========================================================
# 登録状態
# =========================================================

st.caption(
    f"登録銘柄：{len(holdings)}銘柄 / "
    f"表示中：{len(items)}銘柄"
)


# =========================================================
# サマリー
# =========================================================

if holdings:

    total_profit = (
        total_value
        - total_cost
    )


    total_pct = (
        total_profit
        / total_cost
        * 100
        if total_cost > 0
        else 0
    )


    st.subheader(
        "💰 保有株サマリー"
    )


    c1, c2 = st.columns(
        2
    )


    with c1:

        st.metric(
            "評価額",
            f"{total_value:,.0f}円"
        )


    with c2:

        st.metric(
            "含み損益",
            f"{total_profit:+,.0f}円"
        )


    if total_profit >= 0:

        st.success(
            f"合計損益率："
            f"+{total_pct:.2f}%"
        )

    else:

        st.error(
            f"合計損益率："
            f"{total_pct:.2f}%"
        )


    st.caption(
        "⚠️ 危険度の高い銘柄から表示"
    )

    st.divider()


# =========================================================
# 保有株
# =========================================================

st.subheader(
    "📋 保有株"
)


if not holdings:

    st.info(
        "まだ保有株がありません"
    )


for item in items:

    row = item[
        "row"
    ]


    row_id = row[
        "id"
    ]


    code = item[
        "code"
    ]


    name = item[
        "name"
    ]


    # =====================================================
    # タイトル
    # =====================================================

    if (
        name
        and name != code
    ):

        st.markdown(
            f"## {code}"
        )

        st.markdown(
            f"### {name}"
        )

    else:

        st.markdown(
            f"## {code}"
        )


    # =====================================================
    # 最新取得失敗
    # =====================================================

    if not item.get(
        "fresh"
    ):

        if item.get(
            "fallback"
        ):

            st.warning(
                "⚠️ 最新株価を一時的に取得できませんでした。\n\n"
                "前回の自動監視で取得した株価を表示しています。"
            )


            current_price = item[
                "last_price"
            ]


            c1, c2 = st.columns(
                2
            )


            with c1:

                st.metric(
                    "前回取得値",
                    f"{current_price:,.0f}円"
                )


            with c2:

                st.metric(
                    "買値",
                    f"{item['buy_price']:,.0f}円"
                )


            st.write(
                f"株数： **{item['shares']:,}株**"
            )


            st.write(
                f"参考評価額： "
                f"**{item['value']:,.0f}円**"
            )


            if item[
                "profit"
            ] >= 0:

                st.success(
                    f"参考損益："
                    f" +{item['profit']:,.0f}円 "
                    f"（+{item['profit_pct']:.2f}%）"
                )

            else:

                st.error(
                    f"参考損益："
                    f" {item['profit']:,.0f}円 "
                    f"（{item['profit_pct']:.2f}%）"
                )


            if item.get(
                "last_updated"
            ):

                st.caption(
                    "前回データ："
                    f"{item['last_updated']}"
                )


            with st.expander(
                "⚠️ 取得エラー詳細"
            ):

                st.code(
                    item[
                        "error"
                    ]
                )


        else:

            st.error(
                "⚠️ 現在この銘柄の株価を取得できません。\n\n"
                "登録情報は消えていません。"
            )


            st.write(
                f"買値："
                f" **{item['buy_price']:,.0f}円**"
            )


            st.write(
                f"株数："
                f" **{item['shares']:,}株**"
            )


            with st.expander(
                "⚠️ 取得エラー詳細"
            ):

                st.code(
                    item[
                        "error"
                    ]
                )


        # -------------------------------------------------
        # 取得失敗時も編集可能
        # -------------------------------------------------

        with st.expander(
            "✏️ 保有情報を編集"
        ):

            edit_ticker = st.text_input(
                "銘柄コード",
                value=str(
                    row["ticker"]
                ),
                key=f"error_ticker_{row_id}"
            )


            edit_buy = st.number_input(
                "買値",
                min_value=0.0,
                value=float(
                    row["buy_price"]
                ),
                step=1.0,
                key=f"error_buy_{row_id}"
            )


            edit_shares = st.number_input(
                "株数",
                min_value=1,
                value=int(
                    row["shares"]
                ),
                step=1,
                key=f"error_shares_{row_id}"
            )


            if st.button(
                "変更を保存",
                key=f"error_update_{row_id}"
            ):

                (
                    supabase
                    .table(
                        "portfolio"
                    )
                    .update(
                        {
                            "ticker":
                                edit_ticker.strip(),

                            "buy_price":
                                float(
                                    edit_buy
                                ),

                            "shares":
                                int(
                                    edit_shares
                                )
                        }
                    )
                    .eq(
                        "id",
                        row_id
                    )
                    .execute()
                )


                st.cache_data.clear()

                st.rerun()


            if st.button(
                "🗑️ この銘柄を削除",
                key=f"error_delete_{row_id}"
            ):

                (
                    supabase
                    .table(
                        "portfolio"
                    )
                    .delete()
                    .eq(
                        "id",
                        row_id
                    )
                    .execute()
                )


                st.cache_data.clear()

                st.rerun()


        st.divider()

        continue


    # =====================================================
    # 正常取得
    # =====================================================

    a = item[
        "analysis"
    ]


    # =====================================================
    # 現在値
    # =====================================================

    c1, c2 = st.columns(
        2
    )


    with c1:

        st.metric(
            "現在値",

            f"{a['price']:,.0f}円",

            f"{a['change']:+,.0f}円"
        )


    with c2:

        st.metric(
            "買値",

            f"{item['buy_price']:,.0f}円"
        )


    st.write(
        f"株数："
        f" **{item['shares']:,}株**"
    )


    st.write(
        f"評価額："
        f" **{item['value']:,.0f}円**"
    )


    # =====================================================
    # 損益
    # =====================================================

    if item[
        "profit"
    ] >= 0:

        st.success(
            f"含み損益："
            f" +{item['profit']:,.0f}円 "
            f"（+{item['profit_pct']:.2f}%）"
        )

    else:

        st.error(
            f"含み損益："
            f" {item['profit']:,.0f}円 "
            f"（{item['profit_pct']:.2f}%）"
        )


    # =====================================================
    # 判定
    # =====================================================

    if a[
        "level"
    ] in [
        "strong",
        "up"
    ]:

        st.success(
            a[
                "status"
            ]
        )


    elif a[
        "level"
    ] in [
        "neutral",
        "warning"
    ]:

        st.warning(
            a[
                "status"
            ]
        )


    else:

        st.error(
            a[
                "status"
            ]
        )


    # =====================================================
    # 逆指値
    # =====================================================

    active_stop = item[
        "active_stop"
    ]


    gap_yen = item[
        "stop_gap_yen"
    ]


    gap_pct = item[
        "stop_gap_pct"
    ]


    if gap_yen <= 0:

        st.error(
            "🚨 逆指値ライン到達・割れ\n\n"
            f"逆指値："
            f"**{active_stop:,.0f}円**\n\n"
            f"現在値："
            f"**{a['price']:,.0f}円**"
        )


    elif gap_pct <= 2:

        st.warning(
            "⚠️ 逆指値ライン接近\n\n"
            f"逆指値："
            f"**{active_stop:,.0f}円**\n\n"
            f"あと："
            f"**{gap_yen:,.0f}円 "
            f"（{gap_pct:.2f}%）**"
        )


    else:

        st.info(
            f"🛡️ 逆指値："
            f" **{active_stop:,.0f}円**\n\n"
            f"現在値まで："
            f" **{gap_yen:,.0f}円 "
            f"（{gap_pct:.2f}%）**"
        )


    st.caption(
        "逆指値は上がることはありますが、"
        "自動では下がりません。"
    )


    # =====================================================
    # ニュース
    # =====================================================

    with st.expander(
        "📰 関連ニュース"
    ):

        news = get_japanese_news(
            name,
            code
        )


        if not news:

            st.info(
                "関連する日本語ニュースを"
                "取得できませんでした。"
            )


        for i, n in enumerate(
            news,
            start=1
        ):

            st.markdown(
                f"### {i}. "
                f"{n['title']}"
            )


            caption_parts = []


            if n[
                "source"
            ]:

                caption_parts.append(
                    n[
                        "source"
                    ]
                )


            if n[
                "date"
            ]:

                caption_parts.append(
                    n[
                        "date"
                    ]
                )


            if caption_parts:

                st.caption(
                    " / ".join(
                        caption_parts
                    )
                )


            st.link_button(
                "記事を開く",
                n[
                    "url"
                ]
            )


            st.divider()


    # =====================================================
    # 詳細
    # =====================================================

    with st.expander(
        "📊 詳細分析"
    ):

        st.write(
            f"最新営業日："
            f" **{a['latest_date']}**"
        )


        st.write(
            f"価格データ："
            f" **{a['price_source']}**"
        )


        if a[
            "latest_time"
        ] is not None:

            st.write(
                "最終データ時刻："
                f" **{a['latest_time'].strftime('%H:%M')}**"
            )


        st.write(
            f"前日比："
            f" **{a['change_pct']:+.2f}%**"
        )


        st.write(
            f"5日線："
            f" **{a['ma5']:,.0f}円**"
        )


        st.write(
            f"25日線："
            f" **{a['ma25']:,.0f}円**"
        )


        st.write(
            f"75日線："
            f" **{a['ma75']:,.0f}円**"
        )


        st.write(
            f"20日高値："
            f" **{a['recent_high']:,.0f}円**"
        )


        st.write(
            f"20日安値："
            f" **{a['recent_low']:,.0f}円**"
        )


        st.write(
            f"ATR："
            f" **{a['atr14']:,.0f}円**"
        )


        st.write(
            f"判定スコア："
            f" **{a['score']}**"
        )


        st.markdown(
            "#### 🛡️ 逆指値詳細"
        )


        st.write(
            "現在保存中："
            f" **{active_stop:,.0f}円**"
        )


        st.write(
            "今回計算候補："
            f" **{a['stop_candidate']:,.0f}円**"
        )


        if (
            a[
                "stop_candidate"
            ]
            < active_stop
        ):

            st.success(
                "今回候補は下がっていますが、"
                "保存済みの逆指値を維持しています。"
            )


        chart = (
            a[
                "daily"
            ]
            .tail(130)
            [
                [
                    "Close",
                    "MA5",
                    "MA25",
                    "MA75"
                ]
            ]
            .copy()
        )


        chart.columns = [
            "株価",
            "5日線",
            "25日線",
            "75日線"
        ]


        st.line_chart(
            chart
        )


        st.markdown(
            "#### 🔍 判定理由"
        )


        for reason in a[
            "reasons"
        ]:

            st.write(
                reason
            )


    # =====================================================
    # 編集
    # =====================================================

    with st.expander(
        "✏️ 保有情報を編集"
    ):

        edit_ticker = st.text_input(
            "銘柄コード",

            value=str(
                row[
                    "ticker"
                ]
            ),

            key=f"ticker_{row_id}"
        )


        edit_buy = st.number_input(
            "買値",

            min_value=0.0,

            value=float(
                row[
                    "buy_price"
                ]
            ),

            step=1.0,

            key=f"buy_{row_id}"
        )


        edit_shares = st.number_input(
            "株数",

            min_value=1,

            value=int(
                row[
                    "shares"
                ]
            ),

            step=1,

            key=f"shares_{row_id}"
        )


        if st.button(
            "変更を保存",
            key=f"update_{row_id}"
        ):

            update_data = {
                "ticker":
                    edit_ticker.strip(),

                "buy_price":
                    float(
                        edit_buy
                    ),

                "shares":
                    int(
                        edit_shares
                    )
            }


            if (
                edit_ticker.strip()
                != str(
                    row[
                        "ticker"
                    ]
                ).strip()
            ):

                update_data[
                    "stop_price"
                ] = None


            (
                supabase
                .table(
                    "portfolio"
                )
                .update(
                    update_data
                )
                .eq(
                    "id",
                    row_id
                )
                .execute()
            )


            st.cache_data.clear()

            st.rerun()


        if st.button(
            "🔄 逆指値を再計算",
            key=f"reset_stop_{row_id}"
        ):

            (
                supabase
                .table(
                    "portfolio"
                )
                .update(
                    {
                        "stop_price":
                            None
                    }
                )
                .eq(
                    "id",
                    row_id
                )
                .execute()
            )


            st.cache_data.clear()

            st.rerun()


        if st.button(
            "🗑️ この銘柄を削除",
            key=f"delete_{row_id}"
        ):

            (
                supabase
                .table(
                    "portfolio"
                )
                .delete()
                .eq(
                    "id",
                    row_id
                )
                .execute()
            )


            st.cache_data.clear()

            st.rerun()


    st.divider()


# =========================================================
# 新規追加
# =========================================================

st.subheader(
    "➕ 保有株を追加"
)


new_ticker = st.text_input(
    "銘柄コード",
    placeholder="例：7203",
    key="new_ticker"
)


new_buy = st.number_input(
    "買値",
    min_value=0.0,
    step=1.0,
    key="new_buy"
)


new_shares = st.number_input(
    "株数",
    min_value=1,
    value=100,
    step=1,
    key="new_shares"
)


if st.button(
    "保存する",
    type="primary"
):

    if not new_ticker.strip():

        st.warning(
            "銘柄コードを入力してください"
        )


    elif new_buy <= 0:

        st.warning(
            "買値を入力してください"
        )


    else:

        (
            supabase
            .table(
                "portfolio"
            )
            .insert(
                {
                    "ticker":
                        new_ticker.strip(),

                    "buy_price":
                        float(
                            new_buy
                        ),

                    "shares":
                        int(
                            new_shares
                        ),

                    "stop_price":
                        None
                }
            )
            .execute()
        )


        st.cache_data.clear()

        st.rerun()


# =========================================================
# 更新
# =========================================================

st.divider()


if st.button(
    "🔄 最新情報に更新",
    use_container_width=True
):

    st.cache_data.clear()

    st.rerun()


st.caption(
    "株価取得に一時的な障害があっても、"
    "登録銘柄そのものは非表示にならないようにしています。"
)