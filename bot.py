"""
cloudd-bot: you text the bot "Name: message", it shows a preview,
you tap Send, and the message goes out FROM YOUR OWN Telegram account.

Env vars (set in Render -> Environment):
  API_ID     - from my.telegram.org (Jacob Messenger app)
  API_HASH   - from my.telegram.org (keep secret)
  SESSION    - string printed by login.py (keep secret, = your account)
  BOT_TOKEN  - from @BotFather (keep secret)
  PORT       - set automatically by Render
"""
import asyncio
import logging
import os
import sys
import urllib.request

from aiohttp import web
from telethon import Button, TelegramClient, events, functions
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cloudd-bot")


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing environment variable: {name}")
    return value


API_ID = int(env("API_ID"))
API_HASH = env("API_HASH")
SESSION = env("SESSION")
BOT_TOKEN = env("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "10000"))

HELP = (
    "Send me:  Name: message\n\n"
    "Examples:\n"
    "Malika: are you free for dinner tomorrow at 7pm?\n"
    "@username: see you at 7\n\n"
    "I'll show a preview first. Nothing is sent until you tap ✅ Send."
)


def full_name(u):
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return name or (f"@{u.username}" if u.username else str(u.id))


def delete_old_webhook():
    # The old version of this bot set a Bot API webhook; it blocks updates for this version.
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10).read()
        log.info("Old webhook removed (if there was one)")
    except Exception as exc:  # never log the URL, it contains the token
        log.warning("Could not remove webhook: %s", type(exc).__name__)


async def main():
    delete_old_webhook()

    try:
        session = StringSession(SESSION)
    except ValueError:
        sys.exit("SESSION is not a valid session string. Copy the whole string from login.py again.")
    user = TelegramClient(session, API_ID, API_HASH)
    bot = TelegramClient(StringSession(), API_ID, API_HASH)

    await user.connect()
    if not await user.is_user_authorized():
        sys.exit("SESSION is invalid or expired. Run login.py again and update SESSION in Render.")
    me = await user.get_me()
    owner_id = me.id
    log.info("Userbot logged in as %s", full_name(me))

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot started")

    pending = {}  # preview message id -> (recipient, text)

    async def find_contacts(query):
        query = query.strip()
        if query.startswith("@"):
            try:
                return [await user.get_entity(query)]
            except Exception:
                return []
        q = query.lower()
        result = await user(functions.contacts.GetContactsRequest(hash=0))
        people = [u for u in result.users if not u.bot]
        exact = [u for u in people if full_name(u).lower() == q]
        if exact:
            return exact
        return [u for u in people if q in full_name(u).lower()]

    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if event.sender_id != owner_id:
            return
        await event.reply(HELP)

    @bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
    async def on_message(event):
        if event.sender_id != owner_id:
            return  # ignore everyone except you
        raw = event.raw_text or ""
        if ":" not in raw:
            await event.reply("Format:  Name: message\n\nExample:  Malika: free for dinner tomorrow at 7pm?")
            return
        who, text = raw.split(":", 1)
        text = text.strip()
        if not who.strip() or not text:
            await event.reply("Need both a name and a message.  Example:  Malika: hi")
            return

        matches = await find_contacts(who)
        if not matches:
            await event.reply(f"No contact found for “{who.strip()}”. Use the name as saved in your contacts, or @username.")
            return
        if len(matches) > 1:
            names = "\n".join(f"• {full_name(u)}" for u in matches[:10])
            await event.reply(f"Several contacts match “{who.strip()}”:\n{names}\n\nType the full name.")
            return

        recipient = matches[0]
        preview = await event.reply(
            f"To: {full_name(recipient)}\n\n{text}",
            buttons=[[Button.inline("✅ Send", b"send"), Button.inline("❌ Cancel", b"cancel")]],
        )
        pending[preview.id] = (recipient, text)

    @bot.on(events.CallbackQuery)
    async def on_button(event):
        if event.sender_id != owner_id:
            await event.answer("Not allowed")
            return
        item = pending.pop(event.message_id, None)
        if not item:
            await event.answer("This preview expired. Send it again.")
            return
        recipient, text = item
        if event.data == b"send":
            await user.send_message(recipient, text)
            await event.edit(f"✅ Sent to {full_name(recipient)}\n\n{text}")
            log.info("Message sent to a contact")
        else:
            await event.edit("❌ Cancelled")

    # Tiny web page so Render sees the service as healthy (and pingers can keep it awake).
    async def health(_request):
        return web.Response(text="cloudd-bot is running")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health page on port %s", PORT)

    await asyncio.gather(user.run_until_disconnected(), bot.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())
