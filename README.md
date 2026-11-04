# MailboxAI — your mailbox, inside WhatsApp

> Built for the **Meta Global AI Developer Hackathon** — Indie Developer track,
> **Build for Your World** theme. Submitted as *MailboxAI*; 

No app to install. No paper piles. This is **mail, not email**: a user messages
MailboxAI on WhatsApp, verifies once with a driver's license photo, and gets a
mailing address. Family members whose license shows the **same address** join the
same mailbox. Every letter is read, triaged, and acted on by AI — bills get
due-date reminders, junk gets shredded and recycled, and letters can be read
aloud as voice notes.

Businesses — banks, retailers, schools, mortgage companies, local shops —
register once with a validated address, then send letters as **soft mail**
straight to the customer's WhatsApp, or scan a paper letter and send that.
No printing, no postage, no 3–5 day wait.

## The problem in numbers

- US advertising mail used **~2.00 million tons of paper in 2025**
  (~28–47 million trees).
- The average American gets **16 junk pieces a week, 41 lbs a year**,
  and only **~40% is recycled**.
- Email never killed paper mail: courts, tax offices, banks, HOAs, and
  mortgage companies still send paper, by habit or by law.
- Existing virtual mailboxes are business dashboard apps at
  **$10–$40/month plus per-scan fees** — nothing lives where people
  actually read messages.
  
## Why this is powerful: the mailbox lives inside WhatsApp

- **The audience is already there.** More than 3 billion people use
  WhatsApp every month (Meta, April 2025), and more than 2 billion use
  it every day (Meta, January 2026). MailboxAI asks nobody to download
  a new app or learn a new habit.
- **Messages get seen.** People open WhatsApp through the day to talk
  to family and friends, so mail lands where attention already is —
  not in an email inbox that piles up unread, and not in a paper pile
  on the table.
- **For businesses, it is the richest way to reach the end customer.**
  The customer is already on WhatsApp, the message arrives instantly
  with no printing, postage, or 3–5 day wait, and a verified badge
  tells the customer the sender is real. More than 200 million
  businesses already use the WhatsApp Business app (Meta, 2023), so
  customers are used to hearing from businesses here.
- **One channel, both sides.** The same chat that delivers a bill can
  answer it: read it aloud, shred it, search it, or ask about it by
  voice. No separate portal, no separate app.  

## How a user registers (first time)

You cannot search for MailboxAI inside WhatsApp — there is no directory.
People arrive by QR code, `wa.me` link, or saved number (see `demo/qr/`),
then the chat stays in their list like any contact.

1. Scan the QR (or tap the link) → WhatsApp opens a chat with MailboxAI.
2. Send `Hi` → it asks for their full name, as on their driver's license.
3. Send their name → it asks for a photo of their driver's license.
4. Send the license photo → the address is read from it (Spark vision first, OCR fallback, see below). An expiry that parses cleanly and is already past rejects the license; an unparseable expiry never rejects (a misread date must not lock a real user out).
5. Video-code step (real mode, the only validation): the bot issues a
   random 4-digit code (valid 3 minutes, single use) and the user sends
   a short video saying it. Spark vision checks a live person + face
   match to the license, and Meta Voice Transcribe checks the spoken
   code — all three must pass. A photo alone is rejected, and any
   failure issues a fresh code. Five failed verification attempts
   within 10 minutes pause verification for that phone for 10 minutes
   (a server restart clears the pause).
6. ✅ Verified → their mailbox address is their home address from the
   license. They give that address to businesses, and mail starts
   arriving as WhatsApp messages.
7. Household: a family member whose license shows the **same address**
   repeats the same steps and joins the same mailbox instead of getting
   a new one. Existing members get a short alert naming the joiner.

Mock mode (`MAILBOXAI_MOCK_VERIFY=1`) skips the video-code step and verifies any
photo to a fixed demo address. For the demo video, record in REAL mode with
the sample license only (never a real license on camera) — mock mode is only
the fallback if the challenge fails repeatedly on recording day.

## After verification: what the user sees

WhatsApp has no HTML cards or persistent UI — only messages, voice notes,
and reply buttons. So the live version is commands + buttons, not tabs:

- `MAIL` → today's digest card: per-letter lines (number, category, sender,
  subject, due/amount for bills, scam flags) + paper-saved counter.
- `SHRED <number>` → shreds that letter, replies with the paper-saved total.
  Mailbox-checked: a user can only shred letters in their own mailbox.
  Typed SHRED is immediate; the ♻️ **Shred button** asks first ("Yes, shred"
  / "Keep it") so a stray tap can't delete a letter.
