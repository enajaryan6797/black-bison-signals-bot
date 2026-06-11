import os
import telebot
import requests
import time

TOKEN = os.getenv("BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Black Bison Kraken Scanner Started 🚀")

while True:
    try:
        data = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
        ).json()

        print(data)

    except Exception as e:
        print(e)

    time.sleep(300)
