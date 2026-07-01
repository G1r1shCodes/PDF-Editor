import fitz
import os
import glob
from pathlib import Path

base_dir = Path(__file__).resolve().parent / 'tmp_pdf_editor'
latest_pdf = max(glob.glob(str(base_dir / '*' / 'original.pdf')), key=os.path.getmtime)
print(f'Analyzing: {latest_pdf}')

doc = fitz.open(latest_pdf)
page = doc[0]

# Try standard dict instead of rawdict
d = page.get_text("dict")
blocks = d.get("blocks", [])

print("Sample spans from dict:")
for b in blocks:
    if b.get("type") == 0:
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                print(repr(s))
        break