- `READ <number>` → the letter as a voice note (text fallback if audio fails
  — never silence).
- `OPEN <number>` → the full letter on screen (sender, subject, body, bill
  details, scam flag). Mailbox-checked like SHRED. Also a 📖 **Open** button
  on every new-mail notification.
- `TODAY` → today's letters only, newest first (server-local day; letters
  stored before dates were recorded carry no date and never appear here).
- Just ask naturally — "show me today's Barber Shop mail", "I want to see
  the ShopMart coupon" — the bot finds that business's mail (today's first,
  else the latest with a date note) and opens it when there's exactly one.
  Works typed and by voice note.
- Casual chat works too: "thanks", "hi", "bye" get friendly replies, and
  "who are you" / "help" explains the product and every feature (before
  onboarding, it answers and then asks your name).
- `SAVED` → paper-saved counter (sheets + pounds).
- `COUNT` → totals (received / still in / shredded); `FROM <business>` → newest 5 from one sender; `SEARCH <word>` → archive search (sender/subject/body, newest first, unshredded only); `LATEST <number>` / `LATEST <number> FROM <business>`.
- Voice note → transcribed by Meta Muse Voice Transcribe, then run as a command (same commands as typed; `READ` answers as a voice note). Voice also understands "open letter two", "show me today's mail", and "last mail from ShopMart" (newest 1 from that sender).
- Every new-mail notification arrives with ♻️ **Shred** (asks "Yes, shred" /
  "Keep it" before deleting), 🔊 **Read aloud**,
  and 📖 **Open** buttons, plus a `✓ Verified sender` badge when the sender is a verified
  business.

## How a business registers (different door, no license)

Businesses do **not** register in WhatsApp chat — they register on the web
or via API. A driver's license proves a *person*, not a *business at an
address*, so businesses are validated differently, per location (each
grocery store, each ShopMart store, each Barber Shop registers its own
address; HQ registers separately only if it sends from HQ):

1. Fill the form: business name, address, contact, email.
2. Instant check: the address must parse as a real US address, and with
   USPS/Smarty credentials it is checked for actual deliverability.
3. Postcard PIN: a 6-digit code is mailed to that address (the Google
   Business Profile model). Entering it proves the business actually
   receives mail there. Alternative: a business license / utility bill in
   the business name at that address (document check, not yet implemented).
4. Verified → the business can send soft mail, and customers see a
   `✓ Verified sender` badge on those letters.

Note: the notarized USPS Form 1583 rule applies to *receiving* mail on
behalf of a customer (the mailbox holder). A sender business does not file
Form 1583 — it needs this identity/address check so nobody can send fake
"bank letters" through MailboxAI.

## Business sending: three channels

- **Website** — a portal where any business (including a non-technical shop
  owner) composes and sends mail pieces, and sees delivery stats. This is
  the base every business can use on day one.
- **API** — for businesses with their own systems (`POST /mail/upload`).
  This is what lands large senders; their billing system plugs in and sends
  automatically.
- **WhatsApp (roadmap)** — for small shops: photograph a coupon/flyer and
  send it as a mail piece. Scope, decided: **opted-in residents nearby
  only** — a resident opts in to a business or to deals in their area, and
  only then can that business send them mail pieces. No open blasts.
- **Paid deals (roadmap, paid add-on)** — promotional mail pieces
  (e.g. a Barber Shop coupon) to opted-in nearby residents, priced per
  campaign / per thousand sends, above base membership.

## Business membership (paid)

Users are free. Businesses pay a membership to send. New businesses start
on a free **trial**; the admin sets plan/status. A verified business whose
membership is **expired** is blocked at send time (`402 membership_expired`,
no letter stored). Trial and active businesses send normally with the badge.
Re-registering keeps the existing plan/status — a business cannot reset its
fee status by registering again. Card charging itself (e.g. Stripe Checkout)
is not wired yet; the membership status is the hook it flips.

## Admin

The admin is the service owner, proven by one secret key
(`MAILBOXAI_ADMIN_KEY` in `.env`) — not voted on, not per-mailbox. While the
key is unset, every admin route refuses.

- Dashboard: `http://localhost:8000/admin-page` (enter the key, Load).
- Stats (counts only, never letter contents): users, verified users,
  mailboxes, businesses + verified, letters, shredded, sheets saved, and
  breakdowns by membership and category.
