import os
from pathlib import Path


def _load_dotenv():
    """Load a local .env file (if present) so the server works without
    manually exporting variables first. Existing environment variables
    always win over .env values."""
    candidates = [Path(__file__).resolve().parent.parent / ".env",
                  Path.cwd() / ".env"]
    seen = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)
        print(f"[mailboxai] loaded .env from {path}")
        return


_load_dotenv()

"""Configuration from environment. Nothing here is required at import time,
so the app can be smoke-tested without credentials (WhatsApp sends are
then logged instead of delivered — see whatsapp.py)."""
import os

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "mailboxai-verify")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

# Muse Spark via the Meta Model API. Fill these from your Model API
# key page. When unset, triage.py uses the built-in rules engine.
META_MODEL_API_BASE_URL = os.getenv("META_MODEL_API_BASE_URL", "")
META_MODEL_API_KEY = os.getenv("META_MODEL_API_KEY", "")
META_MODEL_NAME = os.getenv("META_MODEL_NAME", "muse-spark")

# Fireworks open-weights junk filter (optional partner credit).
# FIREWORKS_MODEL must be copied exactly from the Fireworks model
# catalogue (accounts/fireworks/models/<name>); unset = not configured.
FIREWORKS_BASE_URL = os.getenv("FIREWORKS_BASE_URL",
                               "https://api.fireworks.ai/inference/v1")
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY", "")
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL", "")

# SAM 3.1 segmentation endpoint (your hosted SAM service). When unset,
# segment.py returns the whole page as one region.
SAM_API_URL = os.getenv("SAM_API_URL", "")

# ONLY for local testing: pretend every license photo verifies to the
# demo address. NEVER enable in production — real verification must run.
MOCK_VERIFY = os.getenv("MAILBOXAI_MOCK_VERIFY", "0") == "1"

# Dev mode: business-register responses include the postcard PIN so the
# flow can be tested without waiting for mail. NEVER enable in production.
DEV_MODE = os.getenv("MAILBOXAI_DEV", "0") == "1"

# Smarty (USPS-based) address validation, for business address checks.
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
MOCK_ADDRESS = "123 Peachtree Street, Atlanta, GA 30309"

DB_PATH = os.getenv("MAILBOXAI_DB", "mailboxai.db")

# Admin key: whoever holds this key is the MailboxAI admin (the service
# owner). Set it in .env; admin endpoints refuse all requests while unset.
ADMIN_KEY = os.getenv("MAILBOXAI_ADMIN_KEY", "")
