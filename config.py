import hashlib
import hmac
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "sirius_web.sqlite3"))
DATA_DIR = BASE_DIR / "data" if os.environ.get("DB_PATH") else BASE_DIR
KEY_FILE = DATA_DIR / "encryption_key.txt"
VAPID_PRIVATE_KEY_FILE = DATA_DIR / "vapid_private_key.pem"
FCM_SERVICE_ACCOUNT_FILE = Path(os.environ.get("FCM_SERVICE_ACCOUNT_FILE", DATA_DIR / "firebase_service_account.json"))
WEB_PUSH_SUBJECT = os.environ.get("WEB_PUSH_SUBJECT", "mailto:admin@sirius.rusanoff.ru")
CANONICAL_HOST = "sirius.rusanoff.ru"
INSTANCE_SEAL_FILE = DATA_DIR / "instance_seal.json"

TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY") or (
    KEY_FILE.read_text().strip() if KEY_FILE.exists() else None
)

HOST = os.environ.get("SIRIUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("SIRIUS_PORT", "8000"))


def _instance_seal_signature(payload: dict) -> str:
    if not TOKEN_ENCRYPTION_KEY:
        return ""
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        TOKEN_ENCRYPTION_KEY.encode(), serialized.encode(), hashlib.sha256
    ).hexdigest()


def create_instance_seal() -> None:
    """Bind this deployment to the private persistent data directory."""
    if not TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("Encryption key is missing; cannot activate this instance.")
    payload = {"host": CANONICAL_HOST, "version": 1}
    seal = {**payload, "signature": _instance_seal_signature(payload)}
    INSTANCE_SEAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTANCE_SEAL_FILE.write_text(json.dumps(seal, separators=(",", ":")) + "\n")
    try:
        INSTANCE_SEAL_FILE.chmod(0o600)
    except OSError:
        pass


def instance_seal_is_valid() -> bool:
    if not TOKEN_ENCRYPTION_KEY or not INSTANCE_SEAL_FILE.exists():
        return False
    try:
        seal = json.loads(INSTANCE_SEAL_FILE.read_text())
        payload = {"host": seal["host"], "version": seal["version"]}
        return (
            payload == {"host": CANONICAL_HOST, "version": 1}
            and hmac.compare_digest(seal["signature"], _instance_seal_signature(payload))
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False
