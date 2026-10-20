import asyncio
import csv
import difflib
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    BotCommand, BotCommandScopeChat, InlineQueryResultArticle, InputTextMessageContent,
    InputMediaDocument,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, ContextTypes, filters, InlineQueryHandler,
)

# 1. BOT TOKEN: set the BOT_TOKEN environment variable on the server (never put it in the code)
TOKEN = os.environ.get("BOT_TOKEN", "")

# Your Telegram ID: only you can use /stats, /users, /backup, /ban, /notify, etc.
OWNER_ID = 1245363299

# True  = when YOU open the bot you only see the owner panel
# False = the bot treats you like a normal user (handy for testing)
OWNER_PANEL_ONLY = True

# Private channel where the movies are stored (numeric ID)
CHANNEL = -1004365835289

# Channels users must join before getting movies
REQUIRED = [
    ("@Movies_banned", "Movie Numbers and main base", "https://t.me/Movies_banned"),
    ("@all_exist_tv_series", "TV Series", "https://t.me/all_exist_tv_series"),
]

# Destination for user movie requests
REQUEST_CHANNEL = "@YouAskWe_Upload"

# Channel URLs
LINKS = {
    "btn_movies":  "https://t.me/Movies_banned",
    "btn_series":  "https://t.me/all_exist_tv_series",
    "btn_request": "https://t.me/YouAskWe_Upload",
}

# FIRST RUN ONLY: Initial seed movies
MOVIES = [
    (1, "Revolutionary Road", [2, 3]),
]

# A user who writes again after this many seconds gets a "welcome back" message (3600 = 1 hour)
RETURN_AFTER_SECONDS = 3600

# Custom Emojis support
CUSTOM_EMOJI = {
    "wave":    ("", "👋"),
    "smile":   ("", "😊"),
    "popcorn": ("", "🍿"),
    "party":   ("", "🎉"),
    "movie":   ("", "🎬"),
}

# ---------- Server settings ----------
# On Render, RENDER_EXTERNAL_URL is set automatically -> the bot runs in webhook mode.
# With no URL (e.g. on your laptop) the bot uses normal polling.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

# Free servers wipe local files on restart, so the database is also kept as a
# pinned file in this chat (your private chat with the bot by default).
BACKUP_CHAT = int(os.environ.get("BACKUP_CHAT_ID", OWNER_ID))
BACKUP_EVERY_SECONDS = 300
BACKUP_FILENAME = "vault_autobackup.json"

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

try:
    from telegram import LinkPreviewOptions
    NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except ImportError:
    NO_PREVIEW = {"disable_web_page_preview": True}

# ---------- Time (Tashkent, UTC+5) ----------
TASHKENT = timezone(timedelta(hours=5))
def now():
    return datetime.now(TASHKENT)
def today():
    return now().strftime("%Y-%m-%d")

# ---------- Database Handling ----------
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_db.json")
DB_EXISTED_AT_START = os.path.exists(DB_FILE)

def ensure_db_keys(db):
    db.setdefault("users", {})
    db.setdefault("movie_counts", {})
    db.setdefault("missing_searches", {})
    db.setdefault("banned_users", [])
    if "movies" not in db:
        db["movies"] = {str(n): {"title": title, "ids": ids} for n, title, ids in MOVIES}
    return db

def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except FileNotFoundError:
        db = {}
    except json.JSONDecodeError:
        os.replace(DB_FILE, DB_FILE + ".broken")
        logging.error("bot_db.json was damaged, saved as bot_db.json.broken")
        db = {}
    return ensure_db_keys(db)

DB = load_db()
_last_save_time = 0

def save_db(force=False):
    global _last_save_time
    t = time.time()
    if not force and t - _last_save_time < 5:
        return
    _last_save_time = t
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_FILE)

# ---------- Auto-backup to Telegram (survives server restarts) ----------
_backup_msg_id = None
_last_backup_hash = None
_backup_ready = False  # stays False if the old backup couldn't be read, so it is never overwritten

def _db_bytes():
    return json.dumps(DB, ensure_ascii=False, indent=1).encode("utf-8")

