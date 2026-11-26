# cloudd-bot

Text your own Telegram bot, and it sends messages from **your account** (Telethon userbot).

- `Malika: are you free at 7?` sends exactly that text
- `ask Malika if she's free for dinner at 7, make it sweet` lets the AI (Groq) write it
- `/who name` shows who the bot finds for a name

People are found in your contacts and recent chats, in any spelling (Малика / Malika).
You always get a preview first. Nothing is sent until you tap ✅ Send.

## Setup
1. Get `API_ID` and `API_HASH` at https://my.telegram.org
2. On your own computer: `pip install telethon && python login.py`, then copy the SESSION string.
   It is full access to your account: keep it secret.
3. Create a bot with @BotFather to get `BOT_TOKEN`.
4. Optional: a free key at https://console.groq.com as `GROQ_API_KEY` (enables AI mode).
5. Put them in Render → Environment. Never commit them to the repo.

Render deploys automatically from `main`. The free plan sleeps after 15 minutes without
HTTP traffic, so keep an UptimeRobot monitor on the service URL.

## Optional settings
- `GROQ_MODEL`: default `openai/gpt-oss-120b`
- `STARTUP_DELAY`: seconds to wait before logging in (default 25 on Render). During a deploy
  the new instance waits for the old one to stop, so the same SESSION is never used from two
  servers at once. Telegram may revoke a SESSION when that happens.
