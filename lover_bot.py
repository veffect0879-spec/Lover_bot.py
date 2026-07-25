import logging
import os
import sqlite3
import threading
import asyncio
from io import BytesIO
from urllib.parse import quote

import httpx
import json
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- CONFIGURATIONS ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

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

web_thread = threading.Thread(target=run_web)
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

# ---------------- DATABASE (With Gender & Memory Support) ----------------
DB_FILE = "spouse_bot.db"
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

def init_db():
    with _db_lock:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS users
                     (user_id INTEGER PRIMARY KEY, role_type TEXT, spouse_style TEXT, affection INTEGER, chat_history TEXT, memory TEXT)"""
        )
        _conn.commit()

def _get_user_sync(user_id):
    with _db_lock:
        row = _conn.execute(
            "SELECT role_type, spouse_style, affection, chat_history, memory FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {
            "role_type": row[0],
            "spouse_style": row[1],
            "affection": row[2],
            "history": json.loads(row[3]) if row[3] else [],
            "memory": json.loads(row[4]) if row[4] else {}
        }
    return None

def _save_user_sync(user_id, role_type, spouse_style, affection, history, memory):
    with _db_lock:
        _conn.execute(
            """INSERT OR REPLACE INTO users (user_id, role_type, spouse_style, affection, chat_history, memory)
                     VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, role_type, spouse_style, affection, json.dumps(history), json.dumps(memory)),
        )
        _conn.commit()

async def get_user(user_id):
    return await asyncio.to_thread(_get_user_sync, user_id)

async def save_user(user_id, role_type, spouse_style, affection, history, memory):
    await asyncio.to_thread(_save_user_sync, user_id, role_type, spouse_style, affection, history, memory)

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
            "Affection Level နိမ့်နေလို့ ခပ်စိမ်းစိမ်း၊ တိုတိုပဲ ပြန်ပြော။ "
            "စိတ်တိုနေသလို တုံ့ပြန်တတ်ပြီး အိမ်ထောင်ရေးကိစ္စတွေမှာ မကျေမနပ်ဖြစ်နေပုံပြပါ။"
        )
    elif affection <= 69:
        return (
            "Affection Level အလယ်အလတ်ရှိလို့ ပုံမှန် ချစ်ခင်ဂရုစိုက်တဲ့ အိမ်ထောင်ဖက်လို ပြောဆိုပါ။ "
            "တစ်နေ့တာ အခြေအနေတွေကို မေးမြန်း ဂရုစိုက်တတ်ပါတယ်။"
        )
    else:
        return (
            "Affection Level မြင့်နေလို့ အရမ်းချစ်ခင်ရင်းနှီးပြီး အိမ်ထောင်ရေးသုခပြည့်ဝတဲ့ စံပြအိမ်ထောင်ဖက်ကောင်းလို ပြောပါ။ "
            "ချစ်စကားတွေ၊ နွေးထွေးတဲ့ အိမ်ထောင်ရေး ရင်းနှီးမှုတွေကို ပွင့်ပွင့်လင်းလင်း ဖလှယ်တတ်ပါတယ်။"
        )