async def restore_from_pinned_backup(bot):
    global DB, _backup_msg_id, _last_backup_hash, _backup_ready
    try:
        chat = await bot.get_chat(BACKUP_CHAT)
    except Exception as e:
        logging.error(f"Auto-backup disabled, can't open backup chat {BACKUP_CHAT}: {e}")
        return
    pinned = chat.pinned_message
    if not (pinned and pinned.document and pinned.document.file_name == BACKUP_FILENAME):
        logging.info("No pinned auto-backup found, starting with the current database.")
        _backup_ready = True
        return
    _backup_msg_id = pinned.message_id
    if DB_EXISTED_AT_START:
        logging.info("Local bot_db.json exists, keeping it instead of the pinned backup.")
        _backup_ready = True
        return
    try:
        file = await bot.get_file(pinned.document.file_id)
        data = json.loads((await file.download_as_bytearray()).decode("utf-8"))
        if "users" not in data or "movies" not in data:
            raise ValueError("backup file has no users/movies")
    except Exception as e:
        logging.error(f"Auto-backup disabled, could not load pinned backup: {e}")
        await alert_owner(bot, "backup-load", f"⚠️ Could not load the pinned auto-backup: {e}\nUse /restore to load a backup by hand.")
        return
    DB = ensure_db_keys(data)
    save_db(force=True)
    _last_backup_hash = hashlib.sha256(_db_bytes()).hexdigest()
    _backup_ready = True
    logging.info(f"Restored database from pinned backup ({len(DB['users'])} users, {len(DB['movies'])} movies).")

async def push_backup(bot):
    global _backup_msg_id, _last_backup_hash
    if not _backup_ready:
        return
    raw = _db_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest == _last_backup_hash:
        return
    caption = f"🤖 Auto-backup {now():%Y-%m-%d %H:%M} (Tashkent)\nKeep this message pinned: the bot loads it after every restart."
    try:
        if _backup_msg_id:
            try:
                await bot.edit_message_media(
                    chat_id=BACKUP_CHAT, message_id=_backup_msg_id,
                    media=InputMediaDocument(io.BytesIO(raw), filename=BACKUP_FILENAME, caption=caption),
                )
                _last_backup_hash = digest
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    _last_backup_hash = digest
                    return
                logging.warning(f"Could not update backup message, sending a new one: {e}")
                _backup_msg_id = None
        msg = await bot.send_document(
            BACKUP_CHAT, document=io.BytesIO(raw), filename=BACKUP_FILENAME,
            caption=caption, disable_notification=True,
        )
        await bot.pin_chat_message(BACKUP_CHAT, msg.message_id, disable_notification=True)
        _backup_msg_id = msg.message_id
        _last_backup_hash = digest
    except Exception as e:
        logging.error(f"Auto-backup failed: {e}")

async def backup_loop(bot):
    while True:
        await asyncio.sleep(BACKUP_EVERY_SECONDS)
        await push_backup(bot)

_backup_task = None

async def on_startup(app):
    global _backup_task
    await restore_from_pinned_backup(app.bot)
    await push_backup(app.bot)
    _backup_task = asyncio.create_task(backup_loop(app.bot))

async def on_stop(app):
    if _backup_task:
        _backup_task.cancel()
    await push_backup(app.bot)

def track_user(user):
    uid = str(user.id)
    rec = DB["users"].get(uid)
    is_new = rec is None
    if is_new:
        rec = {
            "id": user.id,
            "joined": now().strftime("%Y-%m-%d %H:%M"),
            "stage": None,
            "ui_lang": None,
            "requests": 0,
            "pending_movie": None,
            "spam_count": 0,
            "muted_until": 0,
            "strike_count": 0,
        }
        DB["users"][uid] = rec
    rec["first_name"] = user.first_name or ""
    rec["last_name"] = user.last_name or ""
    rec["username"] = user.username or ""
    rec["lang"] = user.language_code or ""
    rec["last_seen"] = now().strftime("%Y-%m-%d %H:%M")
    rec["prev_ts"] = rec.get("last_ts")
    rec["last_ts"] = time.time()
    save_db()
    return rec, is_new

def get_rec(user):
    rec = DB["users"].get(str(user.id))
    if rec is None:
        rec, _ = track_user(user)
    return rec

def full_name(rec):
    return f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip() or "—"

def is_returning(rec):
    prev = rec.get("prev_ts")
    return prev is not None and time.time() - prev > RETURN_AFTER_SECONDS

# ---------- Owner Alerts ----------
_last_alert = {}

async def alert_owner(bot, key, text, cooldown=600):
    t = time.time()
    if t - _last_alert.get(key, 0) < cooldown:
        return
    _last_alert[key] = t
    try:
        await bot.send_message(OWNER_ID, text[:3500])
    except Exception as e:
        logging.error(f"Could not alert the owner: {e}")

def em(name):
    emoji_id, fallback = CUSTOM_EMOJI[name]
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback

# ---------- Trilingual Texts ----------
LANG_NAMES = {"uz": "🇺🇿 O'zbekcha", "en": "🇬🇧 English", "hi": "🇮🇳 Hinglish"}

