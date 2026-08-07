"""
Full card validation: when the QR decodes, cross-check its data against
what's printed on the card - both demographic fields and the face photo -
rather than trusting the QR blindly or the card text blindly.

Two independent kinds of evidence feed into "validated": internal
consistency (does the QR's data match the printed card - name/DOB/gender/
face) and, when a matching certificate is available, real cryptographic
signature verification (see signature_verify.py - now proven against a
real card, not theoretical). The internal-consistency checks catch real
tampering scenarios (a genuine QR glued onto a card with a swapped photo
or altered name) even without a working cert; the signature check, when it
succeeds, is unambiguous proof UIDAI signed that exact data. See
_check_signature() for why a *failed* signature check is handled very
differently from a failed demographic check - it's usually a cert-vintage
mismatch, not evidence of forgery, and is deliberately not treated as
equivalent to a real mismatch.

If the QR is missing or doesn't decode at all, this returns
qr_status="missing_or_unclear" with an explicit `message` - never silently
falls through to showing OCR'd details as if they were validated.

Multi-page input (a 2-page PDF scan - front page, back page): confirmed as
a real gap by building an actual 2-page test PDF (front: face+name+DOB, no
QR; back: QR, no face) and watching the QR come back "missing" when only
page 1 was ever looked at. Fixed by trying QR decode against every page
(first page that decodes wins) and merging OCR/face-match results across
whichever pages have them, instead of assuming page 1 has everything - see
_merge_field()/_best_face_match() below.
"""

import base64
import difflib
import io

import cv2
import numpy as np
from PIL import Image

import os

from qr_decode import decode_qr_from_image
from payload_parser import parse_payload
from verhoeff import is_valid as verhoeff_valid
from card_ocr_fallback import card_fields_from_image, _correct_orientation
from face_match import compare_faces
from image_loader import load_all_pages, _is_pdf
from signature_verify import verify_signature
from pdf_signature_verify import verify_pdf_signature

GENDER_MAP = {"M": "MALE", "F": "FEMALE", "T": "TRANSGENDER"}
NAME_SIMILARITY_THRESHOLD = 0.75

# Proven against real data - see signature_verify.py's module docstring for
# how this was found (empirically, by testing every cert reachable from
# UIDAI's page) and what its True/False actually mean. Bundled as the
# default because it's cheap to try and has real evidence behind it, unlike
# every other cert tried this project - but see DEFAULT_UIDAI_CERT_PATH's
# usage below for why a False from it is handled differently from a True.
DEFAULT_UIDAI_CERT_PATH = os.path.join(
    os.path.dirname(__file__), "certs", "uidai_offline_publickey_26022021.cer"
)

VALIDATED_MEANING = (
    "'Validated' here means the QR's data is internally consistent with "
    "the printed card (matching name/DOB/gender, matching face photo) and "
    "the UID passes its checksum. If the signature check also succeeded, "
    "that's real cryptographic proof UIDAI signed this exact data - the "
    "strongest claim this tool can make. If it didn't succeed, that's "
    "usually because the bundled certificate is the wrong vintage for this "
    "specific card (UIDAI rotates signing keys), not proof the card is "
    "fake - see README 'Signature verification' before reading a failed "
    "signature check as anything more than inconclusive."
)


def _confirmed_value(field: dict) -> str | None:
    """
    Bug this fixes: name/gender/dob comparisons used to read an OCR
    field's raw value regardless of its own confidence tier - so a name
    guess that only 1 of 3 preprocessing passes agreed on (explicitly
    `confirmed: False` by card_ocr_fallback.py's own cross-pass-agreement
    check) could still be compared against the QR and reported as a clean
    "didn't match: name" mismatch. Found on a real card: OCR misread a
    blurry printed name into unrelated garbage, agreement_count 1/3,
    confirmed False - and the verdict still said the name didn't match,
    as if that garbage were a reliable reading. An unconfirmed field is
    exactly the "we don't actually know" case _compute_validation_status
    already has a state for (inconclusive) - so it should feed in as
    "nothing to compare" (None), not as a confident wrong answer.
    """
    return field["value"] if field.get("confirmed") else None


def _name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def _dob_check(parsed: dict, ocr_dob_value: str | None) -> dict:
    fmt = parsed["format"]
    if fmt == "secure_qr":
        qr_value = parsed["fields"].get("dob")
        if not qr_value or not ocr_dob_value:
            return {"qr": qr_value, "card": ocr_dob_value, "match": None}
        return {"qr": qr_value, "card": ocr_dob_value, "match": qr_value == ocr_dob_value}
    if fmt == "old_xml":
        # old format only carries year of birth, not a full date
        qr_yob = parsed["fields"].get("yob")
        if not qr_yob or not ocr_dob_value:
            return {"qr": qr_yob, "card": ocr_dob_value, "match": None}
        ocr_year = ocr_dob_value.split("-")[-1]
        return {"qr": qr_yob, "card": ocr_dob_value, "match": qr_yob == ocr_year}
    return {"qr": None, "card": ocr_dob_value, "match": None}


