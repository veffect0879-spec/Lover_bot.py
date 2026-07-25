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

admin_env = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip().isdigit()]

env_keys = os.environ.get("GEMINI_API_KEYS", "")
if env_keys:
    GEMINI_API_KEYS = [k.strip() for k in env_keys.split(",") if k.strip()]
else:
    GEMINI_API_KEYS = []


