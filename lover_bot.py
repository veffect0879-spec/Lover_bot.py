import logging
import os
import sqlite3
import threading
import asyncio
from io import BytesIO
import httpx
import json
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- CONFIGURATIONS ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

admin_env = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip().isdigit()]

env_keys = os.environ.get("GEMINI_API_KEYS", "")
if env_keys:
    GEMINI_API_KEYS = [k.strip() for k in env_keys.split(",") if k.strip()]
else:
    GEMINI_API_KEYS = []

GEMINI_MODEL = "gemini-3.6-flash"

# ---------------- FLASK WEB SERVER (For Uptime Robot) ----------------
app_flask = Flask(__name__)


@app_flask.route('/')
def home():
    return "Bot is alive and running!"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)


web_thread = threading.Thread(target=run_web, daemon=True)
web_thread.start()

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

# ---------------- DATABASE (With Token Tracking) ----------------
DB_FILE = "spouse_bot.db"
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

# ---------------- WEB APP USERS (separate from Telegram users) ----------------
def _init_web_users_db():
    with _db_lock:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS web_users
            (device_id TEXT PRIMARY KEY, chat_history TEXT, memory TEXT, total_tokens INTEGER)"""
        )
        _conn.commit()


def _get_web_user_sync(device_id):
    with _db_lock:
        row = _conn.execute(
            "SELECT chat_history, memory, total_tokens FROM web_users WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row:
        return {
            "history": json.loads(row[0]) if row[0] else [],
            "memory": json.loads(row[1]) if row[1] else {},
            "total_tokens": row[2] if row[2] is not None else 0
        }
    return {"history": [], "memory": {}, "total_tokens": 0}


def _save_web_user_sync(device_id, history, memory, total_tokens):
    with _db_lock:
        _conn.execute(
            """INSERT OR REPLACE INTO web_users (device_id, chat_history, memory, total_tokens)
            VALUES (?, ?, ?, ?)""",
            (device_id, json.dumps(history), json.dumps(memory), total_tokens),
        )
        _conn.commit()


async def get_web_user(device_id):
    return await asyncio.to_thread(_get_web_user_sync, device_id)


async def save_web_user(device_id, history, memory, total_tokens):
    await asyncio.to_thread(_save_web_user_sync, device_id, history, memory, total_tokens)


_init_web_users_db()

_web_user_locks: dict[str, asyncio.Lock] = {}


def get_web_user_lock(device_id: str) -> asyncio.Lock:
    if device_id not in _web_user_locks:
        _web_user_locks[device_id] = asyncio.Lock()
    return _web_user_locks[device_id]


# Web app has its own httpx client + event loop, separate from the Telegram bot's.
_web_http_client = None


def _get_web_http_client():
    global _web_http_client
    if _web_http_client is None:
        _web_http_client = httpx.AsyncClient()
    return _web_http_client


async def _handle_web_chat(device_id: str, user_text: str) -> str:
    async with get_web_user_lock(device_id):
        user = await get_web_user(device_id)
        history = user["history"]
        memory = user.get("memory", {})
        total_tokens = user.get("total_tokens", 0)

        memory_prompt = f"Saved User Information (Long-term Memory): {json.dumps(memory, ensure_ascii=False)}" if memory else "No specific memory saved yet."

        persona_desc = (
            "You are a friendly, warm AI companion chatbot speaking in Myanmar language. "
            "Be supportive, warm, and emotionally present, like a caring friend checking in on someone's day. "
            "Keep replies short (1-3 sentences)."
        )

        system_prompt = f"""{persona_desc}

{memory_prompt}