TEXTS = {
    "en": {
        "welcome": "{wave} Hello{name}!\n\n{movie} Welcome to Cinema Vault!\n\n{popcorn} Watch thousands of movies and series in high quality!\n\n🚀 How to use:\n✅ Send code or search movies\n✅ Choose via sections below",
        "welcome_back": [
            "{wave} Welcome back{name}! {smile}\n\nSend a movie number or use search {movie}",
            "{wave} Good to see you again{name}! {smile}\n\nReady for a movie? Send a number {popcorn}",
        ],
        "welcome_back_movie": [
            "{wave} Welcome back{name}! How was the movie? {popcorn}\n\nReady for another one? Send a number {movie}",
            "{popcorn} Welcome back{name}! Did you enjoy it?\n\nSend the next movie number {movie}",
        ],
        "hello_reply": "{wave} Hello{name}! {smile}\n\nReady for a movie? Send a movie number {movie}",
        "join_first": "🔒 Please join our channels first, then tap “I joined”.",
        "joined_btn": "✅ I joined",
        "thanks_joined": "✅ Thanks! Now send me a movie number.",
        "not_joined": "⚠️ You haven't joined all channels yet.",
        "not_found": "❌ Movie not found.\n\nRequest it for free using the button below:",
        "request_btn": "📥 Request Free Movie",
        "enjoy": [
            "{popcorn} Enjoy the movie! Have a great time! {smile}",
            "{movie} Sit back, relax, and enjoy the show! {party}",
            "{popcorn} Hope you love it! ✨",
            "{movie} Happy watching! Let's go! 🍿",
        ],
        "error": "⚠️ Something went wrong. Try again later.",
        "sending": "⏳ Sending…",
        "btn_movies": "🎬 Movie numbers",
        "btn_series": "📺 TV series",
        "btn_request": "💬 Request chat",
        "btn_search": "🔍 Search movies",
    },
    "uz": {
        "welcome": "{wave} Assalomu alaykum{name}!\n\n{movie} Cinema Vault botiga xush kelibsiz!\n\n{popcorn} Bot orqali minglab kino, serial va multfilmlarni yuqori sifatda tomosha qiling!\n\n🚀 Foydalanish juda oson:\n✅ Kino kodini yuboring yoki qidiring\n✅ Qulay bo'limlar orqali tanlang",
        "welcome_back": [
            "{wave} Xush kelibsiz{name}! {smile}\n\nKino raqamini yuboring yoki qidiring {movie}",
            "{wave} Sizni yana ko'rib xursandman{name}! {smile}\n\nKino ko'rishga tayyormisiz? Raqam yuboring {popcorn}",
        ],
        "welcome_back_movie": [
            "{wave} Xush kelibsiz{name}! Kino qanday bo'ldi? {popcorn}\n\nYana bittasini ko'ramizmi? Raqam yuboring {movie}",
            "{popcorn} Xush kelibsiz{name}! Kino yoqdimi?\n\nKeyingisini yuboring {movie}",
        ],
        "hello_reply": "{wave} Assalomu alaykum{name}! {smile}\n\nKino ko'rishga tayyormisiz? Raqam yuboring {movie}",
        "join_first": "🔒 Avval kanallarga a'zo bo'ling, so'ng “A'zo bo'ldim” tugmasini bosing.",
        "joined_btn": "✅ A'zo bo'ldim",
        "thanks_joined": "✅ Rahmat! Endi kino raqamini yuboring.",
        "not_joined": "⚠️ Siz hali hamma kanallarga a'zo bo'lmadingiz.",
        "not_found": "❌ Kino topilmadi.\n\nTopolmagan kinoingizni quyidagi tugma orqali bepul so'rov qiling:",
        "request_btn": "📥 Bepul kino so'rash",
        "enjoy": [
            "{popcorn} Yoqimli tomosha! Mazza qilib ko'ring! {smile}",
            "{movie} Kino vaqti! O'zingizni qulay oling va rohatlaning! {party}",
            "{popcorn} Ushbu kino sizga yoqishiga umid qilamiz! ✨",
            "{movie} Maroqli tomosha! 🍿",
        ],
        "error": "⚠️ Xatolik yuz berdi. Keyinroq urinib ko'ring.",
        "sending": "⏳ Yuborilmoqda…",
        "btn_movies": "🎬 Kino raqamlari",
        "btn_series": "📺 Seriallar",
        "btn_request": "💬 So'rov chati",
        "btn_search": "🔍 Film qidiruv",
    },
    "hi": {
        "welcome": "{wave} Hello{name}!\n\n{movie} Cinema Vault me aapka swagat hai!\n\n{popcorn} High quality me hazaron movies aur series dekhein!\n\n🚀 Use karna asan hai:\n✅ Movie code bhejo ya search karo\n✅ Neeche diye sections se chuno",
        "welcome_back": [
            "{wave} Welcome back{name}! {smile}\n\nMovie ka number bhejo {movie}",
            "{wave} Phir dekhke accha laga{name}! {smile}\n\nNumber bhejo {popcorn}",
        ],
        "welcome_back_movie": [
            "{wave} Welcome back{name}! Movie kaisi lagi? {popcorn}\n\nAgli dekhni hai? Number bhejo {movie}",
            "{popcorn} Wapas aaye{name}! Movie pasand aayi?\n\nNumber bhejo {movie}",
        ],
        "hello_reply": "{wave} Assalomu alaykum{name}! {smile}\n\nMovie dekhne ke liye ready ho? Number bhejo {movie}",
        "join_first": "🔒 Pehle channels join karo, phir “Maine join kar liya” dabao.",
        "joined_btn": "✅ Maine join kar liya",
        "thanks_joined": "✅ Thanks! Ab movie ka number bhejo.",
        "not_joined": "⚠️ Tumne saare channels join nahi kiye.",
        "not_found": "❌ Movie nahi mili.\n\nNeeche button se free request karo:",
        "request_btn": "📥 Free Movie Request",
        "enjoy": [
            "{popcorn} Movie ka maza lo! {smile}",
            "{movie} Relax karo aur enjoy karo! {party}",
            "{popcorn} Umeed hai pasand aayegi! ✨",
            "{movie} Mast watch karo! 🍿",
        ],
        "error": "⚠️ Kuch gadbad ho gayi. Try again.",
        "sending": "⏳ Bhej rahe hain…",
        "btn_movies": "🎬 Movie numbers",
        "btn_series": "📺 TV series",
        "btn_request": "💬 Request chat",
        "btn_search": "🔍 Movie search",
    },
}

