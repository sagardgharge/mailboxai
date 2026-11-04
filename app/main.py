"""MailboxAI backend — FastAPI.

Run:  uvicorn app.main:app --reload --port 8000

Endpoints:
  GET  /health          — liveness check
  GET  /webhook         — Meta webhook verification (hub.challenge)
  POST /webhook         — incoming WhatsApp messages
  POST /mail/upload     — CMRA partner / test scan intake (form fields)
  GET  /digest/{mailbox}— today's digest for a mailbox (debug/demo)
"""
from datetime import date, datetime
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               Response)

from . import business, config, digest as digest_mod, face as face_mod
from . import license as license_mod
from . import segment as segment_mod, state, triage, voice as voice_mod, voice_in as voice_in_mod
from . import whatsapp

log = logging.getLogger("mailboxai")
logging.basicConfig(level=logging.INFO)
state.init_db()

app = FastAPI(title="MailboxAI")

# License photos awaiting a selfie match. In-memory only, on purpose:
# a real license photo must never be written to disk or the database.
# A server restart drops the entry, and the user is asked to resend it.
_LICENSE_PHOTOS = {}
_CHALLENGES = {}  # phone -> {code, expires} for the spoken-code step

# WhatsApp message ids already answered. Meta replays a delivery when
# its first attempt is slow or unreachable (tunnel outage), and without
# this the replay re-runs the handler: duplicate digests, duplicate
# videos, duplicate replies. In-memory like the challenges, so a
# restart clears it; a replay after a restart may answer once.
_SEEN_MESSAGE_IDS = set()
_SEEN_MESSAGE_IDS_MAX = 5000


def _is_replay(msg_id):
    return bool(msg_id) and msg_id in _SEEN_MESSAGE_IDS


def _remember_message(msg_id):
    if not msg_id:
        return
    _SEEN_MESSAGE_IDS.add(msg_id)
    if len(_SEEN_MESSAGE_IDS) > _SEEN_MESSAGE_IDS_MAX:
        _SEEN_MESSAGE_IDS.clear()

# Business portal sessions: token -> business_id. In-memory only, on
# purpose for the demo: a server restart logs every business out.
# Production needs real sessions (expiry, secure cookies, rate limits).
_BUSINESS_SESSIONS = {}


def _hash_password(password):
    """PBKDF2-SHA256 hash, stored as salt_hex$hash_hex. Stdlib only."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, 100_000)
    return f"{salt.hex()}${digest.hex()}"


def _check_password(password, stored):
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), 100_000)
        return secrets.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


@app.get("/health")
def health():
    return {"ok": True}


def _require_admin(request):
    """The admin is the service owner, proven by one secret key — not
    voted on by users, not per-mailbox. No key in .env means admin is
    not configured and every admin route refuses."""
    if not config.ADMIN_KEY:
        raise HTTPException(
            status_code=403,
            detail="admin not configured — set MAILBOXAI_ADMIN_KEY")
    given = (request.headers.get("X-Admin-Key")
             or request.query_params.get("key") or "")
    if not secrets.compare_digest(given, config.ADMIN_KEY):
        raise HTTPException(status_code=403, detail="admin key required")


@app.get("/admin/stats")
def admin_stats(request: Request):
    _require_admin(request)
    return state.admin_stats()


@app.get("/admin/businesses")
def admin_businesses(request: Request):
    _require_admin(request)
    return {"businesses": state.list_businesses()}


@app.get("/admin/backup")
def admin_backup(request: Request):
    """Owner-only backup download: the whole mailbox database plus
    every locally stored attachment, as one ZIP. Download it and keep
    a copy OFF this machine (USB drive / encrypted cloud) — a ZIP on
    the same MacBook is not a backup. Never includes .env, and the
    ZIP itself is not encrypted, so store it somewhere safe."""
    _require_admin(request)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(config.DB_PATH):
            z.write(config.DB_PATH, arcname="mailboxai.db")
        d = _attachments_dir()
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                z.write(os.path.join(d, name),
                        arcname=f"attachments/{name}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition":
                 "attachment; filename=mailboxai-backup.zip"})


@app.post("/admin/business/membership")
def admin_set_membership(request: Request, payload: dict):
    _require_admin(request)
    ok = state.set_membership(payload.get("business_id"),
                              plan=payload.get("plan"),
                              membership=payload.get("membership"))
    if not ok:
        raise HTTPException(status_code=404, detail="unknown business")
    return {"ok": True,
            "business": state.get_business(payload["business_id"])}


@app.get("/admin-page", response_class=HTMLResponse)
def admin_page():
    """Admin dashboard UI, served by the API itself (same origin, so no
    CORS setup is needed). Open http://localhost:8000/admin-page."""
    page = Path(__file__).resolve().parent.parent / "demo" / "admin.html"
    if page.is_file():
        return page.read_text(encoding="utf-8")
    return "<p>admin.html not found in demo/.</p>"


@app.get("/business-send", response_class=HTMLResponse)
def business_send_page():
    """Business demo sender, served by the API itself so it can be opened
    from a second phone (tunnel URL or same Wi-Fi). Demo only, no login,
    so keep the URL private."""
    page = Path(__file__).resolve().parent.parent / "demo" / "business-send.html"
    if page.is_file():
        return page.read_text(encoding="utf-8")
    return "<p>business-send.html not found in demo/.</p>"


@app.get("/business-portal", response_class=HTMLResponse)
def business_portal_page():
    """Per-business portal: login, sent-mail list, and a send form with
    the sender locked to the logged-in business. Served by the API
    itself (same origin, no CORS setup)."""
    page = Path(__file__).resolve().parent.parent / "demo" / "business-portal.html"
    if page.is_file():
        return page.read_text(encoding="utf-8")
    return "<p>business-portal.html not found in demo/.</p>"