- Business list with plan/membership controls; suspend a business by setting
  it to `expired`.

## Meta tools used

Nothing mocked is claimed as live.

| Meta tool | Functionality in MailboxAI | Code |
|---|---|---|
| **Muse Spark** (`muse-spark-1.3`) | Letter triage: sender, amount, due date, category, scam detection | `app/muse_client.py`, `app/triage.py` |
| **Muse Spark vision** | License photo reading (Spark-first, Tesseract fallback) + face match + blink liveness + spoken-code challenge | `app/license.py`, `app/face.py`, `app/muse_client.py` (`chat_with_images`) |
| **SAM 3.1** | Segments attached scan photos into letter / envelope / check / coupon regions (Model API sam-3.1 first, legacy SAM_API_URL second, whole-page fallback) | `app/segment.py`, `app/sam_client.py` |
| **Muse Wonder** | Answers spoken mailbox questions (voice note in -> Wonder reasoning over mailbox rows -> voice note back) | `app/wonder.py`, `app/muse_client.py` (model override) |
| **Muse Voice Transcribe** | Transcribes voice-note commands and the spoken challenge code from challenge videos (ffmpeg extracts audio, Model API transcribes) | `app/voice_in.py` |
| **Muse Image** | Visual "today's mail" digest card (illustration + caption with the facts) | `app/image_card.py` |
| **Open-weights** (Fireworks client) | Cheap junk filter first; only unclear letters escalate to Spark | `app/fireworks_client.py`, `app/triage.py` |
| **Muse and Muse Code** | Used | — |
| **WhatsApp Business Platform** | The delivery surface: onboarding, mail, reminders, voice notes, buttons | `app/whatsapp.py`, `app/main.py` |

## Architecture

```
Phone (WhatsApp) ──► Meta WhatsApp Cloud API ──webhook──► FastAPI (app/main.py)
                                                            ├── state.py      (SQLite: users, household, mail, businesses, rules)
                                                            ├── license.py    (license photo → address: Spark vision first, Tesseract fallback)
                                                            ├── face.py       (Spark vision face match + blink liveness)
                                                            ├── mail intake   (POST /mail/upload)
                                                            │     ├── segment.py  (SAM 3.1 regions)
                                                            │     └── triage.py   (Muse Spark / rules)
                                                            ├── muse_client.py (ONE central Muse connection)
                                                            ├── voice.py      (voice-note TTS seam)
                                                            ├── digest.py     (digest card)
                                                            ├── business.py   (business validation + postcard PIN)
                                                            └── whatsapp.py   (Graph API client: text, buttons, audio, media)
```

## How to run

### Prerequisites

- Python 3.9+ and a virtualenv
- `brew install tesseract` — real license OCR (without it: `no_ocr_available`)
- `brew install ffmpeg` — video liveness frames + voice-note conversion
- A Meta app with a WhatsApp test number (or a production number later)

### Option 1 — Try the interactive demos (no install, 2 minutes)

Open any file in `demo/` in a browser:

- `demo/index.html` — consumer demo: onboarding, mailbox, triage, household, paper counter
- `demo/how-a-user-joins.html` — tap-through: how a new user finds and joins MailboxAI
- `demo/business-demo.html` — business demo: register, validate address, send soft mail, scan a paper letter
- `demo/admin.html` — served live at `/admin-page` (needs the backend + key)
- `demo/business-send.html` — served live at `/business-send` (second phone sends real mail, optional attachment: image/PDF/MP4 delivered as WhatsApp media)
- `demo/qr/` — registration QR codes + social share card (test-number QRs — see note below)

Or serve the static demos locally:

```bash
cd demo
python3 server.py
# open http://localhost:8000
```

### Option 2 — Run the real backend

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env (see table below)
python3 -m uvicorn app.main:app --reload --port 8000
```

A new terminal does **not** auto-activate `.venv`: no `(.venv)` in the prompt
means the system python is in use and uvicorn reports "No module named
uvicorn". Run `source .venv/bin/activate` first.

Expose it to Meta with cloudflared (the tunnel must stay running in its own
terminal; restarting it changes the URL):

```bash
cloudflared tunnel --url http://localhost:8000
```

Then in the Meta developers dashboard → your app → **Step 2. Production
setup → Configure Webhooks** (this is the app-wide webhook; it works for the
test number too):

- **Callback URL:** `https://<your-tunnel>.trycloudflare.com/webhook`
- **Verify token:** the same string as `WHATSAPP_VERIFY_TOKEN`
- Verify, save, and make sure `messages` is **Subscribed**.

