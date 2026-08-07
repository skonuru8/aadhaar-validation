"""
Robust image loading - the one place every other module should get pixels
from, instead of calling cv2.imread directly.

Lesson from testing: cv2.imread returns None for HEIC (iPhone's default
photo format, since iOS 11) with this build - every real HEIC upload this
session had to be manually converted with macOS's `sips` before the
pipeline could see it at all. That's not something an unattended pipeline
can lean on - a human doing a manual conversion step outside the app is
the same class of problem as the manual QR cropping this project moved
away from earlier. So: try cv2 first (it already covers jpg/png/avif/etc
via its bundled codecs), and fall back to PIL - extended by pillow-heif to
also open .heic/.heif - for anything cv2's build doesn't recognize.

Second gap, same shape, caught the same way (confirmed with a real test
PDF, not assumed): UIDAI's own e-Aadhaar download IS a PDF, and neither
cv2 nor PIL can open one - `load_image()` raised FileNotFoundError on a
genuine PDF before this. Added a third tier via PyMuPDF (chosen over
pdf2image specifically because it has no system dependency - pdf2image
needs poppler installed separately, pymupdf is a self-contained wheel).
Renders at 300 DPI - low enough not to be slow, high enough that a QR
embedded in the PDF doesn't lose resolution before it ever reaches
qr_decode.py (see that module's own lessons on what upscaling can't fix
once real detail is lost).

`load_image()` itself only ever returns ONE image - page 1 for a PDF - by
design, since every existing caller (quality_gate.py, qr_decode.py,
uid_ocr_fallback.py) has a single-image path-based signature and changing
that would be a much bigger-blast-radius refactor than the gap it fixes.
A genuinely common real case needs more than that, though: a 2-page scan
(front page + back page, e.g. photo/name/DOB on one page, address/QR on
the other) - confirmed as a real gap the same way as the others, by
building an actual 2-page test PDF and watching the QR (on page 2) come
back "missing" while only page 1 was ever looked at. `load_all_pages()`
is the fix for a caller that can search across pages - see
validate_card.py, which tries QR decode against every page and merges
OCR/face-match results across whichever pages have them, rather than
assuming page 1 has everything.
"""

import io

import numpy as np
import cv2
from PIL import Image
import pillow_heif
import pymupdf

pillow_heif.register_heif_opener()

PDF_RENDER_DPI = 300


def _is_pdf(path: str) -> bool:
    if path.lower().endswith(".pdf"):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def load_pdf_all_pages(pdf_path: str) -> list[np.ndarray]:
    """Renders every page of a PDF to a BGR image array, at PDF_RENDER_DPI."""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=PDF_RENDER_DPI)
        pil_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        pages.append(cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR))
    return pages


def load_all_pages(image_path: str) -> list[np.ndarray]:
    """Every page of a PDF, or a 1-element list for a normal image - the
    uniform entry point for a caller that needs to search across pages
    (see validate_card.py) instead of assuming page 1 has everything."""
    if _is_pdf(image_path):
        pages = load_pdf_all_pages(image_path)
        if not pages:
            raise FileNotFoundError(image_path)
        return pages
    return [load_image(image_path)]


def load_image(image_path: str) -> np.ndarray:
    if _is_pdf(image_path):
        pages = load_pdf_all_pages(image_path)
        if not pages:
            raise FileNotFoundError(image_path)
        return pages[0]

    img = cv2.imread(image_path)
    if img is not None:
        return img

    try:
        pil_img = Image.open(image_path)
    except Exception as e:
        raise FileNotFoundError(image_path) from e

    return cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