def t(lang, key, **kw):
    value = TEXTS.get(lang, TEXTS["en"])[key]
    if isinstance(value, list):
        value = random.choice(value)
    emojis = {name: em(name) for name in CUSTOM_EMOJI}
    kw.setdefault("name", "")
    return value.format(**emojis, **kw)

async def send_welcome_back(message, lang, rec, user):
    key = "welcome_back_movie" if rec.get("requests", 0) > 0 else "welcome_back"
    await message.reply_text(t(lang, key, name=name_part(user)), reply_markup=main_nav_kb(lang), parse_mode="HTML")

def name_part(user):
    n = (user.first_name or "").strip()
    return f", {html.escape(n)}" if n else ""

def profile_url(rec):
    if rec.get("username"):
        return f"https://t.me/{rec['username']}"
    return f"tg://user?id={rec['id']}"

# ---------- UI Keyboards ----------
def lang_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"lang:{code}")]
        for code, name in LANG_NAMES.items()
    ])

def main_nav_kb(lang):
    search_btn = InlineKeyboardButton(t(lang, "btn_search"), switch_inline_query_current_chat="")
    b = lambda key: InlineKeyboardButton(t(lang, key), url=LINKS[key])
    return InlineKeyboardMarkup([
        [search_btn],
        [b("btn_movies"), b("btn_series")],
        [b("btn_request")],
    ])

def single_request_kb(lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "request_btn"), callback_data="req_movie")]
    ])

def join_kb(lang):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"📢 {label}", url=link)] for _, label, link in REQUIRED]
        + [[InlineKeyboardButton(t(lang, "joined_btn"), callback_data="check")]]
    )

def owner_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="own:stats"),
         InlineKeyboardButton("👥 Users", callback_data="own:users")],
        [InlineKeyboardButton("🎬 Movie list", callback_data="own:movies"),
         InlineKeyboardButton("➕ Add movie", callback_data="own:add")],
        [InlineKeyboardButton("🔍 Missing searches", callback_data="own:missing")],
        [InlineKeyboardButton("💾 Backup", callback_data="own:backup"),
         InlineKeyboardButton("♻️ Restore", callback_data="own:restore")],
    ])

async def ask_language(bot, chat_id):
    await bot.send_message(
        chat_id,
        "🌐 Choose your language / Tilni tanlang / Apni language chuno:",
        reply_markup=lang_kb(),
        parse_mode="HTML"
    )

# ---------- Search Helpers ----------
def normalize(text):
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())

def movie_tables():
    by_number, by_title = {}, {}
    for n, m in DB["movies"].items():
        item = (int(n), m["title"], m["ids"])
        by_number[int(n)] = item
        by_title[normalize(m["title"])] = item
    return by_number, by_title

def find_movie(text):
    by_number, by_title = movie_tables()
    text = text.strip()

    if text.isdigit():
        return by_number.get(int(text))

    name = normalize(text)
    if not name:
        return None
    if name in by_title:
        return by_title[name]

    no_digits = re.sub(r"\d+$", "", name).strip()
    if no_digits in by_title:
        return by_title[no_digits]

    close = difflib.get_close_matches(name, by_title.keys(), n=1, cutoff=0.85)
    if close:
        return by_title[close[0]]

    return None

_membership_cache = {}

