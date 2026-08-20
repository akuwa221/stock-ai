import streamlit as st
import yfinance as yf
import pandas as pd

from supabase import create_client
from datetime import datetime, time
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
# Supabase接続
# =========================================================

try:

    supabase = create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_KEY"]
    )

except Exception as e:

    st.error(
        f"Supabase接続エラー：{e}"
    )

    st.stop()


# =========================================================
# 補助関数
# =========================================================

def normalize_ticker(code):

    code = str(code).strip()

    if code.isdigit() and len(code) == 4:
        return f"{code}.T"

    return code


def display_ticker(code):

    code = str(code)

    if code.endswith(".T"):
        return code[:-2]

    return code


def to_jst_naive_index(index):

    index = pd.DatetimeIndex(index)

    if index.tz is None:
        return index

    return (
        index
        .tz_convert("Asia/Tokyo")
        .tz_localize(None)
    )


def build_intraday_daily(intraday):

    if intraday.empty:
        return pd.DataFrame()

    temp = intraday.copy()

    temp.index = to_jst_naive_index(
        temp.index
    )

    temp["TradeDate"] = temp.index.date

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
# 60秒キャッシュ
# =========================================================

@st.cache_data(ttl=60, show_spinner=False)
def analyze_stock(code):

    ticker_code = normalize_ticker(
        code
    )

    stock = yf.Ticker(
        ticker_code
    )


    # =====================================================
    # 日足取得
    # =====================================================

    daily = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
        actions=False
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


    # =====================================================
    # fast_info
    # =====================================================

    try:

        fast_last_price = float(
            stock.fast_info[
                "last_price"
            ]
        )

    except Exception:

        fast_last_price = None


    if daily.empty:

        raise ValueError(
            "株価データを取得できませんでした"
        )


    # =====================================================
    # Index整理
    # =====================================================

    daily = daily.copy()

    daily.index = to_jst_naive_index(
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


    if not intraday.empty:

        intraday = intraday.copy()

        intraday.index = (
            to_jst_naive_index(
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


    original_daily_last_date = (
        daily.index[-1].date()
    )


    # =====================================================
    # 1分足 → 日足
    # =====================================================

    intraday_daily = (
        build_intraday_daily(
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
    # 現在時刻
    # =====================================================

    now = datetime.now(
        JST
    )

    today = now.date()

    now_time = now.time()


    morning_open = time(
        9,
        0
    )

    morning_close = time(
        11,
        30
    )

    afternoon_open = time(
        12,
        30
    )

    market_close = time(
        15,
        30
    )


    weekday = (
        now.weekday() < 5
    )


    market_open = (
        weekday
        and
        (
            morning_open
            <= now_time
            < morning_close

            or

            afternoon_open
            <= now_time
            < market_close
        )
    )


    # =====================================================
    # 日足遅延補完
    # =====================================================

    synthetic_added = False


    if (
        intraday_last_date is not None
        and
        intraday_last_date
        > original_daily_last_date
    ):

        latest_intraday_day = (
            intraday_daily
            .iloc[-1]
        )


        close_value = float(
            latest_intraday_day[
                "Close"
            ]
        )


        # 引け後ならYahoo最新値を優先
        if (
            not market_open
            and
            fast_last_price is not None
            and
            fast_last_price > 0
        ):

            close_value = (
                fast_last_price
            )


        high_value = max(
            float(
                latest_intraday_day[
                    "High"
                ]
            ),
            close_value
        )


        low_value = min(
            float(
                latest_intraday_day[
                    "Low"
                ]
            ),
            close_value
        )


        new_row = pd.DataFrame(
            {
                "Open": [
                    float(
                        latest_intraday_day[
                            "Open"
                        ]
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
                        latest_intraday_day[
                            "Volume"
                        ]
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


        synthetic_added = True


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
        daily.index[-1]
        .date()
    )


    # =====================================================
    # 最新価格
    # =====================================================

    price_source = ""
    latest_time = None


    # 場中
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


    # 日足が正式に更新済み
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


    # 日足遅延 + fast_info
    elif (
        fast_last_price is not None
        and
        fast_last_price > 0
    ):

        latest_price = float(
            fast_last_price
        )

        daily.loc[
            daily.index[-1],
            "Close"
        ] = latest_price

        price_source = (
            "Yahoo最新値"
        )


    # 最後の保険
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
    # 高値・安値
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


    # より余裕のある方
    stop_standard = min(
        stop_support,
        stop_atr
    )


    stop_distance = (
        (
            latest_price
            - stop_standard
        )
        /
        latest_price
        * 100
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
        (
            latest_price
            - recent_low
        )
        /
        latest_price
        * 100
    )


    if low_distance <= 2:

        score -= 2

        reasons.append(
            "⚠ 20日安値まで2％以内"
        )


    if volume_ratio >= 1.5:

        score += 1

        reasons.append(
            "○ 出来高が20日平均の1.5倍以上"
        )


    # =====================================================
    # 判定名
    # =====================================================

    if score >= 4:

        status = (
            "🟢 強い上昇"
        )

        level = "strong"


    elif score >= 2:

        status = (
            "🟢 上昇"
        )

        level = "up"


    elif score >= 0:

        status = (
            "🟡 様子見"
        )

        level = "neutral"


    elif score >= -2:

        status = (
            "🟠 注意"
        )

        level = "warning"


    else:

        status = (
            "🔴 危険"
        )

        level = "danger"


    return {
        "ticker": ticker_code,

        "price": latest_price,

        "previous_close": previous_close,

        "change": change,

        "change_pct": change_pct,

        "latest_date": latest_date,

        "latest_time": latest_time,

        "price_source": price_source,

        "synthetic": synthetic_added,

        "ma5": ma5,

        "ma25": ma25,

        "ma75": ma75,

        "recent_high": recent_high,

        "recent_low": recent_low,

        "atr14": atr14,

        "current_volume": current_volume,

        "volume_ratio": volume_ratio,

        "stop_standard": stop_standard,

        "stop_distance": stop_distance,

        "score": score,

        "status": status,

        "level": level,

        "reasons": reasons,

        "daily": daily
    }


# =========================================================
# 保有株読み込み
# =========================================================

try:

    result = (
        supabase
        .table(
            "portfolio"
        )
        .select(
            "*"
        )
        .order(
            "id"
        )
        .execute()
    )

    holdings = (
        result.data
        or []
    )

except Exception as e:

    st.error(
        f"保有株の取得エラー：{e}"
    )

    holdings = []


# =========================================================
# 保有株全体を分析
# =========================================================

analyses = {}

total_cost = 0.0

total_value = 0.0

total_profit = 0.0


for row in holdings:

    try:

        analysis = analyze_stock(
            row["ticker"]
        )

        analyses[
            row["id"]
        ] = analysis


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


        cost = (
            buy_price
            * shares
        )


        value = (
            analysis[
                "price"
            ]
            * shares
        )


        total_cost += cost

        total_value += value

        total_profit += (
            value
            - cost
        )


    except Exception as e:

        analyses[
            row["id"]
        ] = {
            "error": str(
                e
            )
        }


# =========================================================
# 資産サマリー
# =========================================================

if holdings:

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


    if total_cost > 0:

        total_profit_pct = (
            total_profit
            / total_cost
            * 100
        )

        if total_profit >= 0:

            st.success(
                f"合計損益率："
                f"+{total_profit_pct:.2f}%"
            )

        else:

            st.error(
                f"合計損益率："
                f"{total_profit_pct:.2f}%"
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
        "まだ保有株がありません。"
    )


for row in holdings:

    row_id = (
        row[
            "id"
        ]
    )


    code = str(
        row[
            "ticker"
        ]
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


    analysis = analyses.get(
        row_id,
        {}
    )


    st.markdown(
        f"## {code}"
    )


    # =====================================================
    # エラー
    # =====================================================

    if "error" in analysis:

        st.error(
            "株価を取得できませんでした："
            f"{analysis['error']}"
        )

        continue


    current_price = (
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
        (
            current_price
            - buy_price
        )
        /
        buy_price
        * 100
        if buy_price > 0
        else 0
    )


    # =====================================================
    # 現在値・買値
    # =====================================================

    col1, col2 = st.columns(
        2
    )


    with col1:

        st.metric(
            "現在値",
            f"{current_price:,.0f}円",
            f"{analysis['change']:+,.0f}円"
        )


    with col2:

        st.metric(
            "買値",
            f"{buy_price:,.0f}円"
        )


    st.write(
        f"株数： **{shares:,}株**"
    )


    st.write(
        f"評価額： **{value:,.0f}円**"
    )


    # =====================================================
    # 損益
    # =====================================================

    if profit >= 0:

        st.success(
            f"含み損益："
            f" +{profit:,.0f}円"
            f"（+{profit_pct:.2f}%）"
        )

    else:

        st.error(
            f"含み損益："
            f" {profit:,.0f}円"
            f"（{profit_pct:.2f}%）"
        )


    # =====================================================
    # 判定
    # =====================================================

    status = (
        analysis[
            "status"
        ]
    )


    if (
        analysis[
            "level"
        ]
        in [
            "strong",
            "up"
        ]
    ):

        st.success(
            status
        )


    elif (
        analysis[
            "level"
        ]
        == "neutral"
    ):

        st.warning(
            status
        )


    elif (
        analysis[
            "level"
        ]
        == "warning"
    ):

        st.warning(
            status
        )


    else:

        st.error(
            status
        )


    # =====================================================
    # 逆指値参考
    # =====================================================

    st.write(
        "🛡️ 逆指値参考： "
        f"**{analysis['stop_standard']:,.0f}円付近**"
        f"（現在値から "
        f"-{analysis['stop_distance']:.1f}%）"
    )


    # =====================================================
    # 詳細
    # =====================================================

    with st.expander(
        "📊 詳細分析"
    ):

        st.write(
            "最新営業日： "
            f"**{analysis['latest_date']}**"
        )


        st.caption(
            "価格データ："
            f"{analysis['price_source']}"
        )


        if analysis[
            "synthetic"
        ]:

            st.warning(
                "Yahooの日足更新が遅れているため、"
                "1分足などから最新日を補完しています。"
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


        if (
            analysis[
                "ma75"
            ]
            is not None
        ):

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


        # =============================================
        # チャート
        # =============================================

        st.markdown(
            "#### 6か月チャート"
        )


        daily = (
            analysis[
                "daily"
            ]
        )


        chart = daily.tail(
            130
        )[
            [
                "Close",
                "MA5",
                "MA25",
                "MA75"
            ]
        ].copy()


        chart.columns = [
            "株価",
            "5日線",
            "25日線",
            "75日線"
        ]


        st.line_chart(
            chart
        )


        # =============================================
        # 理由
        # =============================================

        st.markdown(
            "#### 判定理由"
        )


        for reason in (
            analysis[
                "reasons"
            ]
        ):

            st.write(
                reason
            )


    # =====================================================
    # 編集
    # =====================================================

    with st.expander(
        "✏️ 保有情報を編集"
    ):

        edit_ticker = (
            st.text_input(
                "銘柄コード",
                value=code,
                key=f"ticker_{row_id}"
            )
        )


        edit_buy = (
            st.number_input(
                "買値",
                min_value=0.0,
                value=buy_price,
                step=1.0,
                key=f"buy_{row_id}"
            )
        )


        edit_shares = (
            st.number_input(
                "株数",
                min_value=1,
                value=shares,
                step=1,
                key=f"shares_{row_id}"
            )
        )


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


                st.success(
                    "変更しました"
                )


                st.cache_data.clear()

                st.rerun()


            except Exception as e:

                st.error(
                    f"変更エラー：{e}"
                )


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
# 新規追加
# =========================================================

st.subheader(
    "➕ 保有株を追加"
)


new_ticker = (
    st.text_input(
        "銘柄コード",
        placeholder="例：7203",
        key="new_ticker"
    )
)


new_buy_price = (
    st.number_input(
        "買値",
        min_value=0.0,
        step=1.0,
        key="new_buy"
    )
)


new_shares = (
    st.number_input(
        "株数",
        min_value=1,
        value=100,
        step=1,
        key="new_shares"
    )
)


if st.button(
    "保存する",
    type="primary"
):

    if (
        new_ticker
        .strip()
        == ""
    ):

        st.warning(
            "銘柄コードを入力してください"
        )


    elif (
        new_buy_price
        <= 0
    ):

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
                                new_buy_price
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
# 更新
# =========================================================

st.divider()


if st.button(
    "🔄 株価を更新"
):

    st.cache_data.clear()

    st.rerun()


st.caption(
    "株価データには遅延・欠損が発生する場合があります。"
    "実際の注文前には証券会社の表示も確認してください。"
)