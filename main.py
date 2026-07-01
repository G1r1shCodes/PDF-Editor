import os
import io
import re
import base64
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# pyrefly: ignore [missing-import]
import fitz  # PyMuPDF
from ai_routes import AiEditPrepareResponse, prepare_ai_edit


# Canonical Tesseract discovery lives in ocr.extraction; reuse it here.
from ocr.extraction import find_tesseract as _find_tesseract


try:
    import pytesseract
    from PIL import Image
    _tess_cmd = _find_tesseract()
    if _tess_cmd:
        pytesseract.pytesseract.tesseract_cmd = _tess_cmd
    HAS_OCR = _tess_cmd is not None
except ImportError:
    HAS_OCR = False

app = FastAPI(title="PDF Editor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_api_route(
    "/ai-edit",
    prepare_ai_edit,
    methods=["POST"],
    response_model=AiEditPrepareResponse,
)

UPLOAD_DIR = Path(__file__).parent / "tmp_pdf_editor"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── Models ────────────────────────────────────────────────────────────────────

class TextBlock(BaseModel):
    id: str
    page: int
    x: float
    y: float
    width: float
    height: float
    text: str
    font_name: str
    font_size: float
    font_flags: int        # bold/italic bitmask from PyMuPDF
    color: str             # hex string
    is_ocr: bool = False   # came from Tesseract rather than embedded text
    background_color: str = "#ffffff"
    baseline: Optional[float] = None   # true text baseline y (PDF points)
    pdf_font: Optional[str] = None     # original embedded font basename


class PageInfo(BaseModel):
    page_number: int
    width: float
    height: float
    image_b64: str         # PNG rendered at 150 DPI, base64
    text_blocks: List[TextBlock]
    is_scanned: bool


class DocumentInfo(BaseModel):
    session_id: str
    filename: str
    num_pages: int
    is_scanned: bool
    pages: List[PageInfo]


class EditOperation(BaseModel):
    block_id: str
    page: int
    text: str
    x: float
    y: float
    width: float
    height: float
    font_name: str
    font_size: float
    color: str
    background_color: Optional[str] = "#ffffff"
    is_ai_edit: bool = False
    edit_id: Optional[str] = None
    baseline: Optional[float] = None
    pdf_font: Optional[str] = None


class SaveRequest(BaseModel):
    session_id: str
    edits: List[EditOperation]


class ReconstructRequest(BaseModel):
    session_id: str
    page_numbers: Optional[List[int]] = None  # None = all pages
    ocr_engine: Optional[str] = None          # "nemotron" | "tesseract" | None/"auto"
    nvidia_api_key: Optional[str] = None       # for Nemotron OCR v2 NIM


