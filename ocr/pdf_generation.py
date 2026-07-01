"""PDF generation from a reconstructed document.

Takes a ``ReconstructedDocument`` (with optional user edits applied) and
renders a clean new PDF using PyMuPDF, preserving the original page
dimensions, element positions, and typography.
"""

from pathlib import Path
from typing import Any, Dict

import fitz  # PyMuPDF


# -- Font mapping ---------------------------------------------------------------

_FONT_MAP = {
    # name -> (normal_code, bold_code)
    "helvetica": ("helv", "hebo"),
    "times": ("tiro", "tibo"),
    "courier": ("cour", "cobo"),
}


def _pick_font_code(bold: bool = False, family: str = "helvetica") -> str:
    """Return a PyMuPDF Base-14 font code."""
    fam = family.lower()
    for key, (normal, bold_code) in _FONT_MAP.items():
        if key in fam:
            return bold_code if bold else normal
    return "hebo" if bold else "helv"


# -- Helpers --------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert CSS hex to PyMuPDF (r, g, b) float tuple."""
    h = (hex_color or "#000000").lstrip("#")
    if len(h) != 6:
        return (0, 0, 0)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return (0, 0, 0)
    return (r / 255, g / 255, b / 255)


# -- Element renderers ----------------------------------------------------------

def _render_header(page: fitz.Page, elem: dict) -> None:
    """Render a header element onto the page."""
    font = _pick_font_code(bold=True)
    size = elem.get("font_size", 16)
    bbox = elem["bbox"]
    color = _hex_to_rgb(elem.get("color", "#000000"))

    text = elem.get("text", "")
    x = bbox["x"]
    y = bbox["y"] + size  # baseline offset

    page.insert_text(
        fitz.Point(x, y),
        text,
        fontname=font,
        fontsize=size,
        color=color,
    )


def _render_paragraph(page: fitz.Page, elem: dict) -> None:
    """Render a paragraph element onto the page."""
    bold = elem.get("bold", False)
    font = _pick_font_code(bold=bold)
    size = elem.get("font_size", 12)
    bbox = elem["bbox"]
    color = _hex_to_rgb(elem.get("color", "#000000"))

    text = elem.get("text", "")
    lines = text.split("\n")

    x = bbox["x"]
    y = bbox["y"] + size  # first baseline
    line_height = size * 1.3

    for line in lines:
        if line.strip():
            page.insert_text(
                fitz.Point(x, y),
                line,
                fontname=font,
                fontsize=size,
                color=color,
            )
        y += line_height


def _render_table(page: fitz.Page, elem: dict) -> None:
    """Render a table element with cell borders and text."""
    rows = elem.get("rows", [])
    if not rows:
        return

    bbox = elem["bbox"]
    font = _pick_font_code(bold=False)
    size = elem.get("font_size", 11)
    color = _hex_to_rgb(elem.get("color", "#000000"))

    col_positions = elem.get("col_positions", [])
    col_widths = elem.get("col_widths", [])
    row_positions = elem.get("row_positions", [])

    num_cols = max(len(col_positions), max((len(r) for r in rows), default=1))
    num_rows = len(rows)

    table_x = bbox["x"]
    table_y = bbox["y"]
    table_width = bbox["width"]

    if not col_widths or len(col_widths) < num_cols:
        cw = table_width / max(num_cols, 1)
        col_widths = [cw] * num_cols

    row_height = size * 1.6
    if not row_positions or len(row_positions) < num_rows:
        row_positions = [table_y + i * row_height for i in range(num_rows)]

    if not col_positions or len(col_positions) < num_cols:
        col_positions = [table_x + sum(col_widths[:i]) for i in range(num_cols)]

    for ri, row in enumerate(rows):
        ry = row_positions[ri] if ri < len(row_positions) else table_y + ri * row_height

        for ci, cell_text in enumerate(row):
            if ci >= num_cols:
                break

            cx = col_positions[ci] if ci < len(col_positions) else table_x + ci * col_widths[0]
            cw = col_widths[ci] if ci < len(col_widths) else col_widths[0]

            cell_rect = fitz.Rect(cx, ry, cx + cw, ry + row_height)
            page.draw_rect(cell_rect, color=(0.4, 0.4, 0.4), width=0.5)

            if cell_text and cell_text.strip():
                text_x = cx + 3
                text_y = ry + size + 2  # baseline
                page.insert_text(
                    fitz.Point(text_x, text_y),
                    cell_text.strip(),
                    fontname=font,
                    fontsize=size,
                    color=color,
                )


