"""MinerU OCR/layout backend (local, self-hosted — no API key required).

Phase 1 integration: MinerU is used purely as a drop-in alternative *text
source* for the `/reconstruct` endpoint, with exactly the same contract as
``nemotron_ocr.extract_ocr_blocks_nemotron`` and Tesseract's
``ocr.extraction.extract_ocr_blocks`` — i.e. it returns a flat list of
``OCRBlock`` (word-level bbox + text in PDF points), which then flows,
unchanged, through the existing ``ocr.layout.analyse_layout`` ->
``ocr.reconstruction.build_editable_document`` pipeline.

MinerU already does its own layout/reading-order analysis internally. This integration
(Phase 2) bypasses ``ocr.layout`` entirely and consumes MinerU's own paragraph, 
table, equation, and heading structures directly to produce a better reconstruction,
preserving native Markdown and LaTeX formulas.

Why subprocess + CLI (not the Python API)
------------------------------------------
MinerU's internal Python API has changed shape across major versions
(``magic_pdf.pipe.UNIPipe`` in 0.x, ``mineru.backend.*`` in 2.x). The CLI
entrypoint (``mineru -p <input> -o <output_dir> -m auto``) is the one
interface the project has kept stable across versions, and it mirrors how
this codebase already treats other AI backends (Gemini, Replicate)
as an external service to shell out to / call over HTTP rather than an
in-process library. That keeps this module resilient to MinerU internals
changing.

Configuration (env or per-request):
  MINERU_BIN        Path to the `mineru` executable. Default: "mineru" (must
                     be on PATH — installed via `pip install -U "mineru[core]"`).
  MINERU_DEVICE      "cpu" | "cuda" | "cuda:0" etc. Default: "cpu".
  MINERU_TIMEOUT     Seconds to wait for a single page. Default: 300.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
import re

import fitz  # PyMuPDF

from ocr.extraction import OCRBlock

DEFAULT_TIMEOUT = 1200
DEFAULT_DEVICE = "cuda"


class MinerUOCRError(RuntimeError):
    """Raised when MinerU cannot produce usable results for a page."""


# ── Availability / configuration ───────────────────────────────────────────

def _mineru_bin() -> str:
    return os.environ.get("MINERU_BIN", "mineru")


def is_configured() -> bool:
    """True if a `mineru` executable can be located.

    Unlike Nemotron (which needs an API key) MinerU is a local model runtime,
    so "configured" just means the CLI is installed and importable.
    """
    return shutil.which(_mineru_bin()) is not None


# ── Running MinerU on a single page ─────────────────────────────────────────

def _render_multi_page_pdf(doc: fitz.Document, page_nums: List[int], out_path: Path) -> None:
    """Write a standalone PDF containing just the requested pages, for MinerU."""
    subset = fitz.open()
    for pn in page_nums:
        subset.insert_pdf(doc, from_page=pn, to_page=pn)
    subset.save(str(out_path))
    subset.close()


def _run_mineru_cli(input_pdf: Path, out_dir: Path, device: str, timeout: int, backend: str = "pipeline", effort: str = "medium", nvidia_api_key: Optional[str] = None, lang: str = "en") -> Path:
    """Invoke the MinerU CLI on a multi-page PDF and return the content-list JSON path."""
    
    # If the user sets backend="auto", we don't pass it so MinerU falls back to its default (hybrid-engine)
    cmd = [
        _mineru_bin(),
        "-p", str(input_pdf),
        "-o", str(out_dir),
        "-m", "auto",
        "--effort", effort,
    ]
    if backend and backend != "auto":
        cmd.extend(["-b", backend])

    # --lang is documented as "pipeline backend only" but helps OCR in all backends.
    # MinerU defaults to Chinese ('ch') OCR when it's omitted, which badly corrupts 
    # English-document OCR.
    if lang:
        cmd.extend(["-l", lang])
    
    if backend in ["hybrid-http-client", "vlm-http-client"]:
        # Route to local proxy to enforce strict JSON/Markdown output formatting
        proxy_url = os.environ.get("MINERU_PROXY_URL", "http://127.0.0.1:8001/v1")
        cmd.extend(["-u", proxy_url])
        
    print(f"DEBUG: Running MinerU with cmd: {cmd}", flush=True)
    env = os.environ.copy()
    # The 4GB GPU is causing Windows Page File (WinError 1455) crashes. 
    # Forcing CPU mode to bypass CUDA memory limitations completely.
    env["CUDA_VISIBLE_DEVICES"] = "-1"
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True,max_split_size_mb:32,garbage_collection_threshold:0.8"
    
    if backend in ["hybrid-http-client", "vlm-http-client"] and nvidia_api_key:
        env["MINERU_VL_API_KEY"] = nvidia_api_key
        env["MINERU_VL_MODEL_NAME"] = "nvidia/llama-3.1-nemotron-nano-vl-8b-v1"
        
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError:
        raise MinerUOCRError(
            f"'{_mineru_bin()}' executable not found. Install with "
            "`pip install -U \"mineru[core]\"`, or set MINERU_BIN to its path."
        )
    except subprocess.TimeoutExpired:
        raise MinerUOCRError(f"MinerU timed out after {timeout}s.")

    if result.returncode != 0:
        raise MinerUOCRError(
            f"MinerU exited with code {result.returncode}: "
            f"{(result.stderr or result.stdout)[-800:]}"
        )

    candidates = list(out_dir.rglob("*_content_list.json"))
    if not candidates:
        raise MinerUOCRError(
            "MinerU ran but produced no *_content_list.json output. "
            f"stdout: {result.stdout[-400:]}"
        )
    return candidates[0]


# ── Parsing MinerU's content list into PageLayout ──────────────────────────

def extract_layouts_mineru_bulk(
    doc: fitz.Document,
    page_nums: List[int],
    device: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    backend: str = "pipeline",
    effort: str = "medium",
    nvidia_api_key: Optional[str] = None,
    lang: str = "en",
) -> tuple[List[dict], str]:
    """Run MinerU on all requested pages at once and return (layouts, md_content).
    Bypasses the word-level OCR extraction and basic layout analysis to preserve
    MinerU's native Markdown, LaTeX formulas, and layout structure.
    """
    if not is_configured():
        raise MinerUOCRError(
            "MinerU is not installed. Run `pip install -U \"mineru[core]\"` "
            "or set MINERU_BIN to a valid executable path."
        )

    device = device or os.environ.get("MINERU_DEVICE", DEFAULT_DEVICE)

    with tempfile.TemporaryDirectory(prefix="mineru_") as tmp:
        tmp_dir = Path(tmp)
        subset_pdf = tmp_dir / "subset.pdf"
        out_dir = tmp_dir / "out"
        out_dir.mkdir()

        _render_multi_page_pdf(doc, page_nums, subset_pdf)
        content_list_path = _run_mineru_cli(subset_pdf, out_dir, device, timeout, backend, effort, nvidia_api_key, lang)

        # Diagnostics: MinerU writes its raw JSON into a TemporaryDirectory that
        # is deleted the moment this function returns, which makes parsing bugs
        # (e.g. dropped headings, bad page_size scaling) impossible to inspect
        # after the fact. Copy the *_content_list.json and *_middle.json out to a
        # persistent debug folder so they can be examined. Best-effort only.
        try:
            debug_dir = Path(__file__).parent / "static" / "mineru_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            for pattern in ("*_content_list.json", "*_middle.json"):
                for f in content_list_path.parent.rglob(pattern):
                    shutil.copy2(f, debug_dir / f.name)
        except Exception as _dbg_err:
            print(f"DEBUG: could not copy MinerU debug output: {_dbg_err}", flush=True)

        data = json.loads(content_list_path.read_text(encoding="utf-8"))
        
        # Load middle json to get page_size for each page
        pdf_info = []
        try:
            middle_path = content_list_path.with_name(content_list_path.name.replace("_content_list.json", "_middle.json"))
            if middle_path.exists():
                middle_data = json.loads(middle_path.read_text(encoding="utf-8"))
                pdf_info = middle_data.get("pdf_info", [])
        except Exception:
            pass

        # Copy images to static directory
        src_images = content_list_path.parent / "images"
        dest_images = Path(__file__).parent / "static" / "images"
        if src_images.exists():
            dest_images.mkdir(parents=True, exist_ok=True)
            for img_file in src_images.iterdir():
                if img_file.is_file():
                    shutil.copy2(img_file, dest_images / img_file.name)

        # Prepare outputs for each page
        layouts_by_idx = {i: {"page": pn, "width": doc[pn].rect.width, "height": doc[pn].rect.height, "elements": []} for i, pn in enumerate(page_nums)}
        
        for idx, block in enumerate(data):
            page_idx = block.get("page_idx", 0)
            if page_idx not in layouts_by_idx:
                continue

            btype = block.get("type", "text")
            text = (block.get("text") or "").strip()
            img_path = block.get("img_path", "")
            
            if not text and btype != "table" and btype != "image":
                continue
            bbox = block.get("bbox")
            if not bbox:
                # MinerU's content_list.json omits bbox on many plain text /
                # heading blocks (tables & images do carry one). Previously such
                # blocks were dropped here -- which is exactly why document titles
                # silently disappeared from the reconstruction. bbox is only
                # stored, never used for layout (both the editor UI and the PDF
                # exporter order elements by reading order), so a placeholder is
                # safe and preserves the text.
                bbox = [0.0, 0.0, 0.0, 0.0]
                
            page_w = layouts_by_idx[page_idx]["width"]
            page_h = layouts_by_idx[page_idx]["height"]

            page_size = block.get("page_size")
            if not page_size and page_idx < len(pdf_info):
                page_size = pdf_info[page_idx].get("page_size")

            if page_size and page_size[0] and page_size[1]:
                sx = page_w / page_size[0]
                sy = page_h / page_size[1]
            else:
                sx = 1.0 if bbox[2] > 1.0 else page_w
                sy = 1.0 if bbox[3] > 1.0 else page_h

            x0, y0, x1, y1 = bbox
            x = round(x0 * sx, 2)
            y = round(y0 * sy, 2)
            w = round((x1 - x0) * sx, 2)
            h = round((y1 - y0) * sy, 2)
            
            num_lines = max(1, text.count("\n") + 1)
            estimated_size = (h / num_lines) * 0.75
            font_size = round(max(8.0, min(estimated_size, 72.0)), 1)
            
            layout_type = "paragraph"
            bold = False
            rows = []

            # MinerU flags headings via a `text_level` field on ordinary "text"
            # blocks (1 = top-level title) rather than a distinct block type, so
            # promote those to headers here.
            text_level = block.get("text_level")
            if btype == "text" and isinstance(text_level, int) and 1 <= text_level <= 2:
                btype = "title"

            if btype in ("title", "header"):
                layout_type = "header"
                bold = True
                font_size = min(max(font_size, 14.0), 28.0)
            elif btype == "list":
                layout_type = "list"
                font_size = min(font_size, 12.0)
            elif btype == "table":
                layout_type = "table"
                table_body = block.get("table_body", "")
                if table_body:
                    tr_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', table_body, re.IGNORECASE | re.DOTALL)
                    for tr in tr_matches:
                        td_matches = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.IGNORECASE | re.DOTALL)
                        clean_row = []
                        for td in td_matches:
                            cell = re.sub(r'<[^>]+>', '', td).strip()
                            try:
                                from html import unescape
                                cell = unescape(cell)
                            except Exception:
                                pass
                            clean_row.append(cell)
                        rows.append(clean_row)
                elif text:
                    # VLM Mode outputs tables as raw Markdown instead of HTML table_body
                    for line in text.strip().split('\n'):
                        line = line.strip()
                        if line.startswith('|'):
                            cells = [c.strip() for c in line.split('|')[1:-1]]
                            # Skip the markdown separator row (e.g., |---|---|)
                            if cells and not all(c.replace('-', '').replace(':', '').strip() == '' for c in cells):
                                rows.append(cells)
                text = ""
            elif btype == "equation":
                layout_type = "equation"
                font_size = min(font_size, 14.0)
            elif btype == "image" or btype == "image_body":
                layout_type = "image"
            elif btype in {"table_caption", "image_caption", "table_footnote", "image_footnote", "footer"}:
                layout_type = "footer"
                font_size = min(font_size, 10.0)
            else:
                layout_type = "paragraph"
                font_size = min(font_size, 12.0)

            # Extract inline captions before the main element
            captions = block.get("table_caption", []) + block.get("image_caption", [])
            for cap_idx, cap in enumerate(captions):
                if isinstance(cap, str) and cap.strip():
                    layouts_by_idx[page_idx]["elements"].append({
                        "id": f"p{layouts_by_idx[page_idx]['page']}_e{idx}_cap{cap_idx}",
                        "type": "header",
                        "text": cap.strip(),
                        "img_path": "",
                        "bbox": {"x": x, "y": max(0, y - 25 - cap_idx*25), "width": w, "height": 20},
                        "font_size": 16.0,
                        "bold": True,
                        "color": "#000000",
                    })

            elem = {
                "id": f"p{layouts_by_idx[page_idx]['page']}_e{idx}",
                "type": layout_type,
                "text": text,
                "img_path": img_path,
                "bbox": {"x": x, "y": y, "width": w, "height": h},
                "font_size": font_size,
                "bold": bold,
                "color": "#000000",
            }
            if rows:
                elem["rows"] = rows
            layouts_by_idx[page_idx]["elements"].append(elem)

            # Extract inline footnotes after the main element
            footnotes = block.get("table_footnote", []) + block.get("image_footnote", [])
            for fn_idx, fn in enumerate(footnotes):
                if isinstance(fn, str) and fn.strip():
                    layouts_by_idx[page_idx]["elements"].append({
                        "id": f"p{layouts_by_idx[page_idx]['page']}_e{idx}_fn{fn_idx}",
                        "type": "paragraph",
                        "text": fn.strip(),
                        "img_path": "",
                        "bbox": {"x": x, "y": y + h + 10 + fn_idx*15, "width": w, "height": 15},
                        "font_size": 10.0,
                        "bold": False,
                        "color": "#666666",
                    })

        # Sort elements top-to-bottom, left-to-right to fix header/footer ordering
        for page_data in layouts_by_idx.values():
            page_data["elements"].sort(key=lambda e: (e["bbox"]["y"], e["bbox"]["x"]))

        # Find the MD file
        md_content = ""
        md_candidates = list(content_list_path.parent.rglob("*.md"))
        if md_candidates:
            md_content = md_candidates[0].read_text(encoding="utf-8")

        return list(layouts_by_idx.values()), md_content