def _portal_business(request):
    """The business behind a Bearer portal token, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    biz_id = _BUSINESS_SESSIONS.get(auth[7:].strip())
    return state.get_business(biz_id) if biz_id else None


@app.post("/business/login")
def business_login(identifier: str = Form(...), password: str = Form(...)):
    """Portal login by business ID or email + password. Returns a
    session token for /business/mail and the portal send form."""
    biz = state.get_business(identifier)
    if biz is None:
        matches = state.get_businesses_by_email(identifier)
        if len(matches) == 1:
            biz = matches[0]
        elif len(matches) > 1:
            return JSONResponse(
                {"ok": False, "reason": "ambiguous_email",
                 "detail": "Several businesses share this email — log in "
                           "with your business ID instead."},
                status_code=400)
    if biz is None:
        return JSONResponse(
            {"ok": False, "reason": "unknown_business",
             "detail": "No business found for that ID or email."},
            status_code=404)
    if not biz.get("password_hash"):
        return JSONResponse(
            {"ok": False, "reason": "no_password",
             "detail": "This business has no portal password yet — "
                       "re-register with the same name and address plus "
                       "a password, then log in."},
            status_code=403)
    if not _check_password(password, biz["password_hash"]):
        return JSONResponse(
            {"ok": False, "reason": "wrong_password",
             "detail": "Wrong password for this business."},
            status_code=401)
    token = secrets.token_urlsafe(32)
    _BUSINESS_SESSIONS[token] = biz["business_id"]
    return {"ok": True, "token": token, "business_id": biz["business_id"],
            "name": biz["name"], "verified": bool(biz["verified"]),
            "plan": biz["plan"], "membership": biz["membership"]}


@app.get("/business/mail")
def business_mail(request: Request):
    """Mail sent by the logged-in business only (token-scoped)."""
    biz = _portal_business(request)
    if biz is None:
        return JSONResponse(
            {"ok": False, "reason": "unauthorized",
             "detail": "Log in at /business-portal first."},
            status_code=401)
    rows = state.get_mail_by_sender(biz["name"])
    return {"ok": True, "business": biz["name"],
            "mail": [{"id": r["id"], "mailbox": r["mailbox"],
                      "subject": r["subject"], "category": r["category"],
                      "due": r.get("due"), "amount": r.get("amount"),
                      "scam": bool(r.get("scam")),
                      "shredded": bool(r.get("shredded")),
                      "attachment": bool(r.get("attachment_media_id"))}
                     for r in rows]}


@app.post("/business/logout")
def business_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        _BUSINESS_SESSIONS.pop(auth[7:].strip(), None)
    return {"ok": True}


@app.get("/webhook")
def verify_webhook(hub_mode: str = "", hub_verify_token: str = "",
                   hub_challenge: str = ""):
    """Meta calls this once when you register the webhook URL."""
    if hub_mode == "subscribe" and hub_verify_token == config.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("verification failed", status_code=403)


def _try_send_digest_image(phone):
    """MAIL bonus: a Muse Image illustration ahead of the text card.

    Best effort only: any failure (not configured, API error, upload
    error) just skips the image and the text card still arrives.
    """
    try:
        from . import image_card as image_card_mod
        image = image_card_mod.generate_digest_image()
        if not image:
            return
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "digest.png")
            with open(path, "wb") as f:
                f.write(image)
            media_id = whatsapp.upload_media(path, "image/png")
            whatsapp.send_image_id(
                phone, media_id,
                caption="📬 Today's mail — details in the card below.")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as exc:
        log.warning("digest image skipped: %s", exc)


_WELCOME_IMAGE = (Path(__file__).resolve().parent.parent
                  / "demo" / "mailbox-welcome.jpg")


def _try_send_welcome_image(phone):
    """First-Hi picture: the test number can't carry a profile photo,
    so the welcome sends the MailboxAI picture itself, captioned to
    save as the contact photo. Missing file or failed send -> text
    only; the welcome never fails over a picture."""
    try:
        if not _WELCOME_IMAGE.is_file():
            return
        media_id = whatsapp.upload_media(str(_WELCOME_IMAGE), "image/jpeg")
        whatsapp.send_image_id(
            phone, media_id,
            caption=("🌳 Use MailboxAI and Save Trees.\n"
                     "📬 MailboxAI — your mailbox, inside WhatsApp!\n"
                     "Save this picture as my contact photo: long-press "
                     "it, tap Save, then set it as the contact's photo."))
    except Exception as exc:
        log.warning("welcome image send failed: %s", exc)


# WhatsApp has no custom fonts, colors, or backgrounds: the card feel
# comes from bold headers, divider lines, and one emoji icon per
# category. Bold wraps whole lines (never half a phrase) so the words
# stay searchable and copy-paste clean.
_DIV = "━━━━━━━━━━━━━━"
_CAT_ICON = {"urgent": "🚨", "bill": "🧾", "personal": "💌", "junk": "🗑️"}


def _cat_label(category):
    return f"{_CAT_ICON.get(category, '📬')} {category.upper()}"


def _paper_saved_text(user):
    saved = state.sheets_saved(user["mailbox"])
    return (f"♻️ Paper saved: {saved} sheets "
            f"(≈{saved * 0.01:.1f} lbs of paper). Every shredded "
            "letter is recycled, not trashed.")


def _help_text():
    """Compact command menu (replaces the old wall-of-text help).

    Every command and its purpose survives — only the commas and the
    word "Reply" went, so the menu scans like an app screen."""
    return ("📬 *MailboxAI — what you can say:*\n" + _DIV + "\n"
            "• MAIL — today's digest\n"
            "• COUNT — totals\n"
            "• FROM <business> — one sender\n"
            "• SEARCH <word> — find an old letter\n"
            "• LATEST <number> — the newest ones\n"
            "• TODAY — only today's mail\n"
            "• OPEN <number> — read a letter fully\n"
            "• READ <number> — hear a letter\n"
            "• SHRED <number> — shred one\n"
            "• SAVED — your paper total\n" + _DIV + "\n"
            "Or just ask naturally — 'show me today's Barber Shop mail'. "
            "Or send a voice note — I'll transcribe it and run your "
            "command.")


def _handle_text(phone, user, text):
    text = text.strip()
    low = text.lower()
    if user is None or user["state"] == "START":
        # First contact: the picture goes first (so the user can save
        # it as the contact photo), then the welcome text asks for
        # their name. Any first message gets it, not just "Hi".
        _try_send_welcome_image(phone)
        # "Who are you?" before onboarding is a question, not a name:
        # answer it, then ask for the name in the same message.
        if _match_conversational(text) == "identity":
            state.save_user(phone, state="AWAITING_NAME")
            return (_identity_guide(None)
                    + "\n\nWhat's your full name, as it appears on "
                      "your driver's license?")
        state.save_user(phone, state="AWAITING_NAME")
        return ("Hi! I'm MailboxAI — your physical mailbox, inside WhatsApp. "
                "What's your full name, as it appears on your driver's license?")
    if user["state"] == "AWAITING_NAME":
        conv = _match_conversational(text)
        if conv == "identity":
            return (_identity_guide(None)
                    + "\n\nWhat's your full name, as it appears on "
                      "your driver's license?")
        if conv is not None:
            # A greeting/thanks/farewell/ack is chat, not a name: answer
            # it briefly and ask for the name again.
            ack = {"greeting": "Hi! 👋 ",
                   "thanks": "You're welcome! 😊 ",
                   "farewell": "Bye! 👋 ",
                   "ack": "👍 "}.get(conv, "")
            return (ack + "What's your full name, as it appears on your "
                          "driver's license?")
        state.save_user(phone, name=text, state="AWAITING_LICENSE")
        return (f"Thanks, {text.split()[0]}! Now send a photo of your driver's "
                "license — I'll verify your address from it.")
    if user["state"] == "AWAITING_SELFIE":
        return _challenge_prompt(phone)
    if user["state"] == "VERIFIED":
        m = re.match(r"shred (\d+)", low)
        if m:
            mail_id = int(m.group(1))
            # Mailbox-checked, like the Shred button: a user can only
            # shred a letter actually in their own mailbox, not another
            # household's letter guessed by number.
            if not any(item["id"] == mail_id
                       for item in state.get_mail(user["mailbox"])):
                return (f"Letter #{mail_id} is already shredded or "
                        "not in your mailbox.")
            state.shred_mail(mail_id)
            saved = state.sheets_saved(user["mailbox"])
            return (f"♻️ Shredded & recycled. Your household has now saved "
                    f"{saved} sheets of paper.")
        if low == "saved":
            return _paper_saved_text(user)
        m = re.match(r"read (\d+)", low)
        if m:
            return _handle_read(phone, user, int(m.group(1)))
        if low in ("mail", "digest"):
            items = state.get_mail(user["mailbox"])
            if not items:
                return "Your mailbox is empty today. New mail will appear here."
            card = digest_mod.build_digest_card(
                items, state.sheets_saved(user["mailbox"]))
            _try_send_digest_image(phone)
            # Quick-reply buttons under the card: the three actions
            # that need no number. OPEN/SHRED/READ stay typed (they
            # need a letter number, which a button cannot carry).
            return ("buttons", card,
                    [("saved", "♻️ Paper saved"),
                     ("count", "📊 Count"),
                     ("help", "❓ Help")])
        if low == "count":
            return _handle_count(user)
        m = re.match(r"latest (\d+)(?:\s+from\s+(.+))?", low)
        if m:
            return _handle_latest(user, int(m.group(1)),
                                  (m.group(2) or "").strip() or None)
        m = re.match(r"from\s+(.+)", low)
        if m:
            return _handle_latest(user, 5, m.group(1).strip())
        m = re.match(r"search\s+(.+)", low)
        if m:
            return _handle_search(user, m.group(1).strip())
        m = re.match(r"(?:open|show) (\d+)", low)
        if m:
            return _handle_open(user, int(m.group(1)))
        if low == "today":
            return _handle_today(user)
        # Casual chat ("thanks mailbox", "who are you") is answered
        # as chat — it must not reach the mail search, which would
        # answer a thank-you with "No letters matching...".
        kind = _match_conversational(text)
        if kind is not None:
            return _conversational_reply(user, kind)
        # Natural language last: "I want to see today's Barber Shop
        # mail" matches no strict command, and must not fall through
        # to the generic help text while the letter sits unread.
        natural = _try_natural_mail(user, text)
        if natural is not None:
            return natural
        return _help_text()
    return "Sorry, I didn't understand that. Send a photo of your license to continue."


def _mail_line(m):
    """One digest-style line for a search/latest result, newest first."""
    subject = m.get("subject") or (m.get("body", "")[:60])
    line = (f"{m['id']}. {_cat_label(m['category'])} — "
            f"*{m['sender']}* — {subject}")
    if m["category"] == "bill":
        line += f" (due {m.get('due', '—')}, {m.get('amount', '—')})"
    if m.get("scam"):
        line += " ⚠ Possible scam"
    if m.get("attachment_media_id"):
        line += " 📎"
    if m.get("created_at"):
        line += f" ({m['created_at'][:10]})"
    return line


def _mail_footer():
    return (_DIV + "\nReply OPEN <number> to read one fully, READ <number> "
            "to hear it, SHRED <number> to shred it.")


def _handle_count(user):
    all_items = state.get_mail(user["mailbox"], include_shredded=True)
    live = [m for m in all_items if not m["shredded"]]
    shredded = len(all_items) - len(live)
    return (f"*📬 You have received {len(all_items)} letters in total: "
            f"{len(live)} still in your mailbox, {shredded} shredded & "
            f"recycled.*\n{_DIV}\nReply MAIL for the digest, FROM "
            "<business> for one sender, or SEARCH <word> to find an old "
            "letter.")


def _handle_latest(user, n, sender=None):
    """Newest-first slice of the mailbox, optionally one sender only.

    Sender match is case-insensitive and exact (ShopMart matches ShopMart,
    not a letter that merely mentions ShopMart in its body). Shredded
    letters are excluded: shred means recycled, not archived.
    """
    items = state.get_mail(user["mailbox"])
    if sender:
        items = [m for m in items if m["sender"].lower() == sender.lower()]
    items.sort(key=lambda m: m["id"], reverse=True)
    total = len(items)
    shown = items[:max(n, 1)]
    if not shown:
        if sender:
            return (f"No mail from '{sender}' in your mailbox. Try SEARCH "
                    "with a word from the letter, e.g. SEARCH coupon.")
        return "Your mailbox is empty today. New mail will appear here."
    display = items[0]["sender"] if sender else None
    head = (f"Latest {len(shown)} from {display} ({total} total, "
            "newest first):" if sender else
            f"Latest {len(shown)} letters (newest first):")
    lines = [f"*{head}*"] + [_mail_line(m) for m in shown]
    if total > len(shown):
        lines.append(f"…and {total - len(shown)} more. Reply FROM "
                     f"{display} or SEARCH to narrow it down."
                     if sender else
                     f"…and {total - len(shown)} more in your mailbox.")
    lines.append(_mail_footer())
    return "\n".join(lines)


def _handle_search(user, q):
    """Archive search (sender/subject/body, newest first, unshredded).

    No date filter: letters stored before the created_at change carry
    no date, so recency is read from newest-first order plus the date
    shown on newer letters.
    """
    results = state.search_mail(user["mailbox"], q)
    if not results:
        return (f"🔍 No letters matching '{q}'. Try a shorter word — the "
                "business name, or a word from the subject like 'coupon'.")
    shown = results[:10]
    lines = [f"*Found {len(results)} matching '{q}' (newest first):*"]
    lines += [_mail_line(m) for m in shown]
    if len(results) > len(shown):
        lines.append(f"…showing the newest {len(shown)}.")
    lines.append(_mail_footer())
    return "\n".join(lines)


def _attachments_dir():
    """Attachment files live next to the database, so one folder copy
    backs up mail + attachments together. Never inside the repo:
    .gitignore excludes attachments/, so the public flip can't leak
    coupons, statements, or IRS documents."""
    return os.path.join(
        os.path.dirname(os.path.abspath(config.DB_PATH)), "attachments")


def _save_attachment(media_id, filename, data):
    """Durable local copy of an uploaded attachment. Returns the path,
    or None when the save fails (the WhatsApp send still proceeds —
    the stored media id remains the fallback)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                  os.path.basename(filename) or "attachment")
    try:
        d = _attachments_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{media_id}_{safe}")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except OSError as exc:
        log.warning("attachment local save failed: %s", exc)
        return None


