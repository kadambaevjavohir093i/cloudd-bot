"""
cloudd-bot: send messages FROM YOUR OWN Telegram account by texting your bot.

Two ways to use it:
  1) Exact text:   Malika: are you free for dinner tomorrow at 7pm?
     -> sends exactly that text.
  2) AI mode:      ask Malika if she's free for dinner tomorrow at 7, make it sweet
     -> AI writes the message for you.
Either way you get a preview first; nothing is sent until you tap ✅ Send.

Start with a time to send it later (Telegram holds it and sends it by itself, even if this bot is offline):
     17:00 Malika: see you tonight        5pm ask Malika if she's free
     tomorrow 9:00 Aziz: happy birthday!  (or in AI mode: "at 5pm tell Malika I'm late")

People are found in your saved contacts AND your recent chats. Spelling does not
have to be exact: Малика / Malika / malikaga all find the same person.

Env vars (Render -> Environment):
  API_ID, API_HASH   - from my.telegram.org
  SESSION            - string printed by login.py (secret = your account)
  BOT_TOKEN          - from @BotFather
  GROQ_API_KEY       - from console.groq.com, free (optional, enables AI mode)
  GROQ_MODEL         - optional, default openai/gpt-oss-120b
  TIMEZONE           - optional, your time zone for scheduled messages, default Asia/Tashkent
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
TIMEZONE = os.environ.get("TIMEZONE", "").strip() or "Asia/Tashkent"
try:
    TZ = ZoneInfo(TIMEZONE)
except Exception:
    sys.exit(f"Unknown TIMEZONE: {TIMEZONE}. Use a name like Asia/Tashkent or Europe/Moscow.")
ZONE_NAME = TIMEZONE.split("/")[-1].replace("_", " ")  # "Tashkent"

HELP = (
    "Two ways to send a message from your account:\n\n"
    "1) Exact text — Name: message\n"
    "   Malika: are you free tomorrow at 7?\n\n"
    "2) AI writes it — just describe what you want\n"
    "   ask Malika if she's free for dinner tomorrow at 7, make it sweet\n"
    "   спроси у Малики, свободна ли она завтра в 7 на ужин\n\n"
    "⏰ Send it later — start with the time:\n"
    "   17:00 Malika: see you tonight\n"
    "   5pm ask Malika if she's free\n"
    "   tomorrow 9:00 Aziz: happy birthday!\n"
    "   or just say it: at 5pm tell Malika I'm running late\n"
    f"Times are {ZONE_NAME} time, whatever your phone's clock says.\n"
    "Telegram sends it by itself at that time, even if this bot is offline.\n\n"
    "You always see a preview first. Nothing is sent or scheduled until you tap the button.\n\n"
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


def preview(recipient, text, note=None, at=None):
    head = f"To: {label(recipient)}"
    if at:
        head += f"\n⏰ Sends {when_text(at)}"
    body = f"{head}\n\n{text}"
    return f"{body}\n\n⚠️ {note}" if note else body


# ---------- scheduling ----------

TIME_PREFIX = re.compile(
    r"(?:(?P<day>today|tomorrow|сегодня|завтра|bugun|ertaga)\s+)?(?P<at>at\s+|в\s+|soat\s+)?"
    r"(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?(?=[\s,])",
    re.I,
)
TOMORROW = ("tomorrow", "завтра", "ertaga")


def local_now():
    return datetime.now(TZ)


def split_time(raw, now):
    """A time at the very start means 'send it then':
    '17:00 Malika: hi' -> (today 17:00, 'Malika: hi'). No time -> (None, raw)."""
    m = TIME_PREFIX.match(raw)
    if not m or not (m["m"] or m["ampm"] or m["at"]):  # a bare '2 ...' is not a time
        return None, raw
    hour, minute = int(m["h"]), int(m["m"] or 0)
    if m["ampm"]:
        if not 1 <= hour <= 12:
            return None, raw
        hour = hour % 12 + (12 if m["ampm"][0].lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None, raw
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = (m["day"] or "").lower()
    if day in TOMORROW:
        at += timedelta(days=1)
    elif not day and at <= now:
        at += timedelta(days=1)  # '9:00' when it is already 14:00 -> tomorrow 9:00
    return at, raw[m.end():].lstrip(" ,")


def parse_send_at(value):
    """The AI's 'YYYY-MM-DD HH:MM' (your local time) -> datetime, or None."""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None


