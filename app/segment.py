"""SAM 3.1 segmentation interface.

Order of attempts for a scan:
  1. Meta Model API SAM 3.1 (sam_client) — only for real image bytes
     (JPEG/PNG magic); text bytes skip this step entirely, so a
     text-only upload never spends a Model API call.
  2. Legacy SAM_API_URL endpoint, if one is set (the original seam).
  3. Whole-page fallback: one region covering the full page.

The pipeline still works at every step — segmentation refines it,
never gates it. Regions are [{label, bbox:[x0,y0,x1,y1]}] in 0..1
coords; mail_upload returns them in its response as "segments".
"""

import json
import urllib.request

from . import config, sam_client

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG"


def _looks_like_image(data):
    return data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)


def segment_scan(image_bytes):
    """Returns a list of {label, bbox:[x0,y0,x1,y1]} (0..1 coords)."""
    if _looks_like_image(image_bytes):
        try:
            regions = sam_client.segment(image_bytes)
            if regions:
                return regions
        except Exception:
            pass  # fall through to legacy / whole-page below
    if config.SAM_API_URL:
        try:
            req = urllib.request.Request(
                config.SAM_API_URL, data=image_bytes,
                headers={"Content-Type": "application/octet-stream"},
                method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["regions"]
        except Exception:
            pass
    return [{"label": "full_page", "bbox": [0, 0, 1, 1]}]
