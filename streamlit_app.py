import streamlit as st
import yfinance as yf
import pandas as pd

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


# =========================================================
# 自動通知チェック時刻
# =========================================================

CHECK_TIMES = [
    time(10, 0),
    time(11, 0),
    time(13, 0),
    time(14, 30),
    time(15, 45),
]


def get_next_check():

    now = datetime.now(JST)

    # 最大7日先まで探す
    for day_offset in range(8):

        target_date = (
            now.date()
            + timedelta(days=day_offset)
        )

        # 土日除外
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


# =========================================================
# 通知状態表示
# =========================================================

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

        next_text = (
            next_check.strftime(
                "%m/%d %H:%M"
            )
        )

    st.write(
        f"次回チェック予定： **{next_text}**"
    )


st.caption(
    "自動チェック：平日 "
    "10:00 / 11:00 / 13:00 / 14:30 / 15:45"
)

st.caption(
    "※ GitHub Actionsの混雑状況により、"
    "実際の実行時刻が数分遅れる場合があります。"
)

st.divider()


# =========================================================
# Supabase接続
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
# 銘柄コード変換
# =========================================================

def normalize_ticker(code):

    code = str(code).strip()

    if code.isdigit() and len(code) == 4:
        return f"{code}.T"

    return code


def simple_ticker(code):

    code = str(code)

    if code.endswith(".T"):
        return code[:-2]

    return code


# =========================================================
# 日時Index
# 日本時間・タイムゾーンなしへ統一
# =========================================================

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
# 1分足 → 日足
# =========================================================

