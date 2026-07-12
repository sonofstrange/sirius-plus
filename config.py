import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "sirius_web.sqlite3"))
DATA_DIR = BASE_DIR / "data" if os.environ.get("DB_PATH") else BASE_DIR
KEY_FILE = DATA_DIR / "encryption_key.txt"
VAPID_PRIVATE_KEY_FILE = DATA_DIR / "vapid_private_key.pem"
WEB_PUSH_SUBJECT = os.environ.get("WEB_PUSH_SUBJECT", "mailto:admin@sirius.rusanoff.ru")
CANONICAL_HOST = os.environ.get("SIRIUS_PLUS_CANONICAL_HOST", "sirius.rusanoff.ru").lower()
DEVELOPMENT_MODE = os.environ.get("SIRIUS_PLUS_DEVELOPMENT") == "1"

TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY") or (
    KEY_FILE.read_text().strip() if KEY_FILE.exists() else None
)

HOST = os.environ.get("SIRIUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("SIRIUS_PORT", "8000"))
