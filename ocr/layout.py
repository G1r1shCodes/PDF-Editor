"""Layout analysis - groups raw OCR words into semantic document structures.

Takes word-level OCR blocks (with Tesseract hierarchy IDs) and produces a
structured page layout containing headers, paragraphs, tables, lists, footers
and form-fields. Representative glyph colours are propagated from the OCR
words up to each element.
"""

from typing import Any, Dict, List, Tuple
import re
import statistics

# Bullet markers that begin an unordered list line
_BULLET_RE = re.compile(r"^\s*([•‣●▪·\*\-–—o])\s+\S")
# Ordered markers: "1.", "2)", "a.", "iv)" etc.
_ORDERED_RE = re.compile(r"^\s*(\(?[0-9]{1,2}|[a-zA-Z]|[ivxlcdmIVXLCDM]{1,4})[\.\)]\s+\S")
# A run of 3+ underscores/dots = a fill-in blank (form field)
_BLANK_RE = re.compile(r"[_․\.]{3,}")
# "Label:" possibly followed by blanks
_LABEL_RE = re.compile(r"^\s*[A-Za-z][\w /]{0,40}:\s*")


# -- Public types ---------------------------------------------------------------

PageLayout = Dict[str, Any]   # see docstring of analyse_layout


# -- Colour helpers -------------------------------------------------------------

def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) != 6:
        return (0, 0, 0)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(r), int(g), int(b))


def _median_color(colors: List[str]) -> str:
    """Median per-channel colour from a list of hex strings."""
    rgbs = [_hex_to_rgb(c) for c in colors if c]
    if not rgbs:
        return "#000000"
    r = int(statistics.median(c[0] for c in rgbs))
    g = int(statistics.median(c[1] for c in rgbs))
    b = int(statistics.median(c[2] for c in rgbs))
    return _rgb_to_hex(r, g, b)


# -- Helpers --------------------------------------------------------------------

def _group_into_lines(blocks: List[dict]) -> List[dict]:
    """Group OCR words into lines using Tesseract's hierarchy IDs.

    Returns a list of line dicts, each with:
        block_num, par_num, line_num, words, bbox, text, font_size, color
    """
    line_map: Dict[Tuple[int, int, int], List[dict]] = {}
    for w in blocks:
        key = (w["block_num"], w["par_num"], w["line_num"])
        line_map.setdefault(key, []).append(w)

    lines = []
    for key, words in sorted(line_map.items()):
        # Sort words left-to-right
        words.sort(key=lambda w: w["bbox"]["x"])
        text = " ".join(w["text"] for w in words)

        xs = [w["bbox"]["x"] for w in words]
        ys = [w["bbox"]["y"] for w in words]
        x2s = [w["bbox"]["x"] + w["bbox"]["width"] for w in words]
        y2s = [w["bbox"]["y"] + w["bbox"]["height"] for w in words]

        bbox = {
            "x": min(xs),
            "y": min(ys),
            "width": round(max(x2s) - min(xs), 2),
            "height": round(max(y2s) - min(ys), 2),
        }

        # Per-line median font size is steadier than per-word values.
        font_sizes = [w["font_size_estimate"] for w in words]
        avg_font = round(statistics.median(font_sizes), 2) if font_sizes else 12
        color = _median_color([w.get("color", "#000000") for w in words])

        lines.append({
            "block_num": key[0],
            "par_num": key[1],
            "line_num": key[2],
            "words": words,
            "bbox": bbox,
            "text": text,
            "font_size": avg_font,
            "color": color,
        })

    return lines


