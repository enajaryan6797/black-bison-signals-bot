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


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"open_signals": [], "closed_signals": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    for price in values[period:]:
        ema_value = price * k + ema_value * (1 - k)

    return ema_value


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


def analyze_pair(pair):
    candles = get_ohlc(pair, interval=15)

    if len(candles) < 60:
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

    if ema20 is None or ema50 is None or rsi14 is None:
        return None

    avg_volume = sum(volumes[-20:]) / 20
    recent_high = max(highs[-11:-1])
    recent_low = min(lows[-11:-1])

    long_condition = (
        ema20 > ema50 and
        last_close > ema20 and
        45 <= rsi14 <= 68 and
        last_close > recent_high and
        last_volume > avg_volume * 1.5
    )

    if long_condition:
        entry = last_close
        stop = recent_low
        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk * 2
        tp2 = entry + risk * 3

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
            "reason": "Trend + breakout + volume",
            "status": "OPEN",
            "created_at": int(time.time())
        }

    short_condition = (
        ema20 < ema50 and
        last_close < ema20 and
        32 <= rsi14 <= 55 and
        last_close < recent_low and
        last_volume > avg_volume * 1.5
    )

    if short_condition:
        entry = last_close
        stop = recent_high
        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 2
        tp2 = entry - risk * 3

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
            "reason": "Trend breakdown + volume",
            "status": "OPEN",
            "created_at": int(time.time())
        }

    return None


def signal_already_open(state, pair, side):
    for s in state["open_signals"]:
        if s["pair"] == pair and s["side"] == side:
            return True

    return False


def send_new_signal(signal):
    state = load_state()

    if signal_already_open(state, signal["pair"], signal["side"]):
        return

    state["open_signals"].append(signal)
    save_state(state)

    pretty_pair = signal["pair"].replace("USD", "/USD")
    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    text = f"""🐃 BLACK BISON SIGNAL

{side_emoji} {pretty_pair} — {signal['side']}

📍 Entry: {signal['entry']:.6f}
🛑 Stop Loss: {signal['stop']:.6f}

🎯 TP1: {signal['tp1']:.6f}
🎯 TP2: {signal['tp2']:.6f}

📊 RSI: {signal['rsi']:.2f}
📈 EMA20: {signal['ema20']:.6f}
📉 EMA50: {signal['ema50']:.6f}

⚡ Reason: {signal['reason']}

📌 Status: OPEN

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
        result_text = None
        pnl_percent = 0

        if side == "LONG":
            if current_price >= signal["tp2"]:
                result = "TP2"
                pnl_percent = ((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price >= signal["tp1"]:
                result = "TP1"
                pnl_percent = ((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
            elif current_price <= signal["stop"]:
                result = "STOP"
                pnl_percent = ((signal["stop"] - signal["entry"]) / signal["entry"]) * 100

        if side == "SHORT":
            if current_price <= signal["tp2"]:
                result = "TP2"
                pnl_percent = ((signal["entry"] - signal["tp2"]) / signal["entry"]) * 100
            elif current_price <= signal["tp1"]:
                result = "TP1"
                pnl_percent = ((signal["entry"] - signal["tp1"]) / signal["entry"]) * 100
            elif current_price >= signal["stop"]:
                result = "STOP"
                pnl_percent = ((signal["entry"] - signal["stop"]) / signal["entry"]) * 100

        if result:
            pretty_pair = pair.replace("USD", "/USD")

            if result == "STOP":
                result_text = f"""🐃 BLACK BISON RESULT

❌ {pretty_pair} — {side}

STOP LOSS HIT

📍 Entry: {signal['entry']:.6f}
🛑 Stop: {signal['stop']:.6f}
💰 Current: {current_price:.6f}

📉 Result: {pnl_percent:.2f}%
"""
            else:
                result_text = f"""🐃 BLACK BISON RESULT

✅ {pretty_pair} — {side}

{result} HIT

📍 Entry: {signal['entry']:.6f}
🎯 {result}: {signal[result.lower()]:.6f}
💰 Current: {current_price:.6f}

📈 Result: +{pnl_percent:.2f}%
"""

            signal["status"] = result
            signal["closed_at"] = int(time.time())
            signal["closed_price"] = current_price
            signal["pnl_percent"] = pnl_percent

            state["closed_signals"].append(signal)

            print(result_text, flush=True)

            if CHAT_ID:
                bot.send_message(CHAT_ID, result_text)

        else:
            still_open.append(signal)

    state["open_signals"] = still_open
    save_state(state)


def scan_market():
    try:
        pairs = get_usd_pairs()
        found = 0

        for pair in pairs:
            signal = analyze_pair(pair)

            if signal:
                send_new_signal(signal)
                found += 1

            time.sleep(0.3)

        if found == 0:
            print("No valid trade setups found", flush=True)

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
        return f"""📊 BLACK BISON STATS

Closed Signals: 0
Open Signals: {open_count}

No completed results yet.
"""

    wins = len([s for s in closed if s["status"] in ["TP1", "TP2"]])
    losses = len([s for s in closed if s["status"] == "STOP"])
    win_rate = (wins / total) * 100
    total_pnl = sum(s.get("pnl_percent", 0) for s in closed)

    return f"""📊 BLACK BISON STATS

Closed Signals: {total}
Open Signals: {open_count}

✅ Wins: {wins}
❌ Losses: {losses}

🏆 Win Rate: {win_rate:.2f}%
📈 Total Result: {total_pnl:.2f}%
"""


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Signal Bot is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken for real setups...")
    check_open_signals()
    scan_market()


@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_stats_text())


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Signal Bot With Tracking Started", flush=True)

bot.infinity_polling()
