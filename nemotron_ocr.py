"""NVIDIA Nemotron OCR v2 backend (NeMo Retriever Image OCR NIM).

Drop-in alternative to the Tesseract path in ``ocr.extraction``: sends a
rendered page image to the NIM ``/v1/infer`` endpoint and converts the
returned text detections into the same ``OCRBlock`` dicts the layout
pipeline consumes.

Nemotron returns word/line boxes + text + reading order, but NOT Tesseract's
block/paragraph/line hierarchy IDs that ``ocr.layout`` groups on, so we
synthesise that hierarchy geometrically (cluster detections into lines, then
lines into paragraphs).

Configuration (env or per-request):
  NVIDIA_API_KEY   Bearer token for the hosted NIM (build.nvidia.com / NGC).
  NVIDIA_OCR_URL   Full infer URL. Default: http://localhost:8001/v1/infer
                   (a locally-deployed NIM, mapped to host port 8001 so it
                   doesn't collide with this app's own FastAPI server on
                   port 8000). For the hosted catalog API set this to the
                   invoke URL shown on the model's Deploy page, if one is
                   available for your model.
"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional

import fitz  # PyMuPDF
from PIL import Image

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _HAS_NUMPY = False

from ocr.extraction import OCRBlock, _sample_glyph_color

# IMPORTANT: this backend's own FastAPI app (main.py) binds to port 8000.
# A self-hosted Nemotron OCR v2 NIM container ALSO defaults to port 8000
# inside its own container, so without remapping, a request here would hit
# this same backend on itself and 404 (no /v1/infer route exists in main.py).
# When you run the NIM container later, map it to a different host port, e.g.:
#   docker run -p 8001:8000 ... nvcr.io/nim/nvidia/nemotron-ocr-v2:latest
# This default assumes that 8001 mapping. Override with NVIDIA_OCR_URL if
# you map it to something else.
DEFAULT_OCR_URL = "http://localhost:8001/v1/infer"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 2
BACKOFF_BASE = 1.5


class NemotronOCRError(RuntimeError):
    """Raised when the Nemotron OCR NIM cannot return usable results."""


def is_configured(api_key: Optional[str] = None) -> bool:
    """True if a key was passed or NVIDIA_API_KEY is set in the environment."""
    return bool(api_key or os.environ.get("NVIDIA_API_KEY"))


def _resolve(api_key: Optional[str], url: Optional[str]):
    key = api_key or os.environ.get("NVIDIA_API_KEY") or ""
    env_url = os.environ.get("NVIDIA_OCR_URL")
    if url:
        endpoint = url
    elif env_url:
        endpoint = env_url
    elif key:
        endpoint = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"
    else:
        endpoint = DEFAULT_OCR_URL
    return key, endpoint


# ── HTTP ────────────────────────────────────────────────────────────────────

def _post_infer(url: str, payload: dict, api_key: str) -> dict:
    """POST to the NIM infer endpoint, surfacing the server error body."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_msg = "unknown error"
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            detail = body
            try:
                detail = json.loads(body).get("error", body)
            except Exception:
                pass
            last_msg = f"HTTP {exc.code} {exc.reason}: {str(detail)[:500]}"
            if attempt < MAX_RETRIES and exc.code in (429, 500, 502, 503, 504):
                time.sleep(BACKOFF_BASE * (2 ** attempt)); continue
            raise NemotronOCRError(last_msg)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_msg = f"network error contacting {url}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * (2 ** attempt)); continue
            raise NemotronOCRError(last_msg)
    raise NemotronOCRError(last_msg)


# ── Hierarchy synthesis ───────────────────────────────────────────────────────