def _group_into_paragraphs(lines: List[dict]) -> List[dict]:
    """Group lines into paragraphs by vertical proximity and block/par ID."""
    par_map: Dict[Tuple[int, int], List[dict]] = {}
    for line in lines:
        key = (line["block_num"], line["par_num"])
        par_map.setdefault(key, []).append(line)

    paragraphs = []
    for key, par_lines in sorted(par_map.items()):
        par_lines.sort(key=lambda l: l["bbox"]["y"])

        # Split a Tesseract paragraph when the vertical gap is large.
        groups = [[par_lines[0]]]
        for i in range(1, len(par_lines)):
            prev = groups[-1][-1]
            curr = par_lines[i]
            gap = curr["bbox"]["y"] - (prev["bbox"]["y"] + prev["bbox"]["height"])
            threshold = max(prev["font_size"], curr["font_size"]) * 1.8
            if gap > threshold:
                groups.append([curr])
            else:
                groups[-1].append(curr)

        for group in groups:
            text = "\n".join(l["text"] for l in group)
            xs = [l["bbox"]["x"] for l in group]
            ys = [l["bbox"]["y"] for l in group]
            x2s = [l["bbox"]["x"] + l["bbox"]["width"] for l in group]
            y2s = [l["bbox"]["y"] + l["bbox"]["height"] for l in group]

            bbox = {
                "x": round(min(xs), 2),
                "y": round(min(ys), 2),
                "width": round(max(x2s) - min(xs), 2),
                "height": round(max(y2s) - min(ys), 2),
            }

            font_sizes = [l["font_size"] for l in group]
            avg_font = round(statistics.median(font_sizes), 2)
            color = _median_color([l.get("color", "#000000") for l in group])

            paragraphs.append({
                "lines": group,
                "bbox": bbox,
                "text": text,
                "font_size": avg_font,
                "color": color,
                "type": "paragraph",
            })

    return paragraphs


def _detect_headers(paragraphs: List[dict]) -> None:
    """Reclassify large, short paragraphs as headers."""
    if not paragraphs:
        return

    font_sizes = [p["font_size"] for p in paragraphs]
    median_size = statistics.median(font_sizes)
    threshold = median_size * 1.35

    for p in paragraphs:
        if p.get("type", "paragraph") != "paragraph":
            continue
        is_large = p["font_size"] >= threshold
        is_short = len(p["text"]) < 80 and "\n" not in p["text"]
        if is_large and is_short:
            p["type"] = "header"
            p["bold"] = True


def _detect_lists(paragraphs: List[dict]) -> None:
    """Mark paragraphs whose lines mostly begin with bullet/number markers."""
    for p in paragraphs:
        if p.get("type", "paragraph") != "paragraph":
            continue
        lines = p.get("lines", [])
        if not lines:
            continue
        marked = 0
        for l in lines:
            t = l["text"]
            if _BULLET_RE.match(t) or _ORDERED_RE.match(t):
                marked += 1
        if marked and marked >= max(1, len(lines) * 0.5):
            p["type"] = "list"


def _detect_form_fields(paragraphs: List[dict]) -> None:
    """Mark paragraphs that look like form fields: 'Label: ____' / blanks."""
    for p in paragraphs:
        if p.get("type", "paragraph") != "paragraph":
            continue
        text = p["text"]
        has_blank = bool(_BLANK_RE.search(text))
        labelled = bool(_LABEL_RE.match(text))
        # A label whose value region is blank or empty after the colon.
        if has_blank and (labelled or ":" in text):
            p["type"] = "form_field"
        elif labelled and len(text.split(":", 1)[-1].strip()) <= 1:
            p["type"] = "form_field"


def _detect_footers(paragraphs: List[dict], page_height: float) -> None:
    """Mark short paragraphs sitting in the bottom page margin as footers."""
    if not page_height:
        return
    cutoff = page_height * 0.90
    for p in paragraphs:
        if p.get("type", "paragraph") != "paragraph":
            continue
        top = p["bbox"]["y"]
        is_bottom = top >= cutoff
        is_short = len(p["text"]) < 120 and p["text"].count("\n") <= 1
        if is_bottom and is_short:
            p["type"] = "footer"