Message the test number from your phone (a test number works for up to 5
allowlisted recipients):

```
Hi
→ bot asks your name
Sagar Gharge
→ bot asks for a license photo
(send the sample license; MOCK_VERIFY=1 passes any photo to the demo address,
 MOCK_VERIFY=0 runs real OCR + the selfie step)
→ ✅ Verified + your mailbox address
```

### Environment variables (`.env`)

`.env` is auto-loaded by `app/config.py` (stdlib loader: project-root or cwd
`.env`, `export ` prefix and quotes handled, existing environment variables
always win). `--reload` restarts on `.py` changes but **not** on `.env`
changes — restart after editing `.env`.

| Variable | Meaning |
|---|---|
| `WHATSAPP_TOKEN` | Access token from the app's Step 1 page (temporary tokens expire after ~24h — regenerate there on a 401) |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone Number ID of the test/production number |
| `WHATSAPP_VERIFY_TOKEN` | Short word you chose (default `mailboxai-verify`); never expires |
| `WHATSAPP_APP_SECRET` | Blank = signature check skipped (fine for local testing) |
| `META_MODEL_API_BASE_URL` | `https://api.meta.ai/v1` |
| `META_MODEL_API_KEY` | Key from `dev.meta.ai/api-keys` (shown once, starts with `LLM_`) |
| `META_MODEL_NAME` | **`muse-spark-1.3`** — the bare `muse-spark` name 404s |
| `MAILBOXAI_MOCK_VERIFY` | `1` = demo mock (any photo passes, fixed demo address, selfie skipped). `0` = real OCR + selfie. Never `1` in production |
| `MAILBOXAI_DEV` | `1` = business-register responses include the postcard PIN (demo only). Never `1` in production |
| `MAILBOXAI_ADMIN_KEY` | Secret admin key; admin routes refuse while unset |
| `TTS_COMMAND` | Voice-note command template, e.g. macOS: `say -o {out}.aiff "{text}" && ffmpeg -y -v error -i {out}.aiff -c:a libopus {out} && rm {out}.aiff`. Blank = letter sent as text |
| `SAM_API_URL` | Legacy SAM endpoint (optional); blank = Model API sam-3.1 tried for image scans, else whole-page fallback |
| `SMARTY_AUTH_ID` / `SMARTY_AUTH_TOKEN` | Blank = format-only address check (says so honestly) |
| `FIREWORKS_API_KEY` / `FIREWORKS_MODEL` | Blank = filter skipped (Spark/rules as before); when set, junk filter runs before Spark; model ID must be copied exactly from the Fireworks catalogue |
| `PARTNER_ADDRESS` | Reserved for a future mail-center leg (a real launch with a CMRA partner would receive paper mail there); the mailbox address today is the home address, so this is currently unused |
| `MAILBOXAI_DB` | Default `mailboxai.db` |

### Test now — the full loop in order

