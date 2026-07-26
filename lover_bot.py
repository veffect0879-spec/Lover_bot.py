import logging
import os
import sqlite3
import threading
import asyncio
from io import BytesIO

import httpx
import json
from flask import Flask, render_template
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
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

# ----------------- CONFIGURATIONS -----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

admin_env = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip().isdigit()]

env_keys = os.environ.get("GEMINI_API_KEYS", "")
if env_keys:
    GEMINI_API_KEYS = [k.strip() for k in env_keys.split(",") if k.strip()]
else:
    GEMINI_API_KEYS = []

GEMINI_MODEL = "gemini-2.5-flash"

# ----------------- FLASK WEB SERVER (For Telegram Mini App & Uptime Robot) -----------------
app_flask = Flask(__name__)

@app_flask.route('/app')
def web_app():
    return render_template('index.html')

@app_flask.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

web_thread = threading.Thread(target=run_web, daemon=True)
web_thread.start()

# ----------------- GEMINI KEY ROTATION -----------------
class KeyRotator:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.current_index = 0
        self.cooldown_until: dict[int, float] = {}
        self.lock = threading.Lock()

    def get_key(self) -> str:
        with self.lock:
            if not self.keys:
                return ""
            now = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0
            for _ in range(len(self.keys)):
                key = self.keys[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.keys)
                if now >= self.cooldown_until.get(id(key), 0):
                    return key
            return self.keys[0]

    def report_exhausted(self, key: str, cooldown: float = 60.0):
        with self.lock:
            now = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0
            self.cooldown_until[id(key)] = now + cooldown

key_rotator = KeyRotator(GEMINI_API_KEYS)

# ----------------- DATABASE SETUP -----------------
DB_NAME = "spouse_bot.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role_type TEXT,
            spouse_style TEXT,
            affection INTEGER DEFAULT 50,
            history TEXT,
            tokens_used INTEGER DEFAULT 0,
            memory TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

async def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, role_type, spouse_style, affection, history, tokens_used, memory FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "role_type": row[1],
            "spouse_style": row[2],
            "affection": row[3],
            "history": json.loads(row[4]) if row[4] else [],
            "tokens_used": row[5],
            "memory": row[6] or ""
        }
    return None

