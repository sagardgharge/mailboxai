"""Muse Wonder voice agent: spoken questions about your own mailbox.

Wonder is Meta's frontier agent model (muse-wonder-1.0) on the same
Meta Model API connection as Spark — same base URL, same key, just
a different model name. Wonder takes audio input and returns TEXT:
it does not speak. So the voice agent works in two steps that are
already proven separately: Voice Transcribe hears the words, Wonder
reasons over them, and the existing say-pipeline speaks the answer
back as a voice note.

Path B (this module): transcript + mailbox rows -> Wonder -> short
spoken-style answer. Path A (Wonder hearing the raw audio directly)
is NOT built here: the audio-part JSON shape for the Model API was
not confirmed from public sources, and a guessed shape would fail
silently. Path B is a real Wonder use — frontier reasoning over a
spoken question about the user's own mail — with zero API-shape risk.

Cost guard: Wonder output costs more than Spark, so answers are
capped short by the prompt (~80 words) and Wonder is only called
for voice notes that are NOT commands (commands keep the free
parser path). Never called for every letter.
"""

import logging

from . import muse_client

log = logging.getLogger("mailboxai.wonder")

WONDER_MODEL = "muse-wonder-1.0"


def is_configured():
    return muse_client.is_configured()


def answer_question(transcript, items):
    """Answer a spoken mailbox question, or None if unavailable.

    items: mail rows (dicts) from state.get_mail, newest first.
    Only the rows given may be used: sender, subject, category, due,
    amount, scam flag. Bodies are NOT sent (privacy + token cost).
    Returns short spoken-style text, or None on any failure — the
    caller then falls back to the typed-command hint, never silence.
    """
    if not muse_client.is_configured():
        return None
    lines = []
    for m in items[:12]:
        line = (f"#{m['id']} [{m['category']}] {m['sender']} — "
                f"{m.get('subject') or ''}")
        if m["category"] == "bill":
            line += f" (due {m.get('due', '—')}, {m.get('amount', '—')})"
        if m.get("scam"):
            line += " [possible scam]"
        lines.append(line)
    mail_list = "\n".join(lines) if lines else "(mailbox is empty)"
    prompt = (
        "You are MailboxAI's voice assistant. Answer the user's spoken "
        "question using ONLY the mailbox list below. Keep the answer "
        "short (under 80 words), in plain spoken language, as if "
        "speaking it aloud. If the list does not contain the answer, "
        "say so plainly — never invent a bill, date, amount, or "
        "sender.\n\n"
        f"Mailbox:\n{mail_list}\n\n"
        f"User asked: {transcript}\nAnswer:")
    try:
        return muse_client.chat(prompt, model=WONDER_MODEL, tag="wonder")
    except Exception as exc:
        log.warning("[wonder] answer failed: %s", exc)
        return None
