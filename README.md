# PDFEdit — Smart PDF Editor

A lean, open-source web PDF editor that handles **both digital PDFs and scanned image PDFs**,
preserving fonts, sizes, and colors when you edit.

Built by combining:
- **OCRmyPDF + Tesseract** → scanned image PDF support
- **PyMuPDF (fitz)** → font metadata extraction from digital PDFs
- **React + PDF.js-style canvas** → click-to-edit text overlay in the browser

---

## Features

| Feature | Digital PDF | Scanned PDF |
|---|---|---|
| Auto-detect type | ✅ | ✅ |
| Extract text with font/size/color | ✅ | ✅ (OCR) |
| Click-to-edit any text block | ✅ | ✅ |
| Font family picker | ✅ | ✅ |
| Font size slider | ✅ | ✅ |
| Color picker | ✅ | ✅ |
| Download edited PDF | ✅ | ✅ |
| All local — no cloud upload | ✅ | ✅ |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (React)                                             │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Left sidebar   │  PDF Canvas         │  Right sidebar │ │
│  │  Upload / zoom  │  Page image + text  │  Font / color  │ │
│  │                 │  overlays           │  properties    │ │
│  └────────────────────────────────────────────────────────┘ │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST (JSON + multipart)
┌───────────────────────────▼─────────────────────────────────┐
│  FastAPI backend (Python)                                    │
│                                                             │
│  POST /upload                                               │
│    ├─ fitz.open()  → detect scanned vs digital              │
│    ├─ Digital → fitz.get_text("rawdict") → font metadata    │
│    └─ Scanned → pdf2image + Tesseract OCR → word boxes      │
│                                                             │
│  POST /save                                                 │
│    └─ fitz.insert_text() with matched font + color          │
└─────────────────────────────────────────────────────────────┘
```

**Libraries used:**

| Library | Role |
|---|---|
| `pymupdf` (fitz) | PDF rendering, font extraction, writing edits back |
| `pytesseract` | OCR engine wrapper |
| `pdf2image` | Convert PDF pages to PIL images for Tesseract |
| `ocrmypdf` | Available for batch OCR pipeline extension |
| `fastapi` + `uvicorn` | REST API server |
| `react` | Frontend UI |

---

## Quick Start (Docker — recommended)

```bash
git clone <this-repo>
cd pdf-editor
docker compose up --build
```

Then open **http://localhost:5173** in your browser.

---

## Quick Start (local dev)

### Backend

```bash
# Prerequisites: Python 3.10+, Tesseract, poppler-utils
# Ubuntu/Debian:  sudo apt install tesseract-ocr poppler-utils
# macOS:          brew install tesseract poppler

cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
# Opens at http://localhost:5173
```

---

## How It Works

### Step 1 — Upload

The backend receives your PDF and for each page:
- Counts extractable text characters
- If `< 20 chars` → treats page as **scanned image** → runs Tesseract OCR
- Otherwise → reads native **font metadata** (name, size, color, bold/italic flags)

### Step 2 — Render

The frontend receives:
- A base64 PNG of each page (rendered at 150 DPI via PyMuPDF)
- A list of text blocks with position, font, size, color

The page image is the background. React renders **invisible div overlays** exactly on top of each text block, styled with the matching CSS font.

### Step 3 — Edit

Click any text block → it appears in the Properties panel on the right.
Change text, font, size, or color → the overlay updates instantly.

### Step 4 — Save

The backend applies all edits:
1. White-rectangle covers the original text area
2. `fitz.insert_text()` writes new text with the chosen font/color
3. The modified PDF is returned as a download

---

## Project Structure

```
pdf-editor/
├── backend/
│   ├── main.py            # FastAPI app — all endpoints
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx        # Entire React app (single file)
│   │   └── main.jsx       # Entry point
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   ├── Dockerfile
│   └── nginx.conf
├── docker-compose.yml
└── README.md
```

---

## Limitations & Known Gaps

- **Font matching for scanned PDFs**: Tesseract doesn't return font names, so we fall back to Helvetica as the base. The size is estimated from bounding box height.
- **Complex layouts**: Multi-column, tables, and rotated text may not align perfectly.
- **Right-to-left text**: Not currently supported.
- **Ligatures and special glyphs**: PyMuPDF's built-in fonts cover standard Latin only.

---

## Extending

### Add more OCR languages

```bash
# Install Tesseract language pack
sudo apt install tesseract-ocr-hin   # Hindi example
```

Then pass `lang="hin"` to `pytesseract.image_to_data()` in `main.py`.

### Swap Tesseract for EasyOCR (better accuracy)

```python
import easyocr
reader = easyocr.Reader(["en"])
results = reader.readtext(img_array, detail=1)
```

### Add page reordering / merge / split

Expose additional PyMuPDF operations as new FastAPI endpoints and add buttons to the sidebar.

---

## License

MIT — fork, extend, ship.
"# PDF-Editor" 
