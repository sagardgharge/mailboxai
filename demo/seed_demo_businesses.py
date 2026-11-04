"""Seed 5 demo businesses for MailboxAI, verify them, and send demo mail.

Runs against YOUR running server (it creates the businesses in your
server's database — this script cannot reach your MacBook from here).

Usage (on the MacBook, server running in Terminal 1):
    python3 seed_demo_businesses.py
    python3 seed_demo_businesses.py "999 Sample Road, Atlanta, GA 30309"
    python3 seed_demo_businesses.py --register-only
    MAILBOXAI_DEMO_BASE_URL=http://127.0.0.1:8000 python3 seed_demo_businesses.py

Defaults: base URL http://127.0.0.1:8000, mailbox "999 Sample Road,
Atlanta, GA 30309" (the sample-license home address). Pass your real
mailbox as the first argument if it differs — it must match the
mailbox line in the customer's Verified message EXACTLY, or the sends
report notified=0 and nobody receives anything.

Requires MAILBOXAI_DEV=1 in .env (so the register response includes
the postcard PIN for verification) and a restart after setting it.
Portal logins: every seeded business gets password demo1234; log in
at /business-portal with the business email + demo1234.
Re-running is safe: businesses re-register under the same id and
re-verify; letters are simply sent again (duplicates in the mailbox).

Demo data only: all five businesses are fictional demo names, and the
letters are fictional demo content — not real
offers from any real business. Business addresses are well-formed demo
addresses, not any real business's address.
Stdlib only — no venv needed.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("MAILBOXAI_DEMO_BASE_URL",
                      "http://127.0.0.1:8000").rstrip("/")
DEFAULT_MAILBOX = "999 Sample Road, Atlanta, GA 30309"

BUSINESSES = [
    ("ShopMart", "offers@shopmart.example",
     "100 Peachtree Street NE, Atlanta, GA 30303", [
        ("Black Friday deals are here",
         "Black Friday is here! Up to 50% off electronics, toys, and "
         "home essentials, November 28-30. Shop early in store or in "
         "the app, and show this message at checkout for an extra 10% "
         "off one item."),
        ("Your ShopMart rewards are waiting",
         "Good news — you've earned $12.40 in ShopMart rewards on "
         "your recent shopping. Your rewards are ready to use on your "
         "next trip, in store or in the app. Rewards expire "
         "December 31, 2026."),
     ]),
    ("Hospital", "care@hospital.example",
     "500 Peachtree Street NE, Atlanta, GA 30308", [
        ("Your hospital appointment is confirmed",
         "Your appointment at Hospital is confirmed for "
         "November 18, 2026 at 10:30 AM with Dr. Patel. Please arrive "
         "15 minutes early and bring your insurance card. To "
         "reschedule, call the office or reply to this message."),
        ("Your hospital bill is ready",
         "Your statement from Hospital is ready. Balance "
         "$185.00, payment due December 5, 2026. You can pay in the "
         "office or by mail."),
     ]),
    ("Barber Shop", "hello@barbershop.example",
     "800 Peachtree Street NE, Atlanta, GA 30308", [
        ("Your $5 off haircut coupon",
         "Barber Shop coupon: $5 off any haircut, valid through "
         "December 31, 2026. No appointment needed — just walk in and "
         "show this message at check-in. One coupon per customer."),
     ]),
    ("Demo Insurance", "notices@demoinsurance.example",
     "400 Peachtree Street NE, Atlanta, GA 30303", [
        ("Your policy renewal is due",
         "Your insurance policy renewal is due by January 15, 2027. "
         "Renew online or contact your agent. Please have your "
         "current policy number ready."),
     ]),
    ("First Demo Bank", "offers@firstdemobank.example",
     "100 Main Street, Atlanta, GA 30309", [
        ("You're pre-approved for a new credit card",
         "You're pre-approved for the First Demo Bank card: earn "
         "60,000 bonus points after you spend $4,000 in the first 3 "
         "months, plus 0% intro APR for 12 months. Apply by "
         "December 31, 2026."),
     ]),
]


def post(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise SystemExit(f"POST {path} failed ({exc.code}): {body or exc}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    register_only = "--register-only" in sys.argv[1:]
    mailbox = args[0] if args else DEFAULT_MAILBOX
    try:
        urllib.request.urlopen(BASE + "/health", timeout=10)
    except Exception as exc:
        raise SystemExit(
            f"Cannot reach the server at {BASE} ({exc}). "
            "Start it first (Terminal 1: uvicorn app.main:app --reload "
            "--port 8000), then re-run this script.")
    print(f"Server: {BASE}")
    print(f"Mailbox: {mailbox}")
    total_letters = 0
    for name, email, address, letters in BUSINESSES:
        reg = post("/business/register",
                   {"name": name, "email": email, "address": address,
                    "password": "demo1234"})
        if not reg.get("ok"):
            raise SystemExit(f"Register failed for {name}: {reg}")
        biz_id = reg["business_id"]
        pin = reg.get("dev_pin")
        if not pin:
            raise SystemExit(
                f"No PIN returned for {name}. Set MAILBOXAI_DEV=1 in .env "
                "and restart the server, then re-run this script.")
        ver = post("/business/verify-pin",
                   {"business_id": biz_id, "pin": pin})
        if not ver.get("ok"):
            raise SystemExit(f"Verify failed for {name}: {ver}")
        print(f"\n{name}: registered + verified (business_id={biz_id})")
        print(f"  portal login: email={email}, password=demo1234")
        if register_only:
            continue
        for subject, body in letters:
            out = post("/mail/upload",
                       {"mailbox": mailbox, "sender": name,
                        "subject": subject, "body_text": body})
            total_letters += 1
            print(f"  sent: {subject} -> mail_id={out.get('mail_id')}, "
                  f"category={out.get('category')}, "
                  f"notified={out.get('notified')}")
            if out.get("notified") == 0:
                print("  WARNING: notified=0 — the mailbox above does not "
                      "match a Verified mailbox. Check the customer's "
                      "Verified message and re-run with that address.")
    print(f"\nDone: {len(BUSINESSES)} businesses verified, "
          f"{total_letters} letters sent.")
    if not register_only:
        print("On the customer's phone, send MAIL for the digest card.")


if __name__ == "__main__":
    main()
