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

# Render ရဲ့ Environment Variable (GEMINI_API_KEYS) မှသာ Key များကို ဖတ်ယူမည်
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

# Background မှာ Flask Server ကို စတင် Run ခြင်း
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

# ---------------- DATABASE ----------------
DB_FILE = "lover_bot.db"
_db_lock = threading.Lock()
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

def init_db():
    with _db_lock:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS users
                     (user_id INTEGER PRIMARY KEY, gender TEXT, age TEXT, affection INTEGER, chat_history TEXT)"""
        )
        _conn.commit()

def _get_user_sync(user_id):
    with _db_lock:
        row = _conn.execute(
            "SELECT gender, age, affection, chat_history FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {"gender": row[0], "age": row[1], "affection": row[2], "history": json.loads(row[3])}
    return None

def _save_user_sync(user_id, gender, age, affection, history):
    with _db_lock:
        _conn.execute(
            """INSERT OR REPLACE INTO users (user_id, gender, age, affection, chat_history)
                     VALUES (?, ?, ?, ?, ?)""",
            (user_id, gender, age, affection, json.dumps(history)),
        )
        _conn.commit()

async def get_user(user_id):
    return await asyncio.to_thread(_get_user_sync, user_id)

async def save_user(user_id, gender, age, affection, history):
    await asyncio.to_thread(_save_user_sync, user_id, gender, age, affection, history)

init_db()

_user_locks: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# ---------------- COMMANDS ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if user and user["gender"] and user["age"]:
        await update.message.reply_text("💖 မောင်လေး/ညီမလေး ပြန်လာပြီလား... စကားပြောရအောင်လေရှင့် ✨")
        return

    keyboard = [
        [InlineKeyboardButton("🙋‍♀️ ချစ်သူကောင်မလေး (Girlfriend)", callback_data="set_gender:female")],
        [InlineKeyboardButton("🙋‍♂️ ချစ်သူကောင်လေး (Boyfriend)", callback_data="set_gender:male")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("✨ မင်္ဂလာပါရှင့်။ AI Lover ရဲ့ လိင်အမျိုးအစားကို ရွေးချယ်ပေးပါနော်-", reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("set_gender:"):
        gender = data.split(":")[1]
        context.user_data["temp_gender"] = gender

        keyboard = [
            [InlineKeyboardButton("🐥 ၁၈ - ၂၂ နှစ် (နုပျို/ချွဲတတ်သူ)", callback_data="set_age:young")],
            [InlineKeyboardButton("👑 ၂၆ - ၃၀ နှစ် (ရင့်ကျက်/ဂရုစိုက်တတ်သူ)", callback_data="set_age:mature")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("✨ ချစ်သူရဲ့ အသက်အရွယ် (စရိုက်) ကို ရွေးချယ်ပေးပါ-", reply_markup=reply_markup)

    elif data.startswith("set_age:"):
        age = data.split(":")[1]
        gender = context.user_data.get("temp_gender", "female")

        await save_user(user_id, gender, age, 50, [])

        gender_text = "ကောင်မလေး" if gender == "female" else "ကောင်လေး"
        age_text = "နုပျိုချွဲနွဲ့တဲ့" if age == "young" else "ရင့်ကျက်တည်ငြိမ်တဲ့"

        await query.edit_message_text(
            f"💖 ဟူ... အခုကစပြီး ကိုယ်က မောင်လေး/ညီမလေးရဲ့ {age_text} {gender_text} ဖြစ်သွားပြီနော်... "
            f"စကားစပြောလို့ရပါပြီရှင့် 💋"
        )

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user:
        await save_user(user_id, user["gender"], user["age"], user["affection"], [])
        await update.message.reply_text("🧹 ပြီးခဲ့တဲ့ စကားပြောမှတ်တမ်းလေးတွေ ရှင်းလိုက်ပြီနော် 💕")
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

    payload = {"contents": messages}

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

        if not user or not user["gender"] or not user["age"]:
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ /start ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးနော်။")
            return

        affection = user["affection"]
        history = user["history"]

        identity = "ချစ်သူကောင်မလေး (Girlfriend)" if user["gender"] == "female" else "ချစ်သူကောင်လေး (Boyfriend)"
        style = (
            "အသက် ၂၀ ဝန်းကျင်ဖြစ်ပြီး အရမ်းချွဲတတ်တယ်၊ စာတိုတိုနဲ့ ရင်းနှီးစွာပြောတယ်၊ Emoji သုံးတယ်"
            if user["age"] == "young"
            else "အသက် ၂၈ ဝန်းကျင်ဖြစ်ပြီး ရင့်ကျက်တယ်၊ တိုတိုနဲ့ အဓိပ္ပါယ်ပါပါ ဂရုစိုက်ပြောတတ်တယ်"
        )

        system_prompt = f"""You are NOT an AI. You are a real human and the user's loving romantic partner ({identity}) in a real relationship.
