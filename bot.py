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
STATE_VERSION = 2

ROUND_TRIP_FEE_PERCENT = 0.50
MAX_OPEN_SIGNALS = 15
MAX_NEW_SIGNALS_PER_SCAN = 3
MIN_QUOTE_VOLUME_5H = 2_000_000


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if state.get("version") != STATE_VERSION:
            return {
                "version": STATE_VERSION,
                "open_signals": [],
                "closed_signals": []
            }

        return state

    except Exception:
        return {
            "version": STATE_VERSION,
            "open_signals": [],
            "closed_signals": []
        }


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

    for i in range(1, period + 1):
        change = values[-i] - values[-i - 1]

        if change >= 0:
            gains.append(change)
        else:
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

    true_ranges = []

    for i in range(-period, 0):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        true_ranges.append(tr)

    return sum(true_ranges) / period


def get_usd_pairs():
    data = requests.get(
        "https://api.kraken.com/0/public/AssetPairs",
        timeout=20
    ).json()["result"]

    pairs = []

    for pair_id, info in data.items():
        wsname = info.get("wsname", "")
        status = info.get("status", "")

        if wsname.endswith("/USD") and status == "online":
            pairs.append(pair_id)

    return pairs


def get_ohlc(pair, interval=15):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    data = requests.get(url, timeout=20).json()

    result = data.get("result", {})
    keys = [k for k in result.keys() if k != "last"]

    if not keys:
        return []

    candles = result[keys[0]]

    parsed = []

    for c in candles:
        parsed.append({
            "time": c[0],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[6])
        })

    return parsed


def get_current_price(pair):
    data = requests.get(
        f"https://api.kraken.com/0/public/Ticker?pair={pair}",
        timeout=20
    ).json()

    result = data.get("result", {})

    if not result:
        return None

    first_key = list(result.keys())[0]
    return float(result[first_key]["c"][0])


def signal_already_open(state, pair):
    for s in state["open_signals"]:
        if s["pair"] == pair:
            return True

    return False


def analyze_pair(pair):
    candles = get_ohlc(pair, interval=15)

    if len(candles) < 80:
        return None

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
        return None

    avg_volume = sum(volumes[-20:]) / 20
    quote_volume_5h = sum(volumes[-20:]) * last_close

    if quote_volume_5h < MIN_QUOTE_VOLUME_5H:
        return None

    recent_high = max(highs[-21:-1])
    recent_low = min(lows[-21:-1])

    stop_distance = max(atr14 * 2.0, last_close * 0.03)
    stop_distance = min(stop_distance, last_close * 0.08)

    long_condition = (
        ema20 > ema50 and
        last_close > ema20 and
        50 <= rsi14 <= 62 and
        last_close > recent_high and
        last_volume > avg_volume * 2.0
    )

    if long_condition:
        entry = last_close
        stop = entry - stop_distance
        risk = entry - stop

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.5

        return {
            "side": "LONG",
            "pair": pair,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "rsi": rsi14,
            "ema20": ema20,
            "ema50": ema50,
            "atr": atr14,
            "quote_volume_5h": quote_volume_5h,
            "reason": "Strong trend + breakout + high volume + ATR stop",
            "status": "OPEN",
            "created_at": int(time.time())
        }

    short_condition = (
        ema20 < ema50 and
        last_close < ema20 and
        38 <= rsi14 <= 50 and
        last_close < recent_low and
        last_volume > avg_volume * 2.0
    )

    if short_condition:
        entry = last_close
        stop = entry + stop_distance
        risk = stop - entry

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.5

        if tp2 <= 0:
            return None

        return {
            "side": "SHORT",
            "pair": pair,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "rsi": rsi14,
            "ema20": ema20,
            "ema50": ema50,
            "atr": atr14,
            "quote_volume_5h": quote_volume_5h,
            "reason": "Trend breakdown + high volume + ATR stop",
            "status": "OPEN",
            "created_at": int(time.time())
        }

    return None


