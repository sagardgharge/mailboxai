"""Business registration + business-address validation.

Two layers (the Google Business Profile model):
  1. Instant check — the address must parse as a real US address, and
     when SMARTY_AUTH_ID / SMARTY_AUTH_TOKEN are set, it is checked
     against Smarty's USPS-based API for deliverability (DPV). Without
     credentials this layer is format-only and says so honestly.
  2. Proof of receipt — a 6-digit PIN "mailed" to the address. The
     business enters the PIN to prove it actually receives mail there.
     A business can alternatively upload a license / utility bill
     showing the same address (document check, not implemented here).

Note: the notarized USPS Form 1583 rule applies to RECEIVING mail on
behalf of a customer (the mailbox holder). A sender business does not
file Form 1583 — it needs this identity/address check so nobody can
send fake "bank letters" through MailboxAI.

PINs live in memory here; use a database table in production.
"""
import hashlib
import json
import random
import re
import time
import urllib.request

from . import config

ADDRESS_RE = re.compile(
    r"^\s*(\d{1,6}\s+[^,]+),\s*([^,]+),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")

_PINS = {}  # business_id -> {"pin": str, "expires": float}
PIN_TTL_SECONDS = 7 * 24 * 3600


def business_id(name, address):
    return "biz_" + hashlib.md5(f"{name}|{address}".encode()).hexdigest()[:10]


def standardize_address(address):
    m = ADDRESS_RE.match(address.strip())
    if not m:
        return None
    street, city, st, z = m.groups()
    return f"{' '.join(street.split())}, {' '.join(city.split()).title()}, " \
           f"{st.upper()} {z}"


def _smarty_check(standardized):
    """Live deliverability check via Smarty USPS Street API.
    Returns True/False, or None when not configured."""
    if not (config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN):
        return None
    url = ("https://us-street.api.smarty.com/street-address"
           f"?auth-id={config.SMARTY_AUTH_ID}"
           f"&auth-token={config.SMARTY_AUTH_TOKEN}"
           f"&street={urllib.request.quote(standardized)}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        results = json.loads(resp.read())
    if not results:
        return False
    analysis = results[0].get("analysis", {})
    return analysis.get("dpv_match_code") in ("Y", "S", "D")


def validate_address(address):
    std = standardize_address(address)
    if not std:
        return {"ok": False, "reason": "format",
                "detail": "Expected: street, city, ST ZIP."}
    live = _smarty_check(std)
    if live is False:
        return {"ok": False, "reason": "not_deliverable",
                "detail": "Address not found as deliverable by the USPS check."}
    return {"ok": True, "standardized": std,
            "method": "smarty_dpv" if live else "format_only",
            "note": None if live else
            "Set SMARTY_AUTH_ID/TOKEN for a live USPS deliverability check."}


def new_pin(biz_id):
    pin = f"{random.randint(0, 999999):06d}"
    _PINS[biz_id] = {"pin": pin, "expires": time.time() + PIN_TTL_SECONDS}
    return pin


def verify_pin(biz_id, pin):
    rec = _PINS.get(biz_id)
    if not rec or time.time() > rec["expires"]:
        return False
    if rec["pin"] == pin:
        del _PINS[biz_id]
        return True
    return False
