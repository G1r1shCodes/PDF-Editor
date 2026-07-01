# PDFEdit - Smart PDF Editor

Local web PDF editor for both digital PDFs and scanned image PDFs.

The app renders each PDF page as an image, overlays editable text blocks in the browser, and writes changed text back into a new PDF.

## Features

| Feature | Digital PDF | Scanned PDF |
|---|---|---|
| Auto-detect page type | Yes | Yes |
| Extract text blocks | Yes | Yes, via OCR |
| Preserve native font size/color where available | Yes | Estimated from OCR boxes |
| Click-to-edit text | Yes | Yes |
| Font, size, and color controls | Yes | Yes |
| Download edited PDF | Yes | Yes |
| Local processing | Yes | Yes |

## Architecture

```text
Browser (React/Vite)
  - Upload PDF
  - Display rendered page PNGs
  - Overlay editable text boxes
  - Send edits to backend

FastAPI backend
  - POST /upload: store PDF, render pages, extract text/OCR boxes
  - POST /save: redact edited regions, insert replacement text, return PDF
  - GET /fonts, GET /health, DELETE /session/{session_id}

PDF/OCR tools
  - PyMuPDF: render, extract native text/font metadata, write edits
  - pytesseract + Tesseract binary: OCR scanned pages
  - Pillow: image bridge for OCR
```

## Project Layout

This repo uses a flat layout:

```text
PDF Editor Tool/
  App.jsx
  main.jsx
  index.html
  vite.config.js
  main.py
  requirements.txt
  Dockerfile
  Dockerfile.frontend
  docker-compose.yml
  nginx.conf
```

## Local Development

### Backend

Prerequisites:

- Python 3.10+
- Tesseract OCR installed

On Windows, the backend auto-detects common Tesseract install paths, including:

```text
C:\Program Files\Tesseract-OCR\tesseract.exe
```

Install Python dependencies:

```powershell
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

Run the backend:

```powershell
./venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

If port 8000 is already in use, either stop the old backend process or use another port and set `VITE_API_URL` for the frontend.

### Frontend

```powershell
npm install
npm run dev
```

Open:

```text
http://localhost:5173
```

The frontend uses `/api` by default. Vite proxies `/api` to `http://localhost:8000`.

## Docker

```powershell
docker compose up --build
```

Then open:

```text
http://localhost:5173
```

Docker runs:

- backend on port 8000
- frontend/nginx on port 5173
- nginx proxies `/api/` to the backend service

## How Editing Works

1. Upload a PDF.
2. Backend stores it in `tmp_pdf_editor/<session_id>/original.pdf`.
3. Each page is rendered to PNG at 150 DPI.
4. Digital pages use PyMuPDF text spans.
5. Scanned pages fall back to Tesseract OCR word boxes.
6. User edits blocks in React.
7. Save request redacts edited boxes and inserts replacement text.
8. Browser downloads `edited.pdf`.

## Limitations

- OCR font family is guessed as Helvetica.
- OCR font size is estimated from word-box height.
- Complex layouts, tables, rotated text, and non-Latin scripts may need more work.
- Edits are stamped into the PDF; this is not a full structured PDF content editor.

## Useful Checks

Backend health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Tesseract check:

```powershell
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
```
