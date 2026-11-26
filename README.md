# cloudd-bot

Telegram bot set up for Render's free plan (webhook mode).

## Deploy
1. Render → New → Blueprint → pick this repo (uses `render.yaml`)
2. Enter `BOT_TOKEN` (from @BotFather) when asked
3. Deploy, then send /start to the bot

Locally: `pip install -r requirements.txt && BOT_TOKEN=xxx python bot.py` (runs in polling mode).
