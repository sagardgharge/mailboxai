"""SQLite state: users, household membership, mail items, paper saved.

States: START -> AWAITING_NAME -> AWAITING_LICENSE -> VERIFIED.
A family member who verifies with an address that already has a mailbox
joins that mailbox instead of getting a new one — the household rule.
"""
import re
import sqlite3
from datetime import datetime, timezone

from . import config


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY, name TEXT, address TEXT,
            mailbox TEXT, state TEXT DEFAULT 'START')""")
        c.execute("""CREATE TABLE IF NOT EXISTS mail (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mailbox TEXT,
            sender TEXT, subject TEXT, body TEXT, category TEXT,
            due TEXT, amount TEXT, scam INTEGER DEFAULT 0,
            shredded INTEGER DEFAULT 0, created_at TEXT)""")
        # Migrate databases created before attachments existed: the
        # WhatsApp media id of a letter's attachment (scan, PDF, coupon
        # image, promo video), plus a durable local copy of the file
        # itself, so reopening a letter can re-send the attachment
        # even if WhatsApp drops the old media id.
        cols = {r[1] for r in c.execute("PRAGMA table_info(mail)")}
        if "attachment_media_id" not in cols:
            c.execute("ALTER TABLE mail"
                      " ADD COLUMN attachment_media_id TEXT")
        if "attachment_mime" not in cols:
            c.execute("ALTER TABLE mail ADD COLUMN attachment_mime TEXT")
        if "attachment_name" not in cols:
            c.execute("ALTER TABLE mail ADD COLUMN attachment_name TEXT")
        if "attachment_path" not in cols:
            c.execute("ALTER TABLE mail ADD COLUMN attachment_path TEXT")
        # Migrate databases created before letters carried a received
        # date: new letters stamp it at insert; old rows keep NULL
        # (shown without a date, still ordered by id as before).
        if "created_at" not in cols:
            c.execute("ALTER TABLE mail ADD COLUMN created_at TEXT")
        # Moat tables: verified businesses, per-household learned rules.
        c.execute("""CREATE TABLE IF NOT EXISTS businesses (
            business_id TEXT PRIMARY KEY, name TEXT, email TEXT,
            address TEXT, verified INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'trial',
            membership TEXT DEFAULT 'trial')""")
        # Migrate databases created before plan/membership existed.
        cols = {r[1] for r in c.execute("PRAGMA table_info(businesses)")}
        if "plan" not in cols:
            c.execute("ALTER TABLE businesses"
                      " ADD COLUMN plan TEXT DEFAULT 'trial'")
        if "membership" not in cols:
            c.execute("ALTER TABLE businesses"
                      " ADD COLUMN membership TEXT DEFAULT 'trial'")
        # Migrate databases created before business portal logins.
        if "password_hash" not in cols:
            c.execute("ALTER TABLE businesses"
                      " ADD COLUMN password_hash TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS sender_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mailbox TEXT,
            sender_match TEXT, action TEXT)""")


def get_user(phone):
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None


def save_user(phone, **fields):
    user = get_user(phone) or {"phone": phone, "state": "START"}
    user.update(fields)
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO users (phone, name, address, mailbox, state)"
            " VALUES (?,?,?,?,?)",
            (phone, user.get("name"), user.get("address"),
             user.get("mailbox"), user.get("state")))
    return user


# Street-suffix abbreviations collapsed to one canonical word.
_SUFFIX = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "blvd": "boulevard", "ln": "lane", "dr": "drive", "ct": "court",
    "pl": "place", "hwy": "highway", "ste": "suite",
    "apartment": "apt", "unit": "apt",
}
# US state abbreviations collapsed to the full name.
_STATE = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "dc": "district of columbia", "fl": "florida",
    "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois",
    "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky",
    "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio",
    "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming",
}


