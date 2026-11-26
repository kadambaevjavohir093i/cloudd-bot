"""
cloudd-bot: send messages FROM YOUR OWN Telegram account by texting your bot.

Two ways to use it:
  1) Exact text:   Malika: are you free for dinner tomorrow at 7pm?
     -> sends exactly that text.
  2) AI mode:      ask Malika if she's free for dinner tomorrow at 7, make it sweet
     -> Claude writes the message for you.
Either way you get a preview first; nothing is sent until you tap ✅ Send.

Env vars (Render -> Environment):
  API_ID, API_HASH   - from my.telegram.org
  SESSION            - string printed by login.py (secret = your account)
  BOT_TOKEN          - from @BotFather
  ANTHROPIC_API_KEY  - from console.anthropic.com (optional, enables AI mode)
  ANTHROPIC_MODEL    - optional, default claude-haiku-5-5
  PORT               - set automatically by Render
"""
import asyncio
import json
import logging
import os
import re
import sys
import urllib.request

import aiohttp
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-haiku-5-5"
PORT = int(os.environ.get("PORT", "10000"))

HELP = (
    "Two ways to send a message from your account:\n\n"
    "1) Exact text — Name: message\n"
    "   Malika: are you free tomorrow at 7?\n\n"
    "2) AI writes it — just describe what you want\n"
    "   ask Malika if she's free for dinner tomorrow at 7, make it sweet\n"
    "   спроси у Малики, свободна ли она завтра в 7 на ужин\n\n"
    "You always see a preview first. Nothing is sent until you tap ✅ Send."
)


def full_name(u):
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return name or (f"@{u.username}" if u.username else str(u.id))


def delete_old_webhook():
    # An older version of this bot set a Bot API webhook; it blocks updates for this version.
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10).read()
        log.info("Old webhook removed (if there was one)")
    except Exception as exc:  # never log the URL, it contains the token
        log.warning("Could not remove webhook: %s", type(exc).__name__)


def looks_like_exact(raw):
    """'Malika: hi' or '@user: hi' -> exact mode. Longer text before ':' -> AI mode."""
    if ":" not in raw:
        return False
    who = raw.split(":", 1)[0].strip()
    return bool(who) and (who.startswith("@") or len(who.split()) <= 2)


async def ai_compose(owner_name, instruction, previous=None):
    """Ask Claude for {"recipient": ..., "message": ...}."""
    system = (
        f"You write Telegram messages that {owner_name} will send from their own account to their contacts. "
        "From the instruction, find the recipient exactly as the user named them (a name or @username, "
        "in its base form, e.g. 'Малики' -> 'Малика', 'Malikaga' -> 'Malika'), and write the message "
        f"in first person as {owner_name}, addressed to that person. "
        "Write in the same language and script the instruction is written in (Russian, Uzbek, English, "
        "or Russian/Uzbek typed in Latin letters) unless the user asks for another language. "
        "Sound like a real person texting: natural, short, casual, no quotation marks, no signature, "
        "no explanations. Follow any tone the user asks for. "
        'Reply with ONLY JSON: {"recipient": "...", "message": "..."}. '
        "If no recipient is clear, use null for recipient."
    )
    content = f"Instruction: {instruction}"
    if previous:
        content += f"\n\nYou already suggested this; write a noticeably different version:\n{previous}"

    async with aiohttp.ClientSession() as http:
        async with http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = (data.get("error") or {}).get("message", str(resp.status))
                raise RuntimeError(f"AI error: {err}")

    text = "".join(block.get("text", "") for block in data.get("content", []))
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("AI returned an unexpected answer")
    result = json.loads(match.group(0))
    return result.get("recipient"), (result.get("message") or "").strip()


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
    owner_name = full_name(me)
    log.info("Userbot logged in as %s", owner_name)
    log.info("AI mode: %s", f"on ({ANTHROPIC_MODEL})" if ANTHROPIC_API_KEY else "off (no ANTHROPIC_API_KEY)")

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot started")

    pending = {}  # preview message id -> {"to": user, "text": str, "instruction": str|None}

    def buttons(with_retry):
        row = [Button.inline("✅ Send", b"send"), Button.inline("❌ Cancel", b"cancel")]
        rows = [row]
        if with_retry:
            rows.append([Button.inline("🔄 Another version", b"retry")])
        return rows

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
        partial = [u for u in people if q in full_name(u).lower()]
        if partial:
            return partial
        # match on first name only (e.g. "Malika" vs contact "Malika ❤️")
        return [u for u in people if (u.first_name or "").lower().startswith(q)]

    async def resolve_one(event, who):
        matches = await find_contacts(who)
        if not matches:
            await event.reply(f"No contact found for “{who}”. Use the name as saved in your contacts, or @username.")
            return None
        if len(matches) > 1:
            names = "\n".join(f"• {full_name(u)}" for u in matches[:10])
            await event.reply(f"Several contacts match “{who}”:\n{names}\n\nUse the full name.")
            return None
        return matches[0]

    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if event.sender_id != owner_id:
            return
        await event.reply(HELP)

    @bot.on(events.NewMessage(func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
    async def on_message(event):
        if event.sender_id != owner_id:
            return  # ignore everyone except you
        raw = (event.raw_text or "").strip()
        if not raw:
            return

        if looks_like_exact(raw):
            who, text = raw.split(":", 1)
            who, text = who.strip(), text.strip()
            if not text:
                await event.reply("Need a message after the name.  Example:  Malika: hi")
                return
            recipient = await resolve_one(event, who)
            if not recipient:
                return
            preview = await event.reply(f"To: {full_name(recipient)}\n\n{text}", buttons=buttons(False))
            pending[preview.id] = {"to": recipient, "text": text, "instruction": None}
            return

        # AI mode
        if not ANTHROPIC_API_KEY:
            await event.reply("AI mode is off (no ANTHROPIC_API_KEY in Render).\nUse:  Name: message")
            return
        thinking = await event.reply("✍️ Writing…")
        try:
            who, text = await ai_compose(owner_name, raw)
        except Exception as exc:
            log.warning("AI compose failed: %s", exc)
            await thinking.edit(f"Couldn't write it: {exc}\nTry again, or use  Name: message")
            return
        if not who:
            await thinking.edit("Who should I send it to? Mention the person's name.")
            return
        if not text:
            await thinking.edit("Couldn't write the message, try rephrasing.")
            return
        await thinking.delete()
        recipient = await resolve_one(event, who)
        if not recipient:
            return
        preview = await event.reply(f"To: {full_name(recipient)}\n\n{text}", buttons=buttons(True))
        pending[preview.id] = {"to": recipient, "text": text, "instruction": raw}

    @bot.on(events.CallbackQuery)
    async def on_button(event):
        if event.sender_id != owner_id:
            await event.answer("Not allowed")
            return
        item = pending.get(event.message_id)
        if not item:
            await event.answer("This preview expired. Send it again.")
            return
        recipient, text = item["to"], item["text"]

        if event.data == b"retry" and item["instruction"]:
            await event.answer("Writing another version…")
            try:
                _, new_text = await ai_compose(owner_name, item["instruction"], previous=text)
            except Exception as exc:
                await event.answer(f"Failed: {exc}"[:190], alert=True)
                return
            if new_text:
                item["text"] = new_text
                await event.edit(f"To: {full_name(recipient)}\n\n{new_text}", buttons=buttons(True))
            return

        pending.pop(event.message_id, None)
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
