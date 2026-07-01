"""Replicate InstructPix2Pix image editing backend.

Sends a cropped document region to a Replicate Inference API endpoint
to perform text replacement. Falls back to a local Pillow renderer
when the API is unavailable.
"""

import base64
import io
import os
import time
from typing import Optional

from PIL import Image
import replicate
from urllib.request import urlopen

from gemini_service import _local_render_fallback

class ReplicateEditError(RuntimeError):
    """Raised when Replicate image editing cannot produce a usable crop."""

MAX_RETRIES = 4
BACKOFF_BASE = 1.5

def _build_prompt(original_text: str, replacement_text: str, context_text: str = "") -> str:
    """Build a prompt for the Pix2Pix model."""
    return (
        f"Change the text '{original_text}' to '{replacement_text}'. "
        "Keep the exact same font, size, and background color."
    )

def send_to_replicate(
    crop: Image.Image,
    original_text: str,
    replacement_text: str,
    api_key: str,
    context_text: str = "",
) -> Image.Image:
    """Edit text on a cropped document region using Replicate InstructPix2Pix."""
    if not api_key:
        raise ReplicateEditError("Replicate API token is required.")

    # Encode crop as PNG -> BytesIO
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    buf.seek(0)
    
    prompt = _build_prompt(original_text, replacement_text, context_text)

    # We use the Replicate Client so it doesn't read os.environ inside the library globally
    client = replicate.Client(api_token=api_key)

    last_msg = "unknown error"

    for attempt in range(MAX_RETRIES + 1):
        try:
            # Send to replicate
            output = client.run(
                "timothybrooks/instruct-pix2pix:30c1d0b916a6f8efce20493f5d61ee27491ab2a60437c13c588468b9810ec23f",
                input={
                    "image": buf,
                    "prompt": prompt
                }
            )
            
            # The output is typically a list of URLs or FileOutput objects
            if not output or len(output) == 0:
                raise ReplicateEditError("No output returned from Replicate")
                
            output_file = output[0]
            
            # If it's a FileOutput object with read(), use it.
            if hasattr(output_file, 'read'):
                output_bytes = output_file.read()
            elif hasattr(output_file, 'url'):
                # Fallback if we just get a URL object
                with urlopen(output_file.url) as resp:
                    output_bytes = resp.read()
            elif isinstance(output_file, str) and output_file.startswith("http"):
                with urlopen(output_file) as resp:
                    output_bytes = resp.read()
            else:
                raise ReplicateEditError(f"Unexpected output type: {type(output_file)}")

            edited_crop = Image.open(io.BytesIO(output_bytes)).convert("RGB")
            
            # Ensure crop size is maintained perfectly
            if edited_crop.size != crop.size:
                edited_crop = edited_crop.resize(crop.size, Image.LANCZOS)
            
            return edited_crop
                
        except Exception as exc:
            last_msg = str(exc)
            
            if attempt < MAX_RETRIES:
                # If rate limited, sleep longer
                delay = BACKOFF_BASE * (2 ** attempt)
                if "429" in last_msg or "throttled" in last_msg.lower():
                    delay = max(delay, 4.0) # wait at least 4 seconds on rate limits
                    
                time.sleep(delay)
                # Need to seek to 0 again for the next try
                buf.seek(0)
                continue
            
            print(f"[WARN] Replicate API failed: {last_msg}")
            break

    print(f"[WARN] Replicate API failed: {last_msg}. Falling back to local Pillow render.")
    return _local_render_fallback(crop, replacement_text)