def normalize_address(address):
    """Canonical form for MATCHING only — displayed addresses are never
    rewritten. Case, commas, periods, extra spaces, street-suffix
    abbreviations (Rd -> road), state abbreviations (GA -> georgia),
    ZIP+4, and N.E. vs NE directionals all collapse, so a business
    typing '999 Sample Rd Atlanta GA 30309' reaches the household
    stored as '999 Sample Road, Atlanta, GA 30309'. 'St' always means
    street here, so a Saint-named street written two ways still
    differs — accepted edge case."""
    if not address:
        return ""
    text = address.lower().replace("#", " apt ")
    text = re.sub(r"\b(\d{5})-\d{4}\b", r"\1", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = text.split()
    # Merge single-letter directionals: "n e" (from "N.E.") -> "ne".
    merged = []
    for tok in tokens:
        if (len(tok) == 1 and tok in "nsew" and merged
                and len(merged[-1]) == 1 and merged[-1] in "nsew"):
            merged[-1] += tok
        else:
            merged.append(tok)
    tokens = merged
    # State abbreviation only for the token before the ZIP (or the last
    # token): this keeps "Elm Ct" (court) apart from "CT" (Connecticut).
    if tokens:
        if len(tokens) >= 2 and re.fullmatch(r"\d{5}", tokens[-1]):
            state_idx = len(tokens) - 2
        else:
            state_idx = len(tokens) - 1
        if tokens[state_idx] in _STATE:
            tokens[state_idx] = _STATE[tokens[state_idx]]
    return " ".join(_SUFFIX.get(t, t) for t in tokens)


def find_mailbox_by_address(address):
    want = normalize_address(address)
    with _conn() as c:
        for row in c.execute(
                "SELECT mailbox, address FROM users "
                "WHERE address IS NOT NULL AND mailbox IS NOT NULL"):
            if normalize_address(row["address"]) == want:
                return row["mailbox"]
        return None


def new_mailbox(address):
    """The mailbox address IS the home address from the license.

    No PMB: a PMB is only needed when a mail center physically receives
    your paper mail (USPS CMRA rules require a PMB/# on mail to a
    commercial receiver). MailboxAI today never touches paper mail —
    businesses send digitally and users scan paper themselves — so the
    mailbox is just an ID, and the home address is the natural one:
    zero address-change friction, and household matching (same address
    = same mailbox) works unchanged. A PMB-at-a-mail-center leg can be
    added later if a real CMRA partner ever receives mail.
    """
    return address


def members_of(mailbox):
    want = normalize_address(mailbox)
    with _conn() as c:
        users = [dict(r) for r in c.execute(
            "SELECT * FROM users WHERE mailbox IS NOT NULL")]
        return [u for u in users if normalize_address(u["mailbox"]) == want]


def add_mail(mailbox, item):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO mail (mailbox, sender, subject, body, category,"
            " due, amount, scam, attachment_media_id, attachment_mime,"
            " attachment_name, attachment_path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mailbox, item["sender"], item["subject"], item.get("body", ""),
             item["category"], item.get("due", "—"), item.get("amount", "—"),
             1 if item.get("scam") else 0,
             item.get("attachment_media_id"),
             item.get("attachment_mime"),
             item.get("attachment_name"),
             item.get("attachment_path"),
             item.get("created_at")
             or datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid


def get_mail(mailbox, include_shredded=False):
    with _conn() as c:
        q = "SELECT * FROM mail WHERE mailbox=?"
        if not include_shredded:
            q += " AND shredded=0"
        return [dict(r) for r in c.execute(q, (mailbox,))]


def shred_mail(mail_id):
    with _conn() as c:
        cur = c.execute("UPDATE mail SET shredded=1 WHERE id=?", (mail_id,))
        return cur.rowcount > 0


def save_business(business_id, name, email, address, password_hash=None):
    """New registrations start on the free trial membership. Re-registering
    keeps the existing plan/membership — a business can't reset its fee
    status by registering again. A password, when given, is set (or
    replaced on re-register); re-registering without one keeps the old."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO businesses"
            " (business_id, name, email, address, verified, plan, membership,"
            " password_hash)"
            " VALUES (?,?,?,?,0,'trial','trial',?)",
            (business_id, name, email, address, password_hash))
        c.execute(
            "UPDATE businesses SET name=?, email=?, address=?"
            " WHERE business_id=?", (name, email, address, business_id))
        if password_hash is not None:
            c.execute(
                "UPDATE businesses SET password_hash=? WHERE business_id=?",
                (password_hash, business_id))


def verify_business(business_id):
    with _conn() as c:
        cur = c.execute("UPDATE businesses SET verified=1 WHERE business_id=?",
                        (business_id,))
        return cur.rowcount > 0


def get_business(business_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM businesses WHERE business_id=?",
                        (business_id,)).fetchone()
        return dict(row) if row else None


def get_businesses_by_email(email):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM businesses WHERE email=?", (email,))]


def get_mail_by_id(mail_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM mail WHERE id=?",
                        (mail_id,)).fetchone()
        return dict(row) if row else None


def get_mail_by_sender(sender):
    """Mail sent by one business, newest first (id order = send order)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mail WHERE sender=? ORDER BY id DESC", (sender,))]


def list_businesses():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM businesses ORDER BY name")]


def set_membership(business_id, plan=None, membership=None):
    """Admin only: change a business's plan / fee status. Returns False
    for an unknown business or an empty update."""
    sets, vals = [], []
    if plan is not None:
        sets.append("plan=?")
        vals.append(plan)
    if membership is not None:
        sets.append("membership=?")
        vals.append(membership)
    if not sets:
        return False
    vals.append(business_id)
    with _conn() as c:
        cur = c.execute(
            f"UPDATE businesses SET {', '.join(sets)} WHERE business_id=?",
            vals)
        return cur.rowcount > 0


def admin_stats():
    """Counts only — never letter contents. This is what the admin sees."""
    with _conn() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        verified_users = c.execute(
            "SELECT COUNT(*) FROM users WHERE state='VERIFIED'").fetchone()[0]
        mailboxes = c.execute(
            "SELECT COUNT(DISTINCT mailbox) FROM users"
            " WHERE mailbox IS NOT NULL").fetchone()[0]
        biz = c.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        biz_verified = c.execute(
            "SELECT COUNT(*) FROM businesses WHERE verified=1").fetchone()[0]
        by_membership = {r[0]: r[1] for r in c.execute(
            "SELECT membership, COUNT(*) FROM businesses GROUP BY membership")}
        mail_total = c.execute("SELECT COUNT(*) FROM mail").fetchone()[0]
        shredded = c.execute(
            "SELECT COUNT(*) FROM mail WHERE shredded=1").fetchone()[0]
        by_category = {r[0]: r[1] for r in c.execute(
            "SELECT category, COUNT(*) FROM mail GROUP BY category")}
    return {"users": users, "users_verified": verified_users,
            "mailboxes": mailboxes, "businesses": biz,
            "businesses_verified": biz_verified,
            "businesses_by_membership": by_membership,
            "mail_total": mail_total, "mail_shredded": shredded,
            "sheets_saved": shredded * 2, "mail_by_category": by_category}


def get_verified_business(name):
    """A business whose address was proven (postcard PIN). Matching is
    case-insensitive on the sender name."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM businesses WHERE LOWER(name)=LOWER(?)"
            " AND verified=1 LIMIT 1", (name,)).fetchone()
        return dict(row) if row else None


def add_rule(mailbox, sender_match, action):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sender_rules (mailbox, sender_match, action)"
            " VALUES (?,?,?)", (mailbox, sender_match, action))
        return cur.lastrowid


def get_rule(mailbox, sender):
    """A household's learned rule for this sender, if any. Rules are
    the data moat: they encode what this household taught MailboxAI,
    and they don't transfer to a competitor."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM sender_rules WHERE mailbox=?"
            " AND LOWER(?) LIKE '%' || LOWER(sender_match) || '%'"
            " ORDER BY id DESC LIMIT 1", (mailbox, sender)).fetchone()
        return dict(row) if row else None


def search_mail(mailbox, q):
    """Searchable archive: years of mail history is a switching cost.
    Leaving means losing the archive."""
    like = f"%{q}%"
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mail WHERE mailbox=? AND shredded=0 AND"
            " (sender LIKE ? OR subject LIKE ? OR body LIKE ?)"
            " ORDER BY id DESC", (mailbox, like, like, like))]


def sheets_saved(mailbox):
    """2 sheets per shredded letter (letter + envelope)."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM mail WHERE mailbox=?"
                      " AND shredded=1", (mailbox,)).fetchone()[0]
        return n * 2
