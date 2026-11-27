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
  GROQ_API_KEY       - from console.groq.com, free (optional, enables AI mode)
  GROQ_MODEL         - optional, default openai/gpt-oss-120b
  STARTUP_DELAY      - optional, seconds to wait before logging in (default 25 on Render, 0 elsewhere)
  PORT               - set automatically by Render
"""
import asyncio
import difflib
import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
import urllib.request

import aiohttp
from aiohttp import web
from telethon import Button, TelegramClient, errors, events, functions
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
# Render sets RENDER=true. See main() for why we wait there.
STARTUP_DELAY = int(os.environ.get("STARTUP_DELAY", "").strip() or (25 if os.environ.get("RENDER") else 0))

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


def preview(recipient, text, note=None):
    body = f"To: {label(recipient)}\n\n{text}"
    return f"{body}\n\n⚠️ {note}" if note else body


def send_error_text(exc):
    """Short human explanation of why Telegram refused to send."""
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram asks to wait {exc.seconds} s before sending more."
    if isinstance(exc, errors.PeerFloodError):
        return "Telegram has limited your account from messaging people who are not your contacts. Check @SpamBot."
    if isinstance(exc, errors.YouBlockedUserError):
        return "You have blocked this person. Unblock them first."
    return str(exc) or type(exc).__name__


# ---------- AI ----------

def delete_old_webhook():
    # An older version of this bot set a Bot API webhook; it blocks updates for this version.
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10).read()
        log.info("Old webhook removed (if there was one)")
    except Exception as exc:  # never log the URL, it contains the token
        log.warning("Could not remove webhook: %s", type(exc).__name__)


def looks_like_exact(raw):
    """'Malika: hi' or '@user: hi' -> exact mode. Longer text before ':' or a link -> AI mode."""
    if ":" not in raw or re.match(r"\w+://", raw):
        return False
    who = raw.split(":", 1)[0].strip()
    return bool(who) and (who.startswith("@") or len(who.split()) <= 2)


async def ai_compose(owner_name, instruction, previous=None):
    """Ask the AI for {"recipient": ..., "message": ...}."""
    system = (
        f"You write Telegram messages that {owner_name} will send from their own account to their contacts. "
        "From the instruction, find the recipient exactly as the user named them (a name or @username, "
        "in its base form, e.g. 'Малики' -> 'Малика', 'Malikaga' -> 'Malika'), and write the message "
        f"in first person as {owner_name}, to send to that person. "
        "Do NOT put the recipient's name or @username in the message (no 'Hi Malika', no 'Yo Malika', "
        "no 'Привет, Малика') - the chat already shows who it is for; just write the message itself. "
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
            try:
                data = await resp.json(content_type=None)
            except ValueError:  # e.g. an HTML error page
                raise RuntimeError(f"AI error: HTTP {resp.status}") from None
            if resp.status != 200:
                err = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                raise RuntimeError(f"AI error: {err or resp.status}")

    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    match = re.search(r"\{.*\}", text, re.S)
    try:
        result = json.loads(match.group(0)) if match else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        raise RuntimeError("AI returned an unexpected answer")
    recipient = result.get("recipient")
    return recipient, drop_name((result.get("message") or "").strip(), recipient)


def drop_name(message, recipient):
    """Safety net in case the AI still greets by name: 'Yo Malika, what's up?' -> 'Yo, what's up?'.
    Only the name near the start (the greeting) is removed, so normal words later stay intact."""
    name = str(recipient or "").lstrip("@").strip()
    if not name or not message:
        return message
    m = re.search(rf"[ \t]*,?[ \t]*(?<!\w)@?{re.escape(name)}(?!\w)", message, re.I)
    if not m or len(message[:m.start()].split()) > 3:
        return message
    head, tail = message[:m.start()], message[m.end():]
    if not head.strip():  # the message started with the name: 'Malika, how are you?'
        tail = tail.lstrip(" \t,!.:;-—–")
        tail = tail[:1].upper() + tail[1:]
    cleaned = (head + tail).strip()
    return cleaned or message


# ---------- bot ----------

async def start_health_page():
    """Tiny web page so Render sees the service as healthy (and UptimeRobot keeps it awake)."""
    async def health(_request):
        return web.Response(text="cloudd-bot is running")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health page on port %s", PORT)
    return runner


async def main():
    # On deploy Render starts the new instance and stops the old one only after the new one
    # answers on its port. If both were logged in at once, Telegram could see the same SESSION
    # from two servers and revoke it (AuthKeyDuplicatedError) - then login.py has to be run again.
    # So: open the port first, give the old instance time to stop, and only then log in.
    runner = await start_health_page()
    if STARTUP_DELAY:
        log.info("Waiting %s s for the previous instance to stop", STARTUP_DELAY)
        await asyncio.sleep(STARTUP_DELAY)

    delete_old_webhook()

    try:
        session = StringSession(SESSION)
    except ValueError:
        sys.exit("SESSION is not a valid session string. Copy the whole string from login.py again.")
    # The account only sends messages, it doesn't need a stream of updates from all your chats.
    user = TelegramClient(session, API_ID, API_HASH, receive_updates=False)
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

    # preview message id -> {"to": user, "text": str, "instruction": str|None, "busy": bool}
    # "busy" is set while sending or writing another version, so a second tap can't send twice.
    pending = {}
    choosing = {}  # "which one?" message id -> {"candidates": [...], "text": str, "instruction": str|None}
    people_cache = {"at": 0.0, "list": []}

    def send_buttons(with_retry):
        rows = [[Button.inline("✅ Send", b"send"), Button.inline("❌ Cancel", b"cancel")]]
        if with_retry:
            rows.append([Button.inline("🔄 Another version", b"retry")])
        return rows

    def new_pending(recipient, text, instruction):
        return {"to": recipient, "text": text, "instruction": instruction, "busy": False}

    async def edit_preview(event, text, buttons):
        try:
            await event.edit(text, buttons=buttons, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass  # same text as already shown (e.g. the same error twice)

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

    async def propose(event, who, text, instruction, found=None):
        """Find the person and show a preview (or ask which person is meant)."""
        matches, suggestions = found if found is not None else await find_people(who)
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
            shown = await event.reply(
                preview(matches[0], text), buttons=send_buttons(bool(instruction)), parse_mode=None
            )
            pending[shown.id] = new_pending(matches[0], text, instruction)
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
            found = await find_people(who)
            if found[0] or not GROQ_API_KEY or who.startswith("@"):
                await propose(event, who, text, None, found)
                return
            # "напиши Малике: привет" looks like "Name: text", but nobody is called
            # "напиши Малике" - so it is a request; let the AI read it below.

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
        mid = event.message_id

        # "which person?" step
        if mid in choosing:
            item = choosing.pop(mid)
            if not data.startswith(b"pick:"):
                await event.edit("❌ Cancelled")
                return
            recipient = item["candidates"][int(data[5:])]
            pending[mid] = new_pending(recipient, item["text"], item["instruction"])
            await event.edit(
                preview(recipient, item["text"]), buttons=send_buttons(bool(item["instruction"])), parse_mode=None
            )
            return

        item = pending.get(mid)
        if not item:
            await event.answer("This preview expired. Send it again.")
            return
        if item["busy"]:
            await event.answer("Wait a second…")
            return
        recipient = item["to"]
        with_retry = bool(item["instruction"])

        if data == b"retry" and with_retry:
            item["busy"] = True  # before any await, so a quick second tap is refused
            note = None
            try:
                await event.answer("Writing another version…")
                _, new_text = await ai_compose(owner_name, item["instruction"], previous=item["text"])
                if new_text:
                    item["text"] = new_text
                else:
                    note = "Couldn't write another version, try again."
            except Exception as exc:
                log.warning("AI retry failed: %s", exc)
                note = f"Couldn't write another version: {exc}"
            finally:
                item["busy"] = False
            await edit_preview(event, preview(recipient, item["text"], note), send_buttons(True))
            return

        if data == b"send":
            item["busy"] = True  # before any await, so a double tap can't send twice
            try:
                await event.answer("Sending…")
                await user.send_message(recipient, item["text"], parse_mode=None)
            except Exception as exc:
                log.warning("Send failed: %s", type(exc).__name__)
                note = f"Not sent: {send_error_text(exc)}"
            else:
                pending.pop(mid, None)
                await event.edit(f"✅ Sent to {label(recipient)}\n\n{item['text']}", parse_mode=None)
                log.info("Message sent to a contact")
                return
            finally:
                item["busy"] = False
            # keep the preview and its buttons, so you can tap Send again later
            await edit_preview(event, preview(recipient, item["text"], note), send_buttons(with_retry))
            return

        pending.pop(mid, None)
        await event.edit("❌ Cancelled")

    # Run until Render stops us (SIGTERM) or a client drops for good, then close the Telegram
    # connections cleanly so the next instance starts on a free SESSION.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stopping = asyncio.ensure_future(stop.wait())
    clients = [asyncio.ensure_future(c.run_until_disconnected()) for c in (user, bot)]
    await asyncio.wait([stopping, *clients], return_when=asyncio.FIRST_COMPLETED)

    log.info("Shutting down")
    stopping.cancel()
    await asyncio.gather(user.disconnect(), bot.disconnect(), return_exceptions=True)
    results = await asyncio.gather(*clients, return_exceptions=True)
    await runner.cleanup()
    for result in results:
        if isinstance(result, Exception):
            raise result  # non-zero exit, Render restarts the service


if __name__ == "__main__":
    asyncio.run(main())
