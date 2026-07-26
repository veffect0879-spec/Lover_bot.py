import logging
import os
import sqlite3
import threading
import asyncio
from io import BytesIO
import base64
import json

import httpx
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------------- KEEP ALIVE (FLASK) ----------------
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

def keep_alive():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

keep_alive()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- CONFIGURATIONS ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

env_keys = os.environ.get("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [k.strip() for k in env_keys.split(",") if k.strip()] if env_keys else []
GEMINI_MODEL = "gemini-3.6-flash"

# ---------------- GEMINI KEY ROTATION ----------------
class KeyRotator:
    def __init__(self, keys: list[str]):
        self._keys = keys
        self._cooldown_until: dict[str, float] = {}
        self._idx = 0
        self._lock = threading.Lock()

    def _next_pacific_midnight_epoch(self) -> float:
        import time
        now = time.time()
        pacific_offset = 8 * 3600
        secs_into_pacific_day = (now - pacific_offset) % 86400
        return now + (86400 - secs_into_pacific_day)

    def mark_exhausted(self, key: str):
        with self._lock:
            self._cooldown_until[key] = self._next_pacific_midnight_epoch()
        logger.warning(f"Gemini key ...{key[-6:]} marked exhausted until next daily reset.")

    def mark_cooldown(self, key: str, seconds: float):
        import time
        with self._lock:
            self._cooldown_until[key] = time.time() + seconds

    def available_keys_in_order(self) -> list[str]:
        import time
        with self._lock:
            now = time.time()
            if not self._keys:
                return []
            ordered = self._keys[self._idx:] + self._keys[: self._idx]
            self._idx = (self._idx + 1) % len(self._keys)
        return [k for k in ordered if self._cooldown_until.get(k, 0) <= now]

key_rotator = KeyRotator(GEMINI_API_KEYS)

# ---------------- DATABASE ----------------
DB_FILE = "spouse_bot.db"
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

def init_db():
    with _db_lock:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS users
               (user_id INTEGER PRIMARY KEY, role_type TEXT, spouse_style TEXT, affection INTEGER, chat_history TEXT, memory TEXT, total_tokens INTEGER)"""
        )
        _conn.commit()

def _get_user_sync(user_id):
    with _db_lock:
        row = _conn.execute(
            "SELECT role_type, spouse_style, affection, chat_history, memory, total_tokens FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {
            "role_type": row[0],
            "spouse_style": row[1],
            "affection": row[2],
            "history": json.loads(row[3]) if row[3] else [],
            "memory": json.loads(row[4]) if row[4] else {},
            "total_tokens": row[5] if row[5] is not None else 0
        }
    return None

def _save_user_sync(user_id, role_type, spouse_style, affection, history, memory, total_tokens):
    with _db_lock:
        _conn.execute(
            """INSERT OR REPLACE INTO users (user_id, role_type, spouse_style, affection, chat_history, memory, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, role_type, spouse_style, affection, json.dumps(history), json.dumps(memory), total_tokens),
        )
        _conn.commit()

def _get_total_users_count_sync() -> int:
    with _db_lock:
        row = _conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0

async def get_user(user_id):
    return await asyncio.to_thread(_get_user_sync, user_id)

async def save_user(user_id, role_type, spouse_style, affection, history, memory, total_tokens):
    await asyncio.to_thread(_save_user_sync, user_id, role_type, spouse_style, affection, history, memory, total_tokens)

async def get_total_users_count():
    return await asyncio.to_thread(_get_total_users_count_sync)

init_db()

_user_locks: dict[int, asyncio.Lock] = {}
def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# ---------------- HANDLERS ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user and user["role_type"]:
        await update.message.reply_text("💖 အိမ်ပြန်ရောက်ပြီလားရှင်... စောင့်နေတယ်နော် ✨")
        return
    keyboard = [
        [InlineKeyboardButton("👨‍🦰 ကျွန်တော်က ယောက်ျားလေး (ဇနီးသည်)", callback_data="set_role:wife")],
        [InlineKeyboardButton("👩‍🦰 ကျွန်မက မိန်းကလေး (ခင်ပွန်းသည်)", callback_data="set_role:husband")],
    ]
    await update.message.reply_text("✨ အိမ်ထောင်ဖက် ပုံစံကို ရွေးချယ်ပေးပါနော်-", reply_markup=InlineKeyboardMarkup(keyboard))

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📖 Commands: /start, /status, /users, /reset, /help", parse_mode="Markdown")

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if not user or not user["role_type"]:
        await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။")
        return
    await update.message.reply_text(f"📊 Affection: {user['affection']}/100\nTokens: {user['total_tokens']:,}")

