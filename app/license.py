"""Driver's-license scanning: Muse Spark vision first, Tesseract fallback.

What this does: reads a license photo and pulls out the name, mailing
address, date of birth, expiry and license number. That is what the
mailbox needs: the address on the license becomes the mailbox address,
and family members whose license shows the same address join the same
mailbox.

What this does NOT do: prove the license is genuine. Reading text —
whether by Spark vision or by OCR — cannot check holograms, barcodes
against a DMV record, or liveness. A production mailbox still needs an
ID-verification provider (Stripe Identity, Veriff, Onfido, ...) plus
the notarized USPS Form 1583 that CMRA rules require. For this demo,
extraction is the honest claim: "reads name + address from the license
photo."

Privacy note for real mode: the license photo is sent to Meta's Model
API for the Spark vision read (the same connection as triage and the
selfie face match). The Tesseract fallback runs locally on the server.
Test with the sample license (DOB and license number masked; expiry
is a real future date), never a real license on camera.

Modes (in order):
  1. MAILBOXAI_MOCK_VERIFY=1 -> canned demo result (any photo passes).
     Demo mode only; the address is the fixed MOCK_ADDRESS.
  2. Otherwise -> Muse Spark vision first (reads the photo, returns
     fields as JSON; masked/unreadable fields come back null, never
     guessed). No Spark key, a bad reply, or no usable address ->
     falls through to Tesseract, so one failure never blocks
     verification.
  3. Tesseract OCR fallback (install it: `brew install tesseract`).
     No Tesseract -> 'no_ocr_available'. No readable address ->
     'address_not_found'. The app must NOT silently verify.

Only the address gates success, plus one safety rule: an expiry that
parses cleanly and is already past rejects the license (see
is_expired). Name, DOB and license number are best-effort extras; a
missing one never blocks verification, and an expiry that cannot be
parsed never rejects — a misread date must not lock a real user out.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime

from . import config

# US licenses mostly follow the AAMVA field layout: 1 = name,
# 2 = address, 3 = date of birth, 4d = license number, 4b = expiry.
# State labels vary (Georgia DDS included), so we try AAMVA numbers,
# printed labels, and generic patterns, in that order.

_SUFFIX = (r"Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|"
           r"Court|Ct|Parkway|Pkwy|Highway|Hwy|Way|Trail|Trl|Place|Pl")

ADDRESS_RE = re.compile(
    r"\d{1,6}\s+[A-Za-z0-9 .'\-#]+?\b(?:" + _SUFFIX + r")\b"
    r"(?:\s+(?:Apt|Unit|Suite|Ste|PMB|#)\s*[A-Za-z0-9\-]+)?"
    r".{0,80}?\b[A-Z]{2}\s+\d{5}(?:-\d{4})?",
    re.IGNORECASE | re.DOTALL)

CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z][A-Za-z .'\-]{2,}?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)")

DL_RE = re.compile(
    r"(?:DL|LIC(?:ENSE)?(?:\s*NO\.?)?|ID(?:\s*NO\.?)?|4d)\D{0,12}"
    r"([A-Z0-9][A-Z0-9\-]{4,20})",
    re.IGNORECASE)

# A line starting with "DL NO" / "4d" is the real license-number field.
# The generic DL_RE alone would also match "LICENSE" in a card header
# ("DRIVER'S LICENSE" -> captures the next word, e.g. the surname), so
# the anchored form must win.
DL_LINE_RE = re.compile(
    r"(?m)^\s*(?:DL\s*NO\.?|4d)\D{0,12}([A-Z0-9][A-Z0-9\-]{4,20})",
    re.IGNORECASE)

_SPARK_PROMPT = (
    "Read this driver's license photo and extract the printed fields. "
    "Return JSON only, no other text: "
    '{"name": ..., "address": ..., "dob": ..., "expiry": ..., '
    '"license_number": ...}. '
    "Rules: name as printed (LAST, FIRST is fine); address as one line "
    "(street, city, state, ZIP); dates as MM/DD/YYYY; use null for any "
    "field that is masked, blurred, or unreadable; NEVER guess or invent "
    "a value; if no license is visible, return all nulls."
)


def _extract_dl(text):
    m = DL_LINE_RE.search(text)
    if m:
        return m.group(1)
    # Fallback: skip the card header line -- "DRIVER'S LICENSE" would
    # otherwise match the LIC alternative and capture the next word
    # (e.g. the surname). Header lines never hold the DL number.
    body = "\n".join(ln for ln in text.splitlines()
                     if "DRIVER" not in ln.upper())
    m = DL_RE.search(body)
    return m.group(1) if m else None


def _ocr_passes(image_bytes):
    """Return a list of OCR texts, trying page-segmentation modes.

    psm 6 (uniform text block) reads licenses best; psm 3 (automatic)
    is the fallback, used only if psm 6 yields no address. Returns None
    when Tesseract is not installed. Never raises: an OCR failure just
    means that pass contributes nothing.
    """
    if not shutil.which("tesseract"):
        return None
    fd, path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        texts = []
        for psm in ("6", "3"):
            try:
                out = subprocess.run(
                    ["tesseract", path, "stdout", "--psm", psm],
                    capture_output=True, text=True, timeout=60)
            except Exception:
                continue
            if out.stdout.strip():
                texts.append(out.stdout)
            if texts and _extract_address(texts[-1]):
                break
        return texts
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _flat(text):
    return " ".join(text.replace("\n", " ").split())


def _extract_address(text):
    flat = _flat(text)
    m = ADDRESS_RE.search(flat)
    if m:
        addr = " ".join(m.group(0).split())
        # Drop a leading AAMVA field number ("2 123 Main St..." -> the
        # "2" is the field label, not part of the street number).
        addr = re.sub(r"^\d\s+(?=\d)", "", addr)
        return addr
    # Fallback: a street line plus a separate city/state/ZIP line.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.match(r"\d{1,6}\s+\S+", ln) and re.search(
                r"\b(?:" + _SUFFIX + r")\b", ln, re.IGNORECASE):
            for nxt in lines[i + 1:i + 3]:
                cm = CITY_STATE_ZIP_RE.search(nxt)
                if cm:
                    return f"{ln}, {cm.group(0)}"
    return None


def _extract_name(text, expected_name):
    # ALL-CAPS "LAST, FIRST" line (the AAMVA field-1 style). A two-letter
    # second part is a state code ("ATLANTA, GA"), not a first name.
    for ln in text.splitlines():
        ln = ln.strip()
        m = re.match(r"^([A-Z][A-Z' \-]+),\s*([A-Z][A-Z' \-]+)$", ln)
        if m and len(m.group(2).strip()) > 2 \
                and not CITY_STATE_ZIP_RE.search(ln):
            last, first = m.group(1).strip(), m.group(2).strip()
            return f"{first.title()} {last.title()}"
    # Labeled name line, e.g. "1 DOE JOHN" or "1 DOE, JOHN".
    m = re.search(r"(?m)^\s*1\s+([A-Z][A-Z' \-,]+)$", text)
    if m:
        raw = " ".join(m.group(1).split())
        if "," in raw:
            last, first = [p.strip() for p in raw.split(",", 1)]
            return f"{first.title()} {last.title()}"
        return raw.title()
    return expected_name


def _date_after(text, pattern):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1) if m else None


_EXPIRY_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d")


def parse_expiry_date(value):
    """Parse a license expiry string to a date, or None.

    Only numeric US formats are accepted (MM/DD/YYYY, MM-DD-YYYY,
    YYYY-MM-DD). Anything else returns None — never guess a date from
    a free-form model reply.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def is_expired(expiry_value, today=None):
    """True only when the expiry parses AND is before today.

    Missing, masked, or unparseable values never reject: the readers
    drop masked fields, and a misread date must not lock a real user
    out. An expiry of today itself still counts as valid.
    """
    parsed = parse_expiry_date(expiry_value)
    if parsed is None:
        return False
    if today is None:
        today = date.today()
    return parsed < today


