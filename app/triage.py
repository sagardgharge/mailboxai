"""Mail triage: bill / urgent / junk / personal + amount + due date + scam.

Two engines, one interface:
  1. Muse Spark (Meta Model API) when META_MODEL_API_BASE_URL + KEY are
     set. The request uses the common chat-completions shape (see
     muse_client.py); the exact base URL / model name come from the
     Model API docs.
  2. Rules engine fallback (no credentials). This is also what the
     open-weights junk filter replaces for cheap mail: run the small
     model first, escalate only unclear letters to Muse Spark, and
     save API credits.
"""
import json
import re

from . import config

AMOUNT_RE = re.compile(r"\$\s?[\d,]+\.\d{2}")
DUE_RE = re.compile(
    r"(?:due|payment due|by)\s+([A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE)
SCAM_RE = re.compile(
    r"you (have )?won|grand prize|processing fee|send.*bank details|"
    r"act fast|offer expires in 24 hours|claim.*prize", re.IGNORECASE)
BILL_WORDS = ["bill", "statement", "payment due", "balance", "invoice"]
URGENT_WORDS = ["action required", "verify", "unusual transaction",
                "temporary hold", "final notice", "court", "eviction"]


def _rules_triage(sender, text):
    low = text.lower()
    amount = (AMOUNT_RE.search(text) or [None]).group(0) if AMOUNT_RE.search(text) else "—"
    due_m = DUE_RE.search(text)
    due = due_m.group(1) if due_m else "—"
    scam = bool(SCAM_RE.search(text))
    if scam:
        category = "junk"
    elif any(w in low for w in URGENT_WORDS):
        category = "urgent"
    elif any(w in low for w in BILL_WORDS):
        category = "bill"
    elif any(w in low for w in ["sale", "clearance", "% off", "unsubscribe",
                                "showroom", "offer"]):
        category = "junk"
    else:
        category = "personal"
    return {"sender": sender, "category": category, "amount": amount,
            "due": due, "scam": scam, "engine": "rules"}


def _spark_triage(sender, text):
    # Triage goes through the one central Muse connection
    # (muse_client.py).
    from . import muse_client
    prompt = (
        "You are a mail triage assistant. Read this letter and reply with"
        " ONLY JSON: {\"category\": \"bill|urgent|junk|personal\","
        " \"amount\": \"$X or null\", \"due\": \"date or null\","
        " \"scam\": true|false}.\n\n"
        f"Sender: {sender}\nLetter:\n{text}")
    content = muse_client.chat(prompt, tag="triage")
    # Models often wrap JSON in ```json fences — strip them, or a good
    # Spark reply would fail parsing and silently fall back to rules.
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        out = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Muse Spark returned non-JSON triage: {exc}")
    if out.get("category") not in ("bill", "urgent", "junk", "personal"):
        raise ValueError(
            f"Muse Spark returned unknown category: {out.get('category')!r}")
    scam = out.get("scam")
    if isinstance(scam, str):
        scam = scam.strip().lower() in ("true", "yes", "1")
    return {"sender": sender, "category": out["category"],
            "amount": out.get("amount") or "—",
            "due": out.get("due") or "—",
            "scam": bool(scam), "engine": "muse-spark"}


def triage_letter(sender, text):
    """Always returns {sender, category, amount, due, scam, engine}.

    Cheap first: a configured open-weights model (Fireworks) judges
    junk before Spark is spent. Junk here is final (advertising /
    promotions only — bills, statements, legal and personal letters
    are never junk by that prompt); anything else, or any Fireworks
    failure, falls through to Spark, then rules. Nothing is ever
    lost to a filter failure.
    """
    try:
        from . import fireworks_client
        if fireworks_client.is_configured() and fireworks_client.is_junk(sender, text):
            return {"sender": sender, "category": "junk", "amount": "",
                    "due": "", "scam": False, "engine": "open-weights"}
    except Exception as exc:  # fall through to Spark/rules
        print(f"[triage] open-weights filter failed ({exc}); escalating")
    if config.META_MODEL_API_BASE_URL and config.META_MODEL_API_KEY:
        try:
            return _spark_triage(sender, text)
        except Exception as exc:  # fall back rather than lose the letter
            print(f"[triage] Muse Spark failed ({exc}); using rules engine")
    return _rules_triage(sender, text)