def _compute_validation_status(checks: list) -> tuple[str, bool | None]:
    """
    checks is a list of True/False/None values (one per comparison - name,
    gender, DOB, face, checksum). Returns (validation_status, validated).

    Bug this fixes: a naive `all(known_checks)` collapses "nothing was
    comparable" (e.g. a card image with no face or demographic text on it -
    every check is None) into the same False as "something was compared
    and didn't match". Those are different claims and need a 3rd state -
    caught by testing a real card's QR-only back panel, not hypothetical.
    """
    known = [c for c in checks if c is not None]
    if not known:
        return "inconclusive", None
    if all(known):
        return "validated", True
    return "not_validated", False


def _extract_qr_photo(parsed: dict):
    photo_b64 = parsed.get("photo_jpeg_base64")
    if not photo_b64:
        return None
    photo_bytes = base64.b64decode(photo_b64)
    pil_img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def _decode_qr_across_pages(pages: list) -> dict:
    """First page that decodes wins - a front/back scan can have the QR on
    either page, and we don't know which ahead of time."""
    for page in pages:
        result = decode_qr_from_image(page)
        if result["decoded"]:
            return result
    return {"decoded": False, "data": None, "engines_agreed": False}


def _merge_field(field_results: list) -> dict:
    """Best result for one OCR field (dob/gender/pincode/address/name)
    across multiple pages - a confirmed value beats an unconfirmed one, an
    unconfirmed value beats nothing. Real scenario this handles: DOB/name
    on the front page, address/pincode on the back - each field's real
    answer usually only exists on ONE of the pages, not both."""
    confirmed = [f for f in field_results if f.get("confirmed")]
    if confirmed:
        return max(confirmed, key=lambda f: f["agreement_count"])
    with_value = [f for f in field_results if f.get("value")]
    if with_value:
        return max(with_value, key=lambda f: f.get("agreement_count", 0))
    return field_results[0]


def _merge_ocr_across_pages(per_page_ocr: list) -> dict:
    merged = {key: _merge_field([p[key] for p in per_page_ocr])
              for key in ("dob", "gender", "pincode", "address", "name")}
    merged["uid_confirmed_candidates"] = sorted(
        set().union(*(set(p["uid_confirmed_candidates"]) for p in per_page_ocr))
    )
    merged["uid_checksum_valid_candidates"] = sorted(
        set().union(*(set(p["uid_checksum_valid_candidates"]) for p in per_page_ocr))
    )
    return merged


def _best_face_match(qr_photo, upright_pages: list) -> dict | None:
    """Try the QR's photo against every page, use whichever one actually
    has a face - a scan's QR page usually doesn't (see module docstring's
    2-page test case), so trying only the QR's own page would wrongly read
    as face_found=False instead of just checking the wrong page."""
    if not upright_pages:
        return None
    results = [compare_faces(qr_photo, page) for page in upright_pages]
    found = [r for r in results if r["face_found_b"]]
    if not found:
        return results[0]
    return max(found, key=lambda r: r["cosine_similarity"] if r["cosine_similarity"] is not None else -1)


def _check_signature(parsed: dict, uidai_cert_path: str | None) -> dict:
    """
    Returns {"attempted": bool, "cert_used": str|None, "valid": bool|None}.

    Deliberately NOT symmetric between True and False, because the two
    outcomes carry very different weight (see signature_verify.py): a True
    is essentially unambiguous - RSA signatures don't validate by chance -
    but a False is far more likely to mean "wrong cert vintage" than "fake
    card", since UIDAI rotates signing keys and we only have one real
    working cert, proven against exactly one real card so far. Callers
    should feed `valid is True` into an aggregate "validated" check as
    strong evidence, but NOT feed `valid is False` in as if it were a
    demographic mismatch - that would systematically bias every card
    signed by a different-vintage key toward "not validated", which is
    worse than not checking at all.
    """
    if parsed["format"] != "secure_qr" or not uidai_cert_path:
        return {"attempted": False, "cert_used": None, "valid": None}
    valid = verify_signature(parsed["signed_data"], parsed["signature"], uidai_cert_path)
    return {"attempted": True, "cert_used": os.path.basename(uidai_cert_path), "valid": valid}