def _render_list(page: fitz.Page, elem: dict) -> None:
    """Render a bullet/ordered list, one item per line."""
    items = elem.get("items")
    if not items:
        # Fall back to splitting text on newlines
        items = [l for l in elem.get("text", "").split("\n") if l.strip()]
    if not items:
        return

    font = _pick_font_code(bold=False)
    size = elem.get("font_size", 12)
    bbox = elem["bbox"]
    color = _hex_to_rgb(elem.get("color", "#000000"))
    ordered = elem.get("list_kind") == "ordered"

    x = bbox["x"]
    y = bbox["y"] + size
    line_height = size * 1.4
    indent = size * 1.4

    for i, item in enumerate(items):
        marker = f"{i + 1}." if ordered else "•"
        page.insert_text(fitz.Point(x, y), marker, fontname=font, fontsize=size, color=color)
        page.insert_text(fitz.Point(x + indent, y), item.strip(),
                         fontname=font, fontsize=size, color=color)
        y += line_height


def _render_footer(page: fitz.Page, elem: dict) -> None:
    """Render a footer line, slightly smaller and grey."""
    font = _pick_font_code(bold=False)
    size = max(elem.get("font_size", 9), 7)
    bbox = elem["bbox"]
    color = _hex_to_rgb(elem.get("color", "#555555"))
    page.insert_text(
        fitz.Point(bbox["x"], bbox["y"] + size),
        elem.get("text", ""),
        fontname=font, fontsize=size, color=color,
    )


def _render_form_field(page: fitz.Page, elem: dict) -> None:
    """Render a form field as ``Label: <value or underline>``."""
    font = _pick_font_code(bold=False)
    size = elem.get("font_size", 11)
    bbox = elem["bbox"]
    color = _hex_to_rgb(elem.get("color", "#000000"))

    label = elem.get("label", "")
    text = elem.get("text", "")
    x = bbox["x"]
    y = bbox["y"] + size

    if label and label not in text:
        text = f"{label}: {text}".strip()
    page.insert_text(fitz.Point(x, y), text, fontname=font, fontsize=size, color=color)

    # Underline the remaining field width as a fill-in hint.
    line_y = y + 2
    right = bbox["x"] + bbox.get("width", 0)
    if right > x:
        page.draw_line(
            fitz.Point(x, line_y),
            fitz.Point(right, line_y),
            color=(0.6, 0.6, 0.6),
            width=0.5,
        )


# -- Renderer map ---------------------------------------------------------------

_RENDERERS = {
    "header": _render_header,
    "paragraph": _render_paragraph,
    "list": _render_list,
    "footer": _render_footer,
    "form_field": _render_form_field,
    "table": _render_table,
}


# -- Public entry point ---------------------------------------------------------

def generate_pdf(
    document: dict,
    output_path: Path,
) -> None:
    """Render a reconstructed (and optionally edited) document to a new PDF.

    Parameters
    ----------
    document : dict
        A ``ReconstructedDocument`` dict with ``pages`` list.
    output_path : Path
        Where to write the output PDF.
    """
    doc = fitz.open()  # new empty PDF

    try:
        for page_data in document.get("pages", []):
            width = page_data.get("width", 595)
            height = page_data.get("height", 842)

            page = doc.new_page(width=width, height=height)

            for elem in page_data.get("elements", []):
                renderer = _RENDERERS.get(elem.get("type"), _render_paragraph)
                try:
                    renderer(page, elem)
                except Exception as e:
                    print(f"[WARN] Failed to render element {elem.get('id')}: {e}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path))
    finally:
        doc.close()