async def save_user(user_id: int, role_type: str, spouse_style: str, affection: int, history: list, tokens_used: int, memory: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, role_type, spouse_style, affection, history, tokens_used, memory)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            role_type = excluded.role_type,
            spouse_style = excluded.spouse_style,
            affection = excluded.affection,
            history = excluded.history,
            tokens_used = excluded.tokens_used,
            memory = excluded.memory
    ''', (user_id, role_type, spouse_style, affection, json.dumps(history), tokens_used, memory))
    conn.commit()
    conn.close()

# ----------------- KEYBOARDS & HELPERS -----------------
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Status စစ်ရန်"), KeyboardButton("❓ Help")],
        [KeyboardButton("🧹 Chat ရှင်းရန်")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_setup_keyboard():
    keyboard = [
        [InlineKeyboardButton("🙋‍♂️ ကျွနု်တ်က ယောက်ျားလေး (ဇနီးသည် ရှာမည်)", callback_data="set_role:wife")],
        [InlineKeyboardButton("🙋‍♀️ ကျွနု်တ်က မိန်းကလေး (ခင်ပွန်းသည် ရှာမည်)", callback_data="set_role:husband")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_affection_tone(affection: int) -> str:
    if affection <= 30:
        return (
            "Affection Level နိမ့်နေလို့ ခပ်စိမ်းစိမ်း၊ တိုတိုပဲ ပြန်ပြောပါ။ "
            "စိတ်တိုနေသလို တုံ့ပြန်တတ်ပြီး အိမ်ထောင်ရေးကို စူပူမှန်မပျက်ဖြစ်နေပြပါစေ။"
        )
    elif affection <= 69:
        return (
            "Affection Level အလယ်အလတ်ရှိလို့ ပုံမှန် ချစ်ခင်ဂရုစိုက်တဲ့ အိမ်ထောင်ဖက်လို ပြောဆိုပါ။ "
            "တစ်နေ့တာ အခြေအနေတွေကို မေးမြန်း၊ ဂရုစိုက်တတ်ပါတယ်။"
        )
    else:
        return (
            "Affection Level မြင့်နေလို့ အချစ်ဆုံးခင်ပွန်း/ဇနီး၊ အိမ်ထောင်ရေးသုခပြည့်ဝတဲ့ စံပြအိမ်ထောင်ဖက်ကောင်းလို ပြုမူပါ။ "
            "ချစ်စကားတွေ၊ နွေးထွေးတဲ့ အိမ်ထောင်ရေး ရရင်းနှီးမှုတွေကို ပွင့်ပွင့်လင်းလင်း ဖော်ပြတတ်ပါတယ်။"
    )
        # ----------------- COMMANDS -----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    web_app_url = "https://lover-bot-py.onrender.com/app"
    
    keyboard = [
        [InlineKeyboardButton("💖 အိမ်ထောင်ဖက် App ဖွင့်ရန်", web_app=WebAppInfo(url=web_app_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "✨ မင်္ဂလာပါရှင်... အောက်ပါခလုတ်ကိုနှိပ်ပြီး Mini App မျက်နှာပြင်သို့ တိုက်ရိုက်ဝင်ရောက်နိုင်ပါပြီ 💕",
        reply_markup=reply_markup
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **AI Spouse Bot - User Guide** 📝\n\n"
        "• ဒီဘော့တ်ကတော့ သင့်ရဲ့ ကျား/မ အနေအထားအပေါ်မူတည်ပြီး ဇနီးသည် (သို့မဟုတ်) ခခင်ပွန်းသည် အဖြစ် အဖော်ပြုပေးမယ့် AI ပါရှင်။\n"
        "🛠 **အသုံးပြုနိုင်သော ခလုတ်များ အစုံများ:**\n"
        "• 📊 **Status စစ်ရန်** - လိုအပ်သည့် Affection Level၊ မှတ်ဉာဏ်နှင့် အသုံးပြုသည့် Token အရေအတွက်ကို စစ်ဆေးရန်။\n"
        "• ❓ **Help** - လမ်းညွှန်ချက်ကြည့်ရန်။\n"
        "• 🧹 **Chat ရှင်းရန်** - စကားပြောမှတ်တမ်းများ (Chat History) ကို ရှင်းလင်းရန်။\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if not user or not user["role_type"]:
        await update.message.reply_text(
            "❌ ကျေးဇူးပြု၍ အောက်ပါလင့်ခ်ကိုနှိပ်ပြီး အရင် Setup လုပ်ပေးပါဦးရှင်။",
            reply_markup=get_main_keyboard()
        )
        return

    role_text = "ဇနီးသည် (Wife)" if user["role_type"] == "wife" else "ခင်ပွန်းသည် (Husband)"
    status_msg = (
        f"📊 **အိမ်ထောင်ဖက် အခြေအနေ (Status)** 📊\n\n"
        f"• ဘော့တ်ရဲ့ အနေအထား: <b>{role_text}</b>\n"
        f"• Affection Level: <b>{user['affection']}/100</b> 💕\n"
        f"• သုံးစွဲခဲ့သော စကားလုံး/Token ပမာဏ: <b>{user['tokens_used']} tokens</b> 🔤\n\n"
        f"🧠 <b>မှတ်ဉာဏ်ထဲရှိ အချက်အလက်များ (Memory):</b>\n"
        f"{user['memory'] if user['memory'] else 'မရှိသေးပါ'}"
    )
    await update.message.reply_text(status_msg, parse_mode="HTML", reply_markup=get_main_keyboard())

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user:
        await save_user(user_id, None, None, 50, [], user["tokens_used"], "")
    await update.message.reply_text(
        "🔄 မှတ်တမ်းများကို အသစ်ပြန်လည် စတင်လိုက်ပါပြီ။ ကျေးဇူးပြု၍ Role အသစ်ပြန်လည် ရွေးချယ်ပေးပါရှင်။",
        reply_markup=get_setup_keyboard()
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data.startswith("set_role:"):
        role = data.split(":")[1]
        spouse_style = "ချစ်စရာကောင်းပြီး ဂရုစိုက်တတ်သော ဇနီးလေး" if role == "wife" else "လိမ္မာပြီး တာဝန်ယူတတ်သော ခင်ပွန်းသည်"
        await save_user(user_id, role, spouse_style, 50, [], 0, "")
        
        welcome_text = (
            f"💖 ဟာ... အခုကစပြီး ကိုယ်က ကိုကို့ရဲ့ ဇနီးချောလေး ဖြစ်သွားပြီနော်... "
            f"အိမ်ထောင်ရေး စကားတွေ၊ ချစ်စကားတွေ ပြောလို့ရပါပြီရှင် 💋"
            if role == "wife" else
            f"💖 ဟာ... အခုကစပြီး ကိုယ်က မမရဲ့ ချစ်ခင်ပွန်းသည် ဖြစ်သွားပြီနော်... "
            f"ဘာတွေကူညီပေးရမလဲ မမရေ 🥰"
        )
        await query.edit_message_text(welcome_text)
        await query.message.reply_text("✨ မင်္ဂလာပါရှင်... အောက်ပါ ခလုတ်များကို பயன்படுத்தி စကားပြောလို့ရပါပြီရှင် 👇", reply_markup=get_main_keyboard())

# ----------------- GEMINI API CALL -----------------
async def call_gemini(client: httpx.AsyncClient, contents: list) -> tuple[str, int]:
    for _ in range(len(GEMINI_API_KEYS) if GEMINI_API_KEYS else 1):
        key = key_rotator.get_key()
        if not key:
            return "⚠️ Gemini API Key မရှိသေးပါ သို့မဟုတ် ထည့်ရန်လိုနေပါသည်ရှင်။", 0
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": contents}

        try:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            if response.status_code == 200:
                data = response.json()
                try:
                    candidate = data["candidates"][0]
                    part = candidate["content"]["parts"][0]
                    text = part.get("text", "မပြောတတ်တော့ဘူးရှင်။")
                    usage = data.get("usageMetadata", {})
                    tokens = usage.get("totalTokenCount", 0)
                    return text, tokens
                except (KeyError, IndexError):
                    return "⚠️ AI ဖြေကြားချက်ကို ဖတ်လို့မရပါရှင်။", 0
            elif response.status_code in [429, 503]:
                key_rotator.report_exhausted(key)
                continue
            else:
                return f"⚠️ အမှားအယွင်း ဖြစ်ပေါ်နေပါသည် (Error Code: {response.status_code})", 0
        except Exception:
            key_rotator.report_exhausted(key)
            continue
    return "⚠️ API Key အားလုံး အသုံးပြုမှု ကန့်သတ်ချက် ပြည့်သွားပါပြီရှင်။", 0

# ----------------- MESSAGE HANDLER -----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if not user or not user["role_type"]:
        await update.message.reply_text("✨ မင်္ဂလာပါရှင်... ကျေးဇူးပြု၍ အောက်ပါပုံစံကို ရွေးချယ်ပေးပါရှင် -", reply_markup=get_setup_keyboard())
        return

    text = update.message.text
    if text == "📊 Status စစ်ရန်":
        await status_handler(update, context)
        return
    elif text == "❓ Help":
        await help_handler(update, context)
        return
    elif text == "🧹 Chat ရှင်းရန်":
        await reset_handler(update, context)
        return

    role_desc = "ဇနီးသည်" if user["role_type"] == "wife" else "ခင်ပွန်းသည်"
    tone = get_affection_tone(user["affection"])

    system_instruction = (
        f"သင်သည် အသုံးပြုသူ၏ {role_desc} ဖြစ်သည်။ သဘာဝကျကျ ချစ်ခင်ကြင်နာစွာ မြန်မာလို ပြောဆိုပါ။ "
        f"လက်ရှိ အနေအထားမှာ - {tone} "
        f"မှတ်ဉာဏ်များ: {user['memory']}"
    )

    history = user["history"]
    contents = [{"role": "user", "parts": [{"text": system_instruction}]}]
    for h in history:
        contents.append(h)
    contents.append({"role": "user", "parts": [{"text": text}]})

    client: httpx.AsyncClient = context.bot_data["http_client"]
    await context.bot.send_chat_action(chat_id=update.effective_user.id, action="typing")

    bot_response, req_tokens = await call_gemini(client, contents)
    if not bot_response:
        bot_response = "ကိုကို့ကိုယ့်စုံစလောက်ကို သနားပြုံးလို့ပြရိုင့်ရှင်။ နောက်တစ်ခါ ထပ်ပို့ပေးပါနော် 🥺"

    total_tokens = user["tokens_used"] + req_tokens
    affection = user["affection"]

    await update.message.reply_text(bot_response, reply_markup=get_main_keyboard())

    history.append({"role": "user", "parts": [{"text": text}]})
    history.append({"role": "model", "parts": [{"text": bot_response}]})
    if len(history) > 20:
        history = history[-20:]

    await save_user(user_id, user["role_type"], user["spouse_style"], affection, history, total_tokens, user["memory"])

# ----------------- LIFECYCLE -----------------
async def on_startup(application):
    application.bot_data["http_client"] = httpx.AsyncClient()

async def on_shutdown(application):
    client: httpx.AsyncClient = application.bot_data.get("http_client")
    if client:
        await client.aclose()

# ----------------- MAIN -----------------
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

    print(f"🤖 AI Spouse Bot is running with {len(GEMINI_API_KEYS)} Gemini key(s)...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
