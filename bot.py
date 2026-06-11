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
STATE_VERSION = 100

ROUND_TRIP_FEE_PERCENT = 0.50
SCAN_SECONDS = 300

MIN_CHANGE_PERCENT = 3.0
MIN_QUOTE_VOLUME = 500_000

MIN_STOP_PERCENT = 4.0
MAX_STOP_PERCENT = 8.0

MAX_SIGNALS_PER_SCAN = 5
MAX_OPEN_SIGNALS = 20


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


def get_ticker_data(pairs):
    signals = []

    for i in range(0, len(pairs), 40):
        chunk = ",".join(pairs[i:i + 40])

        data = kraken_get(f"https://api.kraken.com/0/public/Ticker?pair={chunk}")

        if not data:
            continue

        for pair, t in data.get("result", {}).items():
            try:
                price = float(t["c"][0])
                open_price = float(t["o"])
                volume = float(t["v"][1])
                quote_volume = price * volume

                if open_price <= 0:
                    continue

                change = ((price - open_price) / open_price) * 100

                if change >= MIN_CHANGE_PERCENT and quote_volume >= MIN_QUOTE_VOLUME:
                    signals.append({
                        "pair": pair,
                        "price": price,
                        "change": change,
                        "volume": quote_volume
                    })

            except Exception as e:
                print(f"Ticker parse error {pair}: {e}", flush=True)

        time.sleep(0.3)

    signals.sort(key=lambda x: x["change"], reverse=True)
    return signals[:MAX_SIGNALS_PER_SCAN]


def pair_name(pair):
    return pair.replace("USD", "/USD")


def signal_already_open(state, pair):
    return any(s["pair"] == pair for s in state["open_signals"])


def create_signal(raw):
    pair = raw["pair"]
    entry = raw["price"]

    stop_percent = MIN_STOP_PERCENT

    if raw["change"] >= 10:
        stop_percent = 6.0

    if raw["change"] >= 20:
        stop_percent = MAX_STOP_PERCENT

    stop = entry * (1 - stop_percent / 100)
    risk = entry - stop

    tp1 = entry + risk * 1.5
    tp2 = entry + risk * 2.5

    return {
        "pair": pair,
        "side": "LONG",
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "change": raw["change"],
        "volume": raw["volume"],
        "stop_percent": stop_percent,
        "status": "OPEN",
        "created_at": int(time.time())
    }


def send_new_signal(signal):
    state = load_state()

    if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
        print("Max open signals reached", flush=True)
        return

    if signal_already_open(state, signal["pair"]):
        return

    state["open_signals"].append(signal)
    save_state(state)

    tp1_percent = ((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
    tp2_percent = ((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100

    text = f"""🐃 BLACK BISON CRYPTO SIGNAL V1 RELOADED

🟢 {pair_name(signal['pair'])} — LONG

📍 Entry: {signal['entry']:.6f}
🛑 Stop Loss: {signal['stop']:.6f} ({signal['stop_percent']:.2f}%)

🎯 TP1: {signal['tp1']:.6f} (+{tp1_percent:.2f}%)
🎯 TP2: {signal['tp2']:.6f} (+{tp2_percent:.2f}%)

🔥 24H Change: +{signal['change']:.2f}%
📊 Volume: ${signal['volume']:,.0f}

⚡ Reason: Strong daily move + volume
📌 Status: OPEN

⚠️ Not financial advice.
"""

    print(text, flush=True)

    if CHAT_ID:
        bot.send_message(CHAT_ID, text)


def get_current_price(pair):
    data = kraken_get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")

    if not data:
        return None

    result = data.get("result", {})

    if not result:
        return None

    key = list(result.keys())[0]
    return float(result[key]["c"][0])


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

        if current_price >= signal["tp2"]:
            result = "TP2"
            gross_percent = ((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100

        elif current_price >= signal["tp1"]:
            result = "TP1"
            gross_percent = ((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100

        elif current_price <= signal["stop"]:
            result = "STOP"
            gross_percent = ((signal["stop"] - signal["entry"]) / signal["entry"]) * 100

        if result:
            net_percent = gross_percent - ROUND_TRIP_FEE_PERCENT

            signal["status"] = result
            signal["closed_at"] = int(time.time())
            signal["closed_price"] = current_price
            signal["gross_percent"] = gross_percent
            signal["net_percent"] = net_percent

            state["closed_signals"].append(signal)

            emoji = "✅" if result in ["TP1", "TP2"] else "❌"

            text = f"""🐃 BLACK BISON CRYPTO RESULT

{emoji} {pair_name(signal['pair'])} — LONG

Result: {result}

📍 Entry: {signal['entry']:.6f}
💰 Current: {current_price:.6f}

📈 Gross: {gross_percent:.2f}%
💸 Fees: -{ROUND_TRIP_FEE_PERCENT:.2f}%
📊 Net: {net_percent:.2f}%
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
        print(f"Scanning {len(pairs)} Kraken USD pairs with V1 Reloaded...", flush=True)

        raw_signals = get_ticker_data(pairs)

        if not raw_signals:
            print("No V1 Reloaded crypto signals found", flush=True)
            return

        for raw in raw_signals:
            signal = create_signal(raw)
            send_new_signal(signal)

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
        return f"""📊 BLACK BISON CRYPTO STATS

Closed Signals: 0
Open Signals: {open_count}

No completed results yet.
"""

    total = len(closed)
    wins = len([s for s in closed if s["status"] in ["TP1", "TP2"]])
    losses = len([s for s in closed if s["status"] == "STOP"])
    win_rate = (wins / total) * 100
    net_total = sum(s.get("net_percent", 0) for s in closed)

    return f"""📊 BLACK BISON CRYPTO STATS

Closed Signals: {total}
Open Signals: {open_count}

✅ Wins: {wins}
❌ Losses: {losses}

🏆 Win Rate: {win_rate:.2f}%
📊 Net Result: {net_total:.2f}%
"""


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Crypto Bot V1 Reloaded is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken with V1 Reloaded rules...")
    check_open_signals()
    scan_market()


@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_stats_text())


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Crypto Bot V1 Reloaded Started", flush=True)

bot.infinity_polling(skip_pending=True)