def validate_card(image_path: str, uidai_cert_path: str | None = DEFAULT_UIDAI_CERT_PATH) -> dict:
    # PDF signature check runs against the raw uploaded file, independent of
    # everything below - load_all_pages()/card_fields_from_image() only ever
    # read and rasterize this file, never overwrite it, so the original
    # signed bytes are still intact on disk at image_path regardless of
    # where in this function we check them. See pdf_signature_verify.py's
    # module docstring for why this is a separate mechanism from the QR
    # signature check, and its current proof status (mechanism proven,
    # not yet proven against a real UIDAI-signed sample).
    pdf_signature_check = verify_pdf_signature(image_path) if _is_pdf(image_path) else None

    pages = load_all_pages(image_path)
    decode = _decode_qr_across_pages(pages)

    if not decode["decoded"]:
        merged_ocr = _merge_ocr_across_pages([
            card_fields_from_image(_correct_orientation(p)) for p in pages
        ])
        return {
            "qr_status": "missing_or_unclear",
            "validated": False,
            "message": (
                "No usable QR code found on this card - either none is "
                "present, or it's too damaged/low-quality to read. Showing "
                "whatever could be read directly off the card instead. "
                "None of this is validated - there's nothing to cross-check "
                "it against."
            ),
            "extracted": merged_ocr,
            "pdf_signature_check": pdf_signature_check,
        }

    parsed = parse_payload(decode["data"], extract_photo=True)
    if parsed["format"] not in ("old_xml", "secure_qr"):
        merged_ocr = _merge_ocr_across_pages([
            card_fields_from_image(_correct_orientation(p)) for p in pages
        ])
        return {
            "qr_status": "not_aadhaar_qr",
            "validated": False,
            "message": (
                "A QR code was found and decoded, but it isn't Aadhaar "
                "data (could be a different QR on the same page/photo, "
                "e.g. a printer's own watermark). Showing whatever could "
                "be read directly off the card instead - not validated."
            ),
            "extracted": merged_ocr,
            "pdf_signature_check": pdf_signature_check,
        }

    uid = parsed["fields"].get("uid")
    checksum_valid = verhoeff_valid(uid) if uid else None
    signature_check = _check_signature(parsed, uidai_cert_path)

    # Orientation-correct every page once, shared between OCR and face
    # match - both need upright images (neither tolerates rotation on its
    # own the way QR decoding does - see card_fields_from_image's
    # docstring for how this was confirmed as a real bug, not assumed).
    upright_pages = [_correct_orientation(p) for p in pages]
    ocr = _merge_ocr_across_pages([card_fields_from_image(p) for p in upright_pages])

    qr_name = parsed["fields"].get("name")
    ocr_name = _confirmed_value(ocr["name"])
    name_similarity = _name_similarity(qr_name, ocr_name)
    name_match = (
        name_similarity >= NAME_SIMILARITY_THRESHOLD if (qr_name and ocr_name) else None
    )

    qr_gender_raw = parsed["fields"].get("gender")
    qr_gender = GENDER_MAP.get(qr_gender_raw, qr_gender_raw)
    ocr_gender = _confirmed_value(ocr["gender"])
    gender_match = (qr_gender == ocr_gender) if (qr_gender and ocr_gender) else None

    dob_check = _dob_check(parsed, _confirmed_value(ocr["dob"]))

    qr_photo = _extract_qr_photo(parsed)
    face_result = _best_face_match(qr_photo, upright_pages) if qr_photo is not None else None

    # signature_check["valid"] is False far more often for "wrong cert
    # vintage" than "fake card" (see _check_signature docstring) - only a
    # True gets folded into the aggregate as evidence. A False is reported
    # in the output but deliberately excluded here.
    signature_evidence = True if signature_check["valid"] is True else None

    # Same asymmetric treatment as the QR signature check, and for the same
    # reason - an untrusted/unchained result is the expected default until
    # we have a real NIC-signed sample to confirm the chain against (see
    # pdf_signature_verify.py). Only a signature that's both intact and
    # chains to CCA India's root counts as evidence; anything else is
    # reported but not held against the card.
    pdf_signature_evidence = None
    if pdf_signature_check and pdf_signature_check["has_signature"]:
        if any(s.get("trusted") and s.get("intact") for s in pdf_signature_check["signatures"]):
            pdf_signature_evidence = True

    validation_status, validated = _compute_validation_status([
        checksum_valid,
        name_match,
        gender_match,
        dob_check["match"],
        face_result["match"] if face_result else None,
        signature_evidence,
        pdf_signature_evidence,
    ])

    return {
        "qr_status": "decoded",
        "format": parsed["format"],
        "validated": validated,
        "validation_status": validation_status,
        "validated_meaning": VALIDATED_MEANING,
        "checksum_valid": checksum_valid,
        "signature_check": signature_check,
        "pdf_signature_check": pdf_signature_check,
        "name_check": {"qr": qr_name, "card": ocr_name, "similarity": round(name_similarity, 2), "match": name_match},
        "gender_check": {"qr": qr_gender, "card": ocr_gender, "match": gender_match},
        "dob_check": dob_check,
        "face_check": face_result,
        "fields": parsed["fields"],
    }