def _assign_hierarchy(dets: List[dict]) -> None:
    """Add block_num/par_num/line_num/word_num to detections (sorted in place).

    ``dets`` items have keys: x, y, w, h (PDF points), text, conf.
    Groups by vertical overlap into lines, then by vertical gap into paragraphs.
    """
    if not dets:
        return
    dets.sort(key=lambda d: (d["y"], d["x"]))
    heights = sorted(d["h"] for d in dets)
    med_h = heights[len(heights) // 2] or 1.0

    # Cluster into lines: a detection joins the current line if its vertical
    # centre is within ~60% of the median glyph height of the line's centre.
    lines: List[List[dict]] = []
    for d in dets:
        cy = d["y"] + d["h"] / 2
        placed = False
        for ln in lines:
            lcy = sum(w["y"] + w["h"] / 2 for w in ln) / len(ln)
            if abs(cy - lcy) <= med_h * 0.6:
                ln.append(d); placed = True; break
        if not placed:
            lines.append([d])

    # Order lines top-to-bottom; words left-to-right within a line.
    lines.sort(key=lambda ln: min(w["y"] for w in ln))
    for ln in lines:
        ln.sort(key=lambda w: w["x"])

    # Cluster lines into paragraphs by vertical gap.
    par_idx = 0
    prev_bottom = None
    prev_h = med_h
    line_idx_in_par = 0
    for li, ln in enumerate(lines):
        top = min(w["y"] for w in ln)
        lh = max(sum(w["h"] for w in ln) / len(ln), 1.0)
        if prev_bottom is not None and (top - prev_bottom) > max(prev_h, lh) * 1.8:
            par_idx += 1
            line_idx_in_par = 0
        for wi, w in enumerate(ln):
            w["block_num"] = 0
            w["par_num"] = par_idx
            w["line_num"] = line_idx_in_par
            w["word_num"] = wi
        prev_bottom = max(w["y"] + w["h"] for w in ln)
        prev_h = lh
        line_idx_in_par += 1


# ── Public entry point ────────────────────────────────────────────────────────

def extract_ocr_blocks_nemotron(
    page: fitz.Page,
    page_num: int,
    dpi: int = 200,
    api_key: Optional[str] = None,
    url: Optional[str] = None,
    merge_level: str = "word",
    min_confidence: float = 0.3,
) -> List[OCRBlock]:
    """Run Nemotron OCR v2 on a page and return OCRBlocks (bbox in PDF points)."""
    key, endpoint = _resolve(api_key, url)

    # Render page to PNG (RGB) at the requested DPI.
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pix.tobytes("png")
    color_img = Image.open(io.BytesIO(png)).convert("RGB")
    color_arr = np.asarray(color_img) if _HAS_NUMPY else None
    img_w, img_h = color_img.size

    b64 = base64.b64encode(png).decode("ascii")
    payload = {
        "input": [{"type": "image_url", "url": f"data:image/png;base64,{b64}"}]
    }
    body = _post_infer(endpoint, payload, key)

    # Parse: data[0].text_detections[].{text_prediction, bounding_box.points}
    try:
        detections = body["data"][0]["text_detections"]
    except (KeyError, IndexError, TypeError):
        raise NemotronOCRError(f"Unexpected NIM response shape: {str(body)[:300]}")

    page_w, page_h = page.rect.width, page.rect.height
    dets = []
    for det in detections:
        tp = det.get("text_prediction", {})
        text = (tp.get("text") or "").strip()
        conf = float(tp.get("confidence", 0.0))
        if not text or conf < min_confidence:
            continue
        pts = det.get("bounding_box", {}).get("points", [])
        if not pts:
            continue
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        # Normalised [0,1] -> PDF points.
        x = min(xs) * page_w
        y = min(ys) * page_h
        w = (max(xs) - min(xs)) * page_w
        h = (max(ys) - min(ys)) * page_h
        # Pixel bbox for glyph-colour sampling.
        color = "#000000"
        if color_arr is not None:
            color = _sample_glyph_color(
                color_arr, int(min(xs) * img_w), int(min(ys) * img_h),
                max(int((max(xs) - min(xs)) * img_w), 1),
                max(int((max(ys) - min(ys)) * img_h), 1),
            )
        dets.append({
            "x": round(x, 2), "y": round(y, 2),
            "w": round(w, 2), "h": round(h, 2),
            "text": text, "conf": round(conf * 100, 1), "color": color,
        })

    _assign_hierarchy(dets)

    blocks: List[OCRBlock] = []
    for d in dets:
        blocks.append(OCRBlock(
            text=d["text"],
            confidence=d["conf"],
            bbox={"x": d["x"], "y": d["y"], "width": d["w"], "height": d["h"]},
            block_num=d.get("block_num", 0),
            par_num=d.get("par_num", 0),
            line_num=d.get("line_num", 0),
            word_num=d.get("word_num", 0),
            font_size_estimate=round(max(d["h"] * 0.8, 1), 2),
            color=d["color"],
            page=page_num,
        ))
    return blocks