def _parse_json(text):
    """Parse a model reply as JSON; strip ``` fences first, else None."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _clean_spark_value(value):
    """A Spark field value, or None when it is missing/masked/guessed.

    Masked fields come back as "**", "****", or similar; a null, an
    empty string, and the words null/none/masked all mean "not readable".
    Dropping them (instead of storing them) is what keeps a masked card
    from producing bogus DOB / DL / expiry values.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or "*" in v:
        return None
    if v.lower() in ("null", "none", "masked", "n/a", "unknown"):
        return None
    return v


def _normalize_spark_name(raw, expected_name):
    if not raw:
        return expected_name
    name = " ".join(raw.split())
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        if first and last:
            return f"{first.title()} {last.title()}"
    if name.isupper():
        return name.title()
    return name


def _clean_spark_address(raw):
    """A Spark address string, normalized, or None when unusable."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    addr = ", ".join(p for p in parts if p)
    # Drop a leading AAMVA field number, same as the OCR path.
    addr = re.sub(r"^\d\s+(?=\d)", "", addr)
    if not addr or not re.search(r"\d", addr):
        return None
    m = ADDRESS_RE.search(addr)
    if m:
        return " ".join(m.group(0).split())
    # No street-suffix match, but a ZIP is present: accept it as-is
    # rather than discarding an address Spark clearly read.
    if re.search(r"\d{5}(?:-\d{4})?", addr):
        return addr
    return None


def _spark_attempt(image_bytes, expected_name):
    """Try Muse Spark vision. Returns (result, error).

    result is a verify_license-style dict, or None. error is None when
    Spark was simply not configured (silent skip: Tesseract is the
    normal path without a key); otherwise it names why the attempt
    failed, so the caller can mention it if OCR also fails.
    """
    try:
        from . import muse_client
    except Exception as exc:
        return None, f"muse_client import failed: {exc}"
    if not muse_client.is_configured():
        return None, None
    try:
        reply = muse_client.chat_with_images(_SPARK_PROMPT, [image_bytes],
                                             tag="vision-license")
    except Exception as exc:
        return None, f"Spark vision call failed: {exc}"
    data = _parse_json(reply)
    if not isinstance(data, dict):
        return None, "Spark vision reply was not JSON"
    address = _clean_spark_address(_clean_spark_value(data.get("address")))
    if not address:
        return None, "Spark vision returned no usable address"
    name = _normalize_spark_name(_clean_spark_value(data.get("name")),
                                 expected_name)
    result = {"ok": True, "name": name or expected_name,
              "address": address, "method": "muse-spark-vision"}
    for out_key, in_key in (("dob", "dob"), ("expiry", "expiry"),
                            ("license_number", "license_number")):
        value = _clean_spark_value(data.get(in_key))
        if value:
            result[out_key] = value
    return result, None


def verify_license(image_bytes, expected_name=None):
    """Returns dict: {ok, name, address, method, ...} or {ok: False, reason, detail}."""
    if config.MOCK_VERIFY:
        return {"ok": True, "name": expected_name or "Demo User",
                "address": config.MOCK_ADDRESS, "method": "mock"}
    if not image_bytes:
        return {"ok": False, "reason": "no_image",
                "detail": "Could not download the license photo. "
                          "Please resend it."}
    spark_result, spark_error = _spark_attempt(image_bytes, expected_name)
    if spark_result:
        return spark_result
    texts = _ocr_passes(image_bytes)
    if texts is None:
        detail = ("Install Tesseract (`brew install tesseract`) or "
                  "plug in an ID-verification provider "
                  "(see license.py header).")
        if spark_error:
            detail += f" (Spark vision also failed: {spark_error})"
        return {"ok": False, "reason": "no_ocr_available",
                "detail": detail}
    address = None
    used = ""
    for text in texts:
        address = _extract_address(text)
        if address:
            used = text
            break
    if not address:
        detail = ("Could not read an address from the license "
                  "photo. Retake it flat, in good light, with "
                  "all corners visible, and resend.")
        if spark_error:
            detail += f" (Spark vision also failed: {spark_error})"
        return {"ok": False, "reason": "address_not_found",
                "detail": detail}
    result = {"ok": True,
              "name": _extract_name(used, expected_name) or expected_name,
              "address": address,
              "method": "tesseract"}
    dob = _date_after(
        used, r"(?:DOB|Date of Birth|Born|3)\D{0,12}(\d{2}/\d{2}/\d{4})")
    if dob:
        result["dob"] = dob
    expiry = _date_after(
        used, r"(?:EXP|Expires?|4b)\D{0,12}(\d{2}/\d{2}/\d{4})")
    if expiry:
        result["expiry"] = expiry
    dl = _extract_dl(used)
    if dl:
        result["license_number"] = dl
    return result
