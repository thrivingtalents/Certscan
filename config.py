"""App configuration, read from environment variables (or a .env file)."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- core ---
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
    # Public address participants reach, used in QR codes and emails, e.g. https://cert.example.com
    # On Render it defaults to the service's own https://<name>.onrender.com address.
    BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
                or "http://localhost:5000").rstrip("/")
    APP_NAME = os.environ.get("APP_NAME", "CertQR")
    # Time zone used when showing registration times in the admin panel and CSV
    DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "UTC")

    # --- storage ---
    DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024

    # --- email ---
    # Leave SMTP_HOST empty to run in "outbox mode": emails are saved as .eml files
    # in data/outbox instead of being sent. Handy for local testing.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_SSL = _bool("SMTP_SSL", False)          # True for port 465
    SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)  # True for port 587
    MAIL_FROM = os.environ.get("MAIL_FROM", "CertQR <no-reply@example.com>")
    MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "")
    MAIL_MAX_ATTEMPTS = int(os.environ.get("MAIL_MAX_ATTEMPTS", "4"))
    # Seconds to wait between sends (keeps you under provider rate limits)
    MAIL_SEND_INTERVAL = float(os.environ.get("MAIL_SEND_INTERVAL", "0.5"))
    RUN_MAIL_WORKER = _bool("RUN_MAIL_WORKER", True)

    # --- abuse protection ---
    REGISTER_LIMIT_PER_MIN = int(os.environ.get("REGISTER_LIMIT_PER_MIN", "10"))

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
