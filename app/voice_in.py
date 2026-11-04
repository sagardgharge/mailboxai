"""Voice-note input: Meta Muse Voice Transcribe turns speech into text.

Flow for a WhatsApp voice note: download the .ogg (WhatsApp's format) ->
ffmpeg converts it to mono 16-bit WAV at 16 kHz -> one POST to Meta's
ASR file endpoint -> transcript text -> the bot runs it as a command.
transcribe_video() does the same for a selfie video: ffmpeg strips
the audio track out first (-vn), then the same ASR call checks the
spoken challenge code.

Endpoint shape (from Meta's public model cookbook and independent
write-ups of the same API — NOT yet confirmed by a live call, so the
first voice test on a real key is the verification step):
  POST {META_MODEL_API_BASE_URL}/asr/transcribe
  Authorization: Bearer <same Model API key as Spark>
  multipart fields: `request` (JSON, names the model + audioEncoding)
  and `audio`
  (the WAV file). Response JSON carries the whole transcript plus a
  `turns` array; either form is accepted here.

Privacy, same rule as license photos: the voice note goes to Meta's
Model API for transcription, lives only in a temp dir that is always
deleted, and is never stored in the database. A failed transcription
is never silent: the caller gets a reason and tells the user to type
the command instead. No new dependency (stdlib only) and no new env
vars — it reuses META_MODEL_API_BASE_URL / META_MODEL_API_KEY.

Voice OUT is a different job and is NOT Meta here: Meta's voice
models output text, not speech, so letters spoken back to the user
still go through the existing macOS `say` + ffmpeg pipeline (READ).
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

from . import config

log = logging.getLogger("mailboxai.voice")

_MODEL = "muse-voice-transcribe-1.0"


def is_configured():
    return bool(config.META_MODEL_API_BASE_URL
                and config.META_MODEL_API_KEY)


def asr_url():
    base = config.META_MODEL_API_BASE_URL.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/asr/transcribe"


def _to_wav(audio_bytes, tmpdir):
    """WhatsApp .ogg -> mono 16-bit WAV at 16 kHz. None on failure."""
    if shutil.which("ffmpeg") is None:
        return None
    src = os.path.join(tmpdir, "voice.ogg")
    dst = os.path.join(tmpdir, "voice.wav")
    with open(src, "wb") as f:
        f.write(audio_bytes)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src,
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst],
        capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dst):
        log.error("[voice] ffmpeg convert failed: %s", proc.stderr[-300:])
        return None
    with open(dst, "rb") as f:
        return f.read()


def _video_to_wav(video_bytes, tmpdir):
    """Selfie video -> mono 16-bit WAV of its audio track only.

    -vn drops the video stream; the spoken code is all the ASR call
    needs. None when ffmpeg is missing or the track can't be pulled.
    """
    if shutil.which("ffmpeg") is None:
        return None
    src = os.path.join(tmpdir, "video.mp4")
    dst = os.path.join(tmpdir, "voice.wav")
    with open(src, "wb") as f:
        f.write(video_bytes)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-vn",
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst],
        capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dst):
        log.error("[voice] video audio extract failed: %s",
                  proc.stderr[-300:])
        return None
    with open(dst, "rb") as f:
        return f.read()


def _multipart(request_json, wav_bytes):
    boundary = uuid.uuid4().hex
    body = b""
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="request"\r\nContent-Type: application/json\r\n\r\n'
             ).encode() + request_json.encode() + b"\r\n"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="audio"; filename="voice.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode()
    body += wav_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, boundary


def _post_wav(wav):
    """One ASR call for already-converted WAV bytes. Shared by the
    voice-note path and the selfie-video path."""
    body, boundary = _multipart(
        json.dumps({"model": _MODEL, "audioEncoding": "WAV"}), wav)
    req = urllib.request.Request(
        asr_url(), data=body, method="POST",
        headers={
            "Authorization": f"Bearer {config.META_MODEL_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")[:500]
        log.error("[voice] transcribe failed: status=%s body=%s",
                  exc.code, err)
        return {"ok": False, "reason": "transcribe_failed"}
    except Exception as exc:  # timeout, DNS, connection reset
        log.error("[voice] transcribe request failed: %s", exc)
        return {"ok": False, "reason": "transcribe_failed"}
    text = (data.get("transcript") or data.get("text") or "").strip()
    if not text and isinstance(data.get("turns"), list):
        text = " ".join(
            str(t.get("text", "")).strip()
            for t in data["turns"] if t.get("text")).strip()
    if not text:
        return {"ok": False, "reason": "empty_transcript"}
    log.info("[voice] transcript %d chars", len(text))
    return {"ok": True, "text": text}


def transcribe_video(video_bytes):
    """Transcribe the audio track of a selfie video (spoken code).

    Same result shape as transcribe(). The video frames are handled
    by the caller (liveness + face match); this only hears the code.
    """
    if not is_configured():
        return {"ok": False, "reason": "not_configured"}
    if shutil.which("ffmpeg") is None:
        return {"ok": False, "reason": "no_ffmpeg"}
    tmpdir = tempfile.mkdtemp(prefix="mailboxai-voice-")
    try:
        wav = _video_to_wav(video_bytes, tmpdir)
        if wav is None:
            return {"ok": False, "reason": "convert_failed"}
        return _post_wav(wav)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def transcribe(audio_bytes):
    """Transcribe one voice note. Returns {"ok": True, "text": ...} or
    {"ok": False, "reason": ...} with reason one of: not_configured,
    no_ffmpeg, convert_failed, transcribe_failed, empty_transcript."""
    if not is_configured():
        return {"ok": False, "reason": "not_configured"}
    if shutil.which("ffmpeg") is None:
        return {"ok": False, "reason": "no_ffmpeg"}
    tmpdir = tempfile.mkdtemp(prefix="mailboxai-voice-")
    try:
        wav = _to_wav(audio_bytes, tmpdir)
        if wav is None:
            return {"ok": False, "reason": "convert_failed"}
        return _post_wav(wav)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
