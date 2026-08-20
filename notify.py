import os

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
# 利確ライン
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


    ticker_code = (
        normalize_ticker(
            code
        )
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

    daily.index = (
        to_jst_naive(
            daily.index
        )
    )


    try:

        intraday = stock.history(
            period="5d",
            interval="5m",
            auto_adjust=False,
            prepost=False,
            actions=False
        )


    except Exception:

        intraday = pd.DataFrame()


    if not intraday.empty:

        intraday = (
            intraday.dropna(
                subset=[
                    "Open",
                    "High",
                    "Low",
                    "Close"
                ]
            )
        )


        intraday.index = (
            to_jst_naive(
                intraday.index
            )
        )


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


    prior_20_low = float(
        daily[
            "Low"
        ]
        .iloc[:-1]
        .tail(20)
        .min()
    )


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
    # 逆指値
    # =====================================================

    stop_support = (
        prior_20_low
        * 0.995
    )


    stop_atr = (
        price
        - atr14 * 1.5
    )


    stop_candidate = round(
        min(
            stop_support,
            stop_atr
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
            float(
                stop_candidate
            )
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


    # =====================================================
    # 判定
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
        - prior_20_low
    ) / price * 100


    if low_distance <= 2:
        score -= 2


    if score >= 4:

        risk_rank = 4

        status = (
            "🟢 強い上昇"
        )


    elif score >= 2:

        risk_rank = 3

        status = (
            "🟢 上昇"
        )


    elif score >= 0:

        risk_rank = 2

        status = (
            "🟡 様子見"
        )


    elif score >= -2:

        risk_rank = 1

        status = (
            "🟠 注意"
        )


    else:

        risk_rank = 0

        status = (
            "🔴 危険"
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
            tp2
    }


# =========================================================
# データ取得
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


    # -----------------------------------------------------
    # 判定悪化
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 急落
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 逆指値接近
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 逆指値到達
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 20日安値割れ
    # -----------------------------------------------------

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
    # 利確通知
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


    # 利確②まで一気に上がった場合
    # ②だけ通知して①も通知済みにする

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
    # 前回状態保存
    # =====================================================

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