async def is_member(bot, user_id):
    t_now = time.time()
    if user_id in _membership_cache:
        status, ts = _membership_cache[user_id]
        if t_now - ts < 600:
            return status

    for chat, _, _ in REQUIRED:
        try:
            m = await bot.get_chat_member(chat, user_id)
            if m.status not in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            ):
                _membership_cache[user_id] = (False, t_now)
                return False
        except Exception as e:
            logging.error(f"Membership check failed for {chat}: {e}")
            await alert_owner(
                bot, f"member-{chat}",
                f"⚠️ I can't check members of {chat}: {e}\nIs the bot an admin there?",
            )
            _membership_cache[user_id] = (False, t_now)
            return False

    _membership_cache[user_id] = (True, t_now)
    return True

async def track_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.is_bot:
        return

    if str(user.id) in DB.get("banned_users", []):
        return

    if update.effective_chat.type != "private":
        return

    rec, is_new = track_user(user)

    now_ts = time.time()
    if now_ts < rec.get("muted_until", 0):
        return

    if now_ts - rec.get("last_msg_ts", 0) < 1.5:
        rec["spam_count"] = rec.get("spam_count", 0) + 1
        if rec["spam_count"] > 7:
            rec["muted_until"] = now_ts + 60
            rec["spam_count"] = 0
            rec["strike_count"] = rec.get("strike_count", 0) + 1
            save_db()
            if rec["strike_count"] >= 3:
                await alert_owner(context.bot, f"spam-{user.id}", f"⚠️ User {full_name(rec)} ({user.id}) hit multiple spam limits!", cooldown=60)
            return
    else:
        rec["spam_count"] = 0
    rec["last_msg_ts"] = now_ts
    save_db()

    if is_new and user.id != OWNER_ID:
        uname = f"@{html.escape(rec['username'])}" if rec["username"] else "—"
        text = (
            "🆕 <b>New subscriber entered the Vault!</b>\n\n"
            f"👤 Name: {html.escape(full_name(rec))}\n"
            f"🔗 Username: {uname}\n"
            f'🆔 ID: <a href="{profile_url(rec)}">{user.id}</a>\n'
            f"🌐 Language: {html.escape(rec['lang'] or '—')}\n"
            f"📅 Joined: {rec['joined']} (Tashkent)\n\n"
            f'<a href="{profile_url(rec)}">Open profile</a>'
        )
        try:
            await context.bot.send_message(
                OWNER_ID, text, parse_mode="HTML", **NO_PREVIEW
            )
        except Exception as e:
            logging.error(f"Could not send the new-user alert: {e}")

def is_owner_panel(user):
    return user.id == OWNER_ID and OWNER_PANEL_ONLY

# ---------- Core Bot Flows ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_owner_panel(user):
        context.user_data.pop("adding", None)
        await send_owner_panel(context.bot, chat_id)
        return

    rec = get_rec(user)
    rec["stage"] = None
    save_db()

    lang = rec.get("ui_lang")
    if not lang:
        if context.args:
            rec["pending_movie"] = " ".join(context.args)
            save_db()
        await ask_language(context.bot, chat_id)
        return

    if context.args:
        query = " ".join(context.args)
        await handle_movie_request(update, context, query, lang, rec, user)
        return

    if is_returning(rec):
        await send_welcome_back(update.message, lang, rec, user)
    else:
        await update.message.reply_text(t(lang, "welcome", name=name_part(user)), reply_markup=main_nav_kb(lang), parse_mode="HTML")

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await ask_language(context.bot, update.effective_chat.id)

async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    rec = get_rec(user)
    lang = rec.get("ui_lang") or "en"

    if not DB["movies"]:
        await update.message.reply_text("No movies available yet.")
        return

    num = random.choice(list(DB["movies"].keys()))
    movie_item = (int(num), DB["movies"][num]["title"], DB["movies"][num]["ids"])
    await deliver_movie(update, context, movie_item, lang, rec, user)

async def cb_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    lang = q.data.split(":", 1)[1]
    if lang not in TEXTS:
        await q.answer()
        return
    rec = get_rec(q.from_user)
    rec["ui_lang"] = lang
    save_db()
    await q.answer()

    pending = rec.get("pending_movie")
    if pending:
        rec["pending_movie"] = None
        save_db()
        await q.edit_message_text(t(lang, "welcome", name=name_part(q.from_user)), reply_markup=main_nav_kb(lang), parse_mode="HTML")
        class DummyUpdate:
            effective_chat = q.message.chat
            effective_user = q.from_user
            message = q.message
        q.message.text = pending
        await handle_text(DummyUpdate(), context)
    else:
        await q.edit_message_text(t(lang, "welcome", name=name_part(q.from_user)), reply_markup=main_nav_kb(lang), parse_mode="HTML")

