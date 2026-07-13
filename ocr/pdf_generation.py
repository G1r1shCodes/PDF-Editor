"""Generate PDF using Playwright + Markdown + MathJax instead of PyMuPDF manual drawing."""

import sys
from pathlib import Path
from typing import Dict, Any
import markdown

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

def _apply_inline_styles(elem: dict, text: str) -> str:
    if elem.get("type") not in ["paragraph", "header", "title", "list"]:
        return text
        
    font_name = elem.get("font_name")
    font_size = elem.get("font_size")
    color = elem.get("color")
    
    style_parts = []
    if font_name:
        font_css = None
        if "courier" in font_name.lower() or "mono" in font_name.lower():
            font_css = "Courier New, monospace"
        elif "times" in font_name.lower() or "serif" in font_name.lower():
            font_css = "Times New Roman, serif"
        elif "helvetica" in font_name.lower() or "arial" in font_name.lower():
            font_css = "Helvetica, Arial, sans-serif"
        if font_css:
            style_parts.append(f"font-family: {font_css}")
            
    if font_size:
        style_parts.append(f"font-size: {font_size}pt")
    if color:
        style_parts.append(f"color: {color}")
        
    is_bold = elem.get("bold") or (font_name and "bold" in font_name.lower())
    is_italic = elem.get("italic") or (font_name and ("italic" in font_name.lower() or "oblique" in font_name.lower()))
    
    if is_bold:
        style_parts.append("font-weight: bold")
    if is_italic:
        style_parts.append("font-style: italic")
        
    if style_parts:
        style_str = "; ".join(style_parts)
        return f'<span style="{style_str}">{text}</span>'
    return text

def _document_to_markdown(doc: dict) -> str:
    md_lines = []
    for page in doc.get("pages", []):
        for elem in page.get("elements", []):
            typ = elem.get("type")
            text = elem.get("text", "")
            if typ in ["header", "title"]:
                styled_text = _apply_inline_styles(elem, text)
                md_lines.append(f"## {styled_text}\n")
            elif typ == "paragraph":
                styled_text = _apply_inline_styles(elem, text)
                md_lines.append(f"{styled_text}\n")
            elif typ == "list":
                items = elem.get("items", [])
                if not items and text:
                    items = [l for l in text.split("\n") if l.strip()]
                for item in items:
                    styled_item = _apply_inline_styles(elem, item)
                    md_lines.append(f"- {styled_item}")
                md_lines.append("\n")
            elif typ == "table":
                rows = elem.get("rows", [])
                if rows:
                    md_lines.append(" | ".join(rows[0]))
                    md_lines.append(" | ".join(["---"] * len(rows[0])))
                    for row in rows[1:]:
                        md_lines.append(" | ".join(row))
                md_lines.append("\n")
            elif typ == "equation":
                md_lines.append(f"$${text}$$\n")
            elif typ == "image":
                img_path = elem.get("img_path", "")
                if img_path:
                    try:
                        import base64
                        import mimetypes
                        # Resolve the absolute path to the static directory where MinerU copied the images
                        base_dir = Path(__file__).parent.parent
                        path_obj = base_dir / "static" / img_path
                        if path_obj.exists():
                            mime_type, _ = mimetypes.guess_type(path_obj)
                            if not mime_type:
                                mime_type = "image/png"
                            with open(path_obj, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode("utf-8")
                            data_uri = f"data:{mime_type};base64,{b64_data}"
                            md_lines.append(f"![image]({data_uri})\n")
                    except Exception as e:
                        print(f"Failed to encode image {img_path}: {e}")
        
        # Add a page break between pages
        md_lines.append('\n<div style="page-break-after: always;"></div>\n')
        
    return "\n".join(md_lines)

async def generate_pdf(document: dict, output_path: Path, font_family: str = None) -> None:
    """Render a reconstructed (and optionally edited) document to a new PDF."""
    if not async_playwright:
        raise RuntimeError("Playwright is not installed. Please run `pip install playwright` and `playwright install chromium`.")

    md_content = _document_to_markdown(document)
    
    # Convert MD to HTML with python-markdown-math extension to preserve LaTeX underscores
    # and the 'tables' extension to properly parse markdown tables into HTML <table> tags.
    html_content = markdown.markdown(md_content, extensions=['mdx_math', 'tables'])

    font_css = "Helvetica, Arial, sans-serif"
    if font_family == "Times Roman" or font_family == "serif":
        font_css = "Times New Roman, Times, serif"
    elif font_family == "Courier" or font_family == "monospace":
        font_css = "Courier New, Courier, monospace"

    template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <script>
          MathJax = {{
            tex: {{
              inlineMath: [['$', '$'], ['\\\\(', '\\\\)']]
            }},
            svg: {{
              fontCache: 'global'
            }}
          }};
        </script>
        <style>
          @page {{ margin: 20mm; }}
          body {{ font-family: {font_css}; line-height: 1.6; max-width: 800px; margin: 0 auto; padding: 0; font-size: 14px; }}
          table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5em; }}
          th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
          th {{ background-color: #f4f4f4; }}
          img {{ max-width: 100%; height: auto; }}
          h2 {{ margin-top: 1.5em; margin-bottom: 0.5em; }}
          p {{ margin-bottom: 1em; }}
        </style>
    </head>
    <body>
    {html_content}
    <script type="text/javascript" id="MathJax-script"
      src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js">
    </script>
    </body>
    </html>
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # Wait until networkidle to ensure MathJax scripts load and execute
        await page.set_content(template, wait_until="networkidle")
        await page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True
        )
        await browser.close()
