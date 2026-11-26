"""
cloudd-bot: send messages FROM YOUR OWN Telegram account by texting your bot.

Two ways to use it:
  1) Exact text:   Malika: are you free for dinner tomorrow at 7pm?
     -> sends exactly that text.
  2) AI mode:      ask Malika if she's free for dinner tomorrow at 7, make it sweet
     -> AI writes the message for you.
Either way you get a preview first; nothing is sent until you tap ✅ Send.

People are found in your saved contacts AND your recent chats. Spelling does not
have to be exact: Малика / Malika / malikaga all find the same person.

Env vars (Render -> Environment):
  API_ID, API_HASH   - from my.telegram.org
  SESSION            - string printed by login.py (secret = your account)
  BOT_TOKEN          - from @BotFather
  GROQ_API_KEY  - from console.groq.com, free (optional, enables AI mode)
  GROQ_MODEL    - optional, default openai/gpt-oss-120b
  PORT               - set automatically by Render
"""
import asyncio
import difflib
import json
import logging
import os
import re
import sys
import time
import unicodedata
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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
PORT = int(os.environ.get("PORT", "10000"))

HELP = (
    "Two ways to send a message from your account:\n\n"
    "1) Exact text — Name: message\n"
    "   Malika: are you free tomorrow at 7?\n\n"
    "2) AI writes it — just describe what you want\n"
    "   ask Malika if she's free for dinner tomorrow at 7, make it sweet\n"
    "   спроси у Малики, свободна ли она завтра в 7 на ужин\n\n"
    "You always see a preview first. Nothing is sent until you tap ✅ Send.\n\n"
    "/who name — check who I find for a name"
)

# ---------- name matching (works across Cyrillic / Latin spelling) ----------

CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}
APOSTROPHES = "'`ʻʼ’‘"