async def deliver_movie(update, context, movie, lang, rec, user):
    number, title, msg_ids = movie
    chat_id = update.effective_chat.id

    sending_msg = await update.message.reply_text(t(lang, "sending"))
    try:
        for msg_id in msg_ids:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=CHANNEL,
                message_id=msg_id,
                protect_content=True,
            )
        try:
            await sending_msg.delete()
        except:
            pass
    except Exception as e:
        try:
            await sending_msg.delete()
        except:
            pass
        logging.error(f"Could not send movie {number} ({title}): {e}")
        await alert_owner(
            context.bot, f"copy-{number}",
            f"⚠️ Could not send movie #{number} ({title}) to {full_name(rec)} (ID {user.id}):\n{e}",
        )
        await update.message.reply_text(t(lang, "error"))
        return

    DB["movie_counts"][str(number)] = DB["movie_counts"].get(str(number), 0) + 1
    rec["requests"] = rec.get("requests", 0) + 1
    save_db()

    # Only sending the enjoy text now, without channel promo list
    enjoy_text = t(lang, "enjoy")
    await update.message.reply_text(enjoy_text, reply_markup=main_nav_kb(lang), parse_mode="HTML", **NO_PREVIEW)

async def handle_movie_request(update, context, text, lang, rec, user):
    if not await is_member(context.bot, user.id):
        await update.message.reply_text(t(lang, "join_first"), reply_markup=join_kb(lang))
        return

    movie = find_movie(text)
    if movie is None:
        cleaned = text.strip().lower()
        if cleaned and not cleaned.isdigit():
            DB["missing_searches"][cleaned] = DB["missing_searches"].get(cleaned, 0) + 1
            save_db()

        await update.message.reply_text(t(lang, "not_found"), reply_markup=single_request_kb(lang), parse_mode="HTML")
        return

    await deliver_movie(update, context, movie, lang, rec, user)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    text = update.message.text
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_owner_panel(user):
        if await add_flow_text(update, context):
            return
        if context.user_data.get("awaiting_restore"):
            context.user_data.pop("awaiting_restore", None)
            doc = update.message.document
            if doc:
                try:
                    file = await context.bot.get_file(doc.file_id)
                    content = await file.download_as_bytearray()
                    new_db = json.loads(content.decode("utf-8"))
                    if "users" in new_db and "movies" in new_db:
                        global DB
                        DB = ensure_db_keys(new_db)
                        save_db(force=True)
                        await push_backup(context.bot)
                        await update.message.reply_text("✅ Database restored successfully!")
                    else:
                        await update.message.reply_text("❌ Invalid backup file format.")
                except Exception as e:
                    await update.message.reply_text(f"❌ Restore failed: {e}")
            else:
                await update.message.reply_text("❌ Please send a valid JSON file for restore.")
            await send_owner_panel(context.bot, chat_id)
            return
        await send_owner_panel(context.bot, chat_id)
        return

    rec = get_rec(user)
    lang = rec.get("ui_lang")
    if not lang:
        rec["pending_movie"] = text
        save_db()
        await ask_language(context.bot, chat_id)
        return

    returning = is_returning(rec)
    if returning:
        await send_welcome_back(update.message, lang, rec, user)

    words = normalize(text).split()
    greetings = {"hello", "hi", "hey", "hola", "salom", "assalomu", "assalom", "salam", "привет", "здравствуйте"}
    if words and words[0] in greetings:
        await update.message.reply_text(t(lang, "hello_reply", name=name_part(user)), reply_markup=main_nav_kb(lang), parse_mode="HTML")
        return

    await handle_movie_request(update, context, text, lang, rec, user)

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rec = get_rec(query.from_user)
    lang = rec.get("ui_lang") or "en"
    if await is_member(context.bot, query.from_user.id):
        await query.answer()
        await query.message.reply_text(t(lang, "thanks_joined"), reply_markup=main_nav_kb(lang))
    else:
        await query.answer(t(lang, "not_joined"), show_alert=True)

async def cb_request_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rec = get_rec(query.from_user)
    lang = rec.get("ui_lang") or "en"
    rec["stage"] = "awaiting_custom_request"
    save_db()
    await query.message.reply_text("📥 Please type the name of the movie or series you want to request:")

# ---------- Inline Query Search Handler ----------
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip().lower()
    results = []

    for num, m in DB["movies"].items():
        title = m["title"]
        if not query or query in title.lower():
            results.append(
                InlineQueryResultArticle(
                    id=str(num),
                    title=title,
                    description=f"Movie #{num} | Tap to select",
                    input_message_content=InputTextMessageContent(
                        message_text=str(num)
                    )
                )
            )

    await update.inline_query.answer(results[:50], cache_time=5)

# ---------- Owner Panel & Commands ----------
def real_users():
    return [r for r in DB["users"].values() if r["id"] != OWNER_ID]

async def send_owner_panel(bot, chat_id):
    await bot.send_message(
        chat_id,
        "🛠 <b>Cinema Vault Owner Panel</b>\n\n"
        "Tap a button, or use commands:\n"
        "/stats /users /movies /missing /notify /backup /restore",
        parse_mode="HTML",
        reply_markup=owner_kb(),
    )

