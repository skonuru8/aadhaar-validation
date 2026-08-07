"""
Fallback OCR extraction for the rest of the card - name, DOB, gender,
pincode, address - not just the UID (see uid_ocr_fallback.py for that).

Same ceiling as uid_ocr_fallback.py, worth repeating: this is NOT
authentication or even verification. There's no signature, no cross-
referenced QR data. A skilled forgery defeats this trivially. Use it only
when the QR path has already failed entirely (quality_gate rejection or
qr_decode failure) - it exists to get *something* off a card whose QR is
genuinely unreadable, not to replace a working QR.

Validation: only the UID has a real cryptographic-adjacent check (Verhoeff
checksum). Nothing else here has an equivalent - there's no checksum for a
name. What every field DOES get is the same cross-pass-agreement guard
proven for UID: extract from 3 independently-preprocessed variants of the
same image (raw grayscale, 2x upscale, Otsu threshold) and only mark a
result `confirmed: True` if the same value shows up in 2+ of them. This is
a real signal - a one-off misread rarely reproduces itself across
independently different pixel transforms, a genuine printed value usually
does - but it is NOT proof of correctness, just consistency. A card could
consistently misprint or a systematic OCR error could reproduce across all
3 variants; agreement raises confidence, it doesn't create certainty.
DOB additionally gets a real, independent check: `calendar_valid` confirms
the extracted string is an actual calendar date (rejects 31/02/1990-shaped
noise), and `plausible_year` checks it falls in a sane range - both are
structural checks, same category as the UID's Verhoeff checksum, not just
agreement-based.

Orientation: unlike a QR (rotation-tolerant by finder-pattern design - see
qr_decode.py), OCR genuinely needs roughly-upright text to read anything at
all. Corrected once via Tesseract's own OSD (orientation/script detection)
before any of the 3 preprocessing variants run - the purpose-built tool for
this, not a brute-force guess. Only "osd" language data is needed for that,
already bundled with the base tesseract install (see README setup) - no
extra language packs.

Field extraction is regex/keyword-based against the OCR'd text, not layout-
aware - this works because Aadhaar cards always carry an English label
alongside the regional-script one (DOB:, Address:, MALE/FEMALE), which this
relies on rather than trying to parse the regional script itself (only
"eng" is installed - see README). Name has no label at all on a real card,
so it's the least reliable field here even with agreement-checking applied
- a positional heuristic (the line before a recognized DOB line), not a
keyword match, and should be treated as the weakest signal in this
module's output regardless of its `confirmed` flag.
"""

import re
import collections
import datetime

import cv2
import numpy as np
import pytesseract

from image_loader import load_image
from uid_ocr_fallback import uid_candidates_from_texts

_DIGITS_ONLY = re.compile(r"\D")

# Separators seen across real cards: '/', '-', '.', or plain spaces
# ("15 07 1968"-style OCR spacing when the separator itself misreads).
_DOB_PATTERN = re.compile(r"(\d{1,2})\s*[/\-. ]\s*(\d{1,2})\s*[/\-. ]\s*(\d{4})")
_DOB_LABEL = re.compile(r"\b(DOB|D0B|D\.O\.B|Date of Birth|Birth)\b", re.IGNORECASE)
_GENDER_PATTERN = re.compile(r"\b(FEMALE|MALE|TRANSGENDER)\b", re.IGNORECASE)
_PINCODE_LABEL = re.compile(r"\b(PIN\s*CODE|PINCODE|PIN)\b[:\s\-]*", re.IGNORECASE)
_ADDRESS_LABEL = re.compile(r"\bAddress\s*:?\s*$", re.IGNORECASE)
_SKIP_LINE = re.compile(
    r"government of india|unique identification|uidai|aadhaar|www\.|help@|"
    r"^\s*[|;.,\-_]*\s*$",
    re.IGNORECASE,
)

MIN_PLAUSIBLE_YEAR = 1900


def _correct_orientation(img: np.ndarray) -> np.ndarray:
    """Rotate `img` upright using Tesseract's OSD. Falls back to the
    original image untouched if OSD can't determine orientation (blank/
    too little text/too degraded) rather than guessing."""
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        return img

    rotate_by = osd.get("rotate", 0)
    if rotate_by == 0:
        return img
    rotation_map = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    code = rotation_map.get(rotate_by)
    return cv2.rotate(img, code) if code is not None else img


def _preprocessing_variants(img: np.ndarray) -> list:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    variants = [gray, cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)]
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)
    return variants


def _ocr_text(variant: np.ndarray) -> str:
    return pytesseract.image_to_string(variant, config="--psm 11")


def _extract_dob(text: str) -> str | None:
    lines = text.splitlines()
    # Prefer a date on a line that also carries a DOB-ish label - far more
    # reliable than the first date-shaped string anywhere on the card
    # (issue dates, VID-adjacent numbers, etc. can look date-shaped too).
    for line in lines:
        if _DOB_LABEL.search(line):
            m = _DOB_PATTERN.search(line)
            if m:
                return "-".join(m.groups())
    for line in lines:
        m = _DOB_PATTERN.search(line)
        if m:
            return "-".join(m.groups())
    return None


