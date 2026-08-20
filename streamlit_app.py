import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time as time_module
import math

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
# 次回自動チェック
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
# Yahoo株価取得リトライ
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
# 日足の予備取得
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

        return jpx_names[
            code
        ]


    # JPX取得失敗時の予備
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
# 日本語関連ニュース
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
                        "title":
                            title,

                        "source":
                            source,

                        "date":
                            pub_date,

                        "url":
                            link
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
            "Open":
                "first",

            "High":
                "max",

            "Low":
                "min",

            "Close":
                "last",

            "Volume":
                "sum"
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


    # -----------------------------------------------------
    # 日足
    # -----------------------------------------------------

    try:

        daily = retry_history(
            ticker_code,
            period="1y",
            interval="1d",
            attempts=3
        )


    except Exception:

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


    # -----------------------------------------------------
    # 1分足
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # Yahoo最新価格
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 日本時間・市場時間
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # Yahoo日足遅延補完
    # -----------------------------------------------------

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
                d[
                    "Close"
                ]
            )


        new_row = pd.DataFrame(
            {
                "Open": [
                    float(
                        d[
                            "Open"
                        ]
                    )
                ],

                "High": [
                    max(
                        float(
                            d[
                                "High"
                            ]
                        ),
                        close_value
                    )
                ],

                "Low": [
                    min(
                        float(
                            d[
                                "Low"
                            ]
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


    # -----------------------------------------------------
    # Index整理
    # -----------------------------------------------------

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


    latest_time = None


    # -----------------------------------------------------
    # 最新値
    # -----------------------------------------------------

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
        and
        fast_price > 0
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


    # -----------------------------------------------------
    # 前日比
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 移動平均
    # -----------------------------------------------------

    daily["MA5"] = (
        daily[
            "Close"
        ]
        .rolling(5)
        .mean()
    )


    daily["MA25"] = (
        daily[
            "Close"
        ]
        .rolling(25)
        .mean()
    )


    daily["MA75"] = (
        daily[
            "Close"
        ]
        .rolling(75)
        .mean()
    )


    ma5 = float(
        daily[
            "MA5"
        ].iloc[-1]
    )


    ma25 = float(
        daily[
            "MA25"
        ].iloc[-1]
    )


    ma75 = float(
        daily[
            "MA75"
        ].iloc[-1]
    )


    # -----------------------------------------------------
    # 高値・安値
    # -----------------------------------------------------

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


    # 今日を除いた過去20日高値
    prior_20_high = float(
        daily[
            "High"
        ]
        .iloc[:-1]
        .tail(20)
        .max()
    )


    # 今日を除いた過去20日安値
    prior_20_low = float(
        daily[
            "Low"
        ]
        .iloc[:-1]
        .tail(20)
        .min()
    )


    # -----------------------------------------------------
    # 出来高
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # ATR
    # -----------------------------------------------------

    prev_close_series = (
        daily[
            "Close"
        ]
        .shift(1)
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


    # -----------------------------------------------------
    # 逆指値候補
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # テクニカル判定
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 判定ランク
    # -----------------------------------------------------

    if score >= 4:

        status = (
            "🟢 強い上昇"
        )

        level = (
            "strong"
        )

        risk_rank = 4


    elif score >= 2:

        status = (
            "🟢 上昇"
        )

        level = (
            "up"
        )

        risk_rank = 3


    elif score >= 0:

        status = (
            "🟡 様子見"
        )

        level = (
            "neutral"
        )

        risk_rank = 2


    elif score >= -2:

        status = (
            "🟠 注意"
        )

        level = (
            "warning"
        )

        risk_rank = 1


    else:

        status = (
            "🔴 危険"
        )

        level = (
            "danger"
        )

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

        "prior_20_high":
            prior_20_high,

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
# 上がることはあるが、自動では下げない
# =========================================================

def get_stop_data(
    row_id,
    saved_stop,
    saved_initial_stop,
    candidate
):

    candidate = round(
        float(
            candidate
        )
    )


    def clean(value):

        try:

            value = float(
                value
            )


            if pd.isna(
                value
            ):

                return None


            return value


        except Exception:

            return None


    saved = clean(
        saved_stop
    )


    initial = clean(
        saved_initial_stop
    )


    # 既にstop_priceがある既存保有株なら、
    # それを初期逆指値として採用
    if initial is None:

        if saved is not None:

            initial = float(
                saved
            )

        else:

            initial = float(
                candidate
            )


    if saved is None:

        active = float(
            candidate
        )


    else:

        active = max(
            saved,
            float(
                candidate
            )
        )


    update_data = {}


    if clean(
        saved_initial_stop
    ) is None:

        update_data[
            "initial_stop_price"
        ] = initial


    if (
        saved is None
        or active > saved
    ):

        update_data[
            "stop_price"
        ] = active


    if update_data:

        try:

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


        except Exception:

            pass


    return (
        active,
        initial
    )


# =========================================================
# 利確ライン
# =========================================================

def get_take_profit_lines(
    buy_price,
    initial_stop,
    atr14
):

    # 初期逆指値が買値より下なら
    # 買値−逆指値＝1R

    if initial_stop < buy_price:

        risk_per_share = (
            buy_price
            - initial_stop
        )


    else:

        # すでに含み益がある状態で登録した場合など
        risk_per_share = max(
            atr14 * 1.5,
            buy_price * 0.03
        )


    tp1 = round(
        buy_price
        + risk_per_share * 1.5
    )


    tp2 = round(
        buy_price
        + risk_per_share * 2.0
    )


    return (
        risk_per_share,
        float(tp1),
        float(tp2)
    )


# =========================================================
# 保有・売却判断
# =========================================================

def get_position_judgment(
    price,
    buy_price,
    score,
    active_stop,
    stop_gap_pct,
    tp1,
    tp2
):

    if price <= active_stop:

        return (
            "🔴 売却・損切りを検討",

            "現在値が逆指値ラインに到達または"
            "割れています。"
        )


    if price >= tp2:

        return (
            "🎯 利確②到達",

            "2Rの利確目安に到達しています。"
            "残りの利確または逆指値引き上げを"
            "検討する水準です。"
        )


    if price >= tp1:

        return (
            "🟢 一部利確を検討",

            "1.5Rの利確目安に到達しています。"
        )


    if score <= -3:

        return (
            "🔴 売却・損切りを検討",

            "短中期トレンドが弱く、"
            "下落警戒を優先する状態です。"
        )


    if (
        score <= -1
        or stop_gap_pct <= 2
    ):

        return (
            "🟠 保有継続・強く警戒",

            "トレンド悪化または逆指値接近のため、"
            "保有しつつ警戒する状態です。"
        )


    if (
        price < buy_price
        and score <= 0
    ):

        return (
            "🟡 保有継続・警戒",

            "買値を下回っているため、"
            "上昇確認までは警戒が必要です。"
        )


    return (
        "🟢 保有継続",

        "現時点では保有継続を優先できる"
        "テクニカル状態です。"
    )


# =========================================================
# 心理的な節目幅
# =========================================================

def get_price_step(price):

    if price < 500:

        return 10


    elif price < 1000:

        return 25


    elif price < 5000:

        return 100


    elif price < 10000:

        return 250


    elif price < 30000:

        return 500


    else:

        return 1000


# =========================================================
# 段階的な上値ルート
#
# 例：
# 3066 → 3100 → 3200 → 3230〜3233 → 3305
# =========================================================

def build_price_route(
    price,
    prior_20_high,
    tp1,
    tp2
):

    step = get_price_step(
        price
    )


    next_round = (
        math.floor(
            price / step
        )
        * step
        + step
    )


    second_round = (
        next_round
        + step
    )


    levels = []


    def add_level(
        level,
        label
    ):

        try:

            level = float(
                level
            )


        except Exception:

            return


        # 今の株価より上だけを
        # 「これからの目標」として表示
        if level <= price:

            return


        levels.append(
            {
                "price":
                    level,

                "label":
                    label
            }
        )


    # 心理的節目
    add_level(
        next_round,
        "心理的節目"
    )


    add_level(
        second_round,
        "次の心理的節目"
    )


    # 実際の過去高値
    add_level(
        prior_20_high,
        "20日高値"
    )


    # リスクリワード利確
    add_level(
        tp1,
        "利確①"
    )


    add_level(
        tp2,
        "利確②"
    )


    levels.sort(
        key=lambda x: x[
            "price"
        ]
    )


    # -----------------------------------------------------
    # 近い価格を同じゾーンにまとめる
    #
    # 例：
    # 利確① 3230
    # 20日高値 3233
    #
    # → 3230〜3233円
    # -----------------------------------------------------

    cluster_distance = max(
        5,
        step * 0.10
    )


    zones = []


    for level in levels:

        if not zones:

            zones.append(
                {
                    "low":
                        level[
                            "price"
                        ],

                    "high":
                        level[
                            "price"
                        ],

                    "labels":
                        [
                            level[
                                "label"
                            ]
                        ]
                }
            )


            continue


        previous = zones[
            -1
        ]


        if (
            level[
                "price"
            ]
            - previous[
                "high"
            ]
            <= cluster_distance
        ):

            previous[
                "high"
            ] = level[
                "price"
            ]


            if (
                level[
                    "label"
                ]
                not in previous[
                    "labels"
                ]
            ):

                previous[
                    "labels"
                ].append(
                    level[
                        "label"
                    ]
                )


        else:

            zones.append(
                {
                    "low":
                        level[
                            "price"
                        ],

                    "high":
                        level[
                            "price"
                        ],

                    "labels":
                        [
                            level[
                                "label"
                            ]
                        ]
                }
            )


    return {
        "step":
            step,

        "next_round":
            next_round,

        "second_round":
            second_round,

        "zones":
            zones
    }


# =========================================================
# 保有情報編集
# =========================================================

def render_edit_controls(
    row,
    row_id,
    key_prefix=""
):

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

            key=(
                f"{key_prefix}"
                f"ticker_"
                f"{row_id}"
            )
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

            key=(
                f"{key_prefix}"
                f"buy_"
                f"{row_id}"
            )
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

            key=(
                f"{key_prefix}"
                f"shares_"
                f"{row_id}"
            )
        )


        if st.button(
            "変更を保存",

            key=(
                f"{key_prefix}"
                f"update_"
                f"{row_id}"
            )
        ):

            ticker_changed = (
                edit_ticker.strip()
                != str(
                    row[
                        "ticker"
                    ]
                ).strip()
            )


            buy_changed = (
                float(
                    edit_buy
                )
                != float(
                    row[
                        "buy_price"
                    ]
                )
            )


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


            # 銘柄または買値を変えたら
            # 逆指値・利確基準を初期化
            if (
                ticker_changed
                or buy_changed
            ):

                update_data.update(
                    {
                        "stop_price":
                            None,

                        "initial_stop_price":
                            None,

                        "tp1_done":
                            False,

                        "tp2_done":
                            False
                    }
                )


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
            "🔄 逆指値・利確ラインを再計算",

            key=(
                f"{key_prefix}"
                f"reset_"
                f"{row_id}"
            )
        ):

            (
                supabase
                .table(
                    "portfolio"
                )
                .update(
                    {
                        "stop_price":
                            None,

                        "initial_stop_price":
                            None,

                        "tp1_done":
                            False,

                        "tp2_done":
                            False
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

            key=(
                f"{key_prefix}"
                f"delete_"
                f"{row_id}"
            )
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
# 前回GitHub自動監視データ
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
            row[
                "ticker"
            ]
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
# 全登録銘柄分析
# =========================================================

items = []

total_cost_known = 0.0

total_value_known = 0.0

failed_count = 0


with st.spinner(
    "保有株を分析しています..."
):

    for row in holdings:

        raw_code = str(
            row.get(
                "ticker",
                ""
            )
        ).strip()


        display_code = simple_ticker(
            normalize_ticker(
                raw_code
            )
        )


        name = get_company_name(
            raw_code
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


        cost = (
            buy_price
            * shares
        )


        try:

            analysis = analyze_stock(
                raw_code
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


            (
                active_stop,
                initial_stop
            ) = get_stop_data(
                row[
                    "id"
                ],

                row.get(
                    "stop_price"
                ),

                row.get(
                    "initial_stop_price"
                ),

                analysis[
                    "stop_candidate"
                ]
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


            (
                risk_per_share,
                tp1,
                tp2
            ) = get_take_profit_lines(
                buy_price,
                initial_stop,
                analysis[
                    "atr14"
                ]
            )


            tp1_gap = (
                tp1
                - current_price
            )


            tp2_gap = (
                tp2
                - current_price
            )


            tp1_gap_pct = (
                tp1_gap
                / current_price
                * 100
            )


            tp2_gap_pct = (
                tp2_gap
                / current_price
                * 100
            )


            (
                judgment,
                judgment_reason
            ) = get_position_judgment(
                current_price,
                buy_price,
                analysis[
                    "score"
                ],
                active_stop,
                stop_gap_pct,
                tp1,
                tp2
            )


            route = build_price_route(
                price=current_price,
                prior_20_high=analysis[
                    "prior_20_high"
                ],
                tp1=tp1,
                tp2=tp2
            )


            total_cost_known += cost

            total_value_known += value


            items.append(
                {
                    "row":
                        row,

                    "code":
                        display_code,

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

                    "initial_stop":
                        initial_stop,

                    "stop_gap_yen":
                        stop_gap_yen,

                    "stop_gap_pct":
                        stop_gap_pct,

                    "risk_per_share":
                        risk_per_share,

                    "tp1":
                        tp1,

                    "tp2":
                        tp2,

                    "tp1_gap":
                        tp1_gap,

                    "tp2_gap":
                        tp2_gap,

                    "tp1_gap_pct":
                        tp1_gap_pct,

                    "tp2_gap_pct":
                        tp2_gap_pct,

                    "judgment":
                        judgment,

                    "judgment_reason":
                        judgment_reason,

                    "route":
                        route
                }
            )


        except Exception as e:

            failed_count += 1


            # -------------------------------------------------
            # 最新取得失敗時は
            # GitHub Actionsが保存した前回値を探す
            # -------------------------------------------------

            state = (
                alert_states.get(
                    raw_code
                )
                or
                alert_states.get(
                    display_code
                )
                or
                alert_states.get(
                    normalize_ticker(
                        raw_code
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


            if (
                last_price is not None
                and last_price > 0
            ):

                value = (
                    last_price
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


                total_cost_known += cost

                total_value_known += value


                items.append(
                    {
                        "row":
                            row,

                        "code":
                            display_code,

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


            else:

                items.append(
                    {
                        "row":
                            row,

                        "code":
                            display_code,

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
# 危険度順
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
# 保有株サマリー
# =========================================================

if (
    holdings
    and total_cost_known > 0
):

    total_profit = (
        total_value_known
        - total_cost_known
    )


    total_pct = (
        total_profit
        / total_cost_known
        * 100
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
            f"{total_value_known:,.0f}円"
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


    if failed_count:

        st.caption(
            f"※ {failed_count}銘柄は最新株価取得に失敗しています。"
            "取得済みの前回値または登録情報を表示します。"
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
    # 銘柄名
    # =====================================================

    st.markdown(
        f"## {code}"
    )


    if (
        name
        and name != code
    ):

        st.markdown(
            f"### {name}"
        )


    # =====================================================
    # 株価取得失敗
    # 銘柄そのものは消さない
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
                f"株数： "
                f"**{item['shares']:,}株**"
            )


            st.write(
                f"参考評価額： "
                f"**{item['value']:,.0f}円**"
            )


            if item[
                "profit"
            ] >= 0:

                st.success(
                    f"参考損益： "
                    f"+{item['profit']:,.0f}円 "
                    f"（+{item['profit_pct']:.2f}%）"
                )


            else:

                st.error(
                    f"参考損益： "
                    f"{item['profit']:,.0f}円 "
                    f"（{item['profit_pct']:.2f}%）"
                )


            if item.get(
                "last_updated"
            ):

                st.caption(
                    "前回データ："
                    f"{item['last_updated']}"
                )


        else:

            st.error(
                "⚠️ 現在この銘柄の株価を取得できません。\n\n"
                "登録情報は消えていません。"
            )


            st.write(
                f"買値： "
                f"**{item['buy_price']:,.0f}円**"
            )


            st.write(
                f"株数： "
                f"**{item['shares']:,}株**"
            )


        with st.expander(
            "⚠️ 取得エラー詳細"
        ):

            st.code(
                item[
                    "error"
                ]
            )


        render_edit_controls(
            row,
            row_id,
            key_prefix="error_"
        )


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
        f"株数： "
        f"**{item['shares']:,}株**"
    )


    st.write(
        f"評価額： "
        f"**{item['value']:,.0f}円**"
    )


    # =====================================================
    # 損益
    # =====================================================

    if item[
        "profit"
    ] >= 0:

        st.success(
            f"含み損益： "
            f"+{item['profit']:,.0f}円 "
            f"（+{item['profit_pct']:.2f}%）"
        )


    else:

        st.error(
            f"含み損益： "
            f"{item['profit']:,.0f}円 "
            f"（{item['profit_pct']:.2f}%）"
        )


    # =====================================================
    # 保有・売却判断
    # =====================================================

    st.markdown(
        "### 🧭 保有・売却判断"
    )


    judgment = item[
        "judgment"
    ]


    if (
        "🔴" in judgment
        or "損切り" in judgment
    ):

        st.error(
            judgment
        )


    elif (
        "🟠" in judgment
        or "警戒" in judgment
    ):

        st.warning(
            judgment
        )


    else:

        st.success(
            judgment
        )


    st.caption(
        item[
            "judgment_reason"
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
            f"逆指値： **{active_stop:,.0f}円**\n\n"
            f"現在値： **{a['price']:,.0f}円**"
        )


    elif gap_pct <= 2:

        st.warning(
            "⚠️ 逆指値ライン接近\n\n"
            f"逆指値： **{active_stop:,.0f}円**\n\n"
            f"あと： **{gap_yen:,.0f}円 "
            f"（{gap_pct:.2f}%）**"
        )


    else:

        st.info(
            f"🛡️ 逆指値： "
            f"**{active_stop:,.0f}円**\n\n"
            f"現在値まで： "
            f"**{gap_yen:,.0f}円 "
            f"（{gap_pct:.2f}%）**"
        )


    st.caption(
        "逆指値は上がることはありますが、"
        "自動では下がりません。"
    )


    # =====================================================
    # 利確ライン
    # =====================================================

    st.markdown(
        "### 🎯 利確ライン"
    )


    tp1 = item[
        "tp1"
    ]


    tp2 = item[
        "tp2"
    ]


    if a[
        "price"
    ] >= tp2:

        st.success(
            f"🎯 利確②到達： "
            f"**{tp2:,.0f}円**"
        )


    elif a[
        "price"
    ] >= tp1:

        st.success(
            f"🎯 利確①到達： "
            f"**{tp1:,.0f}円**"
        )


    else:

        st.write(
            f"利確①： "
            f"**{tp1:,.0f}円** "
            f"（あと "
            f"{max(item['tp1_gap'], 0):,.0f}円 / "
            f"{max(item['tp1_gap_pct'], 0):.2f}%）"
        )


        st.write(
            f"利確②： "
            f"**{tp2:,.0f}円** "
            f"（あと "
            f"{max(item['tp2_gap'], 0):,.0f}円 / "
            f"{max(item['tp2_gap_pct'], 0):.2f}%）"
        )


    st.write(
        f"📈 20日高値目安： "
        f"**{a['prior_20_high']:,.0f}円**"
    )


    if row.get(
        "tp1_done",
        False
    ):

        st.caption(
            "✅ 利確①の到達通知済み"
        )


    if row.get(
        "tp2_done",
        False
    ):

        st.caption(
            "✅ 利確②の到達通知済み"
        )


    st.caption(
        "利確①＝初期リスクの1.5倍、"
        "利確②＝初期リスクの2倍を"
        "買値に加えた参考値です。"
    )


    # =====================================================
    # 段階的な上値ルート
    # =====================================================

    st.markdown(
        "### 🪜 上値ルート"
    )


    route = item[
        "route"
    ]


    zones = route[
        "zones"
    ]


    if not zones:

        st.success(
            "現在の主要な上値目標をすでに突破しています。"
            "新しい高値形成を確認する局面です。"
        )


    else:

        for index, zone in enumerate(
            zones[:5]
        ):

            low = zone[
                "low"
            ]


            high = zone[
                "high"
            ]


            labels = "・".join(
                zone[
                    "labels"
                ]
            )


            if (
                abs(
                    high - low
                )
                < 1
            ):

                price_text = (
                    f"{low:,.0f}円"
                )


            else:

                price_text = (
                    f"{low:,.0f}"
                    f"〜"
                    f"{high:,.0f}円"
                )


            distance = (
                low
                - a[
                    "price"
                ]
            )


            distance_pct = (
                distance
                / a[
                    "price"
                ]
                * 100
            )


            if index == 0:

                st.info(
                    "① 次に超えたいライン\n\n"
                    f"**{price_text}**\n\n"
                    f"{labels}\n\n"
                    f"あと "
                    f"**{distance:,.0f}円 "
                    f"（{distance_pct:.2f}%）**"
                )


            elif index == 1:

                st.write(
                    "② 突破後の目標： "
                    f"**{price_text}** "
                    f"（{labels}）"
                )


            else:

                st.write(
                    f"{index + 1}️⃣ "
                    f"**{price_text}** "
                    f"（{labels}）"
                )


        # -------------------------------------------------
        # 今どう見るか
        # -------------------------------------------------

        first_zone = (
            zones[
                0
            ]
        )


        first_price = (
            first_zone[
                "low"
            ]
        )


        distance_to_first = (
            first_price
            - a[
                "price"
            ]
        ) / a[
            "price"
        ] * 100


        st.markdown(
            "#### 🧭 今の見方"
        )


        if distance_to_first <= 1:

            st.warning(
                f"現在値は次の節目 "
                f"**{first_price:,.0f}円** のすぐ下です。"
                "ここを突破して、その上で維持できるかを"
                "確認する局面です。"
            )


        elif (
            a[
                "level"
            ]
            in [
                "strong",
                "up"
            ]
        ):

            st.success(
                f"上昇トレンド中です。まず "
                f"**{first_price:,.0f}円** を目標にし、"
                "突破できれば次のゾーンを狙う形です。"
            )


        else:

            st.warning(
                f"まず "
                f"**{first_price:,.0f}円** まで戻せるかを確認。"
                "トレンドが弱い間は、"
                "遠い利確目標より逆指値管理を優先します。"
            )


        st.caption(
            "心理的なキリ番・20日高値・"
            "利確①/②を現在値から近い順に並べています。"
            "株価が節目を超えると、その価格は消えて"
            "次の目標へ自動的に進みます。"
        )


    # =====================================================
    # テクニカル判定
    # =====================================================

    st.markdown(
        "### 📊 テクニカル判定"
    )


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
    # 関連ニュース
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
    # 詳細分析
    # =====================================================

    with st.expander(
        "📊 詳細分析"
    ):

        st.write(
            f"最新営業日： "
            f"**{a['latest_date']}**"
        )


        st.write(
            f"価格データ： "
            f"**{a['price_source']}**"
        )


        if a[
            "latest_time"
        ] is not None:

            st.write(
                "最終データ時刻： "
                f"**{a['latest_time'].strftime('%H:%M')}**"
            )


        if a[
            "synthetic"
        ]:

            st.warning(
                "Yahooの日足更新が遅れているため、"
                "1分足などから最新営業日のデータを"
                "補完しています。"
            )


        st.write(
            f"前日比： "
            f"**{a['change_pct']:+.2f}%**"
        )


        st.write(
            f"5日線： "
            f"**{a['ma5']:,.0f}円**"
        )


        st.write(
            f"25日線： "
            f"**{a['ma25']:,.0f}円**"
        )


        st.write(
            f"75日線： "
            f"**{a['ma75']:,.0f}円**"
        )


        st.write(
            f"20日高値： "
            f"**{a['recent_high']:,.0f}円**"
        )


        st.write(
            f"20日安値： "
            f"**{a['recent_low']:,.0f}円**"
        )


        st.write(
            f"出来高20日平均比： "
            f"**{a['volume_ratio']:.2f}倍**"
        )


        st.write(
            f"ATR： "
            f"**{a['atr14']:,.0f}円**"
        )


        st.write(
            f"判定スコア： "
            f"**{a['score']}**"
        )


        # -------------------------------------------------
        # 逆指値・利確詳細
        # -------------------------------------------------

        st.markdown(
            "#### 🛡️ 逆指値・利確計算"
        )


        st.write(
            f"初期逆指値： "
            f"**{item['initial_stop']:,.0f}円**"
        )


        st.write(
            f"現在保存中の逆指値： "
            f"**{active_stop:,.0f}円**"
        )


        st.write(
            f"今回の逆指値計算候補： "
            f"**{a['stop_candidate']:,.0f}円**"
        )


        st.write(
            f"初期リスク： "
            f"**{item['risk_per_share']:,.0f}円/株**"
        )


        if (
            a[
                "stop_candidate"
            ]
            < active_stop
        ):

            st.success(
                "今回の計算候補は下がっていますが、"
                "保存済みの逆指値を維持しています。"
            )


        # -------------------------------------------------
        # チャート
        # -------------------------------------------------

        st.markdown(
            "#### 📈 チャート"
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


        # -------------------------------------------------
        # 判定理由
        # -------------------------------------------------

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

    render_edit_controls(
        row,
        row_id
    )


    st.divider()


# =========================================================
# 新規保有株追加
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
                        None,

                    "initial_stop_price":
                        None,

                    "tp1_done":
                        False,

                    "tp2_done":
                        False
                }
            )
            .execute()
        )


        st.cache_data.clear()

        st.rerun()


# =========================================================
# 手動更新
# =========================================================

st.divider()


if st.button(
    "🔄 最新情報に更新",
    use_container_width=True
):

    st.cache_data.clear()

    st.rerun()


st.caption(
    "保有・売却判断、逆指値、利確ライン、上値ルートは"
    "テクニカル指標を使った参考情報です。"
    "実際の注文前には証券会社の株価も確認してください。"
)