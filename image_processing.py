import base64
import io
import json
from pathlib import Path
from typing import Any

import fitz
from PIL import Image


def render_page(page: fitz.Page, dpi: int = 300) -> Image.Image:
    """Render a PDF page to a PIL image."""
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def points_bbox_to_pixels(bbox: dict[str, float], dpi: int = 300) -> dict[str, int]:
    """Convert a PDF-points bounding box to image-pixel coordinates."""
    scale = dpi / 72
    return {
        "x": round(bbox["x"] * scale),
        "y": round(bbox["y"] * scale),
        "width": round(bbox["width"] * scale),
        "height": round(bbox["height"] * scale),
    }


def padded_crop_box(
    bbox_px: dict[str, int],
    image_size: tuple[int, int],
    padding_px: int,
) -> tuple[int, int, int, int]:
    """Return a clamped PIL crop box (left, top, right, bottom)."""
    image_width, image_height = image_size
    left = max(0, bbox_px["x"] - padding_px)
    top = max(0, bbox_px["y"] - padding_px)
    right = min(image_width, bbox_px["x"] + bbox_px["width"] + padding_px)
    bottom = min(image_height, bbox_px["y"] + bbox_px["height"] + padding_px)
    return left, top, right, bottom


def crop_region(
    image: Image.Image,
    bbox: dict[str, float],
    padding_points: float = 8,
    dpi: int = 300,
) -> tuple[Image.Image, dict[str, Any]]:
    """Crop a selected PDF bbox from a rendered page image."""
    bbox_px = points_bbox_to_pixels(bbox, dpi=dpi)
    padding_px = round(padding_points * dpi / 72)
    crop_box = padded_crop_box(bbox_px, image.size, padding_px)
    crop = image.crop(crop_box)
    metadata = {
        "dpi": dpi,
        "bbox_points": bbox,
        "bbox_pixels": bbox_px,
        "padding_points": padding_points,
        "padding_pixels": padding_px,
        "crop_box_pixels": {
            "left": crop_box[0],
            "top": crop_box[1],
            "right": crop_box[2],
            "bottom": crop_box[3],
            "width": crop_box[2] - crop_box[0],
            "height": crop_box[3] - crop_box[1],
        },
        "page_image_pixels": {"width": image.width, "height": image.height},
    }
    return crop, metadata


def image_to_base64_png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def save_ai_artifacts(
    output_dir: Path,
    full_page: Image.Image,
    crop: Image.Image,
    metadata: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    full_page.save(output_dir / "full_page.png")
    crop.save(output_dir / "crop.png")
    (output_dir / "coordinates.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