def _extract_gender(text: str) -> str | None:
    m = _GENDER_PATTERN.search(text)
    return m.group(1).upper() if m else None


def _extract_pincode(text: str) -> str | None:
    for line in text.splitlines():
        m = _PINCODE_LABEL.search(line)
        if m:
            digits = _DIGITS_ONLY.sub("", line[m.end():])
            if len(digits) >= 6:
                return digits[:6]
    # fallback: a standalone 6-digit run not part of a longer digit run
    for line in text.splitlines():
        for token in re.findall(r"\b\d{6}\b", line):
            return token
    return None


def _extract_address(text: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _ADDRESS_LABEL.search(line):
            block = []
            for follow in lines[i + 1:i + 8]:
                stripped = follow.strip()
                if not stripped or _SKIP_LINE.search(stripped):
                    if block:
                        break
                    continue
                block.append(stripped)
            if block:
                return ", ".join(block)
    return None


def _extract_name(text: str) -> str | None:
    """
    Weakest field in this module - Aadhaar cards don't label the name
    field at all. Heuristic: the line immediately before a DOB-labeled
    line, if it's mostly alphabetic and a plausible name length.
    """
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if _DOB_LABEL.search(line) or _DOB_PATTERN.search(line):
            for candidate in reversed(lines[:i]):
                if not candidate or _SKIP_LINE.search(candidate):
                    continue
                letters = re.sub(r"[^A-Za-z ]", "", candidate)
                if len(letters) < 3:
                    continue
                if len(letters) / max(len(candidate), 1) < 0.7:
                    continue  # too many non-letters to plausibly be a name
                if not (2 <= len(candidate) <= 40):
                    continue
                return candidate
            break
    return None


def _dob_calendar_check(dob: str | None, current_year: int) -> dict:
    if dob is None:
        return {"calendar_valid": None, "plausible_year": None}
    day, month, year = (int(p) for p in dob.split("-"))
    try:
        datetime.date(year, month, day)
        calendar_valid = True
    except ValueError:
        calendar_valid = False
    plausible_year = MIN_PLAUSIBLE_YEAR <= year <= current_year
    return {"calendar_valid": calendar_valid, "plausible_year": plausible_year}


def _aggregate_field(variant_texts: list, extract_fn) -> dict:
    """
    Runs `extract_fn` against each independently-preprocessed OCR text and
    reports the most common non-null result plus how many of the passes
    agreed on it. `confirmed=True` only when 2+ of the (up to 3) passes
    landed on the exact same value - the same guard already proven for
    UID, generalized to every field this module extracts.
    """
    candidates = [extract_fn(t) for t in variant_texts]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return {"value": None, "agreement_count": 0, "confirmed": False}
    value, count = collections.Counter(candidates).most_common(1)[0]
    return {"value": value, "agreement_count": count, "confirmed": count >= 2}


def card_fields_from_image(upright_img: np.ndarray, current_year: int = 2026) -> dict:
    """
    Core extraction logic, taking an already-loaded, already-orientation-
    corrected image array. card_ocr_fallback() is a thin path-based wrapper
    around this. Exists so a caller that also needs the same upright image
    for something else (validate_card.py's face match) can correct
    orientation exactly once and reuse it, instead of each function loading
    and re-correcting independently - same "share the work already done"
    principle as uid_candidates_from_texts().

    Lesson from testing: face detection genuinely fails on a rotated image
    (confirmed: a 90-degree-rotated photo of a real face scored
    `face_found: False` against the same upright photo, not just a lower
    similarity score) - unlike a QR's finder patterns, neither OCR nor face
    detection tolerates rotation on their own. Both need the same upright
    image; the fix is sharing one correction, not adding a second one to
    face_match.py.
    """
    variant_texts = [_ocr_text(v) for v in _preprocessing_variants(upright_img)]
    uid_result = uid_candidates_from_texts(variant_texts)

    dob = _aggregate_field(variant_texts, _extract_dob)
    dob.update(_dob_calendar_check(dob["value"], current_year))

    return {
        "uid_confirmed_candidates": uid_result["confirmed_candidates"],
        "uid_checksum_valid_candidates": uid_result["checksum_valid_candidates"],
        "dob": dob,
        "gender": _aggregate_field(variant_texts, _extract_gender),
        "pincode": _aggregate_field(variant_texts, _extract_pincode),
        "address": _aggregate_field(variant_texts, _extract_address),
        "name": _aggregate_field(variant_texts, _extract_name),
    }


def card_ocr_fallback(image_path: str, current_year: int = 2026) -> dict:
    """
    Best-effort extraction of whatever's OCR-readable off the card face,
    for use only after the QR path has failed entirely. Corrects
    orientation once via Tesseract OSD, then extracts each field
    independently across 3 preprocessing variants - a miss on one field or
    one variant doesn't block the others. See module docstring for what
    `confirmed` does and doesn't mean.

    `current_year` bounds the DOB plausible-year check - pass the real
    current year in production; nothing in this codebase reads the system
    clock (see payload_parser.py's parse_payload for the same convention).
    """
    img = load_image(image_path)
    upright = _correct_orientation(img)
    return card_fields_from_image(upright, current_year)
