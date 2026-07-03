"""Gemini AI image-editing service.

Sends a full-page scan to Gemini 3.1 Flash Image, asking it to replace
text only inside a highlighted bounding box while preserving every other
pixel.  Falls back to a local Pillow renderer when the API is unavailable.
"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont


class GeminiEditError(RuntimeError):
    """Raised when Gemini image editing cannot produce a usable crop."""


# Network / retry tuning
REQUEST_TIMEOUT = 45          # seconds (raised from 30 for large scanned pages)
MAX_RETRIES = 2               # additional attempts after the first try
BACKOFF_BASE = 1.5            # seconds; delay = BACKOFF_BASE * (2 ** attempt)
GEMINI_MODEL = "gemini-3.1-flash-image"   # Nano Banana 2 (image edit capable)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_full_page_prompt(
    original_text: str,
    replacement_text: str,
    bbox_px: dict,
    page_size: Tuple[int, int],
    context_text: str = "",
) -> str:
    """Build a precise prompt that tells the model *where* to edit."""
    x, y, w, h = bbox_px["x"], bbox_px["y"], bbox_px["width"], bbox_px["height"]
    pw, ph = page_size
    context_block = ""
    if context_text.strip():
        context_block = (
            "\nFor styling reference, the text immediately surrounding that "
            f"region reads: \"{context_text.strip()}\".  Match its font family, "
            "weight, size and colour exactly.\n"
        )
    return (
        "You are a document image editor.  The image you received is a full "
        "scanned page of a PDF document.\n\n"
        f"The page image is {pw}x{ph} pixels.\n"
        f"Inside this page, there is a text region at pixel coordinates:\n"
        f"  top-left  = ({x}, {y})\n"
        f"  size      = {w}x{h} px\n\n"
        f"That region currently reads: \"{original_text}\"\n\n"
        f"Replace ONLY that text with: \"{replacement_text}\"\n"
        f"{context_block}\n"
        "Rules:\n"
        "- Match the exact font family, weight, size, colour and baseline of "
        "the surrounding text.\n"
        "- Preserve the background colour / texture / table lines / borders "
        "that exist behind and around the text.\n"
        "- Do NOT alter ANY other part of the page - every pixel outside the "
        "specified region must remain identical.\n"
        "- Return the complete page image with the edit applied."
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _find_base64_image(obj) -> Optional[str]:
    """Recursively search a JSON response tree for base64 image data."""
    if isinstance(obj, dict):
        if "inlineData" in obj and isinstance(obj["inlineData"], dict):
            return obj["inlineData"].get("data")
        if "image" in obj and isinstance(obj["image"], dict):
            return obj["image"].get("data")
        # Generic "data" field that looks like a large b64 blob
        if "data" in obj and isinstance(obj["data"], str) and len(obj["data"]) > 200:
            return obj["data"]
        for v in obj.values():
            found = _find_base64_image(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_base64_image(item)
            if found:
                return found
    return None


def _is_transient(exc: Exception) -> bool:
    """Decide whether an error is worth retrying."""
    if isinstance(exc, urllib.error.HTTPError):
        # Retry on rate-limit and server-side errors only.
        return exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    return False


def _post_to_gemini(url: str, payload: dict, api_key: str) -> dict:
    """POST to Gemini, surfacing Google's error body, with retry/backoff.

    Raises GeminiEditError with the precise API message (status + JSON body) so
    failures are visible to the user instead of being hidden behind a fallback.
    """
    data = json.dumps(payload).encode("utf-8")
    last_msg = "unknown error"

    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            # Pull the human-readable message out of Google's JSON error if present.
            detail = body
            try:
                j = json.loads(body)
                detail = j.get("error", {}).get("message", body)
            except Exception:
                pass
            last_msg = f"HTTP {exc.code} {exc.reason}: {detail[:500]}"
            transient = exc.code in (408, 429, 500, 502, 503, 504)
            if attempt < MAX_RETRIES and transient:
                delay = BACKOFF_BASE * (2 ** attempt)
                print(f"[WARN] Gemini {last_msg}; retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s")
                time.sleep(delay)
                continue
            raise GeminiEditError(last_msg)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_msg = f"network error: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            raise GeminiEditError(last_msg)

    raise GeminiEditError(last_msg)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_to_gemini(
    full_page: Image.Image,
    crop: Image.Image,
    original_text: str,
    replacement_text: str,
    api_key: str,
    bbox_px: dict,
    context_text: str = "",
) -> Image.Image:
    """Edit text on a scanned document page using Gemini, returning a crop.

    Parameters
    ----------
    full_page : PIL.Image
        The complete page rendered at 300 DPI.
    crop : PIL.Image
        The cropped region (used as fallback dimensions).
    original_text / replacement_text : str
        What to find and what to write.
    api_key : str
        User-supplied Gemini API key.
    bbox_px : dict
        Pixel-level bounding box ``{x, y, width, height}`` at the same
        DPI as *full_page*.
    context_text : str
        Nearby OCR text used to help the model match font styling.

    Returns
    -------
    PIL.Image
        An image with the *same pixel dimensions as crop*, containing the
        edited region.
    """
    if not api_key:
        raise GeminiEditError("Gemini API key is required for AI image editing.")

    # 1.  Encode the full page as PNG -> base64
    buf = io.BytesIO()
    full_page.save(buf, format="PNG")
    page_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    # 2.  Call Gemini generateContent REST API (with retry/backoff)
    prompt = _build_full_page_prompt(
        original_text, replacement_text, bbox_px, full_page.size, context_text
    )
    url = (
        "https://generativelanguage.googleapis.com/v1/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": page_b64}},
            ]
        }],
        "generationConfig": {},
    }


    body = _post_to_gemini(url, payload, api_key)   # raises GeminiEditError with detail
    img_b64 = _find_base64_image(body)
    if not img_b64:
        # 200 OK but no image: usually a safety block or a text-only refusal.
        reason = ""
        try:
            cand = (body.get("candidates") or [{}])[0]
            reason = cand.get("finishReason") or ""
            pf = body.get("promptFeedback", {})
            if pf.get("blockReason"):
                reason = f"blocked: {pf['blockReason']}"
        except Exception:
            pass
        raise GeminiEditError(
            "Gemini returned no image" + (f" ({reason})" if reason else "")
            + ". The request reached the API but produced no edited image."
        )

    edited_page = Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB")
    # Image models may return a different resolution; resize back to the original
    # page size so the bbox pixel coordinates stay valid.
    if edited_page.size != full_page.size:
        edited_page = edited_page.resize(full_page.size, Image.LANCZOS)
    x, y = int(bbox_px["x"]), int(bbox_px["y"])
    w, h = int(bbox_px["width"]), int(bbox_px["height"])
    return edited_page.crop((x, y, x + w, y + h))


# ---------------------------------------------------------------------------
# Local fallback renderer
# ---------------------------------------------------------------------------

def _centre_weighted_bg(crop: Image.Image) -> tuple:
    """Estimate background colour from the crop's border ring (centre-weighted)."""
    pixels = crop.load()
    w, h = crop.size
    samples = []
    # Sample the full border ring rather than only four corners.
    step = max(1, w // 20)
    for x in range(0, w, step):
        samples.append(pixels[x, 0])
        samples.append(pixels[x, h - 1])
    step_y = max(1, h // 20)
    for y in range(0, h, step_y):
        samples.append(pixels[0, y])
        samples.append(pixels[w - 1, y])
    samples = [s[:3] for s in samples if isinstance(s, (tuple, list))]
    if not samples:
        return (255, 255, 255)
    return tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))