def _try_resend_attachment(phone, m):
    """Re-send a stored attachment with a reopened letter. Local bytes
    first (durable even if WhatsApp drops old media); the stored
    WhatsApp media id second (works while WhatsApp retains it).
    Returns True when a file was sent; False means the caller must
    fall back to the original-message note. Never raises."""
    mime = m.get("attachment_mime") or ""
    media_id = m.get("attachment_media_id")
    if not media_id and not m.get("attachment_path"):
        return False
    try:
        path = m.get("attachment_path")
        if path and os.path.exists(path):
            media_id = whatsapp.upload_media(path, mime)
        if not media_id:
            return False
        caption = (f"📎 Attachment for letter #{m['id']} "
                   f"from {m['sender']}")
        if mime.startswith("image/"):
            whatsapp.send_image_id(phone, media_id, caption=caption)
        elif mime == "application/pdf":
            whatsapp.send_document_id(
                phone, media_id, caption=caption,
                filename=m.get("attachment_name") or "attachment")
        elif mime.startswith("video/"):
            whatsapp.send_video_id(phone, media_id, caption=caption)
        else:
            return False
        return True
    except Exception as exc:
        log.warning("attachment re-send failed for letter %s: %s",
                    m["id"], exc)
        return False


def _handle_open(user, mail_id):
    """Full letter on screen: sender, subject, body, bill details,
    scam flag, attachment marker. Mailbox-checked like SHRED, and a
    shredded letter is gone (recycled), so it refuses those too."""
    m = state.get_mail_by_id(mail_id)
    if (m is None or m.get("shredded")
            or state.normalize_address(m["mailbox"])
            != state.normalize_address(user["mailbox"])):
        return f"Letter #{mail_id} is already shredded or not in your mailbox."
    header = f"📬 Letter #{m['id']} from {m['sender']}"
    if state.get_verified_business(m["sender"]):
        header += " ✓ Verified sender"
    lines = [f"*{header}*"]
    lines.append(f"*Subject:* {m.get('subject') or '—'}")
    lines.append(_cat_label(m["category"])
                 + (f" — due {m.get('due', '—')}, {m.get('amount', '—')}"
                    if m["category"] == "bill" else ""))
    if m.get("scam"):
        lines.append("⚠ Possible scam — do not send money.")
    lines.append("")
    lines.append(m.get("body", "") or "(no body text)")
    if m.get("attachment_media_id") or m.get("attachment_path"):
        # Reopen shows the file again, not just a pointer to the
        # original message: the file was saved locally at arrival, or
        # falls back to the stored WhatsApp media id. Only when both
        # fail does the user need the original message.
        if _try_resend_attachment(user["phone"], m):
            lines.append("")
            lines.append("📎 Attachment sent above.")
        else:
            lines.append("")
            lines.append("📎 This letter has an attachment "
                         "(see the original message).")
    lines.append(_DIV)
    lines.append(f"Reply SHRED {mail_id} to shred it, "
                 f"or READ {mail_id} to hear it.")
    return "\n".join(lines)


def _is_today(m):
    """True when the letter's recorded date is the server's local day.
    Dateless rows (stored before created_at existed) are never today."""
    created = m.get("created_at")
    if not created:
        return False
    try:
        return datetime.fromisoformat(created).astimezone().date() == date.today()
    except ValueError:
        return False


def _handle_today(user):
    """Today's letters only, newest first. 'Today' is the server's
    local day; letters stored before created_at existed carry no date
    and never appear here — LATEST covers those."""
    items = [m for m in state.get_mail(user["mailbox"]) if _is_today(m)]
    if not items:
        return ("No mail arrived today yet. Reply MAIL for the full "
                "digest, or LATEST 5 for the newest letters.")
    items.sort(key=lambda m: m["id"], reverse=True)
    lines = [f"*📬 Today's mail: {len(items)} letters (newest first):*"]
    lines += [_mail_line(m) for m in items]
    lines.append(_DIV + "\nReply OPEN <number> to read one fully, "
                 "READ <number> to hear it, SHRED <number> to shred it.")
    return "\n".join(lines)


# Filler words dropped when reading a natural mail request.
_NL_STOP = {"i", "me", "my", "please", "show", "see", "want", "get",
            "find", "give", "display", "open", "check", "look", "today",
            "todays", "mail", "mails", "letter", "letters", "email",
            "emails", "the", "a", "an", "for", "from", "of", "to",
            "that", "this", "one", "about", "in", "on", "is", "are",
            "you", "your", "latest", "newest", "recent", "last",
            "first", "all", "any", "and"}


