# PDFEdit - AI-Powered PDF Reconstructor & Editor

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python Badge" />
  <img src="https://img.shields.io/badge/FastAPI-0.100+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI Badge" />
  <img src="https://img.shields.io/badge/React-18.3-20232A?style=for-the-badge&logo=react&logoColor=61DAFB" alt="React Badge" />
  <img src="https://img.shields.io/badge/Vite-5.4-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite Badge" />
  <img src="https://img.shields.io/badge/PyMuPDF-1.23-FF6F61?style=for-the-badge&logo=pdf&logoColor=white" alt="PyMuPDF Badge" />
  <img src="https://img.shields.io/badge/Playwright-1.40-2E8B57?style=for-the-badge&logo=playwright&logoColor=white" alt="Playwright Badge" />
</p>

A robust, offline-first web PDF editor that uses **MinerU** and **Vision-Language Models (VLMs)** to perfectly reconstruct scanned documents, complex tables, and LaTeX math formulas into editable formats.

##  Key Features

* **AI-Powered OCR**: Uses MinerU for state-of-the-art document layout detection.
* **VLM Integration**: Seamlessly offloads complex tables and math formulas to NVIDIA NIM (Llama 3.2 90B Vision) for flawless Markdown/LaTeX extraction.
* **Hybrid Processing**: Runs standard layouts on your local CPU while routing heavy structural analysis to the cloud API.
* **Click-to-Edit**: Interact with parsed paragraphs, headers, and formulas directly in the browser with our floating format properties overlay.
* **Undo / Redo States**: Full editing state history tracking with quick keyboard-like undo/redo capability.
* **Global Uniform Fonts**: Choose one global document font (Helvetica, Times New Roman, Courier) to apply uniformly across the entire PDF, while keeping custom size and formatting edits intact.
* **True PDF Generation**: Rebuilds the final edited document using headless Chromium and MathJax, ensuring LaTeX formulas are rendered perfectly as SVGs in the final PDF.

---

##  Architecture

```text
Browser (React / Vite)
  ├── Upload PDF
  ├── Supply NVIDIA NIM API Key (sessionStorage)
  ├── Display editable blocks (Text, Math, Tables, Images)
  ├── Edit locally with the floating toolbar (Bold, Italic, Color, Font Size)
  └── Trigger Reconstruct/Save
  
FastAPI Backend
  ├── POST /api/upload: Store PDF & initialize session
  ├── POST /api/reconstruct: 
  │     └─ Spawns `mineru` subprocess (CPU-forced to prevent PyTorch OOMs)
  ├── POST /v1/chat/completions (Local Proxy):
  │     ├─ Intercepts MinerU VLM requests
  │     ├─ Limits concurrency (Semaphore=3) to prevent NVIDIA rate-limits
  │     ├─ Auto-retries with exponential backoff on 502/429
  │     └─ Cleans up conversational filler from Llama responses
  └── POST /api/save:
        └─ Redacts original content bounding boxes and draws new text using chosen font family & sizes
```

---

##  Setup & Installation

### 1. Prerequisites
- **Python 3.10+**
- **Node.js 18+**
- **NVIDIA NIM API Key** (or Groq API key) for VLM mode.

### 2. Quick Run (Single-Click)
We have provided a unified runner script in the workspace root:
- Double-click **`start.bat`**
- It will automatically verify and install the required npm dependencies, and spin up both the **FastAPI Backend (port `8007`)** and **React/Vite Frontend (port `5173`)** concurrently in terminal windows.
- Open **`http://localhost:5173`** in your browser.

---

##  Configuration & Hardware Notes

### CPU Fallback & PyTorch OOMs
By default, the backend forces MinerU's layout engine to run entirely on the CPU to prevent Windows Page File (`WinError 1455`) and CUDA `Out of Memory` crashes on GPUs with <= 4GB VRAM.

If you have a powerful GPU (8GB+ VRAM) and want faster layout processing, remove this line in `mineru_ocr.py`:
```python
env["CUDA_VISIBLE_DEVICES"] = "-1"
```

---

##  How it Works (Under the Hood)

1. **Upload:** User uploads a scanned PDF.
2. **Layout Detection:** MinerU scans the page (on local CPU) to detect text blocks, images, tables, and equations.
3. **VLM Routing (Hybrid Mode):** MinerU crops the complex tables and equations and sends them to our local proxy (`http://127.0.0.1:8007/v1`).
4. **Proxy:** The proxy securely forwards the images to NVIDIA's Llama 3.2 90B Vision model, forcing it to output clean Markdown/LaTeX.
5. **Editing:** The user edits the Markdown blocks in the React UI.
6. **Reconstruction:** The backend compiles the Markdown and uses Playwright to render a pristine HTML document (with MathJax for SVGs), printing it out as the final downloadable PDF.
