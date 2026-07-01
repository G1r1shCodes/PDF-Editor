"""Enhanced OCR extraction using Tesseract.

Renders a PDF page, applies image preprocessing (grayscale, Otsu binarisation,
optional deskew and upscaling), then extracts word-level bounding boxes with
Tesseract hierarchy IDs (block, paragraph, line, word) so downstream layout
analysis can group them into semantic structures.

Improvements over the original inline OCR:
  * Single canonical ``find_tesseract`` (imported by main.py too).
  * Image preprocessing for skewed / low-contrast scans (numpy + Pillow).
  * Page-aware PSM: auto (``--psm 3``) with a sparse (``--psm 11``) fallback.
  * Per-word glyph colour sampling instead of forcing black.
  * Configurable OCR language.
"""

import io
import os
import shutil
from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF
from PIL import Image, ImageOps

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is a listed dependency
    _HAS_NUMPY = False


# -- Tesseract discovery (canonical location; main.py imports this) -------------

def find_tesseract() -> Optional[str]:
    """Locate the Tesseract binary, falling back to common install dirs."""
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        os.environ.get("TESSERACT_CMD"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "Tesseract-OCR" / "tesseract.exe"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return None


# Backwards-compatible alias.
_find_tesseract = find_tesseract


# Lazy-initialise pytesseract
_tess_ready = False


def _ensure_tesseract():
    global _tess_ready
    if _tess_ready:
        return
    try:
        import pytesseract as _pt
        cmd = find_tesseract()
        if cmd:
            _pt.pytesseract.tesseract_cmd = cmd
        _tess_ready = True
    except ImportError:
        raise RuntimeError("pytesseract is not installed")


# -- Public types ---------------------------------------------------------------

class OCRBlock(dict):
    """A single OCR-detected word with hierarchy metadata.

    Keys:
        text, confidence, bbox (dict x/y/width/height in PDF points),
        block_num, par_num, line_num, word_num, font_size_estimate,
        color (hex string sampled from the glyph), page
    """
    pass


# -- Image preprocessing --------------------------------------------------------

def _otsu_threshold(gray_arr) -> int:
    """Compute an Otsu threshold (0-255) from a grayscale numpy array."""
    hist, _ = np.histogram(gray_arr, bins=256, range=(0, 256))
    total = gray_arr.size
    if total == 0:
        return 127
    sum_total = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    weight_b = 0.0
    max_between = 0.0
    threshold = 127
    for i in range(256):
        weight_b += hist[i]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += i * hist[i]
        mean_b = sum_b / weight_b
        mean_f = (sum_total - sum_b) / weight_f
        between = weight_b * weight_f * (mean_b - mean_f) ** 2
        if between > max_between:
            max_between = between
            threshold = i
    return int(threshold)


def _estimate_skew_angle(gray_img: Image.Image,
                         max_angle: float = 5.0,
                         step: float = 0.5) -> float:
    """Estimate page skew (degrees) via horizontal projection-profile variance.

    A correctly-deskewed page has text rows that line up, maximising the
    variance of the row-wise ink projection. Searched on a downscaled copy
    for speed.
    """
    if not _HAS_NUMPY:
        return 0.0
    small = gray_img.copy()
    small.thumbnail((600, 600))
    # Ink = white (255) on black so projections sum ink mass.
    binar = small.point(lambda p: 255 if p < 128 else 0)
    best_angle = 0.0
    best_score = -1.0
    angle = -max_angle
    while angle <= max_angle + 1e-9:
        rot = binar.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
        arr = np.asarray(rot, dtype=np.float32)
        proj = arr.sum(axis=1)
        score = float(np.var(proj))
        if score > best_score:
            best_score = score
            best_angle = angle
        angle += step
    return best_angle


def preprocess_for_ocr(color_img: Image.Image,
                       deskew: bool = False,
                       upscale: float = 1.0):
    """Produce (ocr_img, color_img, applied_upscale).

    ``ocr_img``  : grayscale, contrast-stretched, Otsu-binarised image fed to
                   Tesseract.
    ``color_img``: the colour image after the *same* geometric transforms
                   (upscale + deskew) but WITHOUT binarisation, so word boxes
                   map cleanly onto it for glyph-colour sampling.

    Geometric ops preserve the coordinate origin (deskew uses ``expand=False``),
    so callers only need to divide by ``applied_upscale`` to map back to the
    render's pixel grid.
    """
    color = color_img.convert("RGB")

    if upscale and upscale > 1.0:
        new_size = (int(color.width * upscale), int(color.height * upscale))
        color = color.resize(new_size, Image.LANCZOS)

    gray = ImageOps.grayscale(color)
    gray = ImageOps.autocontrast(gray)

    angle = 0.0
    if deskew and _HAS_NUMPY:
        angle = _estimate_skew_angle(gray)
        if abs(angle) >= 0.2:
            gray = gray.rotate(angle, resample=Image.BILINEAR, fillcolor=255, expand=False)
            color = color.rotate(angle, resample=Image.BILINEAR, fillcolor=(255, 255, 255), expand=False)

    # Otsu binarisation (falls back to a fixed threshold without numpy).
    if _HAS_NUMPY:
        thr = _otsu_threshold(np.asarray(gray))
    else:
        thr = 160
    ocr_img = gray.point(lambda p: 255 if p > thr else 0).convert("L")

    return ocr_img, color, (upscale if upscale and upscale > 1.0 else 1.0)


def _sample_glyph_color(color_arr, x: int, y: int, w: int, h: int) -> str:
    """Sample the dominant ink colour inside a word box (darkest ~30% of px)."""
    if not _HAS_NUMPY:
        return "#000000"
    H, W = color_arr.shape[0], color_arr.shape[1]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return "#000000"
    crop = color_arr[y0:y1, x0:x1, :3]
    if crop.size == 0:
        return "#000000"
    lum = crop.sum(axis=2)
    thresh = np.percentile(lum, 30)
    mask = lum <= thresh
    if not mask.any():
        px = crop.reshape(-1, 3).mean(axis=0)
    else:
        px = crop[mask].mean(axis=0)
    return "#{:02x}{:02x}{:02x}".format(int(px[0]), int(px[1]), int(px[2]))


# -- Tesseract invocation -------------------------------------------------------

def _run_tess(img: Image.Image, lang: str, psm: int) -> List[dict]:
    """Run Tesseract once and return raw word rows (pixel coords, processed image)."""
    import pytesseract
    config = f"--psm {psm}"
    data = pytesseract.image_to_data(
        img, lang=lang, config=config, output_type=pytesseract.Output.DICT
    )
    rows = []
    for i in range(len(data["text"])):
        rows.append({
            "text": (data["text"][i] or "").strip(),
            "conf": data["conf"][i],
            "left": data["left"][i],
            "top": data["top"][i],
            "width": data["width"][i],
            "height": data["height"][i],
            "block_num": data["block_num"][i],
            "par_num": data["par_num"][i],
            "line_num": data["line_num"][i],
            "word_num": data["word_num"][i],
        })
    return rows


def _count_words(rows: List[dict], min_confidence: float) -> int:
    n = 0
    for r in rows:
        try:
            c = float(r["conf"])
        except (ValueError, TypeError):
            c = -1.0
        if r["text"] and c >= min_confidence:
            n += 1
    return n


# -- Main extraction function ---------------------------------------------------

def extract_ocr_blocks(
    page: fitz.Page,
    page_num: int,
    dpi: int = 300,
    min_confidence: float = 30,
    lang: str = "eng",
    psm: Optional[int] = None,
    preprocess: bool = True,
    deskew: bool = False,
    upscale: float = 1.0,
) -> List[OCRBlock]:
    """Run Tesseract on a rendered PDF page and return enriched word blocks.

    Parameters
    ----------
    page : fitz.Page
        PyMuPDF page object.
    page_num : int
        Zero-based page index.
    dpi : int
        Rendering resolution for OCR accuracy.
    min_confidence : float
        Words below this confidence are discarded.
    lang : str
        Tesseract language code(s), e.g. ``"eng"`` or ``"eng+deu"``.
    psm : int | None
        Page-segmentation mode. ``None`` = automatic: try ``--psm 3`` (auto)
        and fall back to ``--psm 11`` (sparse) when few words are found,
        keeping whichever yields more.
    preprocess : bool
        Apply grayscale + autocontrast + Otsu binarisation before OCR.
    deskew : bool
        Estimate and correct page skew (safe for the reconstruct path; leave
        off for the overlay path where boxes sit on the un-rotated image).
    upscale : float
        Optional render upscaling (e.g. ``2.0``) for small / low-DPI scans.

    Returns
    -------
    list[OCRBlock]
        Each entry has text, bbox (PDF points), hierarchy IDs, confidence,
        an estimated font size, and a sampled glyph colour.
    """
    _ensure_tesseract()

    # Render the page (colour) at the requested DPI.
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    color_render = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

    if preprocess:
        ocr_img, color_img, applied_upscale = preprocess_for_ocr(
            color_render, deskew=deskew, upscale=upscale
        )
    else:
        ocr_img, color_img, applied_upscale = color_render, color_render, 1.0

    color_arr = np.asarray(color_img) if _HAS_NUMPY else None

    # Pixels (processed image) -> PDF points.
    scale = 72.0 / (dpi * applied_upscale)

    # Page-aware PSM selection.
    if psm is not None:
        rows = _run_tess(ocr_img, lang, psm)
    else:
        rows = _run_tess(ocr_img, lang, 3)
        # Sparse fallback when auto segmentation finds very little.
        if _count_words(rows, min_confidence) < 8:
            sparse = _run_tess(ocr_img, lang, 11)
            if _count_words(sparse, min_confidence) > _count_words(rows, min_confidence):
                rows = sparse

    blocks: List[OCRBlock] = []
    for r in rows:
        text = r["text"]
        try:
            conf = float(r["conf"])
        except (ValueError, TypeError):
            conf = -1.0
        if not text or conf < min_confidence:
            continue

        px, py, pw, ph = r["left"], r["top"], r["width"], r["height"]

        color = _sample_glyph_color(color_arr, px, py, pw, ph) if color_arr is not None else "#000000"

        x = px * scale
        y = py * scale
        w = pw * scale
        h = ph * scale

        # Font size estimate: bbox height x 0.8 (bboxes overestimate glyph size).
        font_size = round(max(h * 0.8, 1), 2)

        blocks.append(OCRBlock(
            text=text,
            confidence=round(conf, 1),
            bbox={"x": round(x, 2), "y": round(y, 2),
                  "width": round(w, 2), "height": round(h, 2)},
            block_num=r["block_num"],
            par_num=r["par_num"],
            line_num=r["line_num"],
            word_num=r["word_num"],
            font_size_estimate=font_size,
            color=color,
            page=page_num,
        ))

    return blocks


def render_page_image(page: fitz.Page, dpi: int = 300) -> Image.Image:
    """Render a PDF page to a PIL Image at the given DPI."""
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
