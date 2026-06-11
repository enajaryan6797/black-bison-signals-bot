import os
import time
import threading
import requests
import telebot

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(TOKEN)

def get_kraken_signals():
    pairs_data = requests.get("https://api.kraken.com/0/public/AssetPairs", timeout=20).json()["result"]

    usd_pairs = []
    for pair_id, info in pairs_data.items():
        wsname = info.get("wsname", "")
        status = info.get("status", "")
        if wsname.endswith("/USD") and status == "online":
            usd_pairs.append(pair_id)

    signals = []

    for i in range(0, len(usd_pairs), 40):
        chunk = ",".join(usd_pairs[i:i+40])
        data = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={chunk}", timeout=20).json()

        for pair, t in data.get("result", {}).items():
            price = float(t["c"][0])
            open_price = float(t["o"])
            volume = float(t["v"][1])
            quote_volume = price * volume

            if open_price > 0:
                change = ((price - open_price) / open_price) * 100

                if change >= 3 and quote_volume >= 500000:
                    signals.append((pair, price, change, quote_volume))

    signals.sort(key=lambda x: x[2], reverse=True)
    return signals[:5]

def send_scan():
    try:
        signals = get_kraken_signals()

        if not signals:
            print("No Kraken signals found", flush=True)
            return

        text = "🐃 BLACK BISON KRAKEN SCANNER\n\n"

        for pair, price, change, volume in signals:
    pretty_pair = pair.replace("USD", "/USD")
    text += f"📈 Pair: {pretty_pair}\n"
            text += f"💰 Price: {price}\n"
            text += f"🔥 Change: +{change:.2f}%\n"
            text += f"📊 Volume: ${volume:,.0f}\n"
            text += "⚠️ Watch only — not financial advice\n\n"

        print(text, flush=True)

        if CHAT_ID:
            bot.send_message(CHAT_ID, text)

    except Exception as e:
        print(f"Scanner error: {e}", flush=True)

def scanner_loop():
    while True:
        send_scan()
        time.sleep(300)

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Black Bison Kraken Scanner is online 🚀")

@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Your chat ID is: {message.chat.id}")

@bot.message_handler(commands=["scan"])
def manual_scan(message):
    bot.reply_to(message, "Scanning Kraken now...")
    send_scan()

threading.Thread(target=scanner_loop, daemon=True).start()

print("Black Bison Kraken Scanner Started", flush=True)
bot.infinity_polling()