def norm(text):
    """Lowercase, transliterate Cyrillic to Latin, drop emoji/punctuation, unify spelling variants."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(ch for ch in text if ch not in APOSTROPHES)
    text = "".join(CYR2LAT.get(ch, ch) for ch in text)
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    for a, b in (("kh", "x"), ("zh", "j"), ("h", "x"), ("w", "v"), ("q", "k"), ("yo", "o"), ("ye", "e"), ("iy", "i")):
        text = text.replace(a, b)
    return " ".join(text.split())


def score(q, name):
    """How well normalized query q matches normalized name (0..1)."""
    if not q or not name:
        return 0.0
    if q == name:
        return 1.0
    qt, nt = q.split(), name.split()
    if all(t in nt for t in qt):
        return 0.95
    if all(any(n.startswith(t) for n in nt) for t in qt):
        return 0.9
    if q in name:
        return 0.85
    best = max(difflib.SequenceMatcher(None, q, x).ratio() for x in nt + [name])
    return best * 0.84


def full_name(u):
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return name or (f"@{u.username}" if u.username else str(u.id))


def label(u):
    name = full_name(u)
    if u.username and not name.startswith("@"):
        name += f" (@{u.username})"
    return name


def rank(query, people):
    """Return (matches, suggestions). matches = confident hits, best first."""
    q = norm(query)
    scored = []
    for u in people:
        s = max(score(q, norm(full_name(u))), score(q, norm(u.username or "")))
        scored.append((s, u))
    scored.sort(key=lambda x: -x[0])
    good = [(s, u) for s, u in scored if s >= 0.63]
    if good:
        top = good[0][0]
        return [u for s, u in good if s >= top - 0.06][:6], []
    return [], [u for s, u in scored if s >= 0.42][:5]


# ---------- AI ----------

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
    """Ask the AI for {"recipient": ..., "message": ...}."""
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
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "content-type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "max_completion_tokens": 1500,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                err = (data.get("error") or {}).get("message", str(resp.status))
                raise RuntimeError(f"AI error: {err}")

    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("AI returned an unexpected answer")
    result = json.loads(match.group(0))
    return result.get("recipient"), (result.get("message") or "").strip()


# ---------- bot ----------

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
    log.info("AI mode: %s", f"on ({GROQ_MODEL})" if GROQ_API_KEY else "off (no GROQ_API_KEY)")

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot started")

    pending = {}   # preview message id -> {"to": user, "text": str, "instruction": str|None}
    choosing = {}  # "which one?" message id -> {"candidates": [...], "text": str, "instruction": str|None}
    people_cache = {"at": 0.0, "list": []}

    def send_buttons(with_retry):
        rows = [[Button.inline("✅ Send", b"send"), Button.inline("❌ Cancel", b"cancel")]]
        if with_retry:
            rows.append([Button.inline("🔄 Another version", b"retry")])
        return rows

    async def load_people():
        """Saved contacts + people from recent chats (cached for 5 minutes)."""
        if people_cache["list"] and time.monotonic() - people_cache["at"] < 300:
            return people_cache["list"]
        found = {}
        try:
            result = await user(functions.contacts.GetContactsRequest(hash=0))
            for u in result.users:
                found[u.id] = u
        except Exception as exc:
            log.warning("Could not load contacts: %s", type(exc).__name__)
        try:
            async for dialog in user.iter_dialogs(limit=300):
                if dialog.is_user:
                    found.setdefault(dialog.entity.id, dialog.entity)
        except Exception as exc:
            log.warning("Could not load chats: %s", type(exc).__name__)
        people = [u for u in found.values() if not getattr(u, "bot", False) and not getattr(u, "deleted", False)]
        log.info("Loaded %d people (contacts + chats)", len(people))
        people_cache["list"], people_cache["at"] = people, time.monotonic()
        return people

    async def find_people(query):
        query = query.strip()
        if query.startswith("@"):
            try:
                return [await user.get_entity(query)], []
            except Exception:
                return [], []
        return rank(query, await load_people())

    async def propose(event, who, text, instruction):
        """Find the person and show a preview (or ask which person is meant)."""
        matches, suggestions = await find_people(who)
        if not matches:
            msg = f"No one found for “{who}”."
            if suggestions:
                msg += "\n\nClosest names:\n" + "\n".join(f"• {label(u)}" for u in suggestions)
                msg += "\n\nSend it again with the name as shown."
            else:
                msg += "\nUse the name as it appears in your Telegram, or @username."
            await event.reply(msg, parse_mode=None)
            return
        if len(matches) == 1:
            preview = await event.reply(
                f"To: {label(matches[0])}\n\n{text}", buttons=send_buttons(bool(instruction)), parse_mode=None
            )
            pending[preview.id] = {"to": matches[0], "text": text, "instruction": instruction}
            return
        rows = [[Button.inline(label(u)[:40], f"pick:{i}".encode())] for i, u in enumerate(matches)]
        rows.append([Button.inline("❌ Cancel", b"cancel")])
        ask = await event.reply(f"Which “{who}” do you mean?", buttons=rows, parse_mode=None)
        choosing[ask.id] = {"candidates": matches, "text": text, "instruction": instruction}

    @bot.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def on_start(event):
        if event.sender_id != owner_id:
            return
        await event.reply(HELP, parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/who(?:\s+(.+))?$"))
    async def on_who(event):
        if event.sender_id != owner_id:
            return
        query = (event.pattern_match.group(1) or "").strip()
        if not query:
            await event.reply("Usage: /who name", parse_mode=None)
            return
        people_cache["at"] = 0.0  # force a fresh list
        matches, suggestions = await find_people(query)
        if matches:
            msg = "Found:\n" + "\n".join(f"• {label(u)}" for u in matches)
        elif suggestions:
            msg = "No exact match. Closest:\n" + "\n".join(f"• {label(u)}" for u in suggestions)
        else:
            msg = f"No one found for “{query}” among {len(people_cache['list'])} people in your contacts and chats."
        await event.reply(msg, parse_mode=None)

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
                await event.reply("Need a message after the name.  Example:  Malika: hi", parse_mode=None)
                return
            await propose(event, who, text, None)
            return

        # AI mode
        if not GROQ_API_KEY:
            await event.reply("AI mode is off (no GROQ_API_KEY in Render).\nUse:  Name: message", parse_mode=None)
            return
        thinking = await event.reply("✍️ Writing…")
        try:
            who, text = await ai_compose(owner_name, raw)
        except Exception as exc:
            log.warning("AI compose failed: %s", exc)
            await thinking.edit(f"Couldn't write it: {exc}\nTry again, or use  Name: message", parse_mode=None)
            return
        if not who:
            await thinking.edit("Who should I send it to? Mention the person's name.")
            return
        if not text:
            await thinking.edit("Couldn't write the message, try rephrasing.")
            return
        await thinking.delete()
        await propose(event, str(who), text, raw)

    @bot.on(events.CallbackQuery)
    async def on_button(event):
        if event.sender_id != owner_id:
            await event.answer("Not allowed")
            return
        data = event.data or b""

        # "which person?" step
        if event.message_id in choosing:
            item = choosing.pop(event.message_id)
            if not data.startswith(b"pick:"):
                await event.edit("❌ Cancelled")
                return
            recipient = item["candidates"][int(data[5:])]
            await event.edit(
                f"To: {label(recipient)}\n\n{item['text']}",
                buttons=send_buttons(bool(item["instruction"])),
                parse_mode=None,
            )
            pending[event.message_id] = {"to": recipient, "text": item["text"], "instruction": item["instruction"]}
            return

        item = pending.get(event.message_id)
        if not item:
            await event.answer("This preview expired. Send it again.")
            return
        recipient, text = item["to"], item["text"]

        if data == b"retry" and item["instruction"]:
            await event.answer("Writing another version…")
            try:
                _, new_text = await ai_compose(owner_name, item["instruction"], previous=text)
            except Exception as exc:
                await event.answer(f"Failed: {exc}"[:190], alert=True)
                return
            if new_text:
                item["text"] = new_text
                await event.edit(f"To: {label(recipient)}\n\n{new_text}", buttons=send_buttons(True), parse_mode=None)
            return

        pending.pop(event.message_id, None)
        if data == b"send":
            await user.send_message(recipient, text, parse_mode=None)
            await event.edit(f"✅ Sent to {label(recipient)}\n\n{text}", parse_mode=None)
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
