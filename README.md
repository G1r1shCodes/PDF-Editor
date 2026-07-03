# PDFEdit - AI-Powered PDF Reconstructor & Editor

A robust, offline-first web PDF editor that uses **MinerU** and **Vision-Language Models (VLMs)** to perfectly reconstruct scanned documents, complex tables, and LaTeX math formulas into editable formats.

## 🚀 Key Features

* **AI-Powered OCR**: Uses MinerU for state-of-the-art document layout detection.
* **VLM Integration**: Seamlessly offloads complex tables and math formulas to NVIDIA NIM (Llama 3.2 90B Vision) for flawless Markdown/LaTeX extraction.
* **Hybrid Processing**: Runs standard layouts on your local CPU while routing heavy structural analysis to the cloud API.
* **Click-to-Edit**: Interact with parsed paragraphs, headers, and formulas directly in the browser.
* **True PDF Generation**: Rebuilds the final edited document using headless Chromium and MathJax, ensuring LaTeX formulas are rendered perfectly as SVGs in the final PDF.

---

## 🏗 Architecture

```text
Browser (React / Vite)
  ├── Upload PDF
  ├── Supply NVIDIA NIM API Key (sessionStorage)
  ├── Display editable blocks (Text, Math, Tables, Images)
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
  └── POST /api/reconstruct/save:
        └─ Merges edits into Markdown and uses Playwright to generate the final PDF
```

---

## 🛠️ Setup & Installation

### 1. Prerequisites
- **Python 3.10+**
- **Node.js 18+**
- **NVIDIA NIM API Key** (or Groq API key) for VLM mode.

### 2. Backend Setup
Create a virtual environment and install the required dependencies (including MinerU):

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install -U "mineru[core]"
playwright install chromium
```

Start the FastAPI server:
```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```
*(The backend handles MinerU OCR, the VLM Proxy, and Playwright PDF generation).*

### 3. Frontend Setup
Install the dependencies and start the Vite development server:

```bash
npm install
npm run dev
```
Open **`http://localhost:5173`** in your browser.

---

## ⚙️ Configuration & Hardware Notes

### CPU Fallback & PyTorch OOMs
By default, the backend forces MinerU's layout engine to run entirely on the CPU to prevent Windows Page File (`WinError 1455`) and CUDA `Out of Memory` crashes on GPUs with <= 4GB VRAM.

If you have a powerful GPU (8GB+ VRAM) and want faster layout processing, remove this line in `mineru_ocr.py`:
```python
env["CUDA_VISIBLE_DEVICES"] = "-1"
```

### NVIDIA API Rate Limits
To prevent connection drops from NVIDIA's free API tier, the backend proxy strictly limits VLM concurrency to **3 simultaneous requests**. If you upgrade to a paid enterprise tier, you can drastically speed up table/math extraction by increasing `asyncio.Semaphore(3)` in `main.py` to `20` or higher.

---

## 📝 How it Works (Under the Hood)

1. **Upload:** User uploads a scanned PDF.
2. **Layout Detection:** MinerU scans the page (on local CPU) to detect text blocks, images, tables, and equations.
3. **VLM Routing (Hybrid Mode):** MinerU crops the complex tables and equations and sends them to our local proxy (`http://127.0.0.1:8000/v1`).
4. **Proxy:** The proxy securely forwards the images to NVIDIA's Llama 3.2 90B Vision model, forcing it to output clean Markdown/LaTeX.
5. **Editing:** The user edits the Markdown blocks in the React UI.
6. **Reconstruction:** The backend compiles the Markdown and uses Playwright to render a pristine HTML document (with MathJax for SVGs), printing it out as the final downloadable PDF.
