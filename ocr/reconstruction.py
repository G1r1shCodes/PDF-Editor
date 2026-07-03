"""Document reconstruction — builds an editable document structure from layouts.

Takes one or more ``PageLayout`` dicts (from ``layout.analyse_layout``) and
produces a ``ReconstructedDocument`` that the frontend can render as an
editable structured view.
"""

from typing import Any, Dict, List


ReconstructedDocument = Dict[str, Any]


def build_editable_document(
    page_layouts: List[dict],
) -> ReconstructedDocument:
    """Convert analysed page layouts into a frontend-editable document.

    Each element gets:
      - ``id`` : unique identifier (``p{page}_e{index}``)
      - ``type`` : "header" | "paragraph" | "table"
      - ``text`` : editable text content
      - ``bbox`` : position in PDF points
      - ``font_size`` : estimated font size
      - ``bold`` : whether the element appears bold
      - For tables: ``rows`` (list of lists of cell strings),
        ``col_widths``, ``col_positions``
    """
    pages = []

    for layout in page_layouts:
        page_num = layout["page"]
        elements = []

        for elem in layout.get("elements", []):
            text = elem.get("text", "")
            entry: Dict[str, Any] = {
                "id": elem.get("id", f"p{page_num}_e{len(elements)}"),
                "type": elem["type"],
                "text": text,
                "bbox": elem.get("bbox", {"x": 0, "y": 0, "width": 0, "height": 0}),
                "font_size": elem.get("font_size", 12),
                "bold": elem.get("bold", False),
                "color": elem.get("color", "#000000"),
            }
            if "img_path" in elem:
                entry["img_path"] = elem["img_path"]

            if elem["type"] == "table":
                entry["rows"] = elem.get("rows", [])
                entry["col_widths"] = elem.get("col_widths", [])
                entry["col_positions"] = elem.get("col_positions", [])
            elif elem["type"] == "image":
                entry["img_path"] = elem.get("img_path", "")
            elif elem["type"] == "list":
                # Derive items from text lines when the detector didn't supply them.
                items = elem.get("items") or [l for l in text.split("\n") if l.strip()]
                entry["items"] = items
                entry["list_kind"] = elem.get("list_kind", "bullet")
            elif elem["type"] == "form_field":
                label = elem.get("label") or (text.split(":", 1)[0].strip() if ":" in text else "")
                entry["label"] = label

            elements.append(entry)

        pages.append({
            "page": page_num,
            "width": layout.get("width", 595),
            "height": layout.get("height", 842),
            "elements": elements,
        })

    return {"pages": pages}


def apply_edits(
    document: ReconstructedDocument,
    edits: Dict[str, Dict[str, Any]],
) -> ReconstructedDocument:
    """Apply user edits to a reconstructed document.

    Parameters
    ----------
    document : ReconstructedDocument
        The original document structure.
    edits : dict
        Mapping of element ID → dict of fields to override.
        For paragraphs/headers: ``{"text": "new text"}``
        For tables: ``{"rows": [["A", "B"], ["C", "D"]]}``

    Returns
    -------
    ReconstructedDocument
        A new document with edits merged in.
    """
    import copy
    result = copy.deepcopy(document)

    for page in result["pages"]:
        for elem in page["elements"]:
            eid = elem["id"]
            if eid in edits:
                elem_edits = edits[eid]
                for key, value in elem_edits.items():
                    elem[key] = value
                # If table text was edited via rows, rebuild the text
                if elem["type"] == "table" and "rows" in elem_edits:
                    elem["text"] = "\n".join(
                        " | ".join(row) for row in elem["rows"]
                    )
                # If list items were edited, rebuild the text
                if elem["type"] == "list" and "items" in elem_edits:
                    elem["text"] = "\n".join(elem.get("items", []))
                # The editor edits lists as plain text; keep items in sync so
                # the PDF renderer (which prefers items) reflects the edit.
                elif elem["type"] == "list" and "text" in elem_edits:
                    elem["items"] = [l for l in elem["text"].split("\n") if l.strip()]

    return result
