"""Selfie + liveness verification, powered by Muse Spark vision.

Meta tool used here: Muse Spark (multimodal) via the Meta Model API —
the same live connection triage already uses, extended with images
(see muse_client.chat_with_images). Spark looks at the photos and
judges: same person as the license photo? Live moving person, or a
static photo / screen? SAM 3.1, Muse Wonder and Muse Image are NOT
used here: segmentation, voice and image generation do not answer
"is this the same live person", vision comparison does.

Flow (see main.py):
  1. License photo passes OCR (license.py) -> app asks for a selfie.
  2. Selfie VIDEO -> check_liveness (blink / movement across frames)
     AND match_faces_best (best video frame vs license photo).
  3. Selfie PHOTO -> match_faces only; the reply says so honestly
     (no blink check ran for a photo).
  4. Match (and liveness, for video) -> mailbox verification completes.

Honesty rules (same as license.py):
  - A failed check NEVER verifies. No key, bad reply, no clear face ->
    ok=False, and the handler asks the user to retry. There is NO mock
    face match: a verification step that always says "yes" would be a
    lie, so demo mode skips the selfie step entirely instead.
  - This is demo-grade, not bank-grade. A video of a screen can fool
    a basic blink check; production needs an ID-verification provider
    (Stripe Identity, Veriff, Onfido, ...) with real liveness.
  - Photos are compared in memory. Frames from a video live in a temp
    directory that is deleted after the check.
"""
import json
import os
import shutil
import subprocess
import tempfile

from . import muse_client


def _parse_json(content):
    """Strip ``` fences (models love them) and parse one JSON object."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    out = json.loads(stripped)
    if not isinstance(out, dict):
        raise ValueError("Spark reply was not a JSON object")
    return out


def match_faces(license_bytes, selfie_bytes):
    """Compare a license photo with a selfie.

    Returns {ok, match, confidence, reason, method}. ok=False means the
    CHECK failed (no key, bad reply) — not a mismatch. A mismatch is
    ok=True with match=False.
    """
    if not muse_client.is_configured():
        return {"ok": False, "reason": "not_configured",
                "detail": "Muse Spark is not configured."}
    prompt = (
        "First image: the photo printed on a driver's license. "
        "Second image: a live selfie. Are they the same person? "
        "Judge by facial structure only — face shape, eyes, nose, "
        "mouth, ears, and how the features sit together. Do NOT judge "
        "by lighting, photo quality, skin-tone shifts caused by "
        "lighting, camera angle, or apparent age or weight differences "
        "between an older ID photo and a live selfie: the license "
        "photo is small and compressed, and lighting changes how skin "
        "tone and age look. If either image has no clear face, "
        "same_person must be false. "
        "Reply with ONLY this JSON, no other text: "
        '{"same_person": true, "confidence": 85, "reason": "..."} '
        "where confidence is 0-100.")
    try:
        reply = muse_client.chat_with_images(
            prompt, [license_bytes, selfie_bytes], tag="vision-face")
        out = _parse_json(reply)
        same = out.get("same_person")
        if not isinstance(same, bool):
            raise ValueError("same_person was not true/false")
        conf = out.get("confidence", 0)
        conf = max(0, min(100, int(conf)))
        return {"ok": True, "match": same, "confidence": conf,
                "reason": str(out.get("reason", ""))[:200],
                "method": "muse-spark"}
    except Exception as exc:
        return {"ok": False, "reason": "spark_failed",
                "detail": f"Muse Spark face check failed: {exc}"[:200]}


def match_faces_best(license_bytes, frames):
    """Compare the license photo against every video frame.

    One blink, blur, or turned-head frame must not fail a real user,
    so a match on ANY frame passes. With no matching frame, the
    highest-confidence mismatch is returned (its reason is the most
    informative). A stranger still fails every frame independently.
    With no frames at all, falls back to a single empty-frame check,
    which reports the face check as unavailable.
    """
    if not frames:
        return match_faces(license_bytes, b"")
    best = None
    for frame in frames:
        result = match_faces(license_bytes, frame)
        if result.get("ok") and result.get("match"):
            return result
        if result.get("ok") and (best is None
                                 or result.get("confidence", 0)
                                 > best.get("confidence", 0)):
            best = result
    if best is not None:
        return best
    return {"ok": False, "reason": "spark_failed",
            "detail": "Muse Spark face check failed on every frame."}


def extract_frames(video_bytes):
    """Pull up to 3 JPEG frames (start / middle / end) from a video.

    Returns None when ffmpeg is not installed, [] when extraction
    produced nothing usable. The temp directory is always deleted.
    """
    if not shutil.which("ffmpeg"):
        return None
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "in.mp4")
        with open(src, "wb") as f:
            f.write(video_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src,
             "-vf", "fps=1", "-frames:v", "3",
             os.path.join(tmp, "frame_%d.jpg")],
            capture_output=True, timeout=60)
        frames = []
        for name in sorted(os.listdir(tmp)):
            if name.startswith("frame_") and name.endswith(".jpg"):
                with open(os.path.join(tmp, name), "rb") as f:
                    frames.append(f.read())
        return frames
    except Exception:
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_liveness(video_bytes):
    """Judge whether a video shows a live person (blink / movement).

    Returns {ok, live, reason, method}. ok=False means the check itself
    could not run (no ffmpeg, video too short, Spark failed).
    """
    frames = extract_frames(video_bytes)
    if frames is None:
        return {"ok": False, "reason": "no_ffmpeg",
                "detail": "ffmpeg is not installed on the server."}
    if len(frames) < 2:
        return {"ok": False, "reason": "video_too_short",
                "detail": "Could not get frames from that video."}
    if not muse_client.is_configured():
        return {"ok": False, "reason": "not_configured",
                "detail": "Muse Spark is not configured."}
    prompt = (
        "These are frames from a short video, in order. Is this a live "
        "person moving in front of the camera (blinking, head moving, "
        "expression changing), or a static photo / screen being held up? "
        "Reply with ONLY this JSON, no other text: "
        '{"live": true, "reason": "..."}')
    try:
        reply = muse_client.chat_with_images(prompt, frames,
                                             tag="vision-liveness")
        out = _parse_json(reply)
        live = out.get("live")
        if not isinstance(live, bool):
            raise ValueError("live was not true/false")
        return {"ok": True, "live": live,
                "reason": str(out.get("reason", ""))[:200],
                "method": "muse-spark"}
    except Exception as exc:
        return {"ok": False, "reason": "spark_failed",
                "detail": f"Muse Spark liveness check failed: {exc}"[:200]}