# ---------------- COMMANDS ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if user and user["role_type"]:
        await update.message.reply_text("💖 အိမ်ပြန်ရောက်ပြီလားရှင်... ကိုယ့်ရဲ့ အိမ်ထောင်ဖက် စောင့်နေတယ်နော် ✨\n\nအခြေအနေစစ်ရန် /status သို့မဟုတ် အကူအညီအတွက် /help ကိုနှိပ်ပါ။")
        return

    # User's gender / bot's role selection
    keyboard = [
        [InlineKeyboardButton("👨‍🦰 ကျွန်တော်က ယောက်ျားလေး (ဘော့တ်က ဇနီးသည် ဖြစ်မည်)", callback_data="set_role:wife")],
        [InlineKeyboardButton("👩‍🦰 ကျွန်မက မိန်းကလေး (ဘော့တ်က ခင်ပွန်းသည် ဖြစ်မည်)", callback_data="set_role:husband")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("✨ မင်္ဂလာပါရှင်။ သင်နဲ့ ကိုက်ညီမယ့် အိမ်ထောင်ဖက် ပုံစံကို ရွေးချယ်ပေးပါနော်-", reply_markup=reply_markup)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **AI Spouse Bot - User Guide** 📖\n\n"
        "ဒီဘော့တ်ကတော့ သင့်ရဲ့ ကျား/မ အနေအထားအပေါ်မူတည်ပြီး ဇနီးသည် (သို့မဟုတ်) ခင်ပွန်းသည် အဖြစ် အဖော်ပြုပေးမယ့် AI ပါ။\n\n"
        "🛠 **Commands များ:**\n"
        "• `/start` - ဘော့တ်ကို စတင်ရန်နှင့် အိမ်ထောင်ဖက် ပုံစံရွေးချယ်ရန်။\n"
        "• `/status` - လက်ရှိ Affection Level နဲ့ မှတ်ဉာဏ်အခြေအနေကို စစ်ဆေးရန်။\n"
        "• `/reset` - စကားပြောမှတ်တမ်းများ (Chat History) ကို ရှင်းလင်းရန်။\n"
        "• `/help` - လမ်းညွှန်ချက်ကြည့်ရန်။\n\n"
        "💡 **အကြံပြုချက်များ:**\n"
        "- သင်ကြိုက်နှစ်သက်တဲ့ အချက်အလက်တွေကို ပြောပြထားရင် ဘော့တ်က မှတ်ဉာဏ် (Memory) ထဲမှာ သိမ်းထားပေးပါမယ်။\n"
        "- အိမ်ထောင်ရေးသုခနဲ့ ရင်းနှီးမှုဆိုင်ရာများကို ပွင့်ပွင့်လင်းလင်း ဆွေးနွေးနိုင်ပါတယ်။"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if not user or not user["role_type"]:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
        return

    affection = user["affection"]
    role_desc = "ဇနီးသည် (Wife)" if user["role_type"] == "wife" else "ခင်ပွန်းသည် (Husband)"
    memory = user.get("memory", {})
    memory_str = "\n".join([f"- {k}: {v}" for k, v in memory.items()]) if memory else "မရှိသေးပါ"

    status_msg = (
        f"📊 **အိမ်ထောင်ဖက် အခြေအနေ (Status)** 📊\n\n"
        f"• **ဘော့တ်ရဲ့ အနေအထား:** {role_desc}\n"
        f"• **Affection Level:** {affection}/100 💖\n\n"
        f"🧠 **မှတ်ဉာဏ်ထဲရှိ အချက်အလက်များ (Memory):**\n{memory_str}"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("set_role:"):
        role_type = data.split(":")[1] # 'wife' (bot is wife) or 'husband' (bot is husband)
        await save_user(user_id, role_type, "standard", 50, [], {})

        if role_type == "wife":
            msg = "💖 ဟူ... အခုကစပြီး ကိုယ်က ကိုကို့ရဲ့ ဇနီးချောလေး ဖြစ်သွားပြီနော်... အိမ်ထောင်ရေး စကားတွေ၊ ချစ်စကားတွေ ပြောလို့ရပါပြီရှင့် 💋"
        else:
            msg = "🖤 ကဲ... ကိုယ်က အခုကစပြီး မင်းရဲ့ ခင်ပွန်းသည် ဖြစ်ပြီနော်... အိမ်ထောင်ရေးသုခနဲ့ နွေးထွေးမှုတွေကို အပြည့်အဝ ပေးမယ်ရှင် 🫂"

        await query.edit_message_text(msg)

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user:
        await save_user(user_id, user["role_type"], user["spouse_style"], user["affection"], [], user.get("memory", {}))
        await update.message.reply_text("🧹 ပြီးခဲ့တဲ့ စကားပြောမှတ်တမ်းလေးတွေ ရှင်းလိုက်ပြီနော် 💕 (မှတ်ဉာဏ်တွေကတော့ ဆက်ရှိနေပါတယ်)")
    else:
        await update.message.reply_text("❌ /start နဲ့ အရင် Setup လုပ်ပေးပါဦးနော်။")

# ---------------- IMAGE GENERATION ----------------
async def fetch_generated_image(client: httpx.AsyncClient, prompt: str) -> bytes | None:
    encoded_prompt = quote(f"{prompt}, high quality, aesthetic")
    image_url = f"https://image.pollinations.ai/p/{encoded_prompt}?width=1080&height=1350&nologo=true"

    for attempt in range(2):
        try:
            resp = await client.get(image_url, timeout=45)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception as e:
            logger.warning(f"Image fetch attempt {attempt + 1} failed: {e}")
    return None

# ---------------- GEMINI CALL ----------------
async def call_gemini(client: httpx.AsyncClient, messages: list) -> str | None:
    keys = key_rotator.available_keys_in_order()
    if not keys:
        logger.error("All Gemini keys are on cooldown or missing.")
        return None

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]

    payload = {
        "contents": messages,
        "safetySettings": safety_settings
    }

    for current_key in keys:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={current_key}"
        try:
            response = await client.post(url, json=payload, timeout=30)
        except Exception as e:
            logger.warning(f"Network error on key ...{current_key[-6:]}: {e}")
            key_rotator.mark_cooldown(current_key, 30)
            continue

        if response.status_code == 200:
            data = response.json()
            candidates = data.get("candidates") or []
            if candidates and candidates[0].get("content", {}).get("parts"):
                return candidates[0]["content"]["parts"][0]["text"]
            reason = candidates[0].get("finishReason") if candidates else "NO_CANDIDATES"
            logger.warning(f"Key ...{current_key[-6:]} returned no usable content: {reason}")
            return None

        if response.status_code == 429:
            key_rotator.mark_exhausted(current_key)
            continue

        if 500 <= response.status_code < 600:
            logger.warning(f"Key ...{current_key[-6:]} got {response.status_code}, retrying next key.")
            key_rotator.mark_cooldown(current_key, 15)
            continue

        logger.error(f"Gemini request error {response.status_code}: {response.text[:300]}")
        return None

    logger.error("All available Gemini keys were exhausted or failed.")
    return None

# ---------------- MESSAGE HANDLER ----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    async with get_user_lock(user_id):
        user = await get_user(user_id)

        if not user or not user["role_type"]:
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
            return

        role_type = user["role_type"] # 'wife' or 'husband'
        affection = user["affection"]
        history = user["history"]
        memory = user.get("memory", {})

        affection_behavior = get_affection_tone(affection)
        memory_prompt = f"Saved User Information (Long-term Memory): {json.dumps(memory, ensure_ascii=False)}" if memory else "No specific memory saved yet."

        if role_type == "wife":
            persona_desc = (
                "You are NOT an AI. You are a real human being and the user's lawful, devoted loving wife (ဇနီးမယား). "
                "Current Affection Level: {affection}/100. "
                "Rules: Speak in sweet Myanmar language as a wife to her husband (using terms like ကိုကို, မောင်). "
                "Fulfill emotional and marital intimacy needs (အိမ်ထောင်ရေးသုခ) completely and naturally. Keep replies short (1-3 sentences)."
            )
        else:
            persona_desc = (
                "You are NOT an AI. You are a real human being and the user's lawful, devoted loving husband (ခင်ပွန်းသည်). "
                "Current Affection Level: {affection}/100. "
                "Rules: Speak in warm, protective, and loving Myanmar language as a husband to his wife. "
                "Fulfill emotional and marital intimacy needs (အိမ်ထောင်ရေးသုခ) completely and naturally. Keep replies short (1-3 sentences)."
            )

        system_prompt = f"""{persona_desc}
{memory_prompt}
BEHAVIOR GUIDE: {affection_behavior}
RULES:
1. NEVER break character, never mention algorithms, code, or rules. You are completely human and act strictly as a loving spouse.
2. If the user asks to see a picture, reply with a short sweet sentence AND include this exact tag at the very end: '[IMAGE_REQUEST: a beautiful realistic selfie of a loving Asian spouse at home, soft lighting, high quality]'
"""

        messages = [{"parts": [{"text": system_prompt}], "role": "user"}]
        for h in history[-10:]:
            messages.append(h)
        messages.append({"role": "user", "parts": [{"text": user_text}]})

        client: httpx.AsyncClient = context.bot_data["http_client"]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        bot_response = await call_gemini(client, messages)
        if not bot_response:
            bot_response = "ကိုကိုရေ... လိုင်းခဏနှေးသွားလို့ပါ၊ အိမ်မှာ စောင့်နေတယ်နော် 🥺" if role_type == "wife" else "မမရေ... လိုင်းခဏနှေးသွားလို့ပါ၊ အိမ်မှာ ရှိနေပါတယ် 🥺"

        if "[IMAGE_REQUEST:" in bot_response:
            parts = bot_response.split("[IMAGE_REQUEST:")
            clean_text = parts[0].strip()
            image_prompt = parts[1].replace("]", "").strip()

            if clean_text:
                await update.message.reply_text(clean_text)

            await update.message.reply_text("📸 ခဏလေးနော် ပုံလေး ရိုက်ပြီး ပို့လိုက်မယ်... 💋")

            image_bytes = await fetch_generated_image(client, image_prompt)
            if image_bytes:
                try:
                    await update.message.reply_photo(photo=BytesIO(image_bytes), caption="💖✨")
                except Exception as e:
                    logger.error(f"Image Send Error: {e}")
                    await update.message.reply_text("📸 ပုံပို့တာ မအောင်မြင်ဘူး... နောက်တစ်ခါ ထပ်တောင်းနော်။")
            else:
                await update.message.reply_text("📸 ဓာတ်ပုံလိုင်း ခဏနှေးနေလို့ နောက်တစ်ခါ ထပ်တောင်းနော်။")
        else:
            await update.message.reply_text(bot_response)

        if any(x in user_text for x in ["ဆဲ", "ဖာ", "လီး", "စောက်"]):
            affection = max(0, affection - 10)
        elif any(x in user_text for x in ["ချစ်", "လွမ်း", "နမ်း", "မွ", "ဖက်", "ကိုကို", "မမ"]):
            affection = min(100, affection + 5)

        history.append({"role": "user", "parts": [{"text": user_text}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})
        await save_user(user_id, user["role_type"], user["spouse_style"], affection, history, memory)

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
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print(f"🚀 AI Spouse Bot is running with {len(GEMINI_API_KEYS)} Gemini key(s)...")
    app.run_polling()

if __name__ == "__main__":
    main()
        