def _detect_tables(paragraphs: List[dict], page_width: float) -> List[dict]:
    """Detect table-like structures from consecutive column-aligned rows."""
    result = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]

        if p.get("type", "paragraph") == "paragraph":
            # If the paragraph is a single multi-line block that is internally a table,
            # we break it apart into 1-line paragraphs so the existing logic can build it.
            if _looks_like_multi_line_table(p):
                fake_paragraphs = []
                for line in p.get("lines", []):
                    fake_paragraphs.append({
                        "type": "paragraph",
                        "lines": [line],
                        "bbox": line["bbox"],
                        "font_size": line["font_size"],
                        "text": line["text"]
                    })
                # Re-insert the fake paragraphs and continue from the same index
                paragraphs = paragraphs[:i] + fake_paragraphs + paragraphs[i+1:]
                p = paragraphs[i]

            if _looks_like_table_row(p):
                table_rows = [p]
                j = i + 1
                while j < len(paragraphs):
                    candidate = paragraphs[j]
                    if candidate.get("type", "paragraph") != "paragraph":
                        break
                    
                    # If a candidate is a multi-line table that should be appended to the current table,
                    # we should also expand it. But typically if it's closely spaced, it would have been
                    # merged into the same paragraph in nemotron_ocr.py. For safety, we can just let it 
                    # break or we expand candidates too. Expanding candidates inline is safer.
                    if _looks_like_multi_line_table(candidate):
                        fake_paragraphs = []
                        for line in candidate.get("lines", []):
                            fake_paragraphs.append({
                                "type": "paragraph",
                                "lines": [line],
                                "bbox": line["bbox"],
                                "font_size": line["font_size"],
                                "text": line["text"]
                            })
                        paragraphs = paragraphs[:j] + fake_paragraphs + paragraphs[j+1:]
                        candidate = paragraphs[j]

                    gap = candidate["bbox"]["y"] - (table_rows[-1]["bbox"]["y"] + table_rows[-1]["bbox"]["height"])
                    if gap > table_rows[-1]["font_size"] * 2.5:
                        break
                    if _looks_like_table_row(candidate) or _is_continuation_row(candidate, table_rows):
                        table_rows.append(candidate)
                        j += 1
                    else:
                        break

                if len(table_rows) >= 2:
                    result.append(_build_table(table_rows))
                    i = j
                    continue

        result.append(p)
        i += 1

    return result


def _looks_like_table_row(paragraph: dict) -> bool:
    """Check if a paragraph looks like a table row (2+ column-like word groups)."""
    if paragraph.get("type", "paragraph") != "paragraph":
        return False
    if not paragraph.get("lines"):
        return False

    all_words = []
    for line in paragraph["lines"]:
        all_words.extend(line.get("words", []))

    if len(all_words) < 2:
        return False

    all_words.sort(key=lambda w: w["bbox"]["x"])
    gaps = []
    for k in range(1, len(all_words)):
        prev_right = all_words[k - 1]["bbox"]["x"] + all_words[k - 1]["bbox"]["width"]
        curr_left = all_words[k]["bbox"]["x"]
        gap = curr_left - prev_right
        if gap > 0:
            gaps.append(gap)

    if not gaps:
        return False

    avg_gap = statistics.mean(gaps)
    large_gaps = [g for g in gaps if g > avg_gap * 2 and g > 15]
    return len(large_gaps) >= 1


def _looks_like_multi_line_table(paragraph: dict) -> bool:
    """Check if a multi-line paragraph is actually a tightly packed table."""
    if paragraph.get("type", "paragraph") != "paragraph":
        return False
    lines = paragraph.get("lines", [])
    if len(lines) < 2:
        return False

    table_like_lines = 0
    for line in lines:
        fake_p = {"type": "paragraph", "lines": [line]}
        if _looks_like_table_row(fake_p):
            table_like_lines += 1

    # If at least half the lines look like table rows, we classify the whole block as a table.
    return table_like_lines >= max(2, len(lines) * 0.5)


def _is_continuation_row(candidate: dict, existing_rows: list) -> bool:
    """Check if candidate is vertically close and horizontally overlapping."""
    if not existing_rows:
        return False
    last = existing_rows[-1]
    gap = candidate["bbox"]["y"] - (last["bbox"]["y"] + last["bbox"]["height"])
    return gap < last["font_size"] * 2.0 and gap >= 0