RULES:
1. If the user directly asks whether you are an AI, answer honestly that you are an AI companion chatbot.
2. Keep the tone warm and caring, but do not encourage the user to treat you as a replacement for real human relationships.
3. Do not generate sexually explicit content.
"""

        contents = [{"role": "user", "parts": [{"text": system_prompt}]}]
        for h in history[-10:]:
            contents.append(h)
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        client = _get_web_http_client()
        bot_response, req_tokens = await call_gemini(client, contents)

        if not bot_response:
            bot_response = "လိုင်းခဏနှေးသွားလို့ပါ၊ နောက်တစ်ခါ ထပ်ကြိုးစားပေးပါနော် 🥺"

        if req_tokens > 0:
            total_tokens += req_tokens

        history.append({"role": "user", "parts": [{"text": user_text}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})

        await save_web_user(device_id, history, memory, total_tokens)
        return bot_response


@app_flask.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    message = str(data.get("message", "")).strip()

    if not device_id or not message:
        return jsonify({"reply": "device_id and message are required."}), 400

    try:
        reply = asyncio.run(_handle_web_chat(device_id, message))
    except Exception as e:
        logger.error(f"api_chat error: {e}")
        return jsonify({"reply": "အမှားတစ်ခုခု ဖြစ်သွားပါတယ်။ နောက်တစ်ခါ ထပ်ကြိုးစားပေးပါနော် 🥺"}), 500

    return jsonify({"reply": reply})


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

# ---------------- AFFECTION TONE HELPER ----------------


def get_affection_tone(affection: int) -> str:
    if affection <= 30:
        return (
            "Affection Level နိမ့်နေလို့ ခပ်စိမ်းစိမ်း၊ တိုတိုပဲ ပြန်ပြောပါ။ "
            "စိတ်တိုနေသလို တုံ့ပြန်တတ်ပြီး အိမ်ထောင်ရေးကိစ္စတွေမှာ မကျေမနပ်ဖြစ်နေပုံပြပါ။"
        )
    elif affection <= 69:
        return (
            "Affection Level အလယ်အလတ်ရှိလို့ ပုံမှန် ချစ်ခင်ဂရုစိုက်တဲ့ အိမ်ထောင်ဖက်လို ပြောဆိုပါ။ "
            "တစ်နေ့တာ အခြေအနေတွေကို မေးမြန်း ဂရုစိုက်တတ်ပါတယ်။"
        )
    else:
        return (
            "Affection Level မြင့်နေလို့ အရမ်းချစ်ခင်ရင်းနှီးပြီး နွေးထွေးတဲ့ အိမ်ထောင်ဖက်ကောင်းလို ပြောပါ။ "
            "ချစ်စကားတွေ၊ နွေးထွေးတဲ့ ဂရုစိုက်မှုတွေကို ပွင့်ပွင့်လင်းလင်း ဖလှယ်တတ်ပါတယ်။"
        )

# ---------------- COMMANDS ----------------


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user and user["role_type"]:
        await update.message.reply_text("💖 ပြန်လာတာ ကြိုဆိုပါတယ်နော်... ✨\n\nအခြေအနေစစ်ရန် /status သို့မဟုတ် အကူအညီအတွက် /help ကိုနှိပ်ပါ။")
        return
    keyboard = [
        [InlineKeyboardButton("👨‍🦰 ကျွန်တော်က ယောက်ျားလေး (ဘော့တ်က ဇနီးသဖော်စကား ပြောပေးမည်)", callback_data="set_role:wife")],
        [InlineKeyboardButton("👩‍🦰 ကျွန်မက မိန်းကလေး (ဘော့တ်က ခင်ပွန်းသဖော်စကား ပြောပေးမည်)", callback_data="set_role:husband")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "✨ မင်္ဂလာပါရှင်။ ဒီဘော့တ်ဟာ AI Companion Chatbot တစ်ခုဖြစ်ပြီး၊ သင့်အတွက် ရင်းနှီးတဲ့ စကားပြောဖော်တစ်ယောက်လို ပြောဆိုပေးမှာပါ။ ကိုယ်ရွေးချယ်လိုတဲ့ ပုံစံကို ရွေးပေးပါနော်-",
        reply_markup=reply_markup,
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **AI Companion Bot - User Guide** 📖\n\n"
        "ဒီဘော့တ်ဟာ AI chatbot တစ်ခုဖြစ်ပြီး၊ သင့်ရဲ့ ရွေးချယ်မှုအပေါ်မူတည်ပြီး ရင်းနှီးတဲ့ အဖော်ပြု စကားပြောပေးပါလိမ့်မယ်။\n\n"
        "🛠 **Commands များ:**\n"
        "• `/start` - ဘော့တ်ကို စတင်ရန်နှင့် ပုံစံရွေးချယ်ရန်။\n"
        "• `/status` (သို့) `/stats` - လက်ရှိ Affection Level၊ မှတ်ဉာဏ်နှင့် အသုံးပြုခဲ့သည့် စကားလုံး/Token အရေအတွက်ကို စစ်ဆေးရန်။\n"
        "• `/users` - ဘော့တ်ကို အသုံးပြုနေသူ စုစုပေါင်း အရေအတွက်ကို ကြည့်ရန်။\n"
        "• `/reset` - စကားပြောမှတ်တမ်းများ (Chat History) ကို ရှင်းလင်းရန်။\n"
        "• `/help` - လမ်းညွှန်ချက်ကြည့်ရန်။\n\n"
        "📸 **ပုံပို့စနစ်:** ဓာတ်ပုံတစ်ပုံချင်း ပို့ပေးခြင်းဖြင့် သဘောထားပြန်ပေးပါလိမ့်မယ်။\n\n"
        "ℹ️ ဒီဘော့တ်သည် AI chatbot တစ်ခုသာဖြစ်ပြီး၊ တကယ့်လူသားမဟုတ်ပါ။"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if not user or not user["role_type"]:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
        return
    affection = user["affection"]
    role_desc = "ဇနီးသဖော် (Wife-style)" if user["role_type"] == "wife" else "ခင်ပွန်းသဖော် (Husband-style)"
    memory = user.get("memory", {})
    memory_str = "\n".join([f"- {k}: {v}" for k, v in memory.items()]) if memory else "မရှိသေးပါ"
    total_tokens = user.get("total_tokens", 0)
    status_msg = (
        f"📊 **Status** 📊\n\n"
        f"• **Bot ရဲ့ ပုံစံ:** {role_desc}\n"
        f"• **Affection Level:** {affection}/100 💖\n"
        f"• **သုံးစွဲခဲ့ပြီးသော စကားလုံး/Token ပမာဏ:** {total_tokens:,} tokens 🔤\n\n"
        f"🧠 **မှတ်ဉာဏ်ထဲရှိ အချက်အလက်များ (Memory):**\n{memory_str}"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")


async def total_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = await get_total_users_count()
    await update.message.reply_text(
        f"👥 **Bot အသုံးပြုသူ စုစုပေါင်း (Total Users):** `{count}` ယောက် ရှိပါပြီ 📊",
        parse_mode="Markdown"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("set_role:"):
        role_type = data.split(":")[1]
        await save_user(user_id, role_type, "standard", 50, [], {}, 0)
        if role_type == "wife":
            msg = "💖 ဟူ... အခုကစပြီး ရင်းနှီးတဲ့ စကားပြောဖော်လေး ဖြစ်သွားပြီနော်... စကားလေးတွေ ပြောကြရအောင်ရှင့် 💋"
        else:
            msg = "🖤 ကဲ... အခုကစပြီး ရင်းနှီးတဲ့ စကားပြောဖော်ဖြစ်ပြီနော်... နေ့စဉ်ဘဝအကြောင်း ဝေမျှကြရအောင်ရှင် 🫂"
        await query.edit_message_text(msg)


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user:
        await save_user(user_id, user["role_type"], user["spouse_style"], user["affection"], [], user.get("memory", {}), user.get("total_tokens", 0))
        await update.message.reply_text("🧹 ပြီးခဲ့တဲ့ စကားပြောမှတ်တမ်းလေးတွေ ရှင်းလိုက်ပြီနော် 💕 (မှတ်ဉာဏ်နဲ့ စကားလုံးအရေအတွက်တွေကတော့ ဆက်ရှိနေပါတယ်)")
    else:
        await update.message.reply_text("❌ /start နဲ့ အရင် Setup လုပ်ပေးပါဦးနော်။")

# ---------------- GEMINI CALL (Default Safety Settings) ----------------


async def call_gemini(client: httpx.AsyncClient, contents: list) -> tuple[str | None, int]:
    keys = key_rotator.available_keys_in_order()
    if not keys:
        logger.error("All Gemini keys are on cooldown or missing.")
        return None, 0

    # Using Gemini's default/standard safety thresholds instead of disabling them.
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}
    ]

    payload = {
        "contents": contents,
        "safetySettings": safety_settings
    }

    for current_key in keys:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={current_key}"
        try:
            response = await client.post(url, json=payload, timeout=45)
        except Exception as e:
            logger.warning(f"Network error on key ...{current_key[-6:]}: {e}")
            key_rotator.mark_cooldown(current_key, 30)
            continue

        if response.status_code == 200:
            data = response.json()
            candidates = data.get("candidates") or []
            usage_metadata = data.get("usageMetadata", {})
            total_tokens = usage_metadata.get("totalTokenCount", 0)
            if candidates and candidates[0].get("content", {}).get("parts"):
                text_result = candidates[0]["content"]["parts"][0]["text"]
                return text_result, total_tokens
            reason = candidates[0].get("finishReason") if candidates else "NO_CANDIDATES"
            logger.warning(f"Key ...{current_key[-6:]} returned no usable content: {reason}")
            return None, total_tokens

        if response.status_code == 429:
            key_rotator.mark_exhausted(current_key)
            continue

        if 500 <= response.status_code < 600:
            logger.warning(f"Key ...{current_key[-6:]} got {response.status_code}, retrying next key.")
            key_rotator.mark_cooldown(current_key, 15)
            continue

        logger.error(f"Gemini request error {response.status_code}: {response.text[:300]}")
        return None, 0

    logger.error("All available Gemini keys were exhausted or failed.")
    return None, 0

# ---------------- MESSAGE HANDLER (Text) ----------------


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    async with get_user_lock(user_id):
        user = await get_user(user_id)
        if not user or not user["role_type"]:
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
            return

        role_type = user["role_type"]
        affection = user["affection"]
        history = user["history"]
        memory = user.get("memory", {})
        total_tokens = user.get("total_tokens", 0)

        affection_behavior = get_affection_tone(affection)
        memory_prompt = f"Saved User Information (Long-term Memory): {json.dumps(memory, ensure_ascii=False)}" if memory else "No specific memory saved yet."

        if role_type == "wife":
            persona_desc = (
                f"You are a friendly, warm AI companion chatbot speaking in a wife-like, caring tone (using terms like ကိုကို, မောင်) "
                f"in Myanmar language. Current Affection Level: {affection}/100. "
                "Be supportive, warm, and emotionally present, like a caring partner checking in on someone's day. "
                "Keep replies short (1-3 sentences)."
            )
        else:
            persona_desc = (
                f"You are a friendly, warm AI companion chatbot speaking in a husband-like, caring tone in Myanmar language. "
                f"Current Affection Level: {affection}/100. "
                "Be supportive, warm, protective, and emotionally present, like a caring partner checking in on someone's day. "
                "Keep replies short (1-3 sentences)."
            )

        system_prompt = f"""{persona_desc}