class ReconstructSaveRequest(BaseModel):
    session_id: str
    edits: dict  # element_id -> {text: ..., rows: [...], ...}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_background_color(page: fitz.Page, bbox: fitz.Rect) -> str:
    """Detect the dominant background colour of a region.

    Uses the MODAL (most frequent) colour of the sampled pixels instead of a
    corner average, so the minority of dark glyph pixels can no longer drag the
    estimate toward grey. Handles white cells and coloured header cells alike.
    """
    try:
        clip_rect = bbox & page.rect
        if clip_rect.is_empty:
            return "#ffffff"

        pix = page.get_pixmap(clip=clip_rect, alpha=False)
        w, h, n = pix.width, pix.height, pix.n
        if w <= 0 or h <= 0 or n < 3:
            return "#ffffff"

        samples = pix.samples
        total = w * h
        step = max(1, total // 4000)   # cap work on large regions
        counts: dict = {}
        idx = 0
        while idx < total:
            base = idx * n
            r, g, b = samples[base], samples[base + 1], samples[base + 2]
            # Quantise to 16-level buckets so near-identical bg pixels group.
            key = (r & 0xF0, g & 0xF0, b & 0xF0)
            bucket = counts.get(key)
            if bucket is None:
                counts[key] = [1, r, g, b]
            else:
                bucket[0] += 1; bucket[1] += r; bucket[2] += g; bucket[3] += b
            idx += step

        if not counts:
            return "#ffffff"

        best = max(counts.values(), key=lambda v: v[0])
        cnt = best[0]
        ar, ag, ab = best[1] // cnt, best[2] // cnt, best[3] // cnt
        return f"#{ar:02x}{ag:02x}{ab:02x}"
    except Exception as e:
        print(f"[WARN] Failed to detect background color: {e}")
        return "#ffffff"

def _is_scanned_page(page: fitz.Page, threshold: int = 20) -> bool:
    """Return True if a page has fewer than `threshold` text characters; likely a scan."""
    text = page.get_text("text").strip()
    return len(text) < threshold


def _color_to_hex(color) -> str:
    """Convert PyMuPDF color value to CSS hex string.
    
    PyMuPDF rawdict returns color as a packed RGB integer (e.g. 0x000000 for black,
    0xFF0000 for red). Other APIs may return float tuples (0-1 per channel).
    """
    if color is None:
        return "#000000"
    # rawdict gives a single int: packed 0xRRGGBB
    if isinstance(color, int):
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
        return f"#{r:02x}{g:02x}{b:02x}"
    # Single float = grayscale 0-1
    if isinstance(color, float):
        v = int(color * 255)
        return f"#{v:02x}{v:02x}{v:02x}"
    # Tuple/list of 3 floats (0-1 each)
    if hasattr(color, '__len__') and len(color) == 3:
        r, g, b = [int(c * 255) for c in color]
        return f"#{r:02x}{g:02x}{b:02x}"
    return "#000000"


def _hex_to_fitz_color(hex_color: str):
    """Convert CSS hex to PyMuPDF (r, g, b) float tuple."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r / 255, g / 255, b / 255)


def _span_text(span: dict) -> str:
    """Get span text from rawdict (chars) or dict (text) output."""
    text = span.get("text", "")
    if not text:
        chars = span.get("chars")
        if chars:
            text = "".join(c.get("c", "") for c in chars)
    return text.strip()


def _normalize_font_name(font: str, flags: int) -> str:
    """Map PDF font metadata to names the editor UI understands."""
    name_lower = font.lower()
    bold = bool(flags & 16)
    italic = bool(flags & 2)
    mono = bool(flags & 8) or "courier" in name_lower or "mono" in name_lower
    serif = bool(flags & 4) or "times" in name_lower or "serif" in name_lower or name_lower.startswith("cidfont")

    if mono:
        return "Courier Bold" if bold else "Courier"
    if serif:
        if bold:
            return "Times Bold"
        if italic:
            return "Times Italic"
        return "Times Roman"
    if bold:
        return "Helvetica Bold"
    if italic:
        return "Helvetica Oblique"
    return "Helvetica"


def _render_page_image(page: fitz.Page, dpi: int = 150) -> str:
    """Render a PDF page to a base64-encoded PNG string."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode()


def _extract_text_blocks_native(page: fitz.Page, page_num: int) -> List[TextBlock]:
    """Extract LINE-level text blocks with font metadata from a digital PDF page.

    Spans on the same visual line are merged into a single editable block so the
    user edits whole lines, not glyph-level fragments. The line's typography is
    taken from its dominant (widest) span; the true baseline is the span origin.
    """
    blocks: List[TextBlock] = []
    block_counter = 0

    # get_text("dict") gives us per-span font details AND full text strings
    data = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    for block in data.get("blocks", []):
        if block.get("type") != 0:   # type 0 = text block
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue

            # Concatenate span texts left-to-right (already ordered in dict mode)
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue

            # Union bbox across spans
            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)

            # Dominant span = widest, drives font / colour / size
            dom = max(spans, key=lambda s: s["bbox"][2] - s["bbox"][0])
            # Baseline: span origin y (true text baseline), fall back to bbox
            origin = dom.get("origin")
            baseline = round(origin[1], 2) if origin else round(y1 - (y1 - y0) * 0.2, 2)

            rect = fitz.Rect(x0, y0, x1, y1)
            bg_color = _detect_background_color(page, rect)

            blocks.append(TextBlock(
                id=f"p{page_num}_b{block_counter}",
                page=page_num,
                x=round(x0, 2),
                y=round(y0, 2),
                width=round(x1 - x0, 2),
                height=round(y1 - y0, 2),
                text=text,
                font_name=_normalize_font_name(dom.get("font", ""), dom.get("flags", 0)),
                font_size=round(dom.get("size", 12), 2),
                font_flags=dom.get("flags", 0),
                color=_color_to_hex(dom.get("color", 0)),
                is_ocr=False,
                background_color=bg_color,
                baseline=baseline,
                pdf_font=dom.get("font", ""),
            ))
            block_counter += 1

    return blocks


