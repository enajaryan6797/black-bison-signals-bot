import os
import time
import json
import threading
import requests
import telebot

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(TOKEN)

STATE_FILE = "signals_state.json"
STATE_VERSION = 22

ROUND_TRIP_FEE_PERCENT = 0.50
MAX_OPEN_SIGNALS = 15
MAX_NEW_SIGNALS_PER_SCAN = 3
MIN_QUOTE_VOLUME_5H = 500_000
SCAN_SECONDS = 300


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("version") != STATE_VERSION:
            return {"version": STATE_VERSION, "open_signals": [], "closed_signals": []}
        return state
    except Exception:
        return {"version": STATE_VERSION, "open_signals": [], "closed_signals": []}


def save_state(state):
    state["version"] = STATE_VERSION
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = price * k + value * (1 - k)
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(-period, 0):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    return sum(trs) / period


def kraken_get(url):
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if data.get("error"):
            print(f"KRAKEN ERROR: {data.get('error')}", flush=True)
            return None
        return data
    except Exception as e:
        print(f"KRAKEN REQUEST ERROR: {e}", flush=True)
        return None


def get_usd_pairs():
    data = kraken_get("https://api.kraken.com/0/public/AssetPairs")

    if not data:
        return []

    pairs = []

    for pair_id, info in data["result"].items():
        wsname = info.get("wsname", "")
        status = info.get("status", "")

        if wsname.endswith("/USD") and status == "online":
            pairs.append(pair_id)

    return pairs


def get_ohlc(pair, interval=15):
    data = kraken_get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")

    if not data:
        return []

    result = data.get("result", {})
    keys = [k for k in result.keys() if k != "last"]

    if not keys:
        return []

    raw = result[keys[0]]
    candles = []

    for c in raw:
        candles.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[6])
        })

    return candles


def get_current_price(pair):
    data = kraken_get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")

    if not data:
        return None

    result = data.get("result", {})

    if not result:
        return None

    key = list(result.keys())[0]
    return float(result[key]["c"][0])


def pair_name(pair):
    return pair.replace("USD", "/USD")


def get_metrics(pair):
    candles = get_ohlc(pair, 15)

    if len(candles) < 80:
        return None, "not enough candles"

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    last_close = closes[-1]
    last_volume = volumes[-1]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)

    if ema20 is None or ema50 is None or rsi14 is None or atr14 is None:
        return None, "indicator error"

    avg_volume = sum(volumes[-20:]) / 20
    volume_ratio = last_volume / avg_volume if avg_volume > 0 else 0
    quote_volume_5h = sum(volumes[-20:]) * last_close

    recent_high = max(highs[-21:-1])
    recent_low = min(lows[-21:-1])

    metrics = {
        "pair": pair,
        "price": last_close,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi14,
        "atr": atr14,
        "volume_ratio": volume_ratio,
        "quote_volume_5h": quote_volume_5h,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "breakout_up": last_close > recent_high,
        "breakdown_down": last_close < recent_low,
        "trend_up": ema20 > ema50,
        "trend_down": ema20 < ema50,
    }

    return metrics, None


def signal_already_open(state, pair):
    return any(s["pair"] == pair for s in state["open_signals"])


