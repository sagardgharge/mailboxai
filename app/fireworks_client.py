"""Optional cheap junk filter via Fireworks (open-weights, serverless).

A small open-weights model answers ONE question — junk or not — and
only unclear letters escalate to Spark, saving Model API credits.

Wired into triage.py as the first pass: it runs only when
FIREWORKS_API_KEY + FIREWORKS_MODEL are set in .env; otherwise
triage skips it and the rules engine plays this role.

API shape (OpenAI-compatible):
  base URL https://api.fireworks.ai/inference/v1, endpoint
  /chat/completions, key in FIREWORKS_API_KEY.
  The model id must be copied exactly from the Fireworks model
  catalogue (form: accounts/fireworks/models/<name>) — it is NOT
  guessed here, so an unset FIREWORKS_MODEL means not configured.
"""
import json
import logging
import urllib.request

from . import config

log = logging.getLogger("mailboxai.fireworks")

DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"


def is_configured():
    return bool(config.FIREWORKS_API_KEY and config.FIREWORKS_MODEL)


def completions_url():
    """Full completions endpoint, tolerating a pasted /chat/completions suffix."""
    base = (config.FIREWORKS_BASE_URL or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def chat(prompt, temperature=0):
    """Send one prompt to the configured Fireworks model, return reply text.

    Raises on failure so callers fall back deliberately, never silently.
    """
    if not is_configured():
        raise RuntimeError("Fireworks is not configured "
                           "(set FIREWORKS_API_KEY and FIREWORKS_MODEL)")
    body = json.dumps({
        "model": config.FIREWORKS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        completions_url(), data=body,
        headers={"Authorization": f"Bearer {config.FIREWORKS_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Fireworks returned no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError("Fireworks returned an empty reply")
    log.info("[fireworks] prompt %d chars -> reply %d chars",
             len(prompt), len(content))
    return content


def is_junk(sender, text):
    """True if the letter is junk mail, False if not. Raises on bad replies.

    The model replies with ONLY JSON: {"junk": true|false}.
    Fenced replies (```json ... ```) are stripped, as in triage.py,
    so a good reply is not lost to a fence. Callers must catch the
    raise and fall back to rules/Spark — never treat a failure as junk.
    """
    prompt = (
        "You are a junk-mail filter. Reply with ONLY JSON: "
        "{\"junk\": true|false}. Junk means advertising, promotions, "
        "catalogues, offers and solicitations — not bills, statements, "
        "legal notices, or personal letters.\n\n"
        f"Sender: {sender}\nLetter:\n{text}")
    content = chat(prompt)
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        out = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Fireworks returned non-JSON junk verdict: {exc}")
    junk = out.get("junk")
    if isinstance(junk, str):
        junk = junk.strip().lower() in ("true", "yes", "1")
    if not isinstance(junk, bool):
        raise ValueError(f"Fireworks returned unknown junk verdict: {out!r}")
    return junk