def when_text(at):
    today = local_now().date()
    if at.date() == today:
        day = "today"
    elif at.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{at:%a %d %b}"
    return f"{day} at {at:%H:%M} {ZONE_NAME} time ({until_text(at)})"


def until_text(at):
    """'in 1h 49min' - lets you check the time no matter what your phone's clock is set to."""
    mins = max(0, round((at - local_now()).total_seconds() / 60))
    hours, mins = divmod(mins, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"in {days}d {hours}h"
    return f"in {hours}h {mins}min" if hours else f"in {mins} min"


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
        f"Right now it is {local_now():%A %Y-%m-%d %H:%M} for the user. "
        "If the user wants the message SENT later (e.g. 'send at 5pm', 'tomorrow at 9 write to Aziz', "
        "'в 17:00 напиши Малике', 'soat 5 da Malikaga yoz'), put that moment in send_at as "
        "'YYYY-MM-DD HH:MM' in the user's time. A time that is only part of what the message says "
        "(e.g. 'ask if she is free at 7') is NOT send_at. If it should go now, send_at is null. "
        'Reply with ONLY JSON: {"recipient": "...", "message": "...", "send_at": "YYYY-MM-DD HH:MM" or null}. '
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
    return recipient, drop_name((result.get("message") or "").strip(), recipient), result.get("send_at")


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

    # preview message id -> {"to": user, "text": str, "instruction": str|None, "at": datetime|None, "busy": bool}
    # "at" is when to send (None = now). "busy" is set while sending or writing another version,
    # so a second tap can't send twice.
    pending = {}
    choosing = {}  # "which one?" message id -> {"candidates": [...], "text", "instruction", "at"}
    scheduled = {}  # confirmation message id -> {"to": user, "id": scheduled msg id, "at": datetime, "text": str}
    people_cache = {"at": 0.0, "list": []}

    def send_buttons(with_retry, at=None):
        first = Button.inline("⏰ Schedule", b"send") if at else Button.inline("✅ Send", b"send")
        rows = [[first, Button.inline("❌ Cancel", b"cancel")]]
        if with_retry:
            rows.append([Button.inline("🔄 Another version", b"retry")])
        return rows

    def new_pending(recipient, text, instruction, at=None):
        return {"to": recipient, "text": text, "instruction": instruction, "at": at, "busy": False}

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

    async def propose(event, who, text, instruction, found=None, at=None):
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
                preview(matches[0], text, at=at), buttons=send_buttons(bool(instruction), at), parse_mode=None
            )
            pending[shown.id] = new_pending(matches[0], text, instruction, at)
            return
        rows = [[Button.inline(label(u)[:40], f"pick:{i}".encode())] for i, u in enumerate(matches)]
        rows.append([Button.inline("❌ Cancel", b"cancel")])
        ask = await event.reply(f"Which “{who}” do you mean?", buttons=rows, parse_mode=None)
        choosing[ask.id] = {"candidates": matches, "text": text, "instruction": instruction, "at": at}

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

        # "17:00 Malika: hi" / "5pm ask Malika ..." -> send at that time
        at, raw = split_time(raw, local_now())
        if at and at <= local_now():
            await event.reply(f"{at:%H:%M} today has already passed. Use a later time.", parse_mode=None)
            return
        if not raw:
            await event.reply("Need a message after the time.  Example:  17:00 Malika: hi", parse_mode=None)
            return

        if looks_like_exact(raw):
            who, text = raw.split(":", 1)
            who, text = who.strip(), text.strip()
            if not text:
                await event.reply("Need a message after the name.  Example:  Malika: hi", parse_mode=None)
                return
            found = await find_people(who)
            if found[0] or not GROQ_API_KEY or who.startswith("@"):
                await propose(event, who, text, None, found, at)
                return
            # "напиши Малике: привет" looks like "Name: text", but nobody is called
            # "напиши Малике" - so it is a request; let the AI read it below.

        # AI mode
        if not GROQ_API_KEY:
            await event.reply("AI mode is off (no GROQ_API_KEY in Render).\nUse:  Name: message", parse_mode=None)
            return
        thinking = await event.reply("✍️ Writing…")
        try:
            who, text, send_at = await ai_compose(owner_name, raw)
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
        who = str(who).strip()
        if not who.startswith("@") and f"@{who}".lower() in raw.lower():
            who = "@" + who  # you wrote @username: look up exactly that account, not a fuzzy name
        if at is None and send_at:  # "at 5pm tell Malika I'm late"
            at = parse_send_at(send_at)
            now = local_now()
            if at and at <= now and now - at < timedelta(days=1):
                at += timedelta(days=1)  # "at 10am" when it is already 14:00 -> tomorrow 10:00
            if at is None or at <= now:
                await thinking.edit(
                    "I couldn't work out a future time to send it. Start with the time, e.g.  17:00 Malika: hi",
                    parse_mode=None,
                )
                return
        await thinking.delete()
        await propose(event, who, text, raw, at=at)

    @bot.on(events.CallbackQuery)
    async def on_button(event):
        if event.sender_id != owner_id:
            await event.answer("Not allowed")
            return
        data = event.data or b""
        mid = event.message_id

        # 🗑 under a scheduled message
        if data == b"unschedule":
            item = scheduled.pop(mid, None)  # pop before any await, so a double tap does nothing twice
            if not item:
                await event.answer(
                    "I lost track of it (the bot restarted). Open the chat → Scheduled messages to delete it.",
                    alert=True,
                )
                return
            if item["at"] <= local_now():
                await event.edit(f"✅ Already sent to {label(item['to'])}\n\n{item['text']}", parse_mode=None)
                return
            try:
                await user(functions.messages.DeleteScheduledMessagesRequest(peer=item["to"], id=[item["id"]]))
            except Exception as exc:
                scheduled[mid] = item
                log.warning("Unschedule failed: %s", type(exc).__name__)
                await event.answer(f"Couldn't cancel: {send_error_text(exc)}"[:190], alert=True)
                return
            await event.edit(f"🗑 Cancelled, nothing will be sent to {label(item['to'])}\n\n{item['text']}",
                             parse_mode=None)
            log.info("Scheduled message cancelled")
            return

        # "which person?" step
        if mid in choosing:
            item = choosing.pop(mid)
            if not data.startswith(b"pick:"):
                await event.edit("❌ Cancelled")
                return
            recipient = item["candidates"][int(data[5:])]
            pending[mid] = new_pending(recipient, item["text"], item["instruction"], item["at"])
            await event.edit(
                preview(recipient, item["text"], at=item["at"]),
                buttons=send_buttons(bool(item["instruction"]), item["at"]),
                parse_mode=None,
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
                _, new_text, _ = await ai_compose(owner_name, item["instruction"], previous=item["text"])
                if new_text:
                    item["text"] = new_text
                else:
                    note = "Couldn't write another version, try again."
            except Exception as exc:
                log.warning("AI retry failed: %s", exc)
                note = f"Couldn't write another version: {exc}"
            finally:
                item["busy"] = False
            await edit_preview(event, preview(recipient, item["text"], note, item["at"]), send_buttons(True, item["at"]))
            return

        if data == b"send":
            item["busy"] = True  # before any await, so a double tap can't send twice
            at, sent, note = item["at"], None, None
            try:
                if at and at <= local_now() + timedelta(seconds=30):
                    # the preview waited too long; don't silently send it now
                    item["at"] = None
                    note = f"{at:%H:%M} has already passed. Tap ✅ Send to send it now."
                else:
                    await event.answer("Scheduling…" if at else "Sending…")
                    sent = await user.send_message(recipient, item["text"], parse_mode=None, schedule=at)
            except Exception as exc:
                log.warning("Send failed: %s", type(exc).__name__)
                note = f"Not {'scheduled' if at else 'sent'}: {send_error_text(exc)}"
            finally:
                item["busy"] = False
            if note:
                # keep the preview and its buttons, so you can tap again later
                await edit_preview(event, preview(recipient, item["text"], note, item["at"]),
                                   send_buttons(with_retry, item["at"]))
                return
            pending.pop(mid, None)
            if not at:
                await event.edit(f"✅ Sent to {label(recipient)}\n\n{item['text']}", parse_mode=None)
                log.info("Message sent to a contact")
                return
            done = (f"⏰ Scheduled for {when_text(at)} → {label(recipient)}\n\n{item['text']}\n\n"
                    "Telegram will send it from your account at that time, even if this bot is offline. "
                    "You can also find it in that chat under Scheduled messages.")
            sent_id = getattr(sent, "id", None)
            buttons = None
            if sent_id:
                scheduled[mid] = {"to": recipient, "id": sent_id, "at": at, "text": item["text"]}
                buttons = [[Button.inline("🗑 Cancel scheduled message", b"unschedule")]]
            await event.edit(done, buttons=buttons, parse_mode=None)
            log.info("Message scheduled")
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