def _extract_page_text_blocks(page: fitz.Page, page_num: int, likely_scanned: bool) -> List[TextBlock]:
    """Extract native text first; use OCR only when native text is sparse."""
    text_blocks = _extract_text_blocks_native(page, page_num)
    if len(text_blocks) >= 3 or not likely_scanned:
        return text_blocks

    try:
        ocr_blocks = _ocr_page(page, page_num)
        if ocr_blocks:
            return ocr_blocks
    except Exception as ocr_err:
        print(f"[WARN] OCR failed for page {page_num}: {ocr_err}")

    return text_blocks


def _ocr_page(page: fitz.Page, page_num: int) -> List[TextBlock]:
    """Fallback OCR using Tesseract via the ocr.extraction module.

    Returns word-level text blocks with bounding boxes in PDF points.
    """
    if not HAS_OCR:
        print("[WARN] Tesseract not available, skipping OCR.")
        return []

    from ocr.extraction import extract_ocr_blocks

    # Overlay path: deskew MUST stay off so boxes align to the un-rotated image.
    ocr_blocks = extract_ocr_blocks(page, page_num, dpi=300, deskew=False)

    blocks: List[TextBlock] = []
    for i, ob in enumerate(ocr_blocks):
        bbox = ob["bbox"]
        x, y, w, h = bbox["x"], bbox["y"], bbox["width"], bbox["height"]
        rect = fitz.Rect(x, y, x + w, y + h)
        bg_color = _detect_background_color(page, rect)

        blocks.append(TextBlock(
            id=f"p{page_num}_b{i}_ocr",
            page=page_num,
            x=x, y=y, width=w, height=h,
            text=ob["text"],
            font_name="Helvetica",
            font_size=ob["font_size_estimate"],
            font_flags=0,
            color=ob.get("color", "#000000"),
            is_ocr=True,
            background_color=bg_color,
            baseline=round(y + h * 0.8, 2),
        ))

    return blocks


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/upload", response_model=DocumentInfo)
async def upload_pdf(file: UploadFile = File(...)):
    """
    Accept a PDF (digital or scanned).
    Returns per-page rendered images + extracted/OCR'd text blocks with font data.
    """
    filename = file.filename or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    session_id = base64.urlsafe_b64encode(os.urandom(8)).decode()
    session_dir = UPLOAD_DIR / session_id
    session_dir.mkdir(exist_ok=False)

    pdf_path = session_dir / "original.pdf"
    try:
        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        doc = fitz.open(str(pdf_path))
        try:
            if doc.page_count == 0:
                raise HTTPException(400, "PDF does not contain any pages.")

            pages_info: List[PageInfo] = []
            any_scanned = False

            for page_num, page in enumerate(doc):
                scanned = _is_scanned_page(page)
                if scanned:
                    any_scanned = True

                img_b64 = _render_page_image(page, dpi=150)
                text_blocks = _extract_page_text_blocks(page, page_num, scanned)

                pages_info.append(PageInfo(
                    page_number=page_num,
                    width=page.rect.width,
                    height=page.rect.height,
                    image_b64=img_b64,
                    text_blocks=text_blocks,
                    is_scanned=scanned,
                ))
        finally:
            doc.close()
    except HTTPException:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(400, f"Could not process PDF: {exc}") from exc

    return DocumentInfo(
        session_id=session_id,
        filename=filename,
        num_pages=len(pages_info),
        is_scanned=any_scanned,
        pages=pages_info,
    )