def _parse_specific_mail(user, text):
    """Natural mail request -> (sender, today_only, keyword) or None.

    Sender comes from the mailbox's own senders, matched forgivingly:
    the full name ("Barber Shop"), a distinctive word ("ShopMart"), or
    a singular/plural near-miss ("barber shop" finds "Barber Shop").
    Keyword is what's left after filler words, for the archive search.
    Returns None when the text names neither a sender nor a keyword.
    """
    t = text.lower()
    today_only = "today" in t
    sender = None
    m = re.search(r"\bfrom\s+([a-z0-9 &.'-]+)", t)
    if m:
        parts = m.group(1).split()
        while parts and parts[-1] in _NL_STOP:
            parts.pop()
        sender = " ".join(parts) or None
    if sender is None:
        words = re.findall(r"[a-z]+", t)
        senders = sorted({m["sender"]
                          for m in state.get_mail(user["mailbox"])})
        for s in senders:
            slow = s.lower()
            if slow in t:
                sender = s
                break
            for sw in slow.split():
                if sw in words:
                    sender = s
                    break
                # Singular/plural near-miss only ("clip" finds "Clips").
                # A bare prefix match is wrong: "shop" must not match
                # "shopmart", or "Barber Shop" would steal "ShopMart"
                # mail.
                if len(sw) >= 4 and any(
                        (w == sw + "s" or sw == w + "s")
                        for w in words if len(w) >= 4):
                    sender = s
                    break
            if sender:
                break
    words = [w for w in re.findall(r"[a-z]+", t)
             if w not in _NL_STOP and len(w) > 2]
    keyword = " ".join(words) if words else None
    if sender is None and keyword is None:
        return None
    return (sender, today_only, keyword)


def _handle_specific_mail(user, sender, today_only):
    """One business's mail: today's first, else the latest with a date
    note. A single match opens in full on screen; several list with
    OPEN hints."""
    items = state.get_mail(user["mailbox"])
    want = sender.lower()
    matched = [m for m in items if want in m["sender"].lower()]
    if not matched:
        return (f"No mail from '{sender}' in your mailbox. Reply MAIL "
                "for the digest, or SEARCH with a word from the letter.")
    matched.sort(key=lambda m: m["id"], reverse=True)
    note = None
    if today_only:
        todays = [m for m in matched if _is_today(m)]
        if todays:
            matched = todays
        else:
            newest = matched[0]
            stamp = (newest.get("created_at") or "")[:10] or "no date recorded"
            note = (f"No {sender} mail arrived today — here's the "
                    f"latest one ({stamp}):")
    if len(matched) == 1:
        opened = _handle_open(user, matched[0]["id"])
        return f"{note}\n\n{opened}" if note else opened
    head = note or f"Found {len(matched)} from {sender} (newest first):"
    lines = [f"*{head}*"]
    lines += [_mail_line(m) for m in matched[:5]]
    if len(matched) > 5:
        lines.append("…showing the newest 5.")
    lines.append(_DIV + "\nReply OPEN <number> to read one fully, "
                 "READ <number> to hear it, SHRED <number> to shred it.")
    return "\n".join(lines)


def _match_conversational(text):
    """Casual chat -> kind, or None. Exact-ish matching on purpose:
    "hi, show me my mail" must still reach the mail request, not a
    greeting reply, so greetings/acks match whole messages only."""
    t = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()
    if not t:
        return None
    if t.startswith("thank") or t == "thx" or t.startswith("thx "):
        return "thanks"
    if t in ("hi", "hello", "hey", "hii", "hiii", "hi mailbox",
             "hello mailbox", "hey mailbox", "good morning",
             "good afternoon", "good evening", "namaste"):
        return "greeting"
    if t in ("bye", "goodbye", "bye mailbox", "see you",
             "see you later", "good night"):
        return "farewell"
    if t in ("ok", "okay", "ok mailbox", "got it", "good", "great",
             "nice", "perfect", "cool", "awesome", "sounds good"):
        return "ack"
    if (t == "help" or t.startswith("help ")
            or "who are you" in t or "what are you" in t
            or "what is mailbox" in t or "what s mailbox" in t
            or "what is this" in t or "what can you do" in t
            or "what do you do" in t or "how do i use" in t
            or "how to use" in t or "how does this work" in t
            or "how does mailbox" in t or "tell me about yourself" in t
            or "introduce yourself" in t):
        return "identity"
    return None


def _identity_guide(mailbox):
    """Who MailboxAI is + how to use it, so a new or confused user
    can adjust without memorizing commands."""
    lines = [
        "I'm MailboxAI — your physical mailbox, inside WhatsApp. 📬",
        "",
        "No app to install, no paper piles. Businesses send letters "
        "to your mailbox address, and I deliver them here — sorted "
        "into bill, urgent, junk, or personal, with scam flags.",
        "",
        "*What you can say or type:*",
        "• MAIL — today's digest",
        "• TODAY — only today's letters",
        "• OPEN <number> — read a letter fully on screen",
        "• READ <number> — hear it as a voice note",
        "• SHRED <number> — recycle it (the button asks first)",
        "• COUNT, FROM <business>, SEARCH <word>, LATEST <number>",
        "• Or just ask naturally: \"show me today's Barber Shop mail\"",
    ]
    if mailbox:
        lines += ["", f"*Your mailing address is:*\n{mailbox}"]
    return "\n".join(lines)


def _conversational_reply(user, kind):
    """Short human replies for casual chat (verified users)."""
    if kind == "thanks":
        return ("You're welcome! 😊 Your mail is safe here — send MAIL "
                "anytime to see your letters.")
    if kind == "greeting":
        return ("Hi! 👋 Send MAIL to see your letters, or just ask — "
                "like \"show me today's mail\".")
    if kind == "farewell":
        return ("Bye! 👋 Your mail will be waiting here whenever "
                "you're back.")
    if kind == "ack":
        return "👍 Got it."
    if kind == "identity":
        guide = _identity_guide(user.get("mailbox") if user else None)
        return guide + "\n\nSend MAIL anytime to see your letters."
    return None
def _try_natural_mail(user, text):
    """Natural-language fallback for typed + voice mail requests.

    Runs only after every strict command failed, so it never hijacks
    COUNT/READ/SHRED/OPEN/etc. Returns None when the text is not a
    mail request — the caller then falls back to the help text (typed)
    or Wonder (voice).
    """
    t = text.lower()
    if not any(w in t for w in ("mail", "letter", "email", "coupon",
                                "bill", "today", "from", "show", "see",
                                "want", "get", "find", "open", "display",
                                "give", "latest", "last")):
        return None
    parsed = _parse_specific_mail(user, text)
    if parsed is None:
        return None
    sender, today_only, keyword = parsed
    if sender:
        return _handle_specific_mail(user, sender, today_only)
    return _voice_search(user, keyword)


_VOICE_STOP = {"my", "the", "a", "an", "please", "show", "me", "find",
               "get", "for", "letter", "letters", "mail", "mails",
               "email", "emails", "latest", "newest", "recent", "all",
               "any", "from", "in", "of", "to", "that", "this", "one",
               "about"}
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _voice_num(tok):
    return int(tok) if tok.isdigit() else _NUM_WORDS.get(tok)


