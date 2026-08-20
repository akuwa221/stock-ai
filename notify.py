import os
import math

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from io import BytesIO

import pandas as pd
import requests
import yfinance as yf

from supabase import create_client


JST = ZoneInfo("Asia/Tokyo")

JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/"
    "misc/tvdivq0000001vg2-att/data_j.xls"
)

SUPABASE_URL = os.environ[
    "SUPABASE_URL"
]

SUPABASE_KEY = os.environ[
    "SUPABASE_KEY"
]

NTFY_TOPIC = os.environ[
    "NTFY_TOPIC"
]


supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# =========================================================
# 共通
# =========================================================

def normalize_ticker(code):

    code = str(code).strip()

    if (
        code.isdigit()
        and len(code) == 4
    ):

        return f"{code}.T"

    return code


def to_jst_naive(index):

    index = pd.DatetimeIndex(
        index
    )

    if index.tz is None:
        return index

    return (
        index
        .tz_convert(
            "Asia/Tokyo"
        )
        .tz_localize(None)
    )


# =========================================================
# JPX銘柄名
# =========================================================

def get_jpx_names():

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

            code = str(
                row.get(
                    "コード",
                    ""
                )
            ).strip()


            if code.endswith(".0"):

                code = code[:-2]


            if (
                code.isdigit()
                and len(code) < 4
            ):

                code = code.zfill(4)


            name = str(
                row.get(
                    "銘柄名",
                    ""
                )
            ).strip()


            if code and name:

                result[
                    code
                ] = name


        return result


    except Exception:

        return {}


JPX_NAMES = get_jpx_names()


def company_name(code):

    code = str(code).strip()


    if code in JPX_NAMES:

        return JPX_NAMES[
            code
        ]


    try:

        info = (
            yf.Ticker(
                normalize_ticker(
                    code
                )
            )
            .get_info()
        )


        return (
            info.get(
                "shortName"
            )
            or info.get(
                "longName"
            )
            or code
        )


    except Exception:

        return code


# =========================================================
# 心理的節目
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
# 節目判定
# =========================================================

def inspect_price_level(
    level,
    price,
    daily,
    intraday,
    market_open,
    step
):

    touch_margin = max(
        step * 0.03,
        level * 0.001
    )


    recent_daily = (
        daily
        .tail(5)
        .copy()
    )


    rejection_mask = (
        (
            recent_daily[
                "High"
            ]
            >= level - touch_margin
        )
        &
        (
            recent_daily[
                "Close"
            ]
            < level
        )
    )


    rejection_count = int(
        rejection_mask.sum()
    )


    hold_15min = False

    volume_boost = False

    intraday_confirmed = False


    if not intraday.empty:

        latest_date = (
            intraday.index[-1]
            .date()
        )


        today_intraday = intraday[
            intraday.index.date
            == latest_date
        ]


        if len(today_intraday) >= 15:

            recent15 = (
                today_intraday
                .tail(15)
            )


            hold_15min = bool(
                (
                    recent15[
                        "Close"
                    ]
                    > level
                ).all()
            )


            older = (
                today_intraday
                .iloc[:-15]
                .tail(60)
            )


            if len(older) >= 10:

                old_volume = float(
                    older[
                        "Volume"
                    ].mean()
                )


                new_volume = float(
                    recent15[
                        "Volume"
                    ].mean()
                )


                if old_volume > 0:

                    volume_boost = (
                        new_volume
                        >= old_volume * 1.15
                    )


            intraday_confirmed = (
                price >= level * 1.001
                and
                hold_15min
                and
                volume_boost
            )


    close_confirmed = False


    if not market_open:

        latest = daily.iloc[-1]


        avg_volume = float(
            daily[
                "Volume"
            ]
            .iloc[:-1]
            .tail(20)
            .mean()
        )


        if avg_volume > 0:

            close_confirmed = (
                float(
                    latest[
                        "Close"
                    ]
                )
                >= level * 1.002
                and
                float(
                    latest[
                        "Volume"
                    ]
                )
                >= avg_volume * 1.10
            )


    confirmed = (
        intraday_confirmed
        or close_confirmed
    )


    distance_pct = (
        level - price
    ) / price * 100


    if confirmed:

        state = "confirmed"

    elif price > level:

        state = "testing_above"

    elif rejection_count >= 3:

        state = "strong_resistance"

    elif rejection_count >= 2:

        state = "resistance"

    elif (
        0
        <= distance_pct
        <= 1
    ):

        state = "approaching"

    else:

        state = "below"


    return {
        "level":
            float(level),

        "state":
            state,

        "confirmed":
            confirmed,

        "rejection_count":
            rejection_count
    }