def make_intraday_daily(intraday):

    if intraday.empty:
        return pd.DataFrame()

    temp = intraday.copy()

    temp.index = to_jst_naive(
        temp.index
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
# 銘柄名・ニュース
# 30分キャッシュ
# =========================================================

@st.cache_data(
    ttl=1800,
    show_spinner=False
)
def get_company_and_news(code):

    ticker_code = normalize_ticker(
        code
    )

    stock = yf.Ticker(
        ticker_code
    )


    # -----------------------------------------------------
    # 銘柄名
    # -----------------------------------------------------

    name = simple_ticker(
        ticker_code
    )

    try:

        info = stock.get_info()

        name = (
            info.get("shortName")
            or info.get("longName")
            or name
        )

    except Exception:
        pass


    # -----------------------------------------------------
    # ニュース
    # -----------------------------------------------------

    news_result = []

    try:

        raw_news = stock.get_news(
            count=5,
            tab="news"
        )

    except Exception:

        try:
            raw_news = stock.news
        except Exception:
            raw_news = []


    for item in raw_news:

        try:

            content = item.get(
                "content",
                item
            )

            title = (
                content.get("title")
                or item.get("title")
                or "タイトルなし"
            )

            summary = (
                content.get("summary")
                or content.get("description")
                or item.get("summary")
                or ""
            )


            # 発信元
            provider_data = (
                content.get("provider")
                or {}
            )

            if isinstance(
                provider_data,
                dict
            ):

                publisher = (
                    provider_data.get(
                        "displayName",
                        ""
                    )
                )

            else:

                publisher = str(
                    provider_data
                )


            # URL
            url = ""

            canonical = content.get(
                "canonicalUrl"
            )

            if isinstance(
                canonical,
                dict
            ):

                url = canonical.get(
                    "url",
                    ""
                )


            if not url:

                click_url = content.get(
                    "clickThroughUrl"
                )

                if isinstance(
                    click_url,
                    dict
                ):

                    url = click_url.get(
                        "url",
                        ""
                    )


            if not url:

                url = (
                    item.get("link")
                    or ""
                )


            news_result.append(
                {
                    "title": title,
                    "summary": summary,
                    "publisher": publisher,
                    "url": url
                }
            )

        except Exception:
            continue


    return {
        "name": name,
        "news": news_result
    }


# =========================================================
# 株価分析
# 60秒キャッシュ
# =========================================================

@st.cache_data(
    ttl=60,
    show_spinner=False
)
def analyze_stock(code):

    ticker_code = normalize_ticker(
        code
    )

    stock = yf.Ticker(
        ticker_code
    )


    # =====================================================
    # 日足
    # =====================================================

    daily = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
        actions=False
    )


    if daily.empty:

        raise ValueError(
            "日足を取得できませんでした"
        )


    daily = daily.copy()

    daily.index = to_jst_naive(
        daily.index
    )


    daily = daily.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]
    )


    if len(daily) < 30:

        raise ValueError(
            "分析に必要なデータが不足しています"
        )


    original_daily_last_date = (
        daily.index[-1].date()
    )


    # =====================================================
    # 1分足
    # =====================================================

    try:

        intraday = stock.history(
            period="7d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
            actions=False
        )

    except Exception:

        intraday = pd.DataFrame()


    if not intraday.empty:

        intraday = intraday.copy()

        intraday.index = to_jst_naive(
            intraday.index
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
    # Yahoo最新価格
    # =====================================================

    try:

        fast_price = float(
            stock.fast_info[
                "last_price"
            ]
        )

    except Exception:

        fast_price = None


    # =====================================================
    # 1分足 → 日足
    # =====================================================

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


    weekday = (
        now.weekday() < 5
    )


    market_open = (
        weekday
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
    # 日足更新遅延を補完
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


        high_value = max(
            float(
                d["High"]
            ),
            close_value
        )


        low_value = min(
            float(
                d["Low"]
            ),
            close_value
        )


        new_row = pd.DataFrame(
            {
                "Open": [
                    float(
                        d["Open"]
                    )
                ],

                "High": [
                    high_value
                ],

                "Low": [
                    low_value
                ],

                "Close": [
                    close_value
                ],

                "Volume": [
                    float(
                        d["Volume"]
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

    daily.index = pd.DatetimeIndex(
        daily.index
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
        daily.index[-1].date()
    )


    # =====================================================
    # 最新価格
    # =====================================================

    latest_time = None


    # -----------------------------------------------------
    # 場中
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


    # -----------------------------------------------------
    # 正式日足が最新
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 日足遅延
    # Yahoo最新値
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 最後の保険
    # -----------------------------------------------------

    else:

        latest_price = float(
            daily[
                "Close"
            ].iloc[-1]
        )

        price_source = (
            "補完日足"
        )


    # =====================================================
    # 前日終値
    # =====================================================

    if len(daily) >= 2:

        previous_close = float(
            daily[
                "Close"
            ].iloc[-2]
        )

    else:

        previous_close = (
            latest_price
        )


    change = (
        latest_price
        - previous_close
    )


    change_pct = (
        change
        / previous_close
        * 100
        if previous_close != 0
        else 0
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
        daily[
            "MA5"
        ].iloc[-1]
    )


    ma25 = float(
        daily[
            "MA25"
        ].iloc[-1]
    )


    ma75_raw = (
        daily[
            "MA75"
        ].iloc[-1]
    )


    ma75 = (
        float(
            ma75_raw
        )
        if pd.notna(
            ma75_raw
        )
        else None
    )


    # =====================================================
    # 20日高値・安値
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
    # 逆指値参考
    # =====================================================

    stop_support = (
        recent_low
        * 0.995
    )


    stop_atr = (
        latest_price
        - atr14 * 1.5
    )


    stop_price = min(
        stop_support,
        stop_atr
    )


    stop_distance = (
        latest_price
        - stop_price
    ) / latest_price * 100


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


    if ma75 is not None:

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
    # 数字が小さいほど危険
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

        "volume": current_volume,

        "volume_ratio": volume_ratio,

        "atr14": atr14,

        "stop_price": stop_price,

        "stop_distance": stop_distance,

        "score": score,

        "status": status,

        "level": level,

        "risk_rank": risk_rank,

        "reasons": reasons,

        "daily": daily
    }


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
# 全保有株分析
# =========================================================

items = []

total_cost = 0.0

total_value = 0.0


with st.spinner(
    "保有株を分析しています..."
):

    for row in holdings:

        try:

            analysis = analyze_stock(
                row["ticker"]
            )


            meta = get_company_and_news(
                row["ticker"]
            )


            buy_price = float(
                row[
                    "buy_price"
                ]
            )


            shares = int(
                row[
                    "shares"
                ]
            )


            value = (
                analysis[
                    "price"
                ]
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
                    "row": row,

                    "analysis": analysis,

                    "meta": meta,

                    "buy_price": buy_price,

                    "shares": shares,

                    "value": value,

                    "profit": profit,

                    "profit_pct": profit_pct
                }
            )


        except Exception as e:

            items.append(
                {
                    "row": row,
                    "error": str(e)
                }
            )


# =========================================================
# 危険度順
# =========================================================

def sort_key(item):

    if "error" in item:
        return -1

    return item[
        "analysis"
    ][
        "risk_rank"
    ]


items.sort(
    key=sort_key
)


# =========================================================
# 保有株サマリー
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


    col1, col2 = st.columns(
        2
    )


    with col1:

        st.metric(
            "評価額",
            f"{total_value:,.0f}円"
        )


    with col2:

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
        "⚠️ 危険度の高い銘柄から表示します"
    )

    st.divider()


# =========================================================
# 保有株一覧
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


    # =====================================================
    # エラー
    # =====================================================

    if "error" in item:

        st.error(
            f"{row['ticker']}："
            f"{item['error']}"
        )

        continue


    analysis = item[
        "analysis"
    ]


    meta = item[
        "meta"
    ]


    code = simple_ticker(
        analysis[
            "ticker"
        ]
    )


    name = meta[
        "name"
    ]


    # =====================================================
    # 銘柄
    # =====================================================

    st.markdown(
        f"## {code}　{name}"
    )


    # =====================================================
    # 現在値 / 買値
    # =====================================================

    col1, col2 = st.columns(
        2
    )


    with col1:

        st.metric(
            "現在値",

            f"{analysis['price']:,.0f}円",

            f"{analysis['change']:+,.0f}円"
        )


    with col2:

        st.metric(
            "買値",
            f"{item['buy_price']:,.0f}円"
        )


    st.write(
        f"株数： **{item['shares']:,}株**"
    )


    st.write(
        f"評価額： **{item['value']:,.0f}円**"
    )


    # =====================================================
    # 含み損益
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
    # 判定
    # =====================================================

    if analysis[
        "level"
    ] in [
        "strong",
        "up"
    ]:

        st.success(
            analysis[
                "status"
            ]
        )


    elif analysis[
        "level"
    ] in [
        "neutral",
        "warning"
    ]:

        st.warning(
            analysis[
                "status"
            ]
        )


    else:

        st.error(
            analysis[
                "status"
            ]
        )


    # =====================================================
    # 逆指値
    # =====================================================

    st.write(
        "🛡️ 逆指値参考： "
        f"**{analysis['stop_price']:,.0f}円付近** "
        f"（現在値から "
        f"-{analysis['stop_distance']:.1f}%）"
    )


    # =====================================================
    # ニュース
    # =====================================================

    with st.expander(
        "📰 最新ニュース"
    ):

        news = meta[
            "news"
        ]


        if not news:

            st.info(
                "ニュースを取得できませんでした"
            )


        for i, news_item in enumerate(
            news[:3],
            start=1
        ):

            st.markdown(
                f"### {i}. "
                f"{news_item['title']}"
            )


            if news_item[
                "publisher"
            ]:

                st.caption(
                    news_item[
                        "publisher"
                    ]
                )


            if news_item[
                "summary"
            ]:

                summary = (
                    news_item[
                        "summary"
                    ]
                )


                if len(
                    summary
                ) > 350:

                    summary = (
                        summary[
                            :350
                        ]
                        + "..."
                    )


                st.write(
                    f"**要約：** "
                    f"{summary}"
                )


            if news_item[
                "url"
            ]:

                st.link_button(
                    "記事を開く",
                    news_item[
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
            "最新営業日： "
            f"**{analysis['latest_date']}**"
        )


        if analysis[
            "latest_time"
        ] is not None:

            st.write(
                "最終データ時刻： "
                f"**{analysis['latest_time'].strftime('%H:%M')}**"
            )


        st.write(
            "価格データ： "
            f"**{analysis['price_source']}**"
        )


        if analysis[
            "synthetic"
        ]:

            st.warning(
                "Yahooの日足更新が遅れているため、"
                "最新営業日のデータを補完しています。"
            )


        st.write(
            f"前日比： "
            f"**{analysis['change_pct']:+.2f}%**"
        )


        st.write(
            f"5日線： "
            f"**{analysis['ma5']:,.0f}円**"
        )


        st.write(
            f"25日線： "
            f"**{analysis['ma25']:,.0f}円**"
        )


        if analysis[
            "ma75"
        ] is not None:

            st.write(
                f"75日線： "
                f"**{analysis['ma75']:,.0f}円**"
            )


        st.write(
            f"20日高値： "
            f"**{analysis['recent_high']:,.0f}円**"
        )


        st.write(
            f"20日安値： "
            f"**{analysis['recent_low']:,.0f}円**"
        )


        st.write(
            f"ATR（14日）： "
            f"**{analysis['atr14']:,.0f}円**"
        )


        st.write(
            f"出来高20日平均比： "
            f"**{analysis['volume_ratio']:.2f}倍**"
        )


        st.write(
            f"判定スコア： "
            f"**{analysis['score']}**"
        )


        # =================================================
        # チャート
        # =================================================

        st.markdown(
            "#### 📈 6か月チャート"
        )


        daily = (
            analysis[
                "daily"
            ]
        )


        chart = (
            daily
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


        # =================================================
        # 判定理由
        # =================================================

        st.markdown(
            "#### 🔍 判定理由"
        )


        for reason in analysis[
            "reasons"
        ]:

            st.write(
                reason
            )


    # =====================================================
    # 保有情報編集
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


        # -------------------------------------------------
        # 更新
        # -------------------------------------------------

        if st.button(
            "変更を保存",
            key=f"update_{row_id}"
        ):

            try:

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


            except Exception as e:

                st.error(
                    f"変更エラー：{e}"
                )


        # -------------------------------------------------
        # 削除
        # -------------------------------------------------

        if st.button(
            "🗑️ この銘柄を削除",
            key=f"delete_{row_id}"
        ):

            try:

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


            except Exception as e:

                st.error(
                    f"削除エラー：{e}"
                )


    st.divider()


# =========================================================
# 保有株追加
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

        try:

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
                            )
                    }
                )
                .execute()
            )


            st.cache_data.clear()

            st.rerun()


        except Exception as e:

            st.error(
                f"保存エラー：{e}"
            )


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
    "株価・ニュースには遅延や欠損が発生する場合があります。"
    "実際の売買前には証券会社の株価も確認してください。"
)