def _parse_voice_command(text):
    """Spoken words -> a typed command, ("search", phrase), or None.

    Natural phrasing only: "latest two mails from ShopMart" becomes
    "latest 2 from shopmart", "how many mails do I have" becomes
    "count". Anything unrecognized returns None and the caller says
    what it heard plus the command list — never a silent guess.
    """
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()
    if not t:
        return None
    if ("how many" in t or t == "count" or "number of mails" in t
            or "number of letters" in t):
        return "count"
    m = re.search(r"\bread (?:letter |mail )?(\d+|one|two|three|four|five"
                  r"|six|seven|eight|nine|ten)\b", t)
    if m:
        return f"read {_voice_num(m.group(1))}"
    m = re.search(r"\bshred (?:letter |mail )?(\d+|one|two|three|four|five"
                  r"|six|seven|eight|nine|ten)\b", t)
    if m:
        return f"shred {_voice_num(m.group(1))}"
    m = re.search(r"\b(?:open|show) (?:letter |mail )?(\d+|one|two|three"
                  r"|four|five|six|seven|eight|nine|ten)\b", t)
    if m:
        return f"open {_voice_num(m.group(1))}"
    m = re.search(r"\blatest\s+(\d+|one|two|three|four|five|six|seven"
                  r"|eight|nine|ten)?\s*(?:mails?|letters?)?\s*from\s+(.+)",
                  t)
    if m:
        n = _voice_num(m.group(1)) if m.group(1) else 5
        sender = re.sub(r"\s+(please|thanks|thank you)$", "",
                        m.group(2).strip())
        return f"latest {n} from {sender}"
    m = re.search(r"\blast\s+(?:mail|letter)?\s*from\s+(.+)", t)
    if m:
        sender = re.sub(r"\s+(please|thanks|thank you)$", "",
                        m.group(1).strip())
        if sender:
            return f"latest 1 from {sender}"
    if re.search(r"\blast\s+(?:mail|letter)$", t):
        return "latest 1"
    # "today's mail" normalizes to "today s mail" (apostrophe dropped).
    if "today" in t and ("mail" in t or "letter" in t or t == "today"):
        return "today"
    if "paper saved" in t or t == "saved":
        return "saved"
    if ("digest" in t or t == "mail" or t.endswith(" mail")
            or t.endswith(" mails")):
        return "mail"
    m = re.search(r"\bfrom\s+(.+)", t)
    if m:
        sender = re.sub(r"\s+(please|thanks|thank you)$", "",
                        m.group(1).strip())
        if sender:
            return f"from {sender}"
    m = re.search(r"\b(?:find|search|look for|get)\b\s*(.+)", t)
    if m:
        return ("search", m.group(1).strip())
    if "coupon" in t:
        return ("search", "coupon")
    return None


def _voice_search(user, phrase):
    """Voice search is forgiving where typed SEARCH is exact: it tries
    the whole phrase, then each content word, and reports the word
    that matched — so "find my Barber Shop coupon" still finds the
    coupon even though no letter contains that exact phrase."""
    words = [w for w in re.sub(r"[^\w\s]", " ", phrase.lower()).split()
             if w not in _VOICE_STOP and len(w) > 2]
    tries = []
    for cand in [phrase.strip()] + words:
        if cand and cand not in tries:
            tries.append(cand)
        # Singular fallback: "bills" also tries "bill", "coupons"
        # tries "coupon" — spoken plurals still find the letter.
        if cand and cand.endswith("s") and len(cand) > 3 \
                and cand[:-1] not in tries:
            tries.append(cand[:-1])
    for cand in tries:
        results = state.search_mail(user["mailbox"], cand)
        if results:
            shown = results[:10]
            lines = [f"*Found {len(results)} matching '{cand}' "
                     "(newest first):*"]
            lines += [_mail_line(m) for m in shown]
            if len(results) > len(shown):
                lines.append(f"…showing the newest {len(shown)}.")
            lines.append(_mail_footer())
            return "\n".join(lines)
    return (f"🔍 No letters matching '{phrase}'. Try a shorter word — the "
            "business name, or a word from the subject like 'coupon'.")


def _handle_audio(phone, user, media_id):
    """A voice note from a verified user: transcribe it with Meta's
    Voice Transcribe, then run the words as a command. Text replies
    echo what was heard; a READ reply goes back as a voice note via
    the existing dispatcher path. Failures always answer in text."""
    if user is None or user.get("state") != "VERIFIED":
        return ("Voice commands work after verification — please send "
                "your name or license photo as text or a photo for now.")
    try:
        audio = whatsapp.download_media(media_id)
    except Exception:
        return ("I couldn't download that voice note. Please try again "
                "or type your command.")
    if not audio:
        return ("That voice note came through empty. Please try again "
                "or type your command.")
    result = voice_in_mod.transcribe(audio)
    if not result.get("ok"):
        hints = {
            "not_configured": "voice transcription isn't set up yet "
                              "(Meta Model API key / base URL missing)",
            "no_ffmpeg": "ffmpeg is missing on the server",
            "convert_failed": "the audio couldn't be converted",
            "empty_transcript": "nothing intelligible was heard",
            "transcribe_failed": "Meta's transcription didn't answer",
        }
        return (f"I couldn't hear that clearly "
                f"({hints.get(result.get('reason'), 'transcription failed')}). "
                "Please type your command — MAIL, TODAY, COUNT, FROM ShopMart, "
                "SEARCH coupon, LATEST 2, OPEN <number>, or READ <number>.")
    heard = result["text"]
    # Casual voice chat ("thanks", "who are you") is answered as
    # chat, before command parsing or Wonder.
    kind = _match_conversational(heard)
    if kind is not None:
        return f'🎙️ Heard: "{heard}"\n\n{_conversational_reply(user, kind)}'
    parsed = _parse_voice_command(heard)
    if parsed is None or parsed in ("today", "mail"):
        # A natural request ("today's Barber Shop mail") parses as
        # plain "today" — try the sender-aware reading first, so the
        # actual letter wins over the whole-day list.
        specific = _try_natural_mail(user, heard)
        if specific is not None:
            return f'🎙️ Heard: "{heard}"\n\n{specific}'
    if parsed is None:
        # Not a command: a question for Muse Wonder (transcript +
        # mailbox rows -> spoken-style answer -> voice note back).
        # Wonder unavailable -> the old command hint, never silence.
        try:
            from . import wonder as wonder_mod
            answer = wonder_mod.answer_question(
                heard, state.get_mail(user["mailbox"]))
        except Exception:
            answer = None
        if answer:
            out_path = os.path.join(tempfile.gettempdir(),
                                    "mailboxai_wonder.ogg")
            if voice_mod.synthesize(answer[:1000], out_path):
                return ("audio", out_path, answer)
            return f'🎙️ Heard: "{heard}"\n\n{answer}'
        return (f'🎙️ I heard: "{heard}". I can run MAIL, COUNT, FROM '
                "<business>, SEARCH <word>, LATEST <number>, READ "
                "<number> or SHRED <number> — say one of those, or just "
                "ask for a letter, like \"today's Barber Shop mail\".")
    reply = (_voice_search(user, parsed[1]) if isinstance(parsed, tuple)
             else _handle_text(phone, user, parsed))
    if isinstance(reply, tuple) and reply[0] == "buttons":
        # Voice MAIL: the card as text, still prefixed with what was
        # heard — buttons need a tap, and a voice user is not tapping.
        return f'🎙️ Heard: "{heard}"\n\n{reply[1]}'
    if isinstance(reply, tuple):  # READ -> voice note, sent as-is
        return reply
    return f'🎙️ Heard: "{heard}"\n\n{reply}'


def _complete_verification(phone, user, name, address):
    """Join or create the household mailbox; returns the VERIFIED reply.

    On a join, every existing member gets a short alert (best-effort:
    a failed send never blocks the verification itself). The alert
    names the joiner so the household knows who can now see its mail.
    """
    mailbox = state.find_mailbox_by_address(address)
    if mailbox:
        state.save_user(phone, name=name, address=address,
                        mailbox=mailbox, state="VERIFIED")
        for member in state.members_of(mailbox):
            if member.get("phone") == phone:
                continue
            try:
                whatsapp.send_text(
                    member["phone"],
                    f"🏠 {name} just joined your household mailbox.\n"
                    "Mail for this address will now reach them too.")
            except Exception as exc:
                log.warning("join alert failed for %s: %s",
                            member.get("phone"), exc)
        return (f"✅ Verified! Your license address matches an existing "
                f"household mailbox, so you've joined it:\n{mailbox}\n"
                "Mail for this address will now reach you here.")
    mailbox = state.new_mailbox(address)
    state.save_user(phone, name=name, address=address,
                    mailbox=mailbox, state="VERIFIED")
    return (f"✅ Verified!\nName: {name}\nAddress: {address}\n\n"
            f"Your new mailing address is:\n{mailbox}\n"
            "Give this address to your bank and utilities. Family members "
            "whose license shows this same address can join this mailbox.")


