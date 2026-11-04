"""WhatsApp Cloud API client (Meta Graph API), stdlib only.

Real endpoints, no SDK needed:
  POST  https://graph.facebook.com/v21.0/{phone-number-id}/messages
  GET   https://graph.facebook.com/v21.0/{media-id}          (media URL)
  POST  https://graph.facebook.com/v21.0/{phone-number-id}/media (upload)

Without WHATSAPP_TOKEN set, sends are logged (DRY RUN) instead of
delivered, so the whole flow can be tested locally.
"""
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("mailboxai.whatsapp")
GRAPH = "https://graph.facebook.com/v21.0"


def _post(path, payload, token=None):
    token = token or config.WHATSAPP_TOKEN
    if not token or not config.WHATSAPP_PHONE_NUMBER_ID:
        log.info("[DRY RUN] POST %s %s", path, json.dumps(payload)[:300])
        return {"dry_run": True}
    req = urllib.request.Request(
        f"{GRAPH}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Meta's exact reason (error code + message) lives in the response
        # body, which urllib otherwise throws away -- log it so a 400/401
        # is diagnosable instead of a bare status code.
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = "<unreadable>"
        log.error("WhatsApp send failed: path=%s to=%s status=%s body=%s",
                  path, payload.get("to"), exc.code, body)
        raise


def send_text(to, body):
    """Send a plain WhatsApp text message."""
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    })


def send_audio_link(to, audio_url, caption=None):
    """Send a voice note from a public audio URL (ogg/opus or aac)."""
    audio = {"link": audio_url}
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": audio,
    })


def send_interactive_buttons(to, body, buttons):
    """Send a message with up to 3 reply buttons.

    buttons: list of (id, title); title max 20 chars (WhatsApp limit).
    Tapping a button delivers a message of type "interactive" whose
    button_reply.id is the id given here.
    """
    btns = [{"type": "reply", "reply": {"id": bid, "title": title[:20]}}
            for bid, title in buttons[:3]]
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": body},
                        "action": {"buttons": btns}},
    })


def upload_media(file_path, mime_type):
    """Upload a local file; returns the media id for later sends.

    Manual multipart body (stdlib only, no requests dependency).
    """
    import os
    import uuid
    if not config.WHATSAPP_TOKEN or not config.WHATSAPP_PHONE_NUMBER_ID:
        log.info("[DRY RUN] upload %s", file_path)
        return "dry_run_media_id"
    boundary = uuid.uuid4().hex
    with open(file_path, "rb") as f:
        data = f.read()
    body = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"messaging_product\"\r\n\r\n"
        "whatsapp\r\n"
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"file\"; "
        f"filename=\"{os.path.basename(file_path)}\"\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{GRAPH}/{config.WHATSAPP_PHONE_NUMBER_ID}/media",
        data=body,
        headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", "replace")
        except Exception:
            body_text = "<unreadable>"
        log.error("WhatsApp media upload failed: status=%s body=%s",
                  exc.code, body_text)
        raise


def send_audio_id(to, media_id):
    """Send a voice note from an uploaded media id.

    Ogg/opus audio renders as a voice message in WhatsApp.
    """
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"id": media_id},
    })


def send_image_id(to, media_id, caption=None):
    """Send a photo (jpeg/png) from an uploaded media id."""
    image = {"id": media_id}
    if caption:
        image["caption"] = caption
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image,
    })


def send_document_id(to, media_id, caption=None, filename=None):
    """Send a document (e.g. a PDF statement) from an uploaded media id."""
    doc = {"id": media_id}
    if caption:
        doc["caption"] = caption
    if filename:
        doc["filename"] = filename
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    })


def send_video_id(to, media_id, caption=None):
    """Send a short video (mp4) from an uploaded media id."""
    video = {"id": media_id}
    if caption:
        video["caption"] = caption
    return _post(f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "video",
        "video": video,
    })


def get_media_url(media_id):
    token = config.WHATSAPP_TOKEN
    req = urllib.request.Request(
        f"{GRAPH}/{media_id}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["url"]


def download_media(media_id):
    """Download a user-uploaded media file (e.g. license photo)."""
    url = get_media_url(media_id)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def verify_signature(app_secret, raw_body, signature_header):
    """Check X-Hub-Signature-256. Returns True when no secret is set
    (local testing only) or the HMAC matches."""
    if not app_secret or not signature_header:
        return True
    expected = "sha256=" + hmac.new(
        app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
