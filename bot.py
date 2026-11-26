import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL")  # set automatically by Render
PORT = int(os.environ.get("PORT", 10000))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("cloudd_bot is alive ☁️")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(update.message.text)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    if URL:  # on Render -> webhook mode
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{URL}/{TOKEN}",
        )
    else:  # local testing -> polling
        app.run_polling()


if __name__ == "__main__":
    main()
