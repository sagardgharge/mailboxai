"""SAM 3.1 segmentation via the Meta Model API.

Model sam-3.1, same base URL and key as the rest of the Meta stack.
One call with a scan photo and short concept phrases returns boxes
(and masks) for every match: letter, envelope, check, coupon.

HONEST FLAG: the Responses-API request/response shape here follows
Meta's published SAM-on-Model-API description (model sam-3.1 over
POST /v1/responses), NOT a verified live call — no complete worked
example was available to copy. So this module parses defensively
and returns None on anything unexpected, and segment.py falls back
to the whole page. The first live scan is the validation: Terminal
1 logs "[sam] ..." lines showing what Meta actually returned, and
the /mail/upload response echoes the regions found. Never let a
segmentation failure break a mail upload.

Only image bytes are ever sent (JPEG/PNG magic checked by the
caller in segment.py); text bytes never reach this module.
"""

import base64
import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("mailboxai.sam")

SAM_MODEL = "sam-3.1"
CONCEPTS = ["letter", "envelope", "check", "coupon"]


def _responses_url():
    base = config.META_MODEL_API_BASE_URL.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/responses"


def _valid_bbox(bbox):
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return False
    return 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def _find_regions(node, out):
    """Collect {label, bbox} dicts anywhere in a nested reply.

    Also parses JSON text found in string leaves, since a model may
    answer with a JSON block inside a text field rather than as
    structured output.
    """
    if isinstance(node, str):
        try:
            _find_regions(json.loads(node), out)
        except (ValueError, TypeError):
            pass
        return
    if isinstance(node, dict):
        bbox = node.get("bbox") or node.get("box")
        label = node.get("label") or node.get("concept")
        if bbox and label and _valid_bbox(bbox):
            out.append({"label": str(label),
                        "bbox": [float(v) for v in bbox]})
        for value in node.values():
            _find_regions(value, out)
    elif isinstance(node, list):
        for value in node:
            _find_regions(value, out)


def segment(image_bytes):
    """Regions [{label, bbox}] for a scan photo, or None.

    None when the Model API is not configured, Meta rejects the
    call, or the reply carries no parseable regions — the caller
    falls back to the whole page in every case.
    """
    if not config.META_MODEL_API_BASE_URL or not config.META_MODEL_API_KEY:
        return None
    prompt = ("Find every instance of these concepts in this scan: "
              + ", ".join(CONCEPTS) +
              ". Return each match with its label and bounding box.")
    payload = {
        "model": SAM_MODEL,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image",
                 "image_url": "data:image/jpeg;base64,"
                              + base64.b64encode(image_bytes).decode()},
            ],
        }],
    }
    req = urllib.request.Request(
        _responses_url(), data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {config.META_MODEL_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        log.error("[sam] segment failed: status=%s body=%s",
                  exc.code, exc.read().decode("utf-8", "replace")[:300])
        return None
    except Exception as exc:
        log.error("[sam] segment request failed: %s", exc)
        return None
    log.info("[sam] response top-level keys: %s",
             sorted(data.keys()) if isinstance(data, dict) else type(data))
    regions = []
    _find_regions(data, regions)
    if not regions:
        log.info("[sam] no parseable regions in reply")
        return None
    log.info("[sam] %d regions: %s", len(regions),
             sorted({r["label"] for r in regions}))
    return regions
