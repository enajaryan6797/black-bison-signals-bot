import os
import time
import threading
import requests
import telebot

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(TOKEN)
sent_signals = set()


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

    # LONG setup
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
            "reason": "Trend + breakout + volume"
        }

    # SHORT setup
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
            "reason": "Trend breakdown + volume"
        }

    return None


def send_signal(signal):
    pretty_pair = signal["pair"].replace("USD", "/USD")
    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    signal_key = f"{signal['pair']}-{signal['side']}-{round(signal['entry'], 5)}"

    if signal_key in sent_signals:
        return

    sent_signals.add(signal_key)

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

⚠️ NFA — Not Financial Advice
Trade at your own risk.
"""

    print(text, flush=True)

    if CHAT_ID:
        bot.send_message(CHAT_ID, text)


def scan_market():
    try:
        pairs = get_usd_pairs()
        found = 0

        for pair in pairs:
            signal = analyze_pair(pair)

            if signal:
                send_signal(signal)
                found += 1

            time.sleep(0.3)

        if found == 0:
            print("No valid trade setups found", flush=True)

    except Exception as e:
        print(f"Scanner error: {e}", flush=True)


def scanner_loop():
    while True:
        scan_market()
        time.sleep(300)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Signal Bot is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken for real setups...")
    scan_market()


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Signal Bot Started", flush=True)

bot.infinity_polling()