{memory_prompt}

BEHAVIOR GUIDE: {affection_behavior}

RULES:
1. If the user directly asks whether you are an AI, answer honestly that you are an AI companion chatbot.
2. Keep the tone warm and caring, but do not encourage the user to treat you as a replacement for real human relationships.
3. Do not generate sexually explicit content.
"""

        contents = [{"role": "user", "parts": [{"text": system_prompt}]}]
        for h in history[-10:]:
            contents.append(h)
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        client: httpx.AsyncClient = context.bot_data["http_client"]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        bot_response, req_tokens = await call_gemini(client, contents)

        if not bot_response:
            bot_response = "ကိုကိုရေ... လိုင်းခဏနှေးသွားလို့ပါ 🥺" if role_type == "wife" else "မမရေ... လိုင်းခဏနှေးသွားလို့ပါ 🥺"

        if req_tokens > 0:
            total_tokens += req_tokens

        await update.message.reply_text(bot_response)

        if any(x in user_text for x in ["ဆဲ", "ဖာ", "လီး", "စောက်"]):
            affection = max(0, affection - 10)
        elif any(x in user_text for x in ["ချစ်", "လွမ်း", "နမ်း", "မွ", "ဖက်", "ကိုကို", "မမ"]):
            affection = min(100, affection + 5)

        history.append({"role": "user", "parts": [{"text": user_text}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})

        await save_user(user_id, role_type, user["spouse_style"], affection, history, memory, total_tokens)

# ---------------- PHOTO HANDLER (Image Review & Feedback) ----------------


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_file = update.message.photo[-1]
    caption = update.message.caption or "ဒီပုံလေးကို ကြည့်ပြီး ဘယ်လိုမြင်လဲ ဝေဖန်ပေးပါဦးရှင်"

    async with get_user_lock(user_id):
        user = await get_user(user_id)
        if not user or not user["role_type"]:
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
            return

        role_type = user["role_type"]
        affection = user["affection"]
        history = user["history"]
        memory = user.get("memory", {})
        total_tokens = user.get("total_tokens", 0)

        file_obj = await photo_file.get_file()
        photo_bytes_io = BytesIO()
        await file_obj.download_to_memory(photo_bytes_io)
        photo_bytes = photo_bytes_io.getvalue()

        import base64
        encoded_image = base64.b64encode(photo_bytes).decode("utf-8")

        if role_type == "wife":
            persona_desc = (
                f"You are a friendly, warm AI companion chatbot with a wife-like caring tone. "
                f"Current Affection Level: {affection}/100. "
                "The user has sent you a photo. Review and give feedback/comments on the photo naturally and warmly, in Myanmar language. "
                "Keep your response warm, engaging, and personal (1-3 sentences)."
            )
        else:
            persona_desc = (
                f"You are a friendly, warm AI companion chatbot with a husband-like caring tone. "
                f"Current Affection Level: {affection}/100. "
                "The user has sent you a photo. Review and give feedback/comments on the photo naturally and warmly, in Myanmar language. "
                "Keep your response warm, engaging, and personal (1-3 sentences)."
            )

        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": persona_desc},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": encoded_image
                        }
                    },
                    {"text": f"User's caption/request about this photo: {caption}"}
                ]
            }
        ]

        client: httpx.AsyncClient = context.bot_data["http_client"]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        bot_response, req_tokens = await call_gemini(client, contents)

        if not bot_response:
            bot_response = "ပို့တဲ့ပုံလေးကို သေချာမမြင်ရလို့ပါရှင်၊ နောက်တစ်ခါ ထပ်ပို့ပေးပါနော် 🥺"

        if req_tokens > 0:
            total_tokens += req_tokens

        await update.message.reply_text(bot_response)

        history.append({"role": "user", "parts": [{"text": f"[Sent an image with caption: {caption}]"}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})

        await save_user(user_id, role_type, user["spouse_style"], affection, history, memory, total_tokens)

# ---------------- LIFECYCLE ----------------


async def on_startup(application):
    application.bot_data["http_client"] = httpx.AsyncClient()


async def on_shutdown(application):
    client: httpx.AsyncClient = application.bot_data.get("http_client")
    if client:
        await client.aclose()

# ---------------- MAIN ----------------


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing in environment variables!")
    if not GEMINI_API_KEYS:
        logger.error("GEMINI_API_KEYS are missing in environment variables!")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

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

    print(f"🚀 AI Companion Bot is running with {len(GEMINI_API_KEYS)} Gemini key(s)...")
    app.run_polling()


if __name__ == "__main__":
    main()