def _license_or_reset(phone, user):
    """License bytes for the selfie match, or None (state reset).

    A server restart drops the in-memory photo; the user then resends
    the license instead of hitting a dead end.
    """
    image = _LICENSE_PHOTOS.get(phone)
    if not image:
        state.save_user(phone, state="AWAITING_LICENSE")
    return image


def _handle_image(phone, user, media_id):
    if user is None:
        return "Please start by sending Hi, then your name, then the license photo."
    if user["state"] == "AWAITING_SELFIE":
        return _handle_challenge_image(phone, user)
    if user["state"] == "VERIFIED":
        return ("You're already verified — no need to send your license "
                "again. Send MAIL to see your mail.")
    if user["state"] != "AWAITING_LICENSE":
        return "Please start by sending Hi, then your name, then the license photo."
    try:
        image = whatsapp.download_media(media_id)
    except Exception as exc:
        log.warning("media download failed: %s", exc)
        image = b""
    result = license_mod.verify_license(image, expected_name=user.get("name"))
    if not result.get("ok"):
        return ("I couldn't verify that license photo. " +
                result.get("detail", "Please retake it clearly and resend."))
    # An expiry that parses cleanly and is already past rejects here —
    # before any mailbox is created. An unparseable expiry never
    # rejects (see license.is_expired); mock mode returns no expiry,
    # so this is a no-op in mock mode.
    if license_mod.is_expired(result.get("expiry")):
        return ("This driver's license appears to be expired"
                + (f" (expiry shown: {result['expiry']})"
                   if result.get("expiry") else "")
                + ", so I can't use it to verify you. Please send a "
                  "photo of your current, unexpired license.")
    # Demo mode skips the selfie step: the "license photo" is a stand-in,
    # so a face match against it would be meaningless. Real mode requires
    # the selfie before the mailbox is created.
    if config.MOCK_VERIFY:
        return _complete_verification(phone, user, result["name"],
                                      result["address"])
    _LICENSE_PHOTOS[phone] = image
    state.save_user(phone, name=result["name"], address=result["address"],
                    state="AWAITING_SELFIE")
    return _challenge_prompt(phone)


_CHALLENGE_TTL = 180  # seconds a spoken code stays valid

# Verification-attempt lockout: repeated failures pause verification
# for one phone, so a stolen license can't be brute-forced with video
# after video. In-memory like the challenges themselves, so a server
# restart clears it (the escape hatch if a real user ever trips it).
_LOCKOUT_MAX = 5       # failed attempts ...
_LOCKOUT_WINDOW = 600  # ... within 10 minutes ...
_LOCKOUT_FOR = 600     # ... pause verification for 10 minutes
_FAILED_ATTEMPTS = {}  # phone -> [monotonic timestamps of failures]
_LOCKOUTS = {}         # phone -> monotonic timestamp the pause ends


def _is_locked(phone):
    until = _LOCKOUTS.get(phone)
    if until and until > time.monotonic():
        return True
    _LOCKOUTS.pop(phone, None)
    return False


def _record_failure(phone):
    """Record one failed verification attempt.

    Returns True when this failure trips the lockout. Only real
    verification failures count (liveness / face / spoken-code
    failures via _fail) — server problems, expired codes, and photo
    rejections never count, so a real user is never locked out by
    an outage or a mistake.
    """
    now = time.monotonic()
    attempts = [t for t in _FAILED_ATTEMPTS.get(phone, [])
                if now - t < _LOCKOUT_WINDOW]
    attempts.append(now)
    _FAILED_ATTEMPTS[phone] = attempts
    if len(attempts) >= _LOCKOUT_MAX:
        _LOCKOUTS[phone] = now + _LOCKOUT_FOR
        _FAILED_ATTEMPTS.pop(phone, None)
        return True
    return False


def _new_code():
    return f"{secrets.randbelow(10000):04d}"


def _issue_challenge(phone):
    code = _new_code()
    _CHALLENGES[phone] = {"code": code,
                          "expires": time.monotonic() + _CHALLENGE_TTL}
    return code


def _challenge_prompt(phone):
    """Current valid code, or a fresh one if expired/missing.

    The code is shown dashed (7-3-9-1) so it is easy to say digit by
    digit. A still-valid code is reused for retries; a used or
    expired code is never shown again.
    """
    chal = _CHALLENGES.get(phone)
    if not chal or chal["expires"] <= time.monotonic():
        code = _issue_challenge(phone)
    else:
        code = chal["code"]
    dashed = "-".join(code)
    return (f"Last step for security: send a short video of yourself — "
            f"look at the camera and say the numbers {dashed}. "
            "A photo alone is not accepted. I'll match your face, "
            "check you're live, and check the code.")


def _handle_challenge_image(phone, user):
    """Photos are rejected in the challenge step — video only.

    A photo can't prove liveness and can't speak a fresh code, so
    accepting one would reopen the exact attack this step closes.
    """
    return ("A photo can't prove you're live, so it's not accepted "
            "here. " + _challenge_prompt(phone))


def _normalize_code(text):
    """Speech -> digits: 'seven three nine one' and '7391' both
    become '7391'. Words are matched whole, so 'seventy' never
    becomes '7'. Returns the digit string (possibly empty)."""
    words = {"zero": "0", "oh": "0", "one": "1", "two": "2",
             "three": "3", "four": "4", "five": "5", "six": "6",
             "seven": "7", "eight": "8", "nine": "9"}
    t = text.lower()
    for word, digit in words.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    return re.sub(r"\D", "", t)


def _handle_video(phone, user, media_id):
    """Challenge video: liveness + face match + spoken code, all three.

    Any failure burns the code and issues a fresh one, so a recorded
    correct answer can never be replayed on a later attempt. A
    missing/expired code is never verified against — the user gets a
    fresh code instead. Five failures within 10 minutes pause
    verification for that phone for 10 minutes (see _record_failure).
    Photo selfies no longer exist as a path.
    """
    if user is None:
        return "Please start by sending Hi, then your name, then the license photo."
    if user["state"] == "VERIFIED":
        return ("You're already verified — no need to send another video. "
                "Send MAIL to see your mail.")
    if user["state"] != "AWAITING_SELFIE":
        return "Please start by sending Hi, then your name, then the license photo."
    if _is_locked(phone):
        return ("Too many failed verification attempts — verification "
                "is paused for 10 minutes. Please try again after that.")
    image = _license_or_reset(phone, user)
    if not image:
        _LICENSE_PHOTOS.pop(phone, None)
        _CHALLENGES.pop(phone, None)
        return ("I lost your license photo when the server restarted — "
                "please send it again, then your selfie video.")
    chal = _CHALLENGES.get(phone)
    if not chal or chal["expires"] <= time.monotonic():
        _CHALLENGES.pop(phone, None)
        return ("That code expired — here's a fresh one. "
                + _challenge_prompt(phone))
    code = chal["code"]
    try:
        video = whatsapp.download_media(media_id)
    except Exception as exc:
        log.warning("video download failed: %s", exc)
        video = b""

    def _fail(msg):
        _CHALLENGES.pop(phone, None)
        if _record_failure(phone):
            return (msg + " Too many failed verification attempts — "
                    "verification is paused for 10 minutes. "
                    "Please try again after that.")
        return msg + " " + _challenge_prompt(phone)

    live = face_mod.check_liveness(video)
    if not live.get("ok"):
        if live.get("reason") == "no_ffmpeg":
            return ("Video liveness needs ffmpeg on the server "
                    "(`brew install ffmpeg`). " + _challenge_prompt(phone))
        return _fail("I couldn't confirm a live person in that video "
                     f"({live.get('reason', 'no movement seen')}). "
                     "Please retake it.")
    if not live.get("live"):
        return _fail("I couldn't confirm a live person in that video "
                     f"({live.get('reason', 'no movement seen')}). "
                     "Please retake it.")
    frames = face_mod.extract_frames(video)
    # Best of all frames: one blink/blur frame must not fail a real user.
    result = face_mod.match_faces_best(image, frames)
    if not result.get("ok"):
        return ("Face check is unavailable right now — please try again "
                "in a bit. " + _challenge_prompt(phone))
    if not result.get("match"):
        return _fail("That video doesn't match the license photo "
                     f"({result.get('reason', 'no match')}). "
                     "Please retake it.")
    spoken = voice_in_mod.transcribe_video(video)
    if not spoken.get("ok"):
        if spoken.get("reason") == "not_configured":
            return ("Spoken-code check needs the Meta Model API key "
                    "(META_MODEL_API_BASE_URL / META_MODEL_API_KEY) — "
                    "it's not set. Set it, restart, and send the video again.")
        return _fail("I couldn't hear the code in that video. "
                     "Please retake it, saying the numbers clearly.")
    if code not in _normalize_code(spoken["text"]):
        return _fail("The spoken code didn't match. Please retake it "
                     "with the new code below.")
    _CHALLENGES.pop(phone, None)
    _LICENSE_PHOTOS.pop(phone, None)
    _FAILED_ATTEMPTS.pop(phone, None)
    _LOCKOUTS.pop(phone, None)
    reply = _complete_verification(phone, user, user.get("name"),
                                   user.get("address"))
    return reply + "\n\n✓ Live video, face matched, and spoken code verified."