def _strip_subset_prefix(name: str) -> str:
    """Strip the 6-letter subset tag PDFs prepend, e.g. 'ABCDEF+Arial' → 'Arial'."""
    if "+" in name and len(name.split("+", 1)[0]) == 6:
        return name.split("+", 1)[1]
    return name


class _FontRegistry:
    """Best-effort reuse of a PDF's own embedded fonts when redrawing edited text.

    For each edit we try to render with the glyphs from the original font so the
    typeface is preserved exactly. Subset fonts often lack glyphs for newly-typed
    characters; in that case we fall back to a Base-14 font so output is never
    blank. All failures degrade gracefully to the fallback.
    """

    def __init__(self, doc: fitz.Document):
        self.doc = doc
        self._buffers: dict[str, Optional[bytes]] = {}   # basename → font bytes
        self._loaded = False
        self._registered: dict[tuple, str] = {}          # (page_xref, base) → fontname

    def _load_buffers(self):
        if self._loaded:
            return
        self._loaded = True
        seen = set()
        for pno in range(self.doc.page_count):
            try:
                fonts = self.doc[pno].get_fonts(full=True)
            except Exception:
                continue
            for f in fonts:
                xref = f[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    basefont, ext, _stype, buffer = self.doc.extract_font(xref)
                except Exception:
                    continue
                if not buffer or ext in ("n/a", ""):
                    continue
                base = _strip_subset_prefix(basefont or "")
                if base and base not in self._buffers:
                    self._buffers[base] = buffer

    def font_for(self, page: fitz.Page, pdf_font: Optional[str], text: str, fallback_code: str) -> str:
        """Return a fontname usable in insert_text/insert_textbox.

        Uses the original embedded font only when it covers every glyph in
        ``text``; otherwise returns ``fallback_code``.
        """
        if not pdf_font:
            return fallback_code
        self._load_buffers()
        base = _strip_subset_prefix(pdf_font)
        buffer = self._buffers.get(base)
        if not buffer:
            return fallback_code

        # Verify glyph coverage so new characters don't render blank.
        try:
            probe = fitz.Font(fontbuffer=buffer)
            for ch in set(text):
                if ch.isspace():
                    continue
                if probe.has_glyph(ord(ch)) == 0:
                    return fallback_code
        except Exception:
            return fallback_code

        key = (page.xref, base)
        if key in self._registered:
            return self._registered[key]
        fontname = "F" + re.sub(r"[^A-Za-z0-9]", "", base)[:24] or "Fembed"
        try:
            page.insert_font(fontname=fontname, fontbuffer=buffer)
            self._registered[key] = fontname
            return fontname
        except Exception:
            return fallback_code


def _draw_edit_text(page: fitz.Page, edit: "EditOperation", registry: _FontRegistry) -> None:
    """Draw replacement text, preserving baseline and fitting it to the box.

    A single line that is slightly too wide is shrunk to fit (down to 50%% of the
    original size) rather than wrapping into the row below. Only genuinely long
    or multi-line text falls back to a wrapped text box.
    """
    text = edit.text
    if not text.strip():
        return

    try:
        color = _hex_to_fitz_color(edit.color)
    except (TypeError, ValueError):
        color = (0, 0, 0)

    size = edit.font_size
    fallback = _clean_fontname(edit.font_name)
    fontname = registry.font_for(page, edit.pdf_font, text, fallback)
    baseline = edit.baseline if edit.baseline is not None else edit.y + size

    single_line = "\n" not in text

    if single_line:
        draw_size = size
        fits = True
        try:
            length = fitz.get_text_length(text, fontname=fontname, fontsize=draw_size)
            if edit.width > 0 and length > edit.width + 1:
                # Shrink to fit the original box, but not below 50%% of size.
                floor = max(size * 0.5, 4.0)
                while draw_size > floor:
                    draw_size -= 0.5
                    if fitz.get_text_length(text, fontname=fontname, fontsize=draw_size) <= edit.width + 1:
                        break
                length = fitz.get_text_length(text, fontname=fontname, fontsize=draw_size)
            fits = length <= edit.width + 1
        except Exception:
            draw_size, fits = size, True  # custom font: assume it fits

        if fits:
            page.insert_text(
                fitz.Point(edit.x, baseline), text,
                fontname=fontname, fontsize=draw_size, color=color,
            )
            return

    # Wrap into a box that extends to the page bottom so text never clips.
    box = fitz.Rect(edit.x, edit.y, edit.x + max(edit.width, size * 2), page.rect.height)
    rc = page.insert_textbox(box, text, fontname=fontname, fontsize=size, color=color, align=0)
    if rc < 0:
        # Last resort: single baseline draw (may overflow horizontally).
        page.insert_text(
            fitz.Point(edit.x, baseline), text,
            fontname=fontname, fontsize=size, color=color,
        )

@app.post("/save")
async def save_pdf(req: SaveRequest):
    """
    Apply all user edits and return the modified PDF for download.
    Edits are white-box stamped: original text region is covered then new text drawn.
    """
    session_dir = UPLOAD_DIR / req.session_id
    if not session_dir.exists():
        raise HTTPException(404, "Session not found.")

    src_path = session_dir / "original.pdf"
    out_path = session_dir / "edited.pdf"

    doc = fitz.open(str(src_path))
    try:
        registry = _FontRegistry(doc)

        # Group edits by page
        edits_by_page: dict[int, List[EditOperation]] = {}
        for edit in req.edits:
            if edit.page < 0 or edit.page >= doc.page_count:
                raise HTTPException(400, f"Invalid page index: {edit.page}")
            edits_by_page.setdefault(edit.page, []).append(edit)

        for page_num, page_edits in edits_by_page.items():
            page = doc[page_num]

            # Cover every original text region first, then apply all redactions in one
            # pass; redaction deletes the underlying text/image from the content stream.
            for edit in page_edits:
                rect = fitz.Rect(edit.x, edit.y, edit.x + edit.width, edit.y + edit.height)
                bg_color = _hex_to_fitz_color(edit.background_color or "#ffffff")
                page.add_redact_annot(rect, fill=bg_color)
            page.apply_redactions()

            # Draw replacement text or stamp edited crops
            for edit in page_edits:
                rect = fitz.Rect(edit.x, edit.y, edit.x + edit.width, edit.y + edit.height)
                if edit.is_ai_edit and edit.edit_id:
                    # Stamp the AI-edited crop image back into its region.
                    crop_path = session_dir / "ai_edits" / edit.edit_id / "edited_crop.png"
                    if crop_path.exists():
                        page.insert_image(rect, filename=str(crop_path))
                else:
                    _draw_edit_text(page, edit, registry)

        doc.save(str(out_path))
    finally:
        doc.close()

    return FileResponse(
        str(out_path),
        media_type="application/pdf",
        filename="edited.pdf",
    )


# ── Reconstruct endpoints (scanned-PDF structured editing) ─────────────────────

@app.post("/reconstruct")
async def reconstruct_document(req: ReconstructRequest):
    """Analyse scanned pages and return a structured, editable document."""
    session_dir = UPLOAD_DIR / req.session_id
    if not session_dir.exists():
        raise HTTPException(404, "Session not found.")

    pdf_path = session_dir / "original.pdf"
    doc = fitz.open(str(pdf_path))
    try:
        from ocr.extraction import extract_ocr_blocks
        from ocr.layout import analyse_layout
        from ocr.reconstruction import build_editable_document
        import nemotron_ocr

        engine = (req.ocr_engine or "").lower()
        # Auto-routing requires NVIDIA_OCR_URL to be explicitly set, not just an
        # NVIDIA_API_KEY. A key alone may only be present for the separate
        # FLUX.1-Kontext image-edit feature (nvidia_image_edit.py) and does not
        # mean a Nemotron OCR NIM endpoint is actually reachable. Without an
        # explicit URL, "auto" stays on Tesseract; pass ocr_engine="nemotron"
        # to opt in once a NIM container is running.
        explicit_nemotron_url = bool(os.environ.get("NVIDIA_OCR_URL"))
        use_nemotron = engine == "nemotron" or (
            engine in ("", "auto")
            and explicit_nemotron_url
            and nemotron_ocr.is_configured(req.nvidia_api_key)
        )

        if use_nemotron and not nemotron_ocr.is_configured(req.nvidia_api_key):
            if not os.environ.get("NVIDIA_OCR_URL"):
                raise HTTPException(
                    400, 
                    "NVIDIA API Key is required for Nemotron OCR. Please enter your key, or configure NVIDIA_OCR_URL for a local NIM."
                )

        page_nums = req.page_numbers or list(range(doc.page_count))
        page_layouts = []

        for pn in page_nums:
            if pn < 0 or pn >= doc.page_count:
                raise HTTPException(400, f"Invalid page index: {pn}")
            page = doc[pn]
            if use_nemotron:
                try:
                    ocr_blocks = nemotron_ocr.extract_ocr_blocks_nemotron(
                        page, pn, api_key=req.nvidia_api_key
                    )
                except nemotron_ocr.NemotronOCRError as e:
                    raise HTTPException(400, f"Nemotron OCR error: {e}")
            else:
                # Tesseract path; reconstruct regenerates a clean PDF, deskew safe.
                ocr_blocks = extract_ocr_blocks(page, pn, dpi=300, deskew=True)
            layout = analyse_layout(
                ocr_blocks, page.rect.width, page.rect.height, page_num=pn
            )
            page_layouts.append(layout)

        document = build_editable_document(page_layouts)

        # Cache the reconstruction for the save endpoint.
        import json
        cache_path = session_dir / "reconstruction.json"
        cache_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

        return document
    finally:
        doc.close()


@app.post("/reconstruct/save")
async def save_reconstructed(req: ReconstructSaveRequest):
    """Apply user edits to the cached reconstruction and generate a new PDF."""
    session_dir = UPLOAD_DIR / req.session_id
    if not session_dir.exists():
        raise HTTPException(404, "Session not found.")

    cache_path = session_dir / "reconstruction.json"
    if not cache_path.exists():
        raise HTTPException(400, "No reconstruction cached. Call /reconstruct first.")

    import json
    from ocr.reconstruction import apply_edits
    from ocr.pdf_generation import generate_pdf

    document = json.loads(cache_path.read_text(encoding="utf-8"))
    edited_doc = apply_edits(document, req.edits)

    out_path = session_dir / "reconstructed.pdf"
    generate_pdf(edited_doc, out_path)

    return FileResponse(
        str(out_path),
        media_type="application/pdf",
        filename="reconstructed.pdf",
    )


# ── Font + utility endpoints ──────────────────────────────────────────────────

def _clean_fontname(name: str) -> str:
    """Map a font name to one of PyMuPDF's built-in Base-14 font codes."""
    name_lower = (name or "").lower()
    bold = "bold" in name_lower
    italic = "italic" in name_lower or "oblique" in name_lower
    mono = "courier" in name_lower or "mono" in name_lower
    serif = "times" in name_lower or "serif" in name_lower

    if mono:
        if bold and italic:
            return "cobi"
        if bold:
            return "cobo"
        if italic:
            return "coit"
        return "cour"
    if serif:
        if bold and italic:
            return "tibi"
        if bold:
            return "tibo"
        if italic:
            return "tiit"
        return "tiro"
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


@app.get("/fonts")
def list_fonts():
    """Return the set of fonts the editor can render with."""
    return {
        "fonts": [
            {"name": "Helvetica", "key": "helv"},
            {"name": "Helvetica Bold", "key": "hebo"},
            {"name": "Helvetica Oblique", "key": "heit"},
            {"name": "Times Roman", "key": "tiro"},
            {"name": "Times Bold", "key": "tibo"},
            {"name": "Times Italic", "key": "tiit"},
            {"name": "Courier", "key": "cour"},
            {"name": "Courier Bold", "key": "cobo"},
        ]
    }


@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    """Clean up temporary files for a session."""
    session_dir = UPLOAD_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"status": "deleted"}


@app.get("/health")
def health():
    return {"status": "ok"}