1. **User onboarding:** delete the demo database (`rm mailboxai.db`) so the
   next Hi starts onboarding (a VERIFIED number otherwise gets the digest
   prompt, not onboarding). Restart the server. Hi → welcome picture +
   name → sample license photo → video saying the spoken code → ✅
   Verified with the sample address. The welcome picture
   (`demo/mailbox-welcome.jpg`, captioned "🌳 Use MailboxAI and Save
   Trees." + save-as-contact-photo instruction)
   exists because the test number cannot carry a profile picture —
   the user saves the picture and sets it as the contact's photo.
2. **Commands:** `MAIL` (digest card), `SAVED` (paper counter), `READ 1`
   (voice note), `SHRED 1` (shred + counter).
3. **Business send (second phone):** open `/business-send` (same Wi-Fi via
   the MacBook's IP, or anywhere via the tunnel URL with `/business-send`
   instead of `/webhook`). Register a business → PIN (needs
   `MAILBOXAI_DEV=1`) → verify → send form with the mailbox **exactly** as
   in the Verified message and the sender name **exactly** as registered.
   The phone gets a card — `📬 New mail #N`, the sender in bold with a
   ✓ Verified sender badge, a category icon (🧾 bill, 🚨 urgent, 💌
   personal, 🗑️ junk), the subject, and due/amount for bills — with
   Shred / Read aloud / Open buttons.
   For per-business logins, open `/business-portal` instead: each business
   logs in with its email + portal password and sees only its own sent mail.
   Shortcut: `python3 demo/seed_demo_businesses.py` registers + verifies 5 demo
   businesses (ShopMart, Hospital, Barber Shop, Demo Insurance, First Demo Bank)
   and sends 7 demo letters to a mailbox (default: the sample home address;
   pass another mailbox as the first argument; `--register-only` skips sends).
4. **Admin:** open `/admin-page`, enter the admin key, Load — stats +
   business list + membership controls.
5. **After testing:** delete the test database, regenerate the access token
   (tokens pasted anywhere get retired this way), and re-check `git status`
   shows no `.env` and no `.db` before any push.

Simulate a letter from the terminal instead of the page:

```bash
curl -X POST http://localhost:8000/mail/upload \
  -F mailbox="999 Sample Road, Atlanta, GA 30309" \
  -F sender="Metro Power" -F subject="Electric bill" \
  -F body_text="Your electricity bill is \$142.60. Payment due November 5, 2026."
```

Check the digest: `GET http://localhost:8000/digest/<mailbox>`.

### Business registration (tested flow)

```bash
# 1. Register + validate address (dev mode returns the postcard PIN)
curl -X POST http://localhost:8000/business/register \
  -F name="First Demo Bank" -F email="mailops@firstdemobank.example" \
  -F address="1200 Peachtree Street NE, Atlanta, GA 30309"

# 2. Confirm the PIN (proves the business receives mail there)
curl -X POST http://localhost:8000/business/verify-pin \
  -F business_id=<id-from-step-1> -F pin=<pin-from-step-1>

# 3. Letters from this business now carry a "✓ Verified sender" badge
```

### Eval harness (50-letter test)

```bash
python3 -m app.eval
```

Generates 50 synthetic letters (bills, urgent, junk incl. scams, personal),
runs triage, and reports accuracy per category and the scam flagging rate.
Note: the 50/50 synthetic score measures the harness, not real-world
accuracy — run it on real letters (redacted) for an honest number.

## API endpoints

| Method & path | What it does |
|---|---|
| `GET /health` | Liveness check |
| `GET /webhook` | Meta webhook verification (`hub.challenge`) |
| `POST /webhook` | Incoming WhatsApp messages (text, image, video, interactive buttons) |
| `POST /mail/upload` | Scan intake: triage + notify household members; optional attachment (JPEG/PNG/PDF/MP4, WhatsApp size limits) delivered as WhatsApp media (blocked with `402 membership_expired` for an expired verified business) |
| `GET /admin/backup` | Owner-only (admin key) backup download: the whole mailbox database + every locally stored attachment, as one ZIP. Download it and keep a copy OFF the server — a ZIP on the same machine is not a backup. Never includes `.env`. |

Attachments are stored twice at arrival: sent to WhatsApp (the notification copy) **and** saved next to the database in `attachments/` (the durable copy). Reopening a letter (`OPEN <n>`, 📖 Open button) re-sends the saved file with the letter; older letters without a local copy fall back to the stored WhatsApp media id while WhatsApp retains it, and only then to the "see the original message" note. `attachments/` is git-ignored, so the public repo flip cannot leak coupons, statements, or tax documents. Production backup (roadmap, not the demo): managed database with automated snapshots + versioned, encrypted object storage + per-mailbox export.
| `GET /digest/{mailbox}` | Today's digest for a mailbox (debug/demo) |
| `GET /mail/search?mailbox=&q=` | Search the mail archive |
| `POST /rules` | Add a household sender rule (`always_shred`, `always_urgent`) |
| `POST /business/register` | Validate a business address, start postcard PIN |
| `POST /business/verify-pin` | Confirm PIN → verified sender |
| `GET /admin/stats` | Admin counts (key required) |
| `GET /admin/businesses` | Admin business list (key required) |
| `POST /admin/business/membership` | Admin set plan/status (key required) |
| `GET /admin-page` | Admin dashboard UI |
| `GET /business-send` | Business demo sender UI (second phone) |
| `GET /business-portal` | Per-business portal: login, sent-mail list, send form with sender locked to the login |
| `POST /business/login` | Portal login by business ID or email + password (hashed, PBKDF2) → session token |
| `GET /business/mail` | Mail sent by the logged-in business only (Bearer token) |

## WhatsApp rules that shape the product

- **24-hour window:** once a user messages you, you can reply freely for
  24 hours. After that, only approved message templates.
- **Business-initiated messages need approved templates** (e.g. a "new
  mail arrived" utility template) — get yours approved early.
- **Users must opt in** before you message them — their first "Hi" is
  that opt-in. Promotional mail goes only to opted-in nearby residents.
- **Pricing:** Meta charges per conversation, not per message. The
  "no ads needed" advantage is real; "free" is not — budget for it.
- **No "Login with WhatsApp" OAuth button exists.** What exists is
  WhatsApp OTP login (code sent over WhatsApp) — the right login for
  an optional companion dashboard, while WhatsApp chat stays the product.

## Troubleshooting 

- **Dashboard Test arrives but real messages don't:** two layers to check.
  (1) The app must be **Published/Live** — while unpublished, Meta delivers
  only dashboard test webhooks, never real messages. (2) The app must be
  subscribed to the WhatsApp account: `GET
  https://graph.facebook.com/v21.0/{WABA-ID}/subscribed_apps` must list your
  app; if it is absent, `POST` the same path with the app's token (returns
  `{"success": true}`) and verify with a second GET. The Step 2
  field-level "messages Subscribed" is a different layer and does not prove
  this one.
- **Test sends fail with 131030:** the dashboard test recipient is a fake
  number that can never be allowlisted — unfixable by design, not a real
  failure. Test with one fresh Hi from your own number instead.
- **401 on sends:** the temporary access token expired (~24h). Regenerate
  on the app's Step 1 page → `.env` → restart.
- **No reply + no error:** when `WHATSAPP_TOKEN` is unset, the server only
  logs sends instead of delivering them.
- **Tunnel dead:** cloudflared quick tunnels die when the process stops,
  and a restart changes the URL — update the dashboard callback every time.
- **"No module named uvicorn":** the terminal did not activate `.venv`.
- **403 on a manual webhook self-test:** the handler reads underscored
  params only (`hub_mode`, `hub_verify_token`, `hub_challenge`). Meta's real
  verify requests carry both forms, so dashboard Verify passes; a browser
  self-test must use the underscored form.


## Before real users: the one legal step

A virtual mailbox is a **Commercial Mail Receiving Agency (CMRA)**.
You need a CMRA partner address and a **notarized USPS Form 1583 with
two IDs from every customer** before accepting their mail. The license
photo in this app is step one of that identity check — the notarized
form is the step that makes it legal. Do not onboard real users without it.

## Project structure

```
mailboxai-github/
├── README.md               # this file
├── LICENSE                 # MIT
├── requirements.txt        # fastapi, uvicorn, python-multipart
├── .env.example            # copy to .env, never commit real tokens
├── .gitignore              # .env, *.db, .venv, __pycache__
├── tests/                  # unittest suite (expiry gate, join alerts, triage fallback, challenge lockout)
├── app/
│   ├── main.py             # FastAPI: webhook, mail intake, business, rules, search, admin, demo pages
│   ├── whatsapp.py         # WhatsApp Cloud API client (text, buttons, audio, media; error-body logging)
│   ├── state.py            # SQLite: users, household, mail, businesses (+plan/membership), rules
│   ├── muse_client.py      # ONE central Muse connection (text + vision images)
│   ├── triage.py           # bill/urgent/junk/personal + amount + due + scam
│   ├── segment.py          # SAM 3.1 segmentation interface (Model API first)
│   ├── sam_client.py       # SAM 3.1 via Meta Model API (responses route)
│   ├── wonder.py           # Muse Wonder voice agent (transcript + mailbox -> answer)
│   ├── image_card.py       # Muse Image digest illustration
│   ├── license.py          # license photo → address (Spark vision first, Tesseract fallback + AAMVA parsing)
│   ├── face.py             # Spark vision face match + blink liveness
│   ├── voice.py            # voice-note TTS seam (TTS_COMMAND)
│   ├── digest.py           # digest card (text) + saved counter
│   ├── business.py         # business validation + postcard PIN
│   ├── fireworks_client.py # open-weights client (WIRED into triage first)
│   ├── config.py           # environment configuration + .env auto-load
│   └── eval.py             # 50-letter eval harness
└── demo/
    ├── index.html          # consumer demo (no install)
    ├── how-a-user-joins.html  # tap-through onboarding story
    ├── business-demo.html  # business sender demo + scan-to-send
    ├── admin.html          # admin dashboard (served at /admin-page)
    ├── business-send.html  # business sender (served at /business-send)
    ├── server.py           # optional local server for the static demos
    └── qr/                 # registration QR codes + social share card (test number)
```