def get_round_level_state(
    price,
    daily,
    intraday,
    market_open
):

    step = get_price_step(
        price
    )


    upper_level = (
        math.floor(
            price / step
        )
        * step
        + step
    )


    previous_level = (
        upper_level
        - step
    )


    recent_confirmed = None


    if (
        previous_level > 0
        and
        price > previous_level
    ):

        previous_state = inspect_price_level(
            previous_level,
            price,
            daily,
            intraday,
            market_open,
            step
        )


        if not previous_state[
            "confirmed"
        ]:

            return {
                "step":
                    step,

                "active_level":
                    float(
                        previous_level
                    ),

                "level_state":
                    previous_state,

                "recent_confirmed_level":
                    None
            }


        recent_confirmed = float(
            previous_level
        )


    active_state = inspect_price_level(
        upper_level,
        price,
        daily,
        intraday,
        market_open,
        step
    )


    return {
        "step":
            step,

        "active_level":
            float(
                upper_level
            ),

        "level_state":
            active_state,

        "recent_confirmed_level":
            recent_confirmed
    }


# =========================================================
# 利確
# =========================================================

def get_take_profit_lines(
    buy_price,
    initial_stop,
    atr14
):

    if initial_stop < buy_price:

        risk = (
            buy_price
            - initial_stop
        )

    else:

        risk = max(
            atr14 * 1.5,
            buy_price * 0.03
        )


    tp1 = round(
        buy_price
        + risk * 1.5
    )


    tp2 = round(
        buy_price
        + risk * 2.0
    )


    return (
        risk,
        float(tp1),
        float(tp2)
    )


# =========================================================
# 株チェック
# =========================================================