def _estimate_fg(crop: Image.Image) -> tuple:
    """Estimate foreground (text) colour as the darkest pixel in the centre band."""
    pixels = crop.load()
    w, h = crop.size
    centre_y = h // 2
    band = range(max(0, centre_y - max(2, h // 6)), min(h, centre_y + max(3, h // 6)))
    darkest = (0, 0, 0)
    darkest_lum = 766
    for sy in band:
        for sx in range(w):
            px = pixels[sx, sy][:3]
            lum = px[0] + px[1] + px[2]
            if lum < darkest_lum:
                darkest_lum = lum
                darkest = px
    return darkest if darkest_lum < 600 else (0, 0, 0)


def _local_render_fallback(crop: Image.Image, replacement_text: str) -> Image.Image:
    """Best-effort local rendering when the Gemini API is unavailable."""
    w, h = crop.size
    bg_rgb = _centre_weighted_bg(crop)
    text_rgb = _estimate_fg(crop)

    edited = Image.new("RGB", crop.size, bg_rgb)
    draw = ImageDraw.Draw(edited)

    # Dynamically size the font to the crop height (aim for ~62% of height).
    font = None
    target_h = max(8, int(h * 0.62))
    for path in [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            if os.path.exists(path):
                font = ImageFont.truetype(path, target_h)
                break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()

    # If the text overflows the crop width, shrink the font to fit.
    try:
        for _ in range(6):
            bbox = draw.textbbox((0, 0), replacement_text, font=font)
            tw = bbox[2] - bbox[0]
            if tw <= w or target_h <= 8 or not hasattr(font, "size"):
                break
            target_h = int(target_h * 0.85)
            font = ImageFont.truetype(font.path, target_h)
        bbox = draw.textbbox((0, 0), replacement_text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (w - tw) / 2
        ty = (h - th) / 2 - bbox[1]
    except Exception:
        tx, ty = 4, 4

    draw.text((max(0, tx), max(0, ty)), replacement_text, fill=text_rgb, font=font)
    return edited