def analyze_pair(pair):
    metrics, error = get_metrics(pair)

    if error:
        return None, error, None

    if metrics["quote_volume_5h"] < MIN_QUOTE_VOLUME_5H:
        return None, "low volume", metrics

    price = metrics["price"]

    stop_distance = max(metrics["atr"] * 2.0, price * 0.025)
    stop_distance = min(stop_distance, price * 0.08)

    long_condition = (
        metrics["trend_up"] and
        price > metrics["ema20"] and
        42 <= metrics["rsi"] <= 70 and
        metrics["breakout_up"] and
        metrics["volume_ratio"] >= 1.25
    )

    if long_condition:
        entry = price
        stop = entry - stop_distance
        risk = entry - stop

        return {
            "side": "LONG",
            "pair": pair,
            "entry": entry,
            "stop": stop,
            "tp1": entry + risk * 1.5,
            "tp2": entry + risk * 2.5,
            "rsi": metrics["rsi"],
            "ema20": metrics["ema20"],
            "ema50": metrics["ema50"],
            "atr": metrics["atr"],
            "volume_ratio": metrics["volume_ratio"],
            "quote_volume_5h": metrics["quote_volume_5h"],
            "reason": "V2.2 LONG: trend + breakout + volume",
            "status": "OPEN",
            "created_at": int(time.time())
        }, None, metrics

    short_condition = (
        metrics["trend_down"] and
        price < metrics["ema20"] and
        30 <= metrics["rsi"] <= 58 and
        metrics["breakdown_down"] and
        metrics["volume_ratio"] >= 1.25
    )

    if short_condition:
        entry = price
        stop = entry + stop_distance
        risk = stop - entry

        return {
            "side": "SHORT",
            "pair": pair,
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk * 1.5,
            "tp2": entry - risk * 2.5,
            "rsi": metrics["rsi"],
            "ema20": metrics["ema20"],
            "ema50": metrics["ema50"],
            "atr": metrics["atr"],
            "volume_ratio": metrics["volume_ratio"],
            "quote_volume_5h": metrics["quote_volume_5h"],
            "reason": "V2.2 SHORT: trend + breakdown + volume",
            "status": "OPEN",
            "created_at": int(time.time())
        }, None, metrics

    return None, "conditions not met", metrics


