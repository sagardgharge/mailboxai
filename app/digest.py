"""The 'today's mail' digest.

Text digest works today. The visual digest card is a Muse Image call
in production (set MUSE_IMAGE_API_URL); until then the text digest is
what gets sent, which is the right fallback — a plain summary beats a
missing one.

WhatsApp has no HTML cards and no custom fonts: the card feel comes
from bold headers, divider lines, and one emoji icon per category.
Bold wraps whole lines (never half a phrase) so the words stay
searchable and copy-paste clean.
"""

_DIV = "━━━━━━━━━━━━━━"
_CAT_ICON = {"urgent": "🚨", "bill": "🧾", "personal": "💌", "junk": "🗑️"}


def _cat_label(category):
    return f"{_CAT_ICON.get(category, '📬')} {category.upper()}"


def build_digest_text(mail_items):
    counts = {"bill": 0, "urgent": 0, "junk": 0, "personal": 0}
    for m in mail_items:
        counts[m["category"]] = counts.get(m["category"], 0) + 1
    lines = [f"📬 *MailboxAI — today's mail: {len(mail_items)} letters*"]
    for cat in ("urgent", "bill", "personal", "junk"):
        if counts[cat]:
            lines.append(f"{_CAT_ICON[cat]} {counts[cat]} {cat}")
    bills = [m for m in mail_items if m["category"] == "bill"]
    if bills:
        lines.append("Next due: " + "; ".join(
            f"{m['sender']} {m['amount']} (due {m['due']})" for m in bills))
    scams = [m for m in mail_items if m["scam"]]
    if scams:
        lines.append(f"⚠ {len(scams)} possible scam letter(s) blocked.")
    return "\n".join(lines)


def build_digest_card(mail_items, sheets_saved=0):
    """Rich digest card for WhatsApp (text — WhatsApp has no HTML cards).

    Per-letter lines with numbers, so SHRED <n> and READ <n> can target
    them, plus bills with due + amount, the scam line, and the
    paper-saved counter. A generated image card is the future Muse
    Image step; this formatted card is what ships now.
    """
    lines = [f"📬 *MailboxAI — today's mail: {len(mail_items)} letters*",
             _DIV]
    for m in mail_items:
        subject = m.get("subject") or (m.get("body", "")[:60])
        line = (f"{m['id']}. {_cat_label(m['category'])} — "
                f"*{m['sender']}* — {subject}")
        if m["category"] == "bill":
            line += f" (due {m.get('due', '—')}, {m.get('amount', '—')})"
        if m.get("scam"):
            line += " ⚠ Possible scam"
        if m.get("attachment_media_id"):
            line += " 📎"
        lines.append(line)
    if sheets_saved:
        lines.append(f"♻️ *Paper saved so far: {sheets_saved} sheets "
                     f"(≈{sheets_saved * 0.01:.1f} lbs)*")
    lines.append(_DIV)
    lines.append("Reply OPEN <number> to read one fully, SHRED <number>, "
                 "READ <number>, or SAVED.")
    return "\n".join(lines)
