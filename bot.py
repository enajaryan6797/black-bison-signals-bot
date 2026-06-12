import os
import time
import json
import threading
import requests
import telebot

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(TOKEN)

STATE_FILE = "crypto_v2_learning_state.json"
STATE_VERSION = 200

ROUND_TRIP_FEE_PERCENT = 0.50
SCAN_SECONDS = 300

MAX_OPEN_SIGNALS = 20
MAX_SIGNALS_PER_SCAN = 5

MIN_QUOTE_VOLUME_5H = 500_000

MIN_SCORE_LONG = 7
MIN_SCORE_SHORT = 8

MIN_STOP_PERCENT = 4.0
MAX_STOP_PERCENT = 8.0


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        state = {}

    state.setdefault("version", STATE_VERSION)
    state.setdefault("open_signals", [])
    state.setdefault("closed_signals", [])
    state.setdefault("learning_log", [])

    if state.get("version") != STATE_VERSION:
        state = {
            "version": STATE_VERSION,
            "open_signals": [],
            "closed_signals": [],
            "learning_log": []
        }

    return state


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


def get_ohlc(pair, interval):
    data = kraken_get(
        f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    )

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


def trend_direction(candles):
    if len(candles) < 60:
        return "UNKNOWN"

    closes = [c["close"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    if ema20 is None or ema50 is None:
        return "UNKNOWN"

    price = closes[-1]

    if price > ema20 and ema20 > ema50:
        return "UP"

    if price < ema20 and ema20 < ema50:
        return "DOWN"

    return "SIDEWAYS"


def volume_ratio(candles, lookback=20):
    if len(candles) < lookback + 1:
        return 0

    last_volume = candles[-1]["volume"]
    avg_volume = sum(c["volume"] for c in candles[-lookback - 1:-1]) / lookback

    if avg_volume <= 0:
        return 0

    return last_volume / avg_volume


def breakout_signal(candles):
    if len(candles) < 25:
        return None

    last_close = candles[-1]["close"]
    prev_high = max(c["high"] for c in candles[-21:-1])
    prev_low = min(c["low"] for c in candles[-21:-1])
    vol = volume_ratio(candles)

    if last_close > prev_high and vol >= 1.25:
        return "LONG"

    if last_close < prev_low and vol >= 1.25:
        return "SHORT"

    return None


def btc_market_filter():
    candles_240 = get_ohlc("XXBTZUSD", 240)
    candles_1440 = get_ohlc("XXBTZUSD", 1440)

    trend_4h = trend_direction(candles_240)
    trend_24h = trend_direction(candles_1440)

    if trend_24h == "UP" and trend_4h == "UP":
        return "BULLISH"

    if trend_24h == "DOWN" and trend_4h == "DOWN":
        return "BEARISH"

    return "MIXED"


def pair_name(pair):
    return pair.replace("USD", "/USD")


def signal_already_open(pair):
    state = load_state()
    return any(s["pair"] == pair for s in state["open_signals"])


def analyze_pair(pair, market_status):
    try:
        candles_1440 = get_ohlc(pair, 1440)
        candles_240 = get_ohlc(pair, 240)
        candles_60 = get_ohlc(pair, 60)
        candles_15 = get_ohlc(pair, 15)
        candles_5 = get_ohlc(pair, 5)

        if (
            len(candles_1440) < 60 or
            len(candles_240) < 60 or
            len(candles_60) < 60 or
            len(candles_15) < 60 or
            len(candles_5) < 60
        ):
            return None

        trend_24h = trend_direction(candles_1440)
        trend_4h = trend_direction(candles_240)
        trend_1h = trend_direction(candles_60)

        setup_15m = breakout_signal(candles_15)
        entry_5m = breakout_signal(candles_5)

        price = candles_5[-1]["close"]
        vol_5m = volume_ratio(candles_5)
        vol_15m = volume_ratio(candles_15)

        closes_15m = [c["close"] for c in candles_15]
        rsi_15m = rsi(closes_15m, 14)

        atr_15m = atr(candles_15, 14)

        quote_volume_5h = sum(c["volume"] for c in candles_15[-20:]) * price

        if rsi_15m is None or atr_15m is None:
            return None

        if quote_volume_5h < MIN_QUOTE_VOLUME_5H:
            return None

        long_score = 0
        short_score = 0

        if market_status == "BULLISH":
            long_score += 1
        if market_status == "BEARISH":
            short_score += 1

        if trend_24h == "UP":
            long_score += 2
        if trend_24h == "DOWN":
            short_score += 2

        if trend_4h == "UP":
            long_score += 2
        if trend_4h == "DOWN":
            short_score += 2

        if trend_1h == "UP":
            long_score += 1
        if trend_1h == "DOWN":
            short_score += 1

        if setup_15m == "LONG":
            long_score += 2
        if setup_15m == "SHORT":
            short_score += 2

        if entry_5m == "LONG":
            long_score += 2
        if entry_5m == "SHORT":
            short_score += 2

        if vol_5m >= 1.5 or vol_15m >= 1.5:
            long_score += 1
            short_score += 1

        if 42 <= rsi_15m <= 68:
            long_score += 1

        if 32 <= rsi_15m <= 58:
            short_score += 1

        side = None
        score = 0

        if long_score >= MIN_SCORE_LONG and long_score > short_score:
            side = "LONG"
            score = long_score

        elif short_score >= MIN_SCORE_SHORT and short_score > long_score:
            side = "SHORT"
            score = short_score

        else:
            print(
                f"{pair}: no setup | market={market_status} "
                f"24H={trend_24h} 4H={trend_4h} 1H={trend_1h} "
                f"15M={setup_15m} 5M={entry_5m} "
                f"RSI={rsi_15m:.1f} L={long_score} S={short_score}",
                flush=True
            )
            return None

        stop_distance = max(atr_15m * 2.0, price * (MIN_STOP_PERCENT / 100))
        stop_distance = min(stop_distance, price * (MAX_STOP_PERCENT / 100))

        if side == "LONG":
            stop = price - stop_distance
            risk = price - stop
            tp1 = price + risk * 1.5
            tp2 = price + risk * 2.5

        else:
            stop = price + stop_distance
            risk = stop - price
            tp1 = price - risk * 1.5
            tp2 = price - risk * 2.5

            if tp2 <= 0:
                return None

        return {
            "pair": pair,
            "side": side,
            "entry": price,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "score": score,
            "market": market_status,
            "trend_24h": trend_24h,
            "trend_4h": trend_4h,
            "trend_1h": trend_1h,
            "setup_15m": setup_15m,
            "entry_5m": entry_5m,
            "rsi_15m": rsi_15m,
            "vol_5m": vol_5m,
            "vol_15m": vol_15m,
            "quote_volume_5h": quote_volume_5h,
            "status": "OPEN",
            "created_at": int(time.time())
        }

    except Exception as e:
        print(f"{pair} analysis error: {e}", flush=True)
        return None


def send_new_signal(signal):
    state = load_state()

    if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
        print("Max open signals reached", flush=True)
        return

    if signal_already_open(signal["pair"]):
        return

    state["open_signals"].append(signal)

    state["learning_log"].append({
        "pair": signal["pair"],
        "side": signal["side"],
        "score": signal["score"],
        "features": {
            "market": signal["market"],
            "trend_24h": signal["trend_24h"],
            "trend_4h": signal["trend_4h"],
            "trend_1h": signal["trend_1h"],
            "setup_15m": signal["setup_15m"],
            "entry_5m": signal["entry_5m"],
            "rsi_15m": signal["rsi_15m"],
            "vol_5m": signal["vol_5m"],
            "vol_15m": signal["vol_15m"]
        },
        "result": "OPEN",
        "created_at": int(time.time())
    })

    save_state(state)

    stop_percent = abs((signal["entry"] - signal["stop"]) / signal["entry"]) * 100
    tp1_percent = abs((signal["tp1"] - signal["entry"]) / signal["entry"]) * 100
    tp2_percent = abs((signal["tp2"] - signal["entry"]) / signal["entry"]) * 100

    emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    text = f"""🐃 BLACK BISON CRYPTO V2 LEARNING

{emoji} {pair_name(signal['pair'])} — {signal['side']}

Entry: {signal['entry']:.6f}
Stop: {signal['stop']:.6f} ({stop_percent:.2f}%)

TP1: {signal['tp1']:.6f} ({tp1_percent:.2f}%)
TP2: {signal['tp2']:.6f} ({tp2_percent:.2f}%)

Score: {signal['score']}/10
Market: {signal['market']}
24H: {signal['trend_24h']}
4H: {signal['trend_4h']}
1H: {signal['trend_1h']}
15M: {signal['setup_15m']}
5M: {signal['entry_5m']}

RSI 15M: {signal['rsi_15m']:.2f}
Vol 5M: {signal['vol_5m']:.2f}x
Vol 15M: {signal['vol_15m']:.2f}x

Status: OPEN
"""

    print(text, flush=True)

    if CHAT_ID:
        bot.send_message(CHAT_ID, text)


def check_open_signals():
    state = load_state()

    if not state["open_signals"]:
        return

    still_open = []
    changed = False

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

            for item in state["learning_log"]:
                if (
                    item.get("pair") == signal["pair"] and
                    item.get("created_at") == signal["created_at"] and
                    item.get("result") == "OPEN"
                ):
                    item["result"] = result
                    item["net_percent"] = net_percent
                    item["closed_at"] = int(time.time())

            changed = True

            result_emoji = "✅" if result in ["TP1", "TP2"] else "❌"

            text = f"""🐃 BLACK BISON CRYPTO RESULT V2

{result_emoji} {pair_name(signal['pair'])} — {signal['side']}

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

    if changed:
        save_state(state)
    else:
        save_state(state)


def scan_market():
    try:
        check_open_signals()

        pairs = get_usd_pairs()
        market_status = btc_market_filter()

        print(
            f"Scanning {len(pairs)} crypto pairs | Market={market_status}",
            flush=True
        )

        new_signals = 0

        for pair in pairs:
            if new_signals >= MAX_SIGNALS_PER_SCAN:
                break

            state = load_state()

            if len(state["open_signals"]) >= MAX_OPEN_SIGNALS:
                break

            signal = analyze_pair(pair, market_status)

            if signal:
                send_new_signal(signal)
                new_signals += 1

            time.sleep(0.2)

        if new_signals == 0:
            print("No Crypto V2 Learning setups found", flush=True)

    except Exception as e:
        print(f"Scanner error: {e}", flush=True)


def scanner_loop():
    while True:
        scan_market()
        time.sleep(SCAN_SECONDS)


def get_stats_text():
    state = load_state()

    open_count = len(state["open_signals"])
    closed = state["closed_signals"]

    if not closed:
        return f"""📊 BLACK BISON CRYPTO V2 STATS

Closed Signals: 0
Open Signals: {open_count}

No completed results yet.
"""

    wins = [s for s in closed if s["status"] in ["TP1", "TP2"]]
    losses = [s for s in closed if s["status"] == "STOP"]

    total = len(closed)
    win_rate = (len(wins) / total) * 100
    net_total = sum(float(s.get("net_percent", 0)) for s in closed)

    avg_win = sum(float(s.get("net_percent", 0)) for s in wins) / len(wins) if wins else 0
    avg_loss = sum(float(s.get("net_percent", 0)) for s in losses) / len(losses) if losses else 0

    return f"""📊 BLACK BISON CRYPTO V2 STATS

Closed Signals: {total}
Open Signals: {open_count}

✅ Wins: {len(wins)}
❌ Losses: {len(losses)}

🏆 Win Rate: {win_rate:.2f}%

📊 Net Result: {net_total:.2f}%

🟢 Avg Win: {avg_win:.2f}%
🔴 Avg Loss: {avg_loss:.2f}%
"""


def get_open_text():
    state = load_state()

    if not state["open_signals"]:
        return "No open crypto signals."

    lines = ["📌 OPEN CRYPTO SIGNALS\n"]

    for s in state["open_signals"][:30]:
        lines.append(
            f"{pair_name(s['pair'])} {s['side']} | "
            f"Entry {s['entry']:.6f} | "
            f"SL {s['stop']:.6f} | "
            f"TP1 {s['tp1']:.6f} | "
            f"Score {s['score']}"
        )

    return "\n".join(lines)


def get_learn_text():
    state = load_state()
    logs = [x for x in state["learning_log"] if x.get("result") != "OPEN"]

    if not logs:
        return "🧠 Learning: not enough closed crypto trades yet."

    by_score = {}

    for x in logs:
        score = str(x.get("score", "NA"))
        by_score.setdefault(score, {"wins": 0, "losses": 0, "net": 0, "count": 0})

        by_score[score]["count"] += 1
        by_score[score]["net"] += float(x.get("net_percent", 0))

        if x.get("result") in ["TP1", "TP2"]:
            by_score[score]["wins"] += 1
        elif x.get("result") == "STOP":
            by_score[score]["losses"] += 1

    lines = ["🧠 BLACK BISON CRYPTO LEARNING\n"]

    for score, data in sorted(by_score.items()):
        total = data["wins"] + data["losses"]
        win_rate = (data["wins"] / total) * 100 if total > 0 else 0
        lines.append(
            f"Score {score}: {data['count']} closed | "
            f"W {data['wins']} / L {data['losses']} | "
            f"WR {win_rate:.1f}% | Net {data['net']:.2f}%"
        )

    return "\n".join(lines)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Crypto V2 Learning is online 🚀")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")


@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Crypto V2 Learning...")
    scan_market()


@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_stats_text())


@bot.message_handler(commands=["open"])
def open_signals(message):
    bot.reply_to(message, get_open_text())


@bot.message_handler(commands=["learn"])
def learn(message):
    bot.reply_to(message, get_learn_text())


threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Crypto V2 Learning Started", flush=True)

bot.infinity_polling(skip_pending=True)