async def do_stats(bot, chat_id):
    users = real_users()
    t_ = today()
    new_today = sum(1 for r in users if r.get("joined", "").startswith(t_))
    active_today = sum(1 for r in users if r.get("last_seen", "").startswith(t_))
    total_requests = sum(DB["movie_counts"].values())

    top = sorted(DB["movie_counts"].items(), key=lambda x: x[1], reverse=True)[:5]
    lines = []
    for i, (num, cnt) in enumerate(top, 1):
        m = DB["movies"].get(num)
        lines.append(f"{i}. {m['title'] if m else '#' + num} — {cnt}")
    top_text = "\n".join(lines) or "nothing yet"

    langs = {}
    for r in users:
        key = r.get("ui_lang") or "not chosen"
        langs[key] = langs.get(key, 0) + 1
    lang_text = ", ".join(f"{k}: {v}" for k, v in langs.items()) or "—"

    await bot.send_message(
        chat_id,
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"🆕 New Today: {new_today}\n"
        f"🔥 Active Today: {active_today}\n"
        f"🎬 Movies Sent: {total_requests}\n"
        f"🌐 Languages: {lang_text}\n\n"
        f"🏆 <b>Top Movies:</b>\n{top_text}",
        parse_mode="HTML",
    )

async def do_users(bot, chat_id):
    users = real_users()
    if not users:
        await bot.send_message(chat_id, "No users yet.")
        return
    users_sorted = sorted(users, key=lambda x: x.get("joined", ""), reverse=True)
    lines = ["👥 <b>Recent Vault Members:</b>"]
    for r in users_sorted[:30]:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"• <a href='{profile_url(r)}'>{html.escape(full_name(r))}</a> ({uname}) — joined {r['joined']}")
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", **NO_PREVIEW)

async def do_movies(bot, chat_id):
    if not DB["movies"]:
        await bot.send_message(chat_id, "No movies saved.")
        return
    sorted_movies = sorted(DB["movies"].items(), key=lambda x: int(x[0]))
    lines = ["🎬 <b>Saved Movies in Vault:</b>"]
    for num, m in sorted_movies:
        lines.append(f"#{num}: {html.escape(m['title'])} (IDs: {m['ids']})")

    text = "\n".join(lines)
    if len(text) > 4000:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML")