async def total_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = await get_total_users_count()
    await update.message.reply_text(f"👥 Total Users: `{count}`", parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role_type = query.data.split(":")[1]
    await save_user(query.from_user.id, role_type, "standard", 50, [], {}, 0)
    msg = "💖 ဇနီးသည်အဖြစ် စတင်ပါပြီရှင်။" if role_type == "wife" else "🖤 ခင်ပွန်းသည်အဖြစ် စတင်ပါပြီ။"
    await query.edit_message_text(msg)

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if user:
        await save_user(update.effective_user.id, user["role_type"], user["spouse_style"], user["affection"], [], user.get("memory", {}), user.get("total_tokens", 0))
        await update.message.reply_text("🧹 မှတ်တမ်းများ ရှင်းလင်းပြီးပါပြီ။")

# ---------------- GEMINI API CALL ----------------
async def call_gemini(client: httpx.AsyncClient, contents: list) -> tuple[str | None, int]:
    keys = key_rotator.available_keys_in_order()
    if not keys:
        return None, 0

    safety_settings = [{"category": f"HARM_CATEGORY_{c}", "threshold": "BLOCK_NONE"} for c in ["HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT", "DANGEROUS_CONTENT"]]
    payload = {"contents": contents, "safetySettings": safety_settings}

    for current_key in keys:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={current_key}"
        try:
            response = await client.post(url, json=payload, timeout=45)
        except Exception:
            key_rotator.mark_cooldown(current_key, 30)
            continue

        if response.status_code == 200:
            data = response.json()
            candidates = data.get("candidates") or []
            total_tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            if candidates and candidates[0].get("content", {}).get("parts"):
                return candidates[0]["content"]["parts"][0]["text"], total_tokens
            return None, total_tokens
        elif response.status_code == 429:
            key_rotator.mark_exhausted(current_key)
        elif 500 <= response.status_code < 600:
            key_rotator.mark_cooldown(current_key, 15)

    return None, 0

# ---------------- MESSAGES & PHOTOS HANDLERS ----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    async with get_user_lock(user_id):
        user = await get_user(user_id)
        if not user or not user["role_type"]:
            return

        role_type, affection, history, memory, total_tokens = user["role_type"], user["affection"], user["history"], user.get("memory", {}), user["total_tokens"]
        persona = "wife (ဇနီးမယား)" if role_type == "wife" else "husband (ခင်ပွန်းသည်)"
        system_prompt = f"You are a loving {persona}. Affection: {affection}/100. Keep replies short (1-3 sentences)."

        contents = [{"role": "user", "parts": [{"text": system_prompt}]}] + history[-10:] + [{"role": "user", "parts": [{"text": user_text}]}]
        client = context.bot_data["http_client"]
        
        bot_response, req_tokens = await call_gemini(client, contents)
        bot_response = bot_response or ("ကိုကိုရေ... လိုင်းခဏနှေးလို့ပါ 🥺" if role_type == "wife" else "မမရေ... ခဏစောင့်ပါ 🥺")
        
        if req_tokens > 0:
            total_tokens += req_tokens

        await update.message.reply_text(bot_response)
        history.append({"role": "user", "parts": [{"text": user_text}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})
        await save_user(user_id, role_type, user["spouse_style"], affection, history, memory, total_tokens)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_file = update.message.photo[-1]
    caption = update.message.caption or "ဒီပုံလေးကို ကြည့်ပေးပါဦး"
    async with get_user_lock(user_id):
        user = await get_user(user_id)
        if not user or not user["role_type"]:
            return
        
        file_obj = await photo_file.get_file()
        photo_bytes = BytesIO()
        await file_obj.download_to_memory(photo_bytes)
        encoded_img = base64.b64encode(photo_bytes.getvalue()).decode("utf-8")

        role_type, affection, history, memory, total_tokens = user["role_type"], user["affection"], user["history"], user.get("memory", {}), user["total_tokens"]
        persona = "wife" if role_type == "wife" else "husband"

        contents = [{
            "role": "user",
            "parts": [
                {"text": f"You are a loving {persona}. Review this photo naturally in Myanmar."},
                {"inline_data": {"mime_type": "image/jpeg", "data": encoded_img}},
                {"text": f"Caption: {caption}"}
            ]
        }]
        client = context.bot_data["http_client"]
        bot_response, req_tokens = await call_gemini(client, contents)
        bot_response = bot_response or "ပုံကို သေချာမမြင်ရလို့ပါရှင် 🥺"
        
        if req_tokens > 0:
            total_tokens += req_tokens

        await update.message.reply_text(bot_response)
        history.append({"role": "user", "parts": [{"text": f"[Image: {caption}]"}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})
        await save_user(user_id, role_type, user["spouse_style"], affection, history, memory, total_tokens)

# ---------------- LIFECYCLE & MAIN ----------------
async def on_startup(application):
    application.bot_data["http_client"] = httpx.AsyncClient()

async def on_shutdown(application):
    client = application.bot_data.get("http_client")
    if client:
        await client.aclose()

def main():
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEYS:
        logger.error("Tokens or API keys are missing!")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("stats", status_handler))
    app.add_handler(CommandHandler("totalusers", total_users_handler))
    app.add_handler(CommandHandler("users", total_users_handler))
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
