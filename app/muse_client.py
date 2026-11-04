"""ONE central connection to real Muse Spark — used by every feature.

This is the "one place" rule: every part of MailboxAI that needs Muse
(triage, license reading, digest summaries, scam explanations) calls
this module, and nothing else touches the Model API directly.

Why one place, not one call per feature:
  - API key, base URL, and model name live in exactly one spot
    (app/config.py) — rotating a key is a one-line change.
  - Cost control lives here: logging, timeouts, and the fallback rule
    (if Spark fails, the caller falls back to its local engine instead
    of losing the letter).
  - Testing is easy: with no credentials set, is_configured() is False
    and callers use their local engines — the app still runs.

Features stay SEPARATE modules (triage.py, license.py, digest.py,
voice.py, segment.py) — they just share this one connection. That is
the difference between "one connection" (good) and "one giant module
that does everything" (bad: untestable, and one failure breaks all).

The request uses the common chat-completions shape. The exact base URL
and model name come from the Model API docs
(set them in .env) — the client normalizes a trailing
slash, and a base URL that already ends in /chat/completions.
"""
import json
import logging
import urllib.request

from . import config

log = logging.getLogger("mailboxai.muse")


def is_configured():
    return bool(config.META_MODEL_API_BASE_URL and config.META_MODEL_API_KEY)


def completions_url():
    """Full completions endpoint for the configured base URL.

    Tolerates the two shapes participants usually paste from the docs:
    a bare host (https://api.example.com/v1) and one that already ends
    in /chat/completions — both must produce exactly one such suffix.
    """
    base = config.META_MODEL_API_BASE_URL.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _post(messages, temperature=0, model=None, tag=None):
    """POST one chat-completions request; return the reply text.

    model defaults to META_MODEL_NAME (Spark); Wonder passes
    muse-wonder-1.0 through the same connection. tag labels the
    Terminal 1 log line (triage, vision-license, vision-face,
    vision-liveness, wonder) so a log line names its caller.
    Raises on failure so callers can fall back deliberately.
    """
    if not is_configured():
        raise RuntimeError("Meta Model API is not configured "
                           "(set META_MODEL_API_BASE_URL and META_MODEL_API_KEY)")
    body = json.dumps({
        "model": model or config.META_MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        completions_url(), data=body,
        headers={"Authorization": f"Bearer {config.META_MODEL_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    # Defensive parse: a wrong endpoint shape must raise (so the caller
    # falls back deliberately) instead of crashing on a KeyError.
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Muse Spark returned no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError("Muse Spark returned an empty reply")
    label = f"[muse] {tag} reply" if tag else "[muse] reply"
    log.info("%s %d chars", label, len(content))
    return content


def chat(prompt, temperature=0, model=None, tag=None):
    """Send one text prompt (Spark by default; pass model for Wonder),
    return the reply text. tag labels the Terminal 1 log line."""
    return _post([{"role": "user", "content": prompt}], temperature,
                 model=model, tag=tag)


def chat_with_images(prompt, images, temperature=0, model=None, tag=None):
    """Send a prompt plus JPEG photos to Muse Spark vision.

    Pass model to call a different vision-capable model through the
    same connection.

    images: list of JPEG bytes. They go as base64 data URLs in the
    OpenAI-compatible vision parts — the same Model API connection,
    just with eyes. Used by face.py (license-vs-selfie matching and
    video liveness); nothing else calls the Model API directly.
    """
    import base64
    parts = [{"type": "text", "text": prompt}]
    for img in images:
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}})
    return _post([{"role": "user", "content": parts}], temperature,
                 model=model, tag=tag)