async def do_missing(bot, chat_id):
    missing = DB.get("missing_searches", {})
    if not missing:
        await bot.send_message(chat_id, "No missing searches recorded yet.")
        return
    top = sorted(missing.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["🔍 <b>Top 10 Requested Missing Searches:</b>"]
    for i, (term, count) in enumerate(top, 1):
        lines.append(f"{i}. <b>{html.escape(term)}</b> — {count} searches")
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

async def add_flow_text(update, context):
    data = context.user_data.get("adding")
    if not data:
        return False

    step = data.get("step")
    text = update.message.text

    if step == "number":
        if not text.isdigit():
            await update.message.reply_text("Please enter a valid numeric number for the movie.")
            return True
        data["number"] = int(text)
        data["step"] = "title"
        await update.message.reply_text("Enter the movie title:")
        return True

    elif step == "title":
        data["title"] = text.strip()
        data["step"] = "ids"
        await update.message.reply_text("Forward message(s) from the storage channel or enter message IDs separated by space:")
        return True

    elif step == "ids":
        msg_ids = []
        if update.message.forward_from_message_id:
            msg_ids.append(update.message.forward_from_message_id)
        else:
            parts = text.replace(",", " ").split()
            for p in parts:
                if p.isdigit():
                    msg_ids.append(int(p))

        if not msg_ids:
            await update.message.reply_text("Please provide valid message IDs or forward messages from the channel.")
            return True

        data["ids"] = msg_ids
        num_str = str(data["number"])
        DB["movies"][num_str] = {"title": data["title"], "ids": data["ids"]}
        save_db()

        context.user_data.pop("adding", None)
        await update.message.reply_text(f"✅ Movie #{data['number']} ({data['title']}) added successfully!")
        await send_owner_panel(context.bot, update.effective_chat.id)
        return True

    return False

# ---------- Callback Query Router ----------
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data

    if data.startswith("lang:"):
        await cb_lang(update, context)
        return
    if data == "check":
        await check_join(update, context)
        return
    if data == "req_movie":
        await cb_request_movie(update, context)
        return
    if data.startswith("ban:"):
        if q.from_user.id != OWNER_ID:
            await q.answer("Unauthorized", show_alert=True)
            return
        uid = data.split(":", 1)[1]
        if uid not in DB["banned_users"]:
            DB["banned_users"].append(uid)
            save_db()
        await q.answer("User banned successfully.")
        await q.edit_message_text("🚫 User has been banned.")
        return

    if data.startswith("own:"):
        if q.from_user.id != OWNER_ID:
            await q.answer("Unauthorized", show_alert=True)
            return
        action = data.split(":", 1)[1]
        await q.answer()
        if action == "stats":
            await do_stats(context.bot, q.message.chat_id)
        elif action == "users":
            await do_users(context.bot, q.message.chat_id)
        elif action == "movies":
            await do_movies(context.bot, q.message.chat_id)
        elif action == "missing":
            await do_missing(context.bot, q.message.chat_id)
        elif action == "add":
            context.user_data["adding"] = {"step": "number"}
            await q.message.reply_text("Enter the movie number:")
        elif action == "backup":
            bio = io.BytesIO(json.dumps(DB, ensure_ascii=False, indent=1).encode("utf-8"))
            bio.name = f"bot_db_{today()}.json"
            await context.bot.send_document(q.message.chat_id, document=bio, caption="💾 Here is your vault backup database.")
        elif action == "restore":
            context.user_data["awaiting_restore"] = True
            await q.message.reply_text("Please send the JSON backup file now to restore.")
        return

# ---------- Command Handlers ----------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.user_data["adding"] = {"step": "number"}
    await update.message.reply_text("Enter the movie number:")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove <movie_number>")
        return
    num = context.args[0]
    if num in DB["movies"]:
        title = DB["movies"][num]["title"]
        del DB["movies"][num]
        save_db()
        await update.message.reply_text(f"✅ Movie #{num} ({title}) removed from vault.")
    else:
        await update.message.reply_text("❌ Movie number not found.")

async def cmd_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await do_movies(context.bot, update.effective_chat.id)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await do_stats(context.bot, update.effective_chat.id)

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await do_users(context.bot, update.effective_chat.id)

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    bio = io.BytesIO(json.dumps(DB, ensure_ascii=False, indent=1).encode("utf-8"))
    bio.name = f"bot_db_{today()}.json"
    await update.message.reply_document(document=bio, caption="💾 Here is your vault backup database.")

async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.user_data["awaiting_restore"] = True
    await update.message.reply_text("Please send the JSON backup file now to restore.")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = context.args[0]
    if uid not in DB["banned_users"]:
        DB["banned_users"].append(uid)
        save_db()
    await update.message.reply_text(f"User {uid} banned.")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = context.args[0]
    if uid in DB["banned_users"]:
        DB["banned_users"].remove(uid)
        save_db()
    await update.message.reply_text(f"User {uid} unbanned.")

async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Usage: /notify <user_id> <movie_name>")
        return

    target_id = context.args[0]
    movie_info = " ".join(context.args[1:])

    msg_text = (
        f"🎉 <b>Good news from Cinema Vault!</b>\n\n"
        f"The movie/series you requested (<b>{html.escape(movie_info)}</b>) has been successfully added to our vault!\n\n"
        f"Send the movie number or title now to watch it 🍿"
    )

    try:
        await context.bot.send_message(chat_id=int(target_id), text=msg_text, parse_mode="HTML")
        await update.message.reply_text(f"✅ Notification dispatched to user {target_id}!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to notify user {target_id}: {e}")

async def handle_custom_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rec = get_rec(user)
    if rec.get("stage") != "awaiting_custom_request":
        return False

    rec["stage"] = None
    save_db()

    text = update.message.text
    lang = rec.get("ui_lang") or "en"

    req_text = (
        "📥 <b>New Cinema Vault Request!</b>\n\n"
        f"👤 User: {html.escape(full_name(rec))} (<a href='{profile_url(rec)}'>{user.id}</a>)\n"
        f"📝 Request: {html.escape(text)}\n\n"
        f"💡 <i>Tip: Reply with <code>/notify {user.id} {html.escape(text)}</code> once uploaded!</i>"
    )
    try:
        await context.bot.send_message(REQUEST_CHANNEL, req_text, parse_mode="HTML", **NO_PREVIEW)
    except Exception as e:
        logging.error(f"Failed to send request to request channel: {e}")

    try:
        await context.bot.send_message(OWNER_ID, req_text, parse_mode="HTML", **NO_PREVIEW)
    except Exception as e:
        logging.error(f"Failed to send request to owner: {e}")

    await update.message.reply_text("✅ Your movie request has been successfully sent to the admin team! We'll notify you as soon as it drops 🎉")
    return True

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if await handle_custom_request(update, context):
        return
    await handle_text(update, context)

def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it as an environment variable.")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .post_stop(on_stop)
        .build()
    )

    app.add_handler(TypeHandler(Update, track_all), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("movies", cmd_movies))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("missing", lambda u, c: do_missing(c.bot, u.message.chat_id)))

    # Register the inline query handler for search window popups
    app.add_handler(InlineQueryHandler(inline_query))

    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.Document.ALL, text_router))

    if WEBHOOK_URL:
        # Server mode: Telegram pushes updates to https://<your-app>/telegram
        secret = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
        logging.info(f"Cinema Vault Bot running in webhook mode at {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logging.info("Cinema Vault Bot is fully armed and running (polling)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
