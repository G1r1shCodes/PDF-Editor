"""Base OCR extraction types and utilities.

Tesseract has been removed from this module. This file now only provides
the basic structures and image sampling utilities used by other OCR engines.
"""

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


class OCRBlock(dict):
    """A single OCR-detected word with hierarchy metadata.

    Keys:
        text, confidence, bbox (dict x/y/width/height in PDF points),
        block_num, par_num, line_num, word_num, font_size_estimate,
        color (hex string sampled from the glyph), page
    """
    pass


def _sample_glyph_color(color_arr, x: int, y: int, w: int, h: int) -> str:
    """Sample the dominant ink colour inside a word box (darkest ~30% of px)."""
    if not _HAS_NUMPY or color_arr is None:
        return "#000000"
    H, W = color_arr.shape[0], color_arr.shape[1]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return "#000000"
    crop = color_arr[y0:y1, x0:x1, :3]
    if crop.size == 0:
        return "#000000"
    lum = crop.sum(axis=2)
    thresh = np.percentile(lum, 30)
    mask = lum <= thresh
    if not mask.any():
        px = crop.reshape(-1, 3).mean(axis=0)
    else:
        px = crop[mask].mean(axis=0)
    return "#{:02x}{:02x}{:02x}".format(int(px[0]), int(px[1]), int(px[2]))
