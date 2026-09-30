import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
SYSADMIN_IDS = {int(x) for x in os.getenv("SYSADMIN_IDS", "").replace(" ", "").split(",") if x}
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()   # anthropic | openrouter
PARSE_MODEL = os.getenv("PARSE_MODEL", "claude-sonnet-5")
AUDIT_MODEL = os.getenv("AUDIT_MODEL", "claude-opus-5-5")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
PORT = int(os.getenv("PORT", "8080"))
DEV_NO_AUTH = os.getenv("DEV_NO_AUTH", "0") == "1"

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)
MASTER_XLSX = DATA_DIR / "учет.xlsx"
DB_URL = f"sqlite:///{DATA_DIR / 'lumen.db'}"
