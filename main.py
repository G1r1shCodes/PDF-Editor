import os
import io
import json
import base64
import tempfile
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# pyrefly: ignore [missing-import]
import fitz  # PyMuPDF


def _find_tesseract() -> Optional[str]:
    """Locate the Tesseract binary, falling back to common Windows install dirs."""
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


class SaveRequest(BaseModel):
    session_id: str
    edits: List[EditOperation]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_scanned_page(page: fitz.Page, threshold: int = 20) -> bool:
    """Return True if a page has fewer than `threshold` text characters — likely a scan."""
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
    """Extract text blocks with full font metadata from a digital PDF page."""
    blocks: List[TextBlock] = []
    block_counter = 0

    # get_text("dict") gives us per-span font details AND full text strings
    data = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    for block in data.get("blocks", []):
        if block.get("type") != 0:   # type 0 = text block
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                bbox = span.get("bbox", [0, 0, 0, 0])
                blocks.append(TextBlock(
                    id=f"p{page_num}_b{block_counter}",
                    page=page_num,
                    x=bbox[0],
                    y=bbox[1],
                    width=bbox[2] - bbox[0],
                    height=bbox[3] - bbox[1],
                    text=text,
                    font_name=_normalize_font_name(span.get("font", ""), span.get("flags", 0)),
                    font_size=round(span.get("size", 12), 2),
                    font_flags=span.get("flags", 0),
                    color=_color_to_hex(span.get("color", 0)),
                    is_ocr=False,
                ))
                block_counter += 1

    return blocks


def _extract_page_text_blocks(
    page: fitz.Page, page_num: int, pdf_path: str, likely_scanned: bool
) -> List[TextBlock]:
    """Extract native text first; use OCR only when native text is sparse."""
    text_blocks = _extract_text_blocks_native(page, page_num)
    if len(text_blocks) >= 3 or not likely_scanned:
        return text_blocks

    try:
        ocr_blocks = _ocr_page(page, page_num, pdf_path)
        if ocr_blocks:
            return ocr_blocks
    except Exception as ocr_err:
        print(f"[WARN] OCR failed for page {page_num}: {ocr_err}")

    return text_blocks


def _ocr_page(page: fitz.Page, page_num: int, pdf_path: str) -> List[TextBlock]:
    """Fallback OCR using Tesseract (pytesseract) when a page appears to be a scan.

    Returns word-level text blocks with bounding boxes in PDF points.
    """
    if not HAS_OCR:
        print("[WARN] Tesseract not available, skipping OCR.")
        return []

    # Render the page to a PIL image at 300 DPI for better OCR accuracy.
    zoom = 300 / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))

    # Scale factor from rendered image pixels (300 DPI) back to PDF points (72 DPI).
    scale = 72.0 / 300.0

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    blocks: List[TextBlock] = []
    block_counter = 0
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        # Tesseract uses conf in 0-100; -1 marks non-text layout rows.
        if not text or conf < 30:
            continue

        x = data["left"][i] * scale
        y = data["top"][i] * scale
        w = data["width"][i] * scale
        h = data["height"][i] * scale

        # Bounding-box height slightly overestimates glyph size; trim a touch.
        font_size = round(max(h * 0.8, 1), 2)

        blocks.append(TextBlock(
            id=f"p{page_num}_b{block_counter}_ocr",
            page=page_num,
            x=x,
            y=y,
            width=w,
            height=h,
            text=text,
            font_name="Helvetica",   # best guess for scanned docs
            font_size=font_size,
            font_flags=0,
            color="#000000",
            is_ocr=True,
        ))
        block_counter += 1

    return blocks


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/upload", response_model=DocumentInfo)
async def upload_pdf(file: UploadFile = File(...)):
    """
    Accept a PDF (digital or scanned).
    Returns per-page rendered images + extracted/OCR'd text blocks with font data.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    session_id = base64.urlsafe_b64encode(os.urandom(8)).decode()
    session_dir = UPLOAD_DIR / session_id
    session_dir.mkdir()

    pdf_path = session_dir / "original.pdf"
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    doc = fitz.open(str(pdf_path))
    pages_info: List[PageInfo] = []
    any_scanned = False

    for page_num, page in enumerate(doc):
        scanned = _is_scanned_page(page)
        if scanned:
            any_scanned = True

        # Render preview image
        img_b64 = _render_page_image(page, dpi=150)

        text_blocks = _extract_page_text_blocks(page, page_num, str(pdf_path), scanned)

        pages_info.append(PageInfo(
            page_number=page_num,
            width=page.rect.width,
            height=page.rect.height,
            image_b64=img_b64,
            text_blocks=text_blocks,
            is_scanned=scanned,
        ))

    doc.close()

    return DocumentInfo(
        session_id=session_id,
        filename=file.filename,
        num_pages=len(pages_info),
        is_scanned=any_scanned,
        pages=pages_info,
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

    # Group edits by page
    edits_by_page: dict[int, List[EditOperation]] = {}
    for edit in req.edits:
        edits_by_page.setdefault(edit.page, []).append(edit)

    for page_num, page_edits in edits_by_page.items():
        page = doc[page_num]

        # Cover every original text region first, then apply all redactions in one
        # pass — redaction deletes the underlying text/image from the content stream.
        for edit in page_edits:
            rect = fitz.Rect(edit.x, edit.y, edit.x + edit.width, edit.y + edit.height)
            page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions()

        # Draw replacement text on top of the now-cleared regions.
        for edit in page_edits:
            if edit.text.strip():
                fitz_color = _hex_to_fitz_color(edit.color)
                page.insert_text(
                    fitz.Point(edit.x, edit.y + edit.font_size),
                    edit.text,
                    fontname=_clean_fontname(edit.font_name),
                    fontsize=edit.font_size,
                    color=fitz_color,
                )

    doc.save(str(out_path))
    doc.close()

    return FileResponse(
        str(out_path),
        media_type="application/pdf",
        filename="edited.pdf",
    )


def _clean_fontname(name: str) -> str:
    """Map a font name to one of PyMuPDF's built-in Base-14 font codes."""
    name_lower = name.lower()
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
    # Helvetica family (default)
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
            {"name": "Helvetica Oblique", "key": "helvo"},
            {"name": "Times Roman", "key": "tiro"},
            {"name": "Times Bold", "key": "tibo"},
            {"name": "Times Italic", "key": "timesi"},
            {"name": "Courier", "key": "cour"},
            {"name": "Courier Bold", "key": "cobo"},
        ]
    }


@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    """Clean up temporary files."""
    session_dir = UPLOAD_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return {"status": "deleted"}


@app.get("/health")
def health():
    return {"status": "ok"}
