"""
Fallback UID extraction for when the QR can't be decoded at all (damaged,
too small, too blurred - see README case studies #1/#2 and the aa_data
batch test, where 96% of a 600-image sample never produced a decodable QR).

This is a MUCH weaker check than the QR path. It only proves the printed
12-digit UID is *structurally* valid (passes the Verhoeff checksum) - no
signature, no cross-checked demographic fields, no photo, nothing
cryptographic. A skilled forgery can print a checksum-valid fake number
trivially; this catches typos and naively-fabricated numbers, not a
determined forger. Use it only as a last resort after quality_gate and
qr_decode have both failed, and treat a "valid" result as "plausible",
never as "verified".

Every Aadhaar card prints the UID as a single line, space-grouped in
4-4-4 (e.g. "3587 0818 3542"). Extraction works line-by-line rather than
with a global regex over the whole OCR blob, specifically to avoid
accidentally concatenating digits from two unrelated lines (e.g. a PIN
code line bleeding into a mobile number line) into a false 12-digit run.

Lesson from testing (real false positive, not hypothetical): against a
second photo of a real card, this module's first version returned
"436813101998" as its only checksum-valid candidate - checksum-valid, but
NOT the card's actual UID. The tail of that string ("13101998") is the
printed DOB (13/10/1998) with separators stripped - Tesseract had misread
fragments of nearby text and stitched them into something that happened to
pass Verhoeff by coincidence. The lesson: passing the checksum is necessary
but not sufficient - it filters typos, not "some other printed number that
happens to also be a valid UID shape". A single OCR pass has no way to tell
"confidently read from the right line" apart from "coincidentally valid
noise".

Fix: run OCR against several independently-preprocessed variants of the
same image (raw grayscale, 2x upscale, Otsu threshold) and only trust a
checksum-valid candidate if the *same* 12 digits show up in at least two of
them. A real printed UID reads consistently across preprocessing; a
one-off misread stitched from unrelated fragments generally doesn't
reproduce itself across independently different pixel transforms.
"""

import re
import cv2
import pytesseract

from verhoeff import is_valid as verhoeff_valid
from image_loader import load_image

_DIGITS_ONLY = re.compile(r"\D")


def _candidates_from_text(text: str) -> set[str]:
    found = set()
    for line in text.splitlines():
        digits = _DIGITS_ONLY.sub("", line)
        if len(digits) == 12:
            found.add(digits)
    return found


def _ocr_variants(gray):
    variants = [gray]
    variants.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)
    return variants


def extract_uid_candidates(image_path: str) -> list[str]:
    """
    Kept for backward compatibility / debugging - candidates found on the
    single raw-grayscale pass only, no cross-pass agreement. Prefer
    ocr_uid_fallback() for anything that needs the false-positive guard.
    """
    img = load_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    text = pytesseract.image_to_string(gray, config="--psm 11")
    return sorted(_candidates_from_text(text))


def uid_candidates_from_texts(variant_texts: list[str]) -> dict:
    """
    Core aggregation logic, taking already-OCR'd text from each
    preprocessing variant rather than an image - lets a caller that's
    already run OCR on the same 3 variants for other fields (see
    card_ocr_fallback.py) reuse that work instead of paying for a second,
    redundant round of Tesseract calls on equivalent variants.
    """
    per_variant_candidates = [_candidates_from_text(t) for t in variant_texts]
    all_candidates = sorted(set().union(*per_variant_candidates))
    agreement_count = {
        c: sum(1 for variant_set in per_variant_candidates if c in variant_set)
        for c in all_candidates
    }

    checksum_valid = [c for c in all_candidates if verhoeff_valid(c)]
    # The actual guard: checksum-valid AND seen in 2+ independent passes.
    # Single-pass-only hits are reported for visibility but not trusted.
    confirmed = [c for c in checksum_valid if agreement_count[c] >= 2]

    return {
        "candidates_found": all_candidates,
        "checksum_valid_candidates": checksum_valid,
        "confirmed_candidates": confirmed,
        "agreement_count": agreement_count,
    }


def ocr_uid_from_image(img) -> dict:
    """
    Standalone/path-based use: runs OCR on the 3 preprocessing variants
    itself, then delegates to uid_candidates_from_texts(). If you're
    already OCR'ing the same variants for other fields (card_ocr_fallback.
    py), call uid_candidates_from_texts() directly with that text instead
    of this - it avoids a redundant second round of Tesseract calls.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    variant_texts = [pytesseract.image_to_string(v, config="--psm 11") for v in _ocr_variants(gray)]
    return uid_candidates_from_texts(variant_texts)


def ocr_uid_fallback(image_path: str) -> dict:
    return ocr_uid_from_image(load_image(image_path))
