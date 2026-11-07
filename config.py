import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root if it exists
dotenv_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=dotenv_path)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Comma-separated list of Telegram user IDs allowed to interact with the bot
_allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _allowed_raw:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in _allowed_raw.split(",") if uid.strip().isdigit()}
else:
    ALLOWED_USER_IDS = set()

# Execution mode: "ssh" (execute on remote host) or "local" (execute on this machine)
RUN_MODE = os.getenv("RUN_MODE", "ssh").strip().lower()

# Default multigravity profile
MGY_PROFILE = os.getenv("MGY_PROFILE", "default").strip()

# Default root / working directory for tasks and execution (set via .env on device)
DEFAULT_WORKING_DIR = os.getenv("DEFAULT_WORKING_DIR", "").strip()

# SSH Target Configuration (used if RUN_MODE == "ssh")
SSH_USER = os.getenv("SSH_USER", "").strip()
SSH_HOST = os.getenv("SSH_HOST", "").strip()
SSH_PORT = int(os.getenv("SSH_PORT", "22").strip())
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "").strip() or None
SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "600").strip())

# Session monitor poll interval in seconds
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3").strip())