def send_new_signal(signal):
    state = load_state()

    if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
        print("Max open signals reached", flush=True)
        return

    if signal_already_open(state, signal["pair"]):
        return

    state["open_signals"].append(signal)
    save_state(state)

    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    stop_percent = abs((signal["entry"] - signal["stop"]) / signal["entry"]) * 100
    tp1_percent = abs((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
    tp2_percent = abs((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100

    text = f"""🐃 BLACK BISON SIGNAL V2.2

{side_emoji} {pair_name(signal['pair'])} — {signal['side']}

📍 Entry: {signal['entry']:.6f}
🛑 Stop Loss: {signal['stop']:.6f} ({stop_percent:.2f}%)

🎯 TP1: {signal['tp1']:.6f} ({tp1_percent:.2f}%)
🎯 TP2: {signal['tp2']:.6f} ({tp2_percent:.2f}%)

📊 RSI: {signal['rsi']:.2f}
📈 EMA20: {signal['ema20']:.6f}
📉 EMA50: {signal['ema50']:.6f}
📊 Volume Ratio: {signal['volume_ratio']:.2f}x
💵 5H Volume: ${signal['quote_volume_5h']:,.0f}

⚡ Reason: {signal['reason']}

⚠️ Not financial advice.
"""

    print(text, flush=True)

    if CHAT_ID:
        bot.send_message(CHAT_ID, text)


def check_open_signals():
    state = load_state()

    if not state["open_signals"]:
        return

    still_open = []

    for signal in state["open_signals"]:
        current_price = get_current_price(signal["pair"])

        if current_price is None:
            still_open.append(signal)
            continue

        result = None
        gross_percent = 0

        if signal["side"] == "LONG":
            if current_price >= signal["tp2"]:
                result = "TP2"
                gross_percent = ((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price >= signal["tp1"]:
                result = "TP1"
                gross_percent = ((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price <= signal["stop"]:
                result = "STOP"
                gross_percent = ((signal["stop"] - signal["entry"]) / signal["entry"]) * 100

        if signal["side"] == "SHORT":
            if current_price <= signal["tp2"]:
                result = "TP2"
                gross_percent = ((signal["entry"] - signal["tp2"]) / signal["entry"]) * 100
            elif current_price <= signal["tp1"]:
                result = "TP1"
                gross_percent = ((signal["entry"] - signal["tp1"]) / signal["entry"]) * 100
            elif current_price >= signal["stop"]:
                result = "STOP"
                gross_percent = ((signal["entry"] - signal["stop"]) / signal["entry"]) * 100

        if result:
            net_percent = gross_percent - ROUND_TRIP_FEE_PERCENT

            signal["status"] = result
            signal["closed_at"] = int(time.time())
            signal["closed_price"] = current_price
            signal["gross_percent"] = gross_percent
            signal["net_percent"] = net_percent

            state["closed_signals"].append(signal)

            text = f"""🐃 BLACK BISON RESULT V2.2

{pair_name(signal['pair'])} — {signal['side']}
Result: {result}

Entry: {signal['entry']:.6f}
Current: {current_price:.6f}

Gross: {gross_percent:.2f}%
Fees: -{ROUND_TRIP_FEE_PERCENT:.2f}%
Net: {net_percent:.2f}%
"""

            print(text, flush=True)

            if CHAT_ID:
                bot.send_message(CHAT_ID, text)
        else:
            still_open.append(signal)

    state["open_signals"] = still_open
    save_state(state)


def scan_market():
    try:
        pairs = get_usd_pairs()
        new_signals = 0
        debug_rows = []

        print(f"Scanning {len(pairs)} USD pairs with V2.2...", flush=True)

        for pair in pairs:
            if new_signals >= MAX_NEW_SIGNALS_PER_SCAN:
                break

            state = load_state()

            if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
                break

            signal, reject_reason, metrics = analyze_pair(pair)

            if metrics and len(debug_rows) < 10:
                debug_rows.append(metrics)

            if signal:
                send_new_signal(signal)
                new_signals += 1

            time.sleep(0.25)

        if new_signals == 0:
            print("No valid V2.2 setups found", flush=True)
            print("---- DEBUG SAMPLE ----", flush=True)

            for m in debug_rows:
                print(
                    f"{m['pair']} | price={m['price']:.6f} | "
                    f"RSI={m['rsi']:.2f} | "
                    f"EMA20>{'YES' if m['trend_up'] else 'NO'} | "
                    f"VOL={m['volume_ratio']:.2f}x | "
                    f"UP_BREAK={m['breakout_up']} | "
                    f"DOWN_BREAK={m['breakdown_down']} | "
                    f"5H_VOL=${m['quote_volume_5h']:,.0f}",
                    flush=True
                )

    except Exception as e:
        print(f"Scanner error: {e}", flush=True)


def scanner_loop():
    while True:
        check_open_signals()
        scan_market()
        time.sleep(SCAN_SECONDS)


def get_stats_text():
    state = load_state()
    closed = state["closed_signals"]
    open_count = len(state["open_signals"])

    if not closed:
        return f"""📊 BLACK BISON STATS V2.2

Closed Signals: 0
Open Signals: {open_count}

No completed results yet.
"""

    total = len(closed)
    wins = len([s for s in closed if s["status"] in ["TP1", "TP2"]])
    losses = len([s for s in closed if s["status"] == "STOP"])
    win_rate = (wins / total) * 100
    net_total = sum(s.get("net_percent", 0) for s in closed)

    return f"""📊 BLACK BISON STATS V2.2

Closed Signals: {total}
Open Signals: {open_count}

Wins: {wins}
Losses: {losses}
Win Rate: {win_rate:.2f}%

Net Result: {net_total:.2f}%
"""


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Crypto Bot V2.2 is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken with V2.2 rules...")
    check_open_signals()
    scan_market()


@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_stats_text())


@bot.message_handler(commands=["debug"])
def debug(message):
    pairs = get_usd_pairs()[:10]
    rows = []

    for pair in pairs:
        metrics, error = get_metrics(pair)

        if error:
            rows.append(f"{pair}: {error}")
        else:
            rows.append(
                f"{pair}: RSI {metrics['rsi']:.1f}, "
                f"Vol {metrics['volume_ratio']:.2f}x, "
                f"UpBreak {metrics['breakout_up']}, "
                f"DownBreak {metrics['breakdown_down']}"
            )

        time.sleep(0.25)

    bot.reply_to(message, "🔍 DEBUG V2.2\n\n" + "\n".join(rows))


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Crypto Bot V2.2 Started", flush=True)

bot.infinity_polling(skip_pending=True)