def _handle_read(phone, user, mail_id):
    """READ <n>: the letter as a voice note, or as text if audio fails.

    Returns a text reply, or ("audio", path, fallback_text) for the
    webhook dispatcher to upload + send as a voice note (falling back
    to the text if the upload/send fails — never silence).
    """
    items = state.get_mail(user["mailbox"])
    item = next((m for m in items if m["id"] == mail_id), None)
    if not item:
        return f"No letter #{mail_id} in your mailbox."
    text = (f"Letter from {item['sender']}: "
            f"{item.get('subject') or ''}. {item.get('body', '')}".strip())
    out_path = os.path.join(tempfile.gettempdir(),
                            f"mailboxai_read_{mail_id}.ogg")
    if voice_mod.synthesize(text[:1000], out_path):
        return ("audio", out_path, text)
    return text


def _handle_button(phone, user, button_id):
    """Interactive button taps: shred_<id>, confirm_shred_<id>,
    cancel_shred_<id>, read_<id>, open_<id>, plus the digest quick
    replies saved, count, help.

    The Shred button NEVER deletes on first tap: it asks first, and
    only the confirm tap shreds. A stray tap (or a tap on the wrong
    letter) is harmless — ignoring the question keeps the letter."""
    if user is None or user["state"] != "VERIFIED":
        return "Please finish onboarding first (send Hi)."
    if button_id == "saved":
        return _paper_saved_text(user)
    if button_id == "count":
        return _handle_count(user)
    if button_id == "help":
        return _help_text()
    if button_id.startswith("shred_"):
        try:
            mail_id = int(button_id.split("_", 1)[1])
        except ValueError:
            return None
        # Mailbox-checked: only a letter actually in this mailbox can
        # even reach the confirmation question.
        m = state.get_mail_by_id(mail_id)
        if (m is None or m.get("shredded")
                or state.normalize_address(m["mailbox"])
                != state.normalize_address(user["mailbox"])):
            return f"Letter #{mail_id} is already shredded or not in your mailbox."
        return ("buttons",
                f"♻️ *Shred letter #{mail_id}?*\n"
                f"From {m['sender']} — '{m.get('subject') or '—'}'\n"
                "This recycles it; you won't be able to open it again.",
                [(f"confirm_shred_{mail_id}", "✅ Yes, shred"),
                 (f"cancel_shred_{mail_id}", "❌ Keep it")])
    if button_id.startswith("confirm_shred_"):
        try:
            mail_id = int(button_id.rsplit("_", 1)[1])
        except ValueError:
            return None
        # Re-checked at confirm time: the letter may have been
        # shredded by a typed SHRED while the question was open.
        items = state.get_mail(user["mailbox"])
        if not any(m["id"] == mail_id for m in items):
            return f"Letter #{mail_id} is already shredded or not in your mailbox."
        state.shred_mail(mail_id)
        saved = state.sheets_saved(user["mailbox"])
        return (f"♻️ Shredded & recycled. Your household has now saved "
                f"{saved} sheets of paper.")
    if button_id.startswith("cancel_shred_"):
        try:
            mail_id = int(button_id.rsplit("_", 1)[1])
        except ValueError:
            return None
        items = state.get_mail(user["mailbox"])
        if not any(m["id"] == mail_id for m in items):
            return f"Letter #{mail_id} is already shredded or not in your mailbox."
        return f"Kept it — letter #{mail_id} stays in your mailbox."
    if button_id.startswith("read_"):
        try:
            mail_id = int(button_id.split("_", 1)[1])
        except ValueError:
            return None
        return _handle_read(phone, user, mail_id)
    if button_id.startswith("open_"):
        try:
            mail_id = int(button_id.split("_", 1)[1])
        except ValueError:
            return None
        return _handle_open(user, mail_id)
    return None


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(
            config.WHATSAPP_APP_SECRET, raw,
            request.headers.get("X-Hub-Signature-256")):
        return PlainTextResponse("bad signature", status_code=403)
    payload = json.loads(raw or b"{}")
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if _is_replay(msg.get("id", "")):
                    log.info("duplicate webhook message ignored: %s",
                             msg.get("id", ""))
                    continue
                phone = msg.get("from", "")
                user = state.get_user(phone)
                reply = None
                if msg.get("type") == "text":
                    reply = _handle_text(phone, user, msg["text"]["body"])
                elif msg.get("type") == "image":
                    reply = _handle_image(phone, user, msg["image"]["id"])
                elif msg.get("type") == "video":
                    reply = _handle_video(phone, user, msg["video"]["id"])
                elif msg.get("type") == "audio":
                    reply = _handle_audio(phone, user, msg["audio"]["id"])
                elif msg.get("type") == "interactive":
                    btn = (msg.get("interactive", {}).get("button_reply", {})
                           or {}).get("id", "")
                    reply = _handle_button(phone, user, btn)
                if reply:
                    try:
                        if isinstance(reply, tuple) and reply[0] == "audio":
                            try:
                                media_id = whatsapp.upload_media(
                                    reply[1], "audio/ogg")
                                whatsapp.send_audio_id(phone, media_id)
                            except Exception:
                                whatsapp.send_text(phone, reply[2])
                            finally:
                                try:
                                    os.unlink(reply[1])
                                except OSError:
                                    pass
                        elif isinstance(reply, tuple) and reply[0] == "buttons":
                            # Button replies (shred confirmation): if the
                            # buttons fail to send, fall back to the
                            # question as plain text — the user can
                            # re-tap Shred for a fresh question.
                            try:
                                whatsapp.send_interactive_buttons(
                                    phone, reply[1], reply[2])
                            except Exception:
                                whatsapp.send_text(phone, reply[1])
                        else:
                            whatsapp.send_text(phone, reply)
                    except Exception as exc:
                        # A failed send must not 500 the webhook: Meta would
                        # retry the same message, and the 500 hides the real
                        # send error. Log it and ack the webhook instead.
                        log.error("reply send failed for %s: %s", phone, exc)
                _remember_message(msg.get("id", ""))
    return {"ok": True}


# WhatsApp Cloud API media limits. The app enforces them, so a
# business gets a clear error instead of a failed send later.
_ATTACHMENT_LIMITS = {
    "image/jpeg": 5 * 1024 * 1024,
    "image/png": 5 * 1024 * 1024,
    "application/pdf": 100 * 1024 * 1024,
    "video/mp4": 16 * 1024 * 1024,
}


def _validate_attachment(content_type, size):
    """Returns an error string, or None when the attachment is acceptable."""
    if not content_type or content_type not in _ATTACHMENT_LIMITS:
        return ("Unsupported file type. Send a JPEG/PNG image, a PDF, "
                "or an MP4 video.")
    if size <= 0:
        return "The attached file is empty."
    if size > _ATTACHMENT_LIMITS[content_type]:
        limit_mb = _ATTACHMENT_LIMITS[content_type] // (1024 * 1024)
        return (f"File too large for WhatsApp ({content_type}: "
                f"limit {limit_mb} MB).")
    return None