Your personality style: {style}. Current Affection Level: {affection}/100.
RULES:
1. NEVER say you are an AI, a language model, or a bot. Always act and talk like a real human partner.
2. Keep replies SHORT and conversational (1 to 3 short sentences max), like real texting.
3. Speak in sweet, natural Myanmar language using cute words (မောင်, ကိုကို, ညီမလေး, အသည်း).
4. Understand and respond naturally to internet slang and abbreviations used by the user:
   - "Bb" = Bye Bye
   - "gn" / "Gn" = Good Night
   - "Mornee" = Good Morning
   - "stt" = playful teasing/complaint — respond warmly, don't take it seriously.
5. If the user asks to see a picture or what you're doing, reply with a short sweet sentence AND include this exact tag at the very end: '[IMAGE_REQUEST: a beautiful realistic selfie of a young cute {identity}, soft lighting, high quality]'
"""

        messages = [{"parts": [{"text": system_prompt}], "role": "user"}]
        for h in history[-10:]:
            messages.append(h)
        messages.append({"role": "user", "parts": [{"text": user_text}]})

        client: httpx.AsyncClient = context.bot_data["http_client"]
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        bot_response = await call_gemini(client, messages)
        if not bot_response:
            bot_response = "အသည်းရေ... လိုင်းခဏနှေးသွားလို့ သို့မဟုတ် အကောင့်ခဏနားနေလို့ပါ၊ စာလေး ထပ်ပို့ပါဦးနော် 🥺"

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
                    await update.message.reply_photo(
                        photo=BytesIO(image_bytes), caption="မောင်လေးအတွက် ကိုယ့်ရဲ့ပုံလေးလေ 💖✨"
                    )
                except Exception as e:
                    logger.error(f"Image Send Error: {e}")
                    await update.message.reply_text("📸 ပုံပို့တာ မအောင်မြင်ဘူး အသည်း... နောက်တစ်ခါ ထပ်တောင်းနော်။")
            else:
                await update.message.reply_text("📸 ဓာတ်ပုံလိုင်း ခဏနှေးနေလို့ နောက်တစ်ခါ ထပ်တောင်းနော် အသည်း။")
        else:
            await update.message.reply_text(bot_response)

        if any(x in user_text for x in ["ဆဲ", "ဖာ", "လီး", "စောက်"]):
            affection = max(0, affection - 10)
        elif any(x in user_text for x in ["ချစ်", "လွမ်း", "နမ်း", "မွ"]):
            affection = min(100, affection + 5)

        history.append({"role": "user", "parts": [{"text": user_text}]})
        history.append({"role": "model", "parts": [{"text": bot_response}]})
        await save_user(user_id, user["gender"], user["age"], affection, history)

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
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print(f"🚀 AI Lover Bot is running with {len(GEMINI_API_KEYS)} Gemini key(s)...")
    app.run_polling()

if __name__ == "__main__":
    main()
        
