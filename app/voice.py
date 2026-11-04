"""Voice notes (Muse Wonder in production).

Production: Muse Wonder generates the spoken letter, the audio file is
uploaded to WhatsApp via the media endpoint, and sent as a voice note.
This module keeps that seam: set TTS_COMMAND to a command template like
  espeak-ng -w {out} "{text}"
or point it at your Wonder TTS service. Without one configured, the
caller falls back to sending the letter as text — never silence.
"""
import os
import subprocess

TTS_COMMAND = os.getenv("TTS_COMMAND", "")


def synthesize(text, out_path):
    """Returns out_path on success, else None."""
    if not TTS_COMMAND:
        return None
    cmd = TTS_COMMAND.format(out=out_path, text=text.replace('"', "'"))
    try:
        subprocess.run(cmd, shell=True, timeout=120, check=True)
        return out_path if os.path.exists(out_path) else None
    except Exception:
        return None
