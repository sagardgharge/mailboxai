"""Muse Image digest card: today's mail as a real picture.

Model muse-image-1.0 on the same Meta Model API connection as Spark
(same base URL, same key). One POST to /images/generations returns
base64 image bytes; anything unexpected returns None and the caller
falls back to the text digest card — MAIL must never break because
an image call failed.

Image models garble text and numbers, so the prompt deliberately
contains NO numbers, NO business names, and NO letter contents —
just a scene. The facts (counts, bills due, paper saved) always
travel in the WhatsApp caption / text card that follows the image.
"""

import base64
import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("mailboxai.image")

IMAGE_MODEL = "muse-image-1.0"

_PROMPT = ("Flat friendly illustration of a cheerful red mailbox "
           "overflowing with paper letters, one green leaf beside it, "
           "soft morning colors, clean simple shapes. "
           "No text, no numbers, no words, no letters on the envelopes.")


def _images_url():
    base = config.META_MODEL_API_BASE_URL.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/images/generations"


def generate_digest_image():
    """Today's-mail illustration bytes (PNG/JPEG), or None.

    None when the Model API is not configured, the endpoint rejects
    the call, or the reply carries no base64 image — all handled by
    the caller falling back to the text digest card.
    """
    if not config.META_MODEL_API_BASE_URL or not config.META_MODEL_API_KEY:
        return None
    body = json.dumps({"model": IMAGE_MODEL, "prompt": _PROMPT}).encode()
    req = urllib.request.Request(
        _images_url(), data=body, method="POST",
        headers={"Authorization": f"Bearer {config.META_MODEL_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        log.error("[image] generate failed: status=%s body=%s",
                  exc.code, exc.read().decode("utf-8", "replace")[:300])
        return None
    except Exception as exc:
        log.error("[image] generate request failed: %s", exc)
        return None
    try:
        b64 = (data.get("data") or [{}])[0].get("b64_json")
        if not b64:
            return None
        return base64.b64decode(b64)
    except Exception as exc:
        log.error("[image] bad image reply: %s", exc)
        return None
