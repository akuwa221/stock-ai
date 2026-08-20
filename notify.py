import os
import requests
import yfinance as yf

from supabase import create_client


# =========================================================
# GitHub Secretsから読み込み
# =========================================================

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]


# =========================================================
# Supabase接続
# =========================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# =========================================================
# 日本株コード変換
# =========================================================

def normalize_ticker(code):

    code = str(code).strip()

    if code.isdigit() and len(code) == 4:
        return f"{code}.T"

    return code


# =========================================================
# 1銘柄をチェック
# =========================================================

def check_stock(code):

    ticker_code = normalize_ticker(
        code
    )

    stock = yf.Ticker(
        ticker_code
    )


    # -----------------------------------------------------
    # 過去データ
    # -----------------------------------------------------

    daily = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
        actions=False
    )


    daily = daily.dropna(
        subset=[
            "Close",
            "Low"
        ]
    )


    if len(daily) < 75:
        return None


    # -----------------------------------------------------
    # 最新株価
    # -----------------------------------------------------

    try:

        price = float(
            stock.fast_info[
                "last_price"
            ]
        )

    except Exception:

        price = float(
            daily[
                "Close"
            ].iloc[-1]
        )


    # -----------------------------------------------------
    # 銘柄名
    # -----------------------------------------------------

    try:

        info = stock.get_info()

        name = (
            info.get("shortName")
            or info.get("longName")
            or str(code)
        )

    except Exception:

        name = str(code)


    # -----------------------------------------------------
    # 最新価格を移動平均計算へ反映
    # -----------------------------------------------------

    closes = (
        daily[
            "Close"
        ]
        .copy()
    )

    closes.iloc[-1] = price


    # -----------------------------------------------------
    # 移動平均
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 20日安値
    # -----------------------------------------------------

    recent_low = float(
        daily[
            "Low"
        ]
        .tail(20)
        .min()
    )


    # -----------------------------------------------------
    # 判定スコア
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # 20日安値接近
    # -----------------------------------------------------

    low_distance = (
        price
        - recent_low
    ) / price * 100


    if low_distance <= 2:
        score -= 2


    # -----------------------------------------------------
    # 通知対象判定
    # -----------------------------------------------------

    if score <= -3:

        status = "🔴 危険"

    elif score <= -1:

        status = "🟠 注意"

    else:

        status = None


    return {
        "code": str(code),
        "name": name,
        "price": price,
        "score": score,
        "status": status,
        "recent_low": recent_low
    }


# =========================================================
# Supabaseから保有株取得
# =========================================================

result = (
    supabase
    .table("portfolio")
    .select("*")
    .execute()
)


holdings = (
    result.data
    or []
)


# =========================================================
# 全保有株チェック
# =========================================================

alerts = []


for row in holdings:

    code = row.get(
        "ticker"
    )

    if not code:
        continue

    try:

        checked = check_stock(
            code
        )

        if (
            checked
            and checked["status"]
        ):

            alerts.append(
                checked
            )

    except Exception as e:

        print(
            f"{code}: {e}"
        )


# =========================================================
# 警告対象があるときだけスマホ通知
# =========================================================

if alerts:

    lines = [
        "保有株に注意が必要です。",
        ""
    ]


    for stock in alerts:

        lines.append(
            f"{stock['status']} "
            f"{stock['code']} "
            f"{stock['name']}"
        )

        lines.append(
            f"現在値: "
            f"{stock['price']:,.0f}円"
        )

        lines.append(
            f"20日安値: "
            f"{stock['recent_low']:,.0f}円"
        )

        lines.append(
            f"判定スコア: "
            f"{stock['score']}"
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
            "Title": "Stock AI Alert",
            "Priority": "high",
            "Tags": "warning,chart_with_downwards_trend"
        },

        timeout=30
    )


    response.raise_for_status()

    print(
        "notification sent"
    )


else:

    print(
        "no alert"
    )