@app.post("/mail/upload")
def mail_upload(mailbox: str = Form(...), sender: str = Form(...),
                subject: str = Form(...), body_text: str = Form(""),
                attachment: UploadFile = File(None)):
    """Scan intake. A CMRA partner POSTs here when a letter is scanned;
    body_text is the OCR'd letter. When the upload carries a scan
    photo, SAM 3.1 segments it and the regions are returned in the
    response; per-region OCR is roadmap, not built yet. The letter is
    triaged and every household member is notified on WhatsApp. An
    optional attachment (scan photo, PDF statement, coupon image,
    promo video) is uploaded to WhatsApp and delivered as WhatsApp
    media alongside the notification."""
    # Smart address matching: the business may type the mailbox with
    # different commas, spaces, case, or abbreviations ("999 Sample Rd
    # Atlanta GA 30309"). Resolve to the household's stored mailbox
    # first, so rules, storage, and notifications all use one canonical
    # address. With no match the typed string stays (orphaned letter,
    # notified=0, as before).
    members = state.members_of(mailbox)
    if members:
        mailbox = members[0]["mailbox"]
    item = triage.triage_letter(sender, body_text)
    item.update({"subject": subject, "body": body_text})
    # Moat layer 1: the household's learned rules override AI triage.
    rule = state.get_rule(mailbox, sender)
    rule_applied = None
    shred_after = False
    if rule:
        rule_applied = rule["action"]
        if rule["action"] == "always_shred":
            item["category"] = "junk"
            shred_after = True
        elif rule["action"] == "always_urgent":
            item["category"] = "urgent"
    # Moat layer 2: verified-sender badge (address proven by postcard PIN).
    biz = state.get_verified_business(sender)
    verified_sender = biz is not None
    if biz and biz.get("membership") == "expired":
        # Membership fee unpaid: a verified business that stopped paying
        # can't send through MailboxAI until it renews.
        return JSONResponse(
            {"ok": False, "reason": "membership_expired",
             "detail": "Business membership expired — renew to send again."},
            status_code=402)
    # Attachment: validate, upload to WhatsApp, and store the media
    # id with the mail — before add_mail, so a failed upload stores
    # nothing and the business gets a clear error instead of a letter
    # that silently arrived without its attachment.
    attachment_info = None
    if attachment is not None and getattr(attachment, "filename", None):
        data = attachment.file.read()
        error = _validate_attachment(attachment.content_type, len(data))
        if error:
            return JSONResponse(
                {"ok": False, "reason": "attachment_rejected",
                 "detail": error}, status_code=400)
        safe_name = os.path.basename(attachment.filename) or "attachment"
        tmpdir = tempfile.mkdtemp()
        try:
            tmp_path = os.path.join(tmpdir, safe_name)
            with open(tmp_path, "wb") as f:
                f.write(data)
            try:
                media_id = whatsapp.upload_media(tmp_path,
                                                 attachment.content_type)
            except Exception as exc:
                log.error("attachment upload failed: %s", exc)
                return JSONResponse(
                    {"ok": False, "reason": "attachment_upload_failed",
                     "detail": "Could not upload the attachment to "
                               "WhatsApp; nothing was sent. Please retry."},
                    status_code=502)
            attachment_info = {"attachment_media_id": media_id,
                               "attachment_mime": attachment.content_type,
                               "attachment_name": safe_name,
                               # Durable copy: the file itself is saved
                               # next to the database, so OPEN can
                               # re-send it and the admin backup can
                               # include it. Without this, only
                               # WhatsApp's media id would exist.
                               "attachment_path": _save_attachment(
                                   media_id, safe_name, data)}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    # Segmentation runs on the real scan photo when one was attached
    # (JPEG/PNG); otherwise on the body text bytes, which the SAM
    # path skips and the fallback turns into one whole-page region.
    scan_bytes = body_text.encode()
    if attachment_info and attachment_info["attachment_mime"].startswith("image/"):
        scan_bytes = data
    regions = segment_mod.segment_scan(scan_bytes)
    if attachment_info:
        item.update(attachment_info)
    mail_id = state.add_mail(mailbox, item)
    if shred_after:
        state.shred_mail(mail_id)
    note = (f"📬 *New mail #{mail_id}*\n"
            f"*{sender}*" + (" ✓ Verified sender" if verified_sender else "")
            + f"\n{_cat_label(item['category'])} — {subject}"
            + (f"\nDue {item['due']} • {item['amount']}"
               if item["category"] == "bill" else "")
            + ("\n⚠ Possible scam — do not send money." if item["scam"] else "")
            + (f"\n(your rule applied: {rule_applied})" if rule_applied else "")
            + f"\nTap a button below, or reply SHRED {mail_id}.")
    for member in members:
        try:
            if attachment_info:
                # Media messages carry no buttons, so the file goes with
                # the full note as its caption, then the buttons follow
                # as a short second message.
                mime = attachment_info["attachment_mime"]
                media_id = attachment_info["attachment_media_id"]
                if mime.startswith("image/"):
                    whatsapp.send_image_id(member["phone"], media_id,
                                           caption=note)
                elif mime == "application/pdf":
                    whatsapp.send_document_id(
                        member["phone"], media_id, caption=note,
                        filename=attachment_info["attachment_name"])
                elif mime.startswith("video/"):
                    whatsapp.send_video_id(member["phone"], media_id,
                                           caption=note)
                whatsapp.send_interactive_buttons(
                    member["phone"],
                    "What would you like to do with this letter?",
                    [(f"shred_{mail_id}", "♻️ Shred"),
                     (f"read_{mail_id}", "🔊 Read aloud"),
                     (f"open_{mail_id}", "📖 Open")])
            else:
                whatsapp.send_interactive_buttons(
                    member["phone"], note,
                    [(f"shred_{mail_id}", "♻️ Shred"),
                     (f"read_{mail_id}", "🔊 Read aloud"),
                     (f"open_{mail_id}", "📖 Open")])
        except Exception:
            whatsapp.send_text(member["phone"], note)
    return {"mail_id": mail_id, "category": item["category"],
            "segments": regions, "notified": len(members),
            "verified_sender": verified_sender, "rule_applied": rule_applied,
            "membership": biz["membership"] if biz else None,
            "shredded": shred_after, "attachment": attachment_info}


@app.post("/business/register")
def business_register(name: str = Form(...), email: str = Form(...),
                      address: str = Form(...), password: str = Form("")):
    """Step 1: validate the business address. Step 2: a postcard PIN is
    'mailed' — the business proves receipt by calling /business/verify-pin.
    In dev mode the PIN is returned so the flow can be tested.
    An optional password enables the business portal login
    (/business-portal); re-registering with the same name/address and a
    password sets one for an older business."""
    v = business.validate_address(address)
    if not v["ok"]:
        return {"ok": False, "error": v["reason"], "detail": v["detail"]}
    biz_id = business.business_id(name, address)
    pin = business.new_pin(biz_id)
    pw_hash = _hash_password(password) if password else None
    state.save_business(biz_id, name, email, v["standardized"],
                        password_hash=pw_hash)
    out = {"ok": True, "business_id": biz_id,
           "standardized_address": v["standardized"],
           "validation_method": v["method"],
           "status": "postcard_sent",
           "note": v["note"]}
    if config.DEV_MODE:
        out["dev_pin"] = pin
    return out


@app.post("/business/verify-pin")
def business_verify_pin(business_id: str = Form(...), pin: str = Form(...)):
    ok = business.verify_pin(business_id, pin)
    if ok:
        state.verify_business(business_id)
    return {"ok": ok, "verified_sender": ok}


@app.post("/rules")
def add_sender_rule(mailbox: str = Form(...), sender_match: str = Form(...),
                    action: str = Form(...)):
    """A household teaches MailboxAI: 'always shred SuperSaver',
    'always mark First Demo Bank as urgent'. Rules override AI triage."""
    if action not in ("always_shred", "always_urgent"):
        return {"ok": False, "error": "action must be always_shred or always_urgent"}
    return {"ok": True,
            "rule_id": state.add_rule(mailbox, sender_match, action)}


@app.get("/mail/search")
def search_mail(mailbox: str, q: str):
    return {"mailbox": mailbox, "query": q, "results": state.search_mail(mailbox, q)}


@app.get("/digest/{mailbox}")
def get_digest(mailbox: str):
    items = state.get_mail(mailbox)
    return {"mailbox": mailbox,
            "digest": digest_mod.build_digest_text(items),
            "letters": items,
            "sheets_saved": state.sheets_saved(mailbox)}