def check_stock(row):

    code = str(
        row[
            "ticker"
        ]
    ).strip()


    ticker_code = normalize_ticker(
        code
    )


    stock = yf.Ticker(
        ticker_code
    )


    daily = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
        actions=False
    )


    daily = daily.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close"
        ]
    )


    if len(daily) < 80:

        return None


    daily = daily.copy()

    daily.index = to_jst_naive(
        daily.index
    )


    if "Volume" not in daily.columns:

        daily[
            "Volume"
        ] = 0


    # =====================================================
    # 1分足
    # =====================================================

    try:

        intraday = stock.history(
            period="5d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
            actions=False
        )


    except Exception:

        intraday = pd.DataFrame()


    if not intraday.empty:

        intraday = intraday.dropna(
            subset=[
                "Open",
                "High",
                "Low",
                "Close"
            ]
        )


        intraday.index = to_jst_naive(
            intraday.index
        )


    # =====================================================
    # 最新値
    # =====================================================

    try:

        fast_price = float(
            stock.fast_info[
                "last_price"
            ]
        )


    except Exception:

        fast_price = None


    now = datetime.now(
        JST
    )


    market_open = (
        now.weekday() < 5
        and
        (
            time(9, 0)
            <= now.time()
            < time(11, 30)

            or

            time(12, 30)
            <= now.time()
            < time(15, 30)
        )
    )


    if (
        market_open
        and
        not intraday.empty
    ):

        price = float(
            intraday[
                "Close"
            ].iloc[-1]
        )


    elif (
        fast_price is not None
        and
        fast_price > 0
    ):

        price = fast_price


    else:

        price = float(
            daily[
                "Close"
            ].iloc[-1]
        )


    daily.loc[
        daily.index[-1],
        "Close"
    ] = price


    previous_close = float(
        daily[
            "Close"
        ].iloc[-2]
    )


    change_pct = (
        price
        - previous_close
    ) / previous_close * 100


    # =====================================================
    # 移動平均
    # =====================================================

    closes = daily[
        "Close"
    ]


    ma5 = float(
        closes
        .rolling(5)
        .mean()
        .iloc[-1]
    )


    ma25 = float(
        closes
        .rolling(25)
        .mean()
        .iloc[-1]
    )


    ma75 = float(
        closes
        .rolling(75)
        .mean()
        .iloc[-1]
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
    # ATR
    # =====================================================

    previous_series = (
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
        previous_series
    ).abs()


    tr3 = (
        daily[
            "Low"
        ]
        -
        previous_series
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
    # 基本判定
    # =====================================================

    score = 0


    if price > ma5:
        score += 1
    else:
        score -= 1


    if ma5 > ma25:
        score += 1
    else:
        score -= 1


    if price > ma25:
        score += 1
    else:
        score -= 1


    if ma25 > ma75:
        score += 1
    else:
        score -= 1


    low_distance = (
        price
        - recent_low
    ) / price * 100


    if low_distance <= 2:
        score -= 2


    # =====================================================
    # 抵抗線・突破
    # =====================================================

    round_state = get_round_level_state(
        price,
        daily,
        intraday,
        market_open
    )


    active_info = round_state[
        "level_state"
    ]


    if (
        active_info[
            "state"
        ]
        == "strong_resistance"
    ):

        score -= 1


    if (
        round_state[
            "recent_confirmed_level"
        ]
        is not None
    ):

        score += 1


    # =====================================================
    # 最終危険度
    # =====================================================

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


    # =====================================================
    # 判定連動逆指値
    # =====================================================

    stop_support = (
        prior_20_low
        * 0.995
    )


    base_stop = min(
        stop_support,
        price - atr14 * 1.5
    )


    if risk_rank == 4:

        atr_multiplier = 1.50

        minimum_gap_pct = 0.030


    elif risk_rank == 3:

        atr_multiplier = 1.25

        minimum_gap_pct = 0.025


    elif risk_rank == 2:

        atr_multiplier = 1.00

        minimum_gap_pct = 0.020


    elif risk_rank == 1:

        atr_multiplier = 0.75

        minimum_gap_pct = 0.015


    else:

        atr_multiplier = 0.50

        minimum_gap_pct = 0.012


    defensive_atr_stop = (
        price
        - atr14 * atr_multiplier
    )


    minimum_gap_stop = (
        price
        * (
            1
            - minimum_gap_pct
        )
    )


    defensive_stop = min(
        defensive_atr_stop,
        minimum_gap_stop
    )


    if risk_rank == 4:

        stop_candidate = (
            base_stop
        )

    else:

        stop_candidate = max(
            base_stop,
            defensive_stop
        )


    stop_candidate = min(
        stop_candidate,
        price * 0.995
    )


    stop_candidate = float(
        round(
            stop_candidate
        )
    )


    # =====================================================
    # 保存済み逆指値
    # =====================================================

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


    saved_stop = clean(
        row.get(
            "stop_price"
        )
    )


    initial_stop = clean(
        row.get(
            "initial_stop_price"
        )
    )


    if initial_stop is None:

        if saved_stop is not None:

            initial_stop = float(
                saved_stop
            )

        else:

            initial_stop = float(
                stop_candidate
            )


    if saved_stop is None:

        active_stop = float(
            stop_candidate
        )

    else:

        active_stop = max(
            saved_stop,
            float(stop_candidate)
        )


    portfolio_update = {}


    if clean(
        row.get(
            "initial_stop_price"
        )
    ) is None:

        portfolio_update[
            "initial_stop_price"
        ] = initial_stop


    if (
        saved_stop is None
        or active_stop > saved_stop
    ):

        portfolio_update[
            "stop_price"
        ] = active_stop


    if portfolio_update:

        (
            supabase
            .table(
                "portfolio"
            )
            .update(
                portfolio_update
            )
            .eq(
                "id",
                row[
                    "id"
                ]
            )
            .execute()
        )


    stop_distance = (
        price
        - active_stop
    ) / price * 100


    # =====================================================
    # 利確
    # =====================================================

    buy_price = float(
        row[
            "buy_price"
        ]
    )


    (
        risk,
        tp1,
        tp2
    ) = get_take_profit_lines(
        buy_price,
        initial_stop,
        atr14
    )


    sudden_drop = (
        change_pct <= -3
    )


    stop_near = (
        stop_distance <= 2
    )


    stop_breached = (
        price <= active_stop
    )


    breakdown = (
        price < prior_20_low
    )


    confirmed_level = (
        round_state[
            "recent_confirmed_level"
        ]
    )


    resistance_level = None

    resistance_count = 0


    if (
        active_info[
            "state"
        ]
        in [
            "resistance",
            "strong_resistance"
        ]
    ):

        resistance_level = float(
            active_info[
                "level"
            ]
        )


        resistance_count = int(
            active_info[
                "rejection_count"
            ]
        )


    return {
        "ticker":
            code,

        "name":
            company_name(
                code
            ),

        "price":
            price,

        "change_pct":
            change_pct,

        "score":
            score,

        "risk_rank":
            risk_rank,

        "status":
            status,

        "prior_20_low":
            prior_20_low,

        "stop_price":
            active_stop,

        "stop_distance":
            stop_distance,

        "stop_near":
            stop_near,

        "stop_breached":
            stop_breached,

        "sudden_drop":
            sudden_drop,

        "breakdown":
            breakdown,

        "risk":
            risk,

        "tp1":
            tp1,

        "tp2":
            tp2,

        "confirmed_level":
            confirmed_level,

        "resistance_level":
            resistance_level,

        "resistance_count":
            resistance_count
    }


# =========================================================
# Supabase取得
# =========================================================

portfolio_result = (
    supabase
    .table(
        "portfolio"
    )
    .select("*")
    .execute()
)


holdings = (
    portfolio_result.data
    or []
)


state_result = (
    supabase
    .table(
        "alert_state"
    )
    .select("*")
    .execute()
)


states = {
    str(
        s[
            "ticker"
        ]
    ):
    s

    for s in (
        state_result.data
        or []
    )
}


alerts = []


# =========================================================
# 全銘柄チェック
# =========================================================

for row in holdings:

    code = str(
        row.get(
            "ticker",
            ""
        )
    ).strip()


    if not code:
        continue


    try:

        current = check_stock(
            row
        )


    except Exception as e:

        print(
            f"{code}: {e}"
        )

        continue


    if current is None:
        continue


    previous = states.get(
        code
    )


    reasons = []


    # =====================================================
    # 判定悪化
    # =====================================================

    if previous is not None:

        previous_rank = int(
            previous.get(
                "risk_rank",
                4
            )
        )


        if (
            current[
                "risk_rank"
            ]
            < previous_rank
        ):

            reasons.append(
                "判定悪化 → "
                f"{current['status']}"
            )


    # =====================================================
    # 急落
    # =====================================================

    previous_drop = (
        bool(
            previous.get(
                "sudden_drop",
                False
            )
        )
        if previous
        else False
    )


    if (
        current[
            "sudden_drop"
        ]
        and
        not previous_drop
    ):

        reasons.append(
            f"前日比 "
            f"{current['change_pct']:.2f}%"
        )


    # =====================================================
    # 逆指値接近
    # =====================================================

    previous_stop_near = (
        bool(
            previous.get(
                "stop_near",
                False
            )
        )
        if previous
        else False
    )


    if (
        current[
            "stop_near"
        ]
        and
        not previous_stop_near
    ):

        reasons.append(
            "逆指値まで2%以内"
        )


    # =====================================================
    # 逆指値到達
    # =====================================================

    previous_breached = (
        bool(
            previous.get(
                "stop_breached",
                False
            )
        )
        if previous
        else False
    )


    if (
        current[
            "stop_breached"
        ]
        and
        not previous_breached
    ):

        reasons.append(
            "🚨 逆指値ライン到達・割れ"
        )


    # =====================================================
    # 20日安値割れ
    # =====================================================

    previous_breakdown = (
        bool(
            previous.get(
                "breakdown",
                False
            )
        )
        if previous
        else False
    )


    if (
        current[
            "breakdown"
        ]
        and
        not previous_breakdown
    ):

        reasons.append(
            "20日安値を割りました"
        )


    # =====================================================
    # 節目正式突破
    # =====================================================

    previous_breakout_level = None


    if previous:

        try:

            previous_breakout_level = float(
                previous.get(
                    "breakout_level"
                )
            )

        except Exception:

            previous_breakout_level = None


    confirmed_level = current[
        "confirmed_level"
    ]


    if confirmed_level is not None:

        if (
            previous_breakout_level is None
            or
            confirmed_level
            > previous_breakout_level
        ):

            reasons.append(
                f"✅ {confirmed_level:,.0f}円を正式突破"
            )


    # =====================================================
    # 強い抵抗線になった
    # =====================================================

    previous_resistance_level = None

    previous_resistance_count = 0


    if previous:

        try:

            previous_resistance_level = float(
                previous.get(
                    "resistance_level"
                )
            )

        except Exception:

            previous_resistance_level = None


        try:

            previous_resistance_count = int(
                previous.get(
                    "resistance_count",
                    0
                )
            )

        except Exception:

            previous_resistance_count = 0


    if (
        current[
            "resistance_level"
        ]
        is not None
        and
        current[
            "resistance_count"
        ]
        >= 3
    ):

        if (
            previous_resistance_level
            != current[
                "resistance_level"
            ]
            or
            previous_resistance_count < 3
        ):

            reasons.append(
                f"🧱 {current['resistance_level']:,.0f}円で"
                f"{current['resistance_count']}回跳ね返り"
            )


    # =====================================================
    # 利確
    # =====================================================

    tp1_done = bool(
        row.get(
            "tp1_done",
            False
        )
    )


    tp2_done = bool(
        row.get(
            "tp2_done",
            False
        )
    )


    target_update = {}


    if (
        current[
            "price"
        ]
        >= current[
            "tp2"
        ]
        and
        not tp2_done
    ):

        reasons.append(
            "🎯 利確②到達 "
            f"{current['tp2']:,.0f}円"
        )


        target_update[
            "tp2_done"
        ] = True


        target_update[
            "tp1_done"
        ] = True


    elif (
        current[
            "price"
        ]
        >= current[
            "tp1"
        ]
        and
        not tp1_done
    ):

        reasons.append(
            "🎯 利確①到達 "
            f"{current['tp1']:,.0f}円"
        )


        target_update[
            "tp1_done"
        ] = True


    if target_update:

        (
            supabase
            .table(
                "portfolio"
            )
            .update(
                target_update
            )
            .eq(
                "id",
                row[
                    "id"
                ]
            )
            .execute()
        )


    if reasons:

        current[
            "alert_reasons"
        ] = reasons


        alerts.append(
            current
        )


    # =====================================================
    # 状態保存
    # =====================================================

    stored_breakout_level = (
        previous_breakout_level
    )


    if confirmed_level is not None:

        if (
            stored_breakout_level is None
            or
            confirmed_level
            > stored_breakout_level
        ):

            stored_breakout_level = (
                confirmed_level
            )


    state_data = {
        "ticker":
            code,

        "risk_rank":
            current[
                "risk_rank"
            ],

        "score":
            current[
                "score"
            ],

        "sudden_drop":
            current[
                "sudden_drop"
            ],

        "stop_near":
            current[
                "stop_near"
            ],

        "stop_breached":
            current[
                "stop_breached"
            ],

        "breakdown":
            current[
                "breakdown"
            ],

        "last_price":
            current[
                "price"
            ],

        "breakout_level":
            stored_breakout_level,

        "breakout_confirmed":
            (
                stored_breakout_level
                is not None
            ),

        "resistance_level":
            current[
                "resistance_level"
            ],

        "resistance_count":
            current[
                "resistance_count"
            ],

        "updated_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }


    (
        supabase
        .table(
            "alert_state"
        )
        .upsert(
            state_data,
            on_conflict="ticker"
        )
        .execute()
    )


# =========================================================
# iPhone通知
# =========================================================

if alerts:

    lines = [
        "保有株に重要な変化があります。",
        ""
    ]


    for stock in alerts:

        lines.append(
            f"{stock['status']} "
            f"{stock['ticker']} "
            f"{stock['name']}"
        )


        lines.append(
            f"現在値 "
            f"{stock['price']:,.0f}円 "
            f"({stock['change_pct']:+.2f}%)"
        )


        for reason in stock[
            "alert_reasons"
        ]:

            lines.append(
                f"・{reason}"
            )


        lines.append(
            f"逆指値 "
            f"{stock['stop_price']:,.0f}円"
        )


        lines.append(
            f"利確① "
            f"{stock['tp1']:,.0f}円 / "
            f"利確② "
            f"{stock['tp2']:,.0f}円"
        )


        lines.append("")


    message = "\n".join(
        lines
    )


    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",

        data=message.encode(
            "utf-8"
        ),

        headers={
            "Title":
                "株AI 通知",

            "Priority":
                "high",

            "Tags":
                "warning,chart_with_upwards_trend"
        },

        timeout=30
    )


    response.raise_for_status()


    print(
        "notification sent"
    )


else:

    print(
        "no new alert"
    )