def send_new_signal(signal):
    state = load_state()

    if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
        print("Max open signals reached", flush=True)
        return

    if signal_already_open(state, signal["pair"]):
        return

    state["open_signals"].append(signal)
    save_state(state)

    pretty_pair = signal["pair"].replace("USD", "/USD")
    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    stop_percent = abs((signal["entry"] - signal["stop"]) / signal["entry"]) * 100
    tp1_percent = abs((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
    tp2_percent = abs((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100

    text = f"""🐃 BLACK BISON SIGNAL V2

{side_emoji} {pretty_pair} — {signal['side']}

📍 Entry: {signal['entry']:.6f}
🛑 Stop Loss: {signal['stop']:.6f} ({stop_percent:.2f}%)

🎯 TP1: {signal['tp1']:.6f} ({tp1_percent:.2f}%)
🎯 TP2: {signal['tp2']:.6f} ({tp2_percent:.2f}%)

📊 RSI: {signal['rsi']:.2f}
📈 EMA20: {signal['ema20']:.6f}
📉 EMA50: {signal['ema50']:.6f}
🌊 ATR: {signal['atr']:.6f}
💵 5H Volume: ${signal['quote_volume_5h']:,.0f}

⚡ Reason: {signal['reason']}

📌 Status: OPEN
💸 Fees counted in stats: {ROUND_TRIP_FEE_PERCENT:.2f}%

⚠️ NFA — Not Financial Advice
Trade at your own risk.
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
        pair = signal["pair"]
        side = signal["side"]
        current_price = get_current_price(pair)

        if current_price is None:
            still_open.append(signal)
            continue

        result = None
        gross_percent = 0

        if side == "LONG":
            if current_price >= signal["tp2"]:
                result = "TP2"
                gross_percent = ((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price >= signal["tp1"]:
                result = "TP1"
                gross_percent = ((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price <= signal["stop"]:
                result = "STOP"
                gross_percent = ((signal["stop"] - signal["entry"]) / signal["entry"]) * 100

        if side == "SHORT":
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
            pretty_pair = pair.replace("USD", "/USD")
            net_percent = gross_percent - ROUND_TRIP_FEE_PERCENT

            signal["status"] = result
            signal["closed_at"] = int(time.time())
            signal["closed_price"] = current_price
            signal["gross_percent"] = gross_percent
            signal["net_percent"] = net_percent

            state["closed_signals"].append(signal)

            if result == "STOP":
                text = f"""🐃 BLACK BISON RESULT V2

❌ {pretty_pair} — {side}

STOP LOSS HIT

📍 Entry: {signal['entry']:.6f}
🛑 Stop: {signal['stop']:.6f}
💰 Current: {current_price:.6f}

📉 Gross: {gross_percent:.2f}%
💸 Fees: -{ROUND_TRIP_FEE_PERCENT:.2f}%
📊 Net Result: {net_percent:.2f}%
"""
            else:
                text = f"""🐃 BLACK BISON RESULT V2

✅ {pretty_pair} — {side}

{result} HIT

📍 Entry: {signal['entry']:.6f}
🎯 {result}: {signal[result.lower()]:.6f}
💰 Current: {current_price:.6f}

📈 Gross: +{gross_percent:.2f}%
💸 Fees: -{ROUND_TRIP_FEE_PERCENT:.2f}%
📊 Net Result: +{net_percent:.2f}%
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

        for pair in pairs:
            if new_signals >= MAX_NEW_SIGNALS_PER_SCAN:
                break

            state = load_state()

            if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
                break

            signal = analyze_pair(pair)

            if signal:
                send_new_signal(signal)
                new_signals += 1

            time.sleep(0.35)

        if new_signals == 0:
            print("No valid V2 setups found", flush=True)

    except Exception as e:
        print(f"Scanner error: {e}", flush=True)


def scanner_loop():
    while True:
        check_open_signals()
        scan_market()
        time.sleep(300)


def get_stats_text():
    state = load_state()
    closed = state["closed_signals"]
    open_count = len(state["open_signals"])

    total = len(closed)

    if total == 0:
        return f"""📊 BLACK BISON STATS V2

Closed Signals: 0
Open Signals: {open_count}

No completed results yet.
"""

    wins = len([s for s in closed if s["status"] in ["TP1", "TP2"]])
    losses = len([s for s in closed if s["status"] == "STOP"])

    win_rate = (wins / total) * 100
    gross_total = sum(s.get("gross_percent", 0) for s in closed)
    net_total = sum(s.get("net_percent", 0) for s in closed)

    avg_win = 0
    avg_loss = 0

    win_values = [s.get("net_percent", 0) for s in closed if s["status"] in ["TP1", "TP2"]]
    loss_values = [s.get("net_percent", 0) for s in closed if s["status"] == "STOP"]

    if win_values:
        avg_win = sum(win_values) / len(win_values)

    if loss_values:
        avg_loss = sum(loss_values) / len(loss_values)

    return f"""📊 BLACK BISON STATS V2

Closed Signals: {total}
Open Signals: {open_count}

✅ Wins: {wins}
❌ Losses: {losses}

🏆 Win Rate: {win_rate:.2f}%

📈 Gross Result: {gross_total:.2f}%
💸 Fees Included
📊 Net Result: {net_total:.2f}%

🟢 Avg Win: {avg_win:.2f}%
🔴 Avg Loss: {avg_loss:.2f}%
"""


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Signal Bot V2 is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken with V2 rules...")
    check_open_signals()
    scan_market()


@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_stats_text())


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Signal Bot V2 Started", flush=True)

bot.infinity_polling()