def _build_table(row_paragraphs: List[dict]) -> dict:
    """Convert a list of row-like paragraphs into a structured table block."""
    all_words = []
    for rp in row_paragraphs:
        for line in rp["lines"]:
            all_words.extend(line.get("words", []))

    x_positions = sorted(set(round(w["bbox"]["x"]) for w in all_words))
    col_starts = _cluster_positions(x_positions, min_gap=20)

    rows = []
    for rp in row_paragraphs:
        row_words = []
        for line in rp["lines"]:
            row_words.extend(line.get("words", []))
        row_words.sort(key=lambda w: w["bbox"]["x"])

        cells = [""] * len(col_starts)
        for w in row_words:
            col_idx = _nearest_col(w["bbox"]["x"], col_starts)
            if cells[col_idx]:
                cells[col_idx] += " " + w["text"]
            else:
                cells[col_idx] = w["text"]
        rows.append(cells)

    xs = [rp["bbox"]["x"] for rp in row_paragraphs]
    ys = [rp["bbox"]["y"] for rp in row_paragraphs]
    x2s = [rp["bbox"]["x"] + rp["bbox"]["width"] for rp in row_paragraphs]
    y2s = [rp["bbox"]["y"] + rp["bbox"]["height"] for rp in row_paragraphs]
    font_sizes = [rp["font_size"] for rp in row_paragraphs]

    col_widths = []
    for ci in range(len(col_starts)):
        if ci + 1 < len(col_starts):
            col_widths.append(round(col_starts[ci + 1] - col_starts[ci], 2))
        else:
            col_widths.append(round(max(x2s) - col_starts[ci], 2))

    return {
        "type": "table",
        "rows": rows,
        "col_positions": [round(c, 2) for c in col_starts],
        "col_widths": col_widths,
        "row_positions": [round(rp["bbox"]["y"], 2) for rp in row_paragraphs],
        "bbox": {
            "x": round(min(xs), 2),
            "y": round(min(ys), 2),
            "width": round(max(x2s) - min(xs), 2),
            "height": round(max(y2s) - min(ys), 2),
        },
        "font_size": round(statistics.mean(font_sizes), 2),
        "color": _median_color([w.get("color", "#000000") for w in all_words]),
        "text": "\n".join(" | ".join(row) for row in rows),
    }


def _cluster_positions(positions: list, min_gap: float = 20) -> List[float]:
    """Cluster sorted X positions into column starts."""
    if not positions:
        return []
    clusters = [[positions[0]]]
    for p in positions[1:]:
        if p - clusters[-1][-1] < min_gap:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [statistics.mean(c) for c in clusters]


def _nearest_col(x: float, col_starts: list) -> int:
    """Return the index of the nearest column start."""
    best = 0
    best_dist = abs(x - col_starts[0])
    for i, cs in enumerate(col_starts[1:], 1):
        d = abs(x - cs)
        if d < best_dist:
            best_dist = d
            best = i
    return best


# -- Main entry point -----------------------------------------------------------

def analyse_layout(
    ocr_blocks: List[dict],
    page_width: float,
    page_height: float,
    page_num: int = 0,
) -> PageLayout:
    """Analyse raw OCR blocks and return a structured page layout.

    Parameters
    ----------
    ocr_blocks : list[dict]
        Output from ``extract_ocr_blocks()``.
    page_width, page_height : float
        Page dimensions in PDF points.
    page_num : int
        Zero-based page number.

    Returns
    -------
    PageLayout
        Dict with keys: page, width, height, elements. Each element has at
        least: type (header|paragraph|list|form_field|footer|table), text,
        bbox, font_size, color, and type-specific fields.
    """
    lines = _group_into_lines(ocr_blocks)
    paragraphs = _group_into_paragraphs(lines)

    # Classify paragraph variants before merging tables.
    _detect_headers(paragraphs)
    _detect_lists(paragraphs)
    _detect_form_fields(paragraphs)
    _detect_footers(paragraphs, page_height)

    elements = _detect_tables(paragraphs, page_width)

    # Assign IDs
    for idx, elem in enumerate(elements):
        elem["id"] = f"p{page_num}_e{idx}"

    return {
        "page": page_num,
        "width": page_width,
        "height": page_height,
        "elements": elements,
    }
