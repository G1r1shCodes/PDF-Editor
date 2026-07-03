import base64
import os
from pathlib import Path

import fitz
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from image_processing import crop_region, image_to_base64_png, render_page, save_ai_artifacts


UPLOAD_DIR = Path(__file__).parent / "tmp_pdf_editor"

router = APIRouter()


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class AiEditRequest(BaseModel):
    session_id: str
    page_number: int = Field(ge=0)
    bbox: BoundingBox
    original_text: str
    replacement_text: str
    padding: float = Field(default=8, ge=0, le=72)
    gemini_api_key: Optional[str] = None
    replicate_api_key: Optional[str] = None


class AiEditPrepareResponse(BaseModel):
    status: str
    edit_id: str
    page_number: int
    full_page_image_b64: str
    crop_image_b64: str
    coordinates: dict
    message: str


def _gather_context_text(page: "fitz.Page", page_number: int, bbox: dict,
                         radius_pt: float = 50.0) -> str:
    """Collect words near the target bbox (within `radius_pt` points).

    Uses PyMuPDF's native word extraction (no OCR dependency).
    Provides the AI with surrounding text so it can better match font styling.
    Returns an empty string if nothing is nearby.
    """
    tx0 = bbox["x"] - radius_pt
    ty0 = bbox["y"] - radius_pt
    tx1 = bbox["x"] + bbox["width"] + radius_pt
    ty1 = bbox["y"] + bbox["height"] + radius_pt

    try:
        # get_text("words") returns list of (x0, y0, x1, y1, word, block, line, word_no)
        words = page.get_text("words")
    except Exception:
        return ""

    nearby = []
    for w in words:
        x0, y0, x1, y1, word_text = w[0], w[1], w[2], w[3], w[4]
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        # word centre inside the expanded window, but exclude the target itself
        if tx0 <= cx <= tx1 and ty0 <= cy <= ty1:
            inside_target = (
                bbox["x"] <= cx <= bbox["x"] + bbox["width"]
                and bbox["y"] <= cy <= bbox["y"] + bbox["height"]
            )
            if not inside_target:
                nearby.append(word_text)

    text = " ".join(t for t in nearby if t).strip()
    # Keep the prompt compact.
    return text[:300]


@router.post("/ai-edit", response_model=AiEditPrepareResponse)
def prepare_ai_edit(req: AiEditRequest):
    """Render a high-resolution page image and crop the selected edit region.

    This is Phase 2 of AI Edit Mode. It prepares and stores the image artifacts
    that the Gemini integration will consume in the next phase.
    """
    session_dir = UPLOAD_DIR / req.session_id
    pdf_path = session_dir / "original.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "Session not found.")

    doc = fitz.open(str(pdf_path))
    try:
        if req.page_number >= doc.page_count:
            raise HTTPException(400, f"Invalid page index: {req.page_number}")

        page = doc[req.page_number]
        full_page = render_page(page, dpi=300)
        crop, coordinates = crop_region(
            full_page,
            req.bbox.model_dump(),
            padding_points=req.padding,
            dpi=300,
        )
        # Gather nearby OCR text while the page is still open.
        context_text = _gather_context_text(
            page, req.page_number, req.bbox.model_dump()
        )
    finally:
        doc.close()

    from image_processing import points_bbox_to_pixels
    bbox_px = points_bbox_to_pixels(req.bbox.model_dump(), dpi=300)

    # Provider selection: if the request explicitly names a provider (either
    # key field is non-None, even if empty string), that choice is authoritative
    # and environment variables are NOT consulted to override it. This matters
    # because the frontend's AI Provider dropdown always sends both fields
    # (the unselected one as null) -- so a user picking "Gemini" must not be
    # silently re-routed to NVIDIA just because NVIDIA_API_KEY happens to be
    # set in the backend's environment (e.g. for the separate Nemotron OCR
    # feature). Env-var auto-detection only applies when the request sent
    # neither field at all (both None), e.g. an older or minimal caller.
    provider_explicit = req.replicate_api_key is not None or req.gemini_api_key is not None
    if provider_explicit:
        use_replicate = req.replicate_api_key is not None
    else:
        use_replicate = bool(os.environ.get("REPLICATE_API_TOKEN"))
    
    try:
        if use_replicate:
            from replicate_image_edit import send_to_replicate
            edited_crop = send_to_replicate(
                crop=crop,
                original_text=req.original_text,
                replacement_text=req.replacement_text,
                api_key=req.replicate_api_key or os.environ.get("REPLICATE_API_TOKEN", ""),
                context_text=context_text,
            )
        else:
            from gemini_service import send_to_gemini
            edited_crop = send_to_gemini(
                full_page=full_page,
                crop=crop,
                original_text=req.original_text,
                replacement_text=req.replacement_text,
                api_key=req.gemini_api_key or os.environ.get("GEMINI_API_KEY", ""),
                bbox_px=bbox_px,
                context_text=context_text,
            )
    except Exception as e:
        raise HTTPException(400, f"AI Edit error: {e}")

    edit_id = base64.urlsafe_b64encode(os.urandom(8)).decode()
    output_dir = session_dir / "ai_edits" / edit_id
    save_ai_artifacts(output_dir, full_page, crop, {
        **coordinates,
        "session_id": req.session_id,
        "page_number": req.page_number,
        "original_text": req.original_text,
        "replacement_text": req.replacement_text,
        "context_text": context_text,
    })

    # Save the edited crop image in the artifacts folder
    edited_crop.save(output_dir / "edited_crop.png")

    return AiEditPrepareResponse(
        status="prepared",
        edit_id=edit_id,
        page_number=req.page_number,
        full_page_image_b64=image_to_base64_png(full_page),
        crop_image_b64=image_to_base64_png(edited_crop),  # Return the edited crop to frontend!
        coordinates=coordinates,
        message="AI edit completed successfully.",
    )
