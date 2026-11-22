import os
import re
import logging
from pathlib import Path
from dotenv import load_dotenv

# Base Directory
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Load environment variables
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, override=True)
else:
    load_dotenv()

# Setup Logging
LOG_FORMAT = "%(asctime)s - [%(levelname)s] - %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("VideoForwarderBot")

# Core Credentials
API_ID_RAW = os.getenv("API_ID", "31255603")
API_HASH = os.getenv("API_HASH", "04d7913f5e93bfec24dd4128b183ba25").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8585395437:AAEReXHg5QmZnrWBYmE2lB0TWRkIxU7UToY").strip()

try:
    API_ID = int(API_ID_RAW.strip()) if API_ID_RAW.strip() else None
except ValueError:
    API_ID = None

OWNER_ID_RAW = os.getenv("OWNER_ID", "8721309931").strip()
try:
    OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else 0
except ValueError:
    OWNER_ID = 0

TARGET_CHAT_RAW = os.getenv("TARGET_CHAT_ID", "").strip()
if TARGET_CHAT_RAW:
    try:
        TARGET_CHAT_ID = int(TARGET_CHAT_RAW)
    except ValueError:
        TARGET_CHAT_ID = TARGET_CHAT_RAW
else:
    TARGET_CHAT_ID = None

SOURCE_CHAT_RAW = os.getenv("SOURCE_CHAT_ID", "").strip()
if SOURCE_CHAT_RAW:
    try:
        SOURCE_CHAT_ID = int(SOURCE_CHAT_RAW)
    except ValueError:
        SOURCE_CHAT_ID = SOURCE_CHAT_RAW
else:
    SOURCE_CHAT_ID = None

# Caption Settings
CAPTION_MODE = os.getenv("CAPTION_MODE", "original").strip().lower()
CUSTOM_CAPTION = os.getenv("CUSTOM_CAPTION", "").strip()
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "").strip()

# State holder for dynamic settings
runtime_settings = {
    "owner_id": OWNER_ID,
    "target_chat_id": TARGET_CHAT_ID,
    "source_chat_id": SOURCE_CHAT_ID,
    "caption_mode": CAPTION_MODE,
    "custom_caption": CUSTOM_CAPTION,
    "watermark_text": WATERMARK_TEXT
}

def save_env_file():
    """Persists runtime settings back to .env"""
    try:
        lines = [
            "# ==========================================",
            "# Telegram Video Forwarder & Re-Uploader Bot",
            "# ==========================================",
            f"API_ID={API_ID}",
            f"API_HASH={API_HASH}",
            f"BOT_TOKEN={BOT_TOKEN}",
            f"OWNER_ID={runtime_settings.get('owner_id', 0)}",
            f"TARGET_CHAT_ID={runtime_settings.get('target_chat_id', '') or ''}",
            f"SOURCE_CHAT_ID={runtime_settings.get('source_chat_id', '') or ''}",
            f"CAPTION_MODE={runtime_settings.get('caption_mode', 'original')}",
            f"CUSTOM_CAPTION={runtime_settings.get('custom_caption', '')}",
            f"WATERMARK_TEXT={runtime_settings.get('watermark_text', '')}",
        ]
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.error(f"Failed to persist .env file: {e}")

def update_runtime_setting(key: str, value):
    """Update runtime setting and persist to .env"""
    runtime_settings[key] = value
    logger.info(f"Setting updated: {key} = {value}")
    save_env_file()

def validate_config() -> tuple[bool, str]:
    if not API_ID:
        return False, "API_ID missing or invalid."
    if not API_HASH:
        return False, "API_HASH missing."
    if not BOT_TOKEN:
        return False, "BOT_TOKEN missing."
    return True, "Config valid"
