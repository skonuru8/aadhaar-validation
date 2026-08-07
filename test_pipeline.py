"""
Uses synthetic fixtures only - no real Aadhaar data is checked into this
project, since two of our four test cards belonged to real people
(one a minor) and shouldn't end up baked into a code repo.

SYNTHETIC_UID (123412341234) is a made-up number that happens to pass the
Verhoeff checksum - it is not a real Aadhaar number.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verhoeff import is_valid
from payload_parser import parse_payload, validate_old_xml_fields
from image_loader import load_image
from uid_ocr_fallback import _candidates_from_text
from card_ocr_fallback import (
    _extract_dob, _extract_gender, _extract_pincode, _extract_address, _extract_name,
    _aggregate_field, _dob_calendar_check,
)
from validate_card import (
    _name_similarity, _dob_check, _compute_validation_status, _merge_field,
    _confirmed_value,
)

CURRENT_YEAR = 2026

SYNTHETIC_UID = "123412341234"


def test_verhoeff_valid_number():
    assert is_valid(SYNTHETIC_UID) is True


def test_verhoeff_rejects_altered_digit():
    tampered = SYNTHETIC_UID[:-1] + str((int(SYNTHETIC_UID[-1]) + 1) % 10)
    assert is_valid(tampered) is False


def test_verhoeff_rejects_non_digits():
    assert is_valid("not-a-number") is False


def test_parse_old_xml_format():
    # Shape matches what we actually decoded from the Dhapu Bai card,
    # values replaced with placeholders.
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PrintLetterBarcodeData uid="{SYNTHETIC_UID}" name="Test User" '
        'gender="F" yob="2000" state="Test State"/>'
    )
    result = parse_payload(xml, current_year=CURRENT_YEAR)
    assert result["format"] == "old_xml"
    assert result["signed"] is False
    assert result["fields"]["uid"] == SYNTHETIC_UID
    assert result["fields"]["name"] == "Test User"
    assert result["field_validation"]["valid"] is True
    assert result["field_validation"]["issues"] == []


def test_old_xml_validation_flags_missing_required_fields():
    # Well-formed XML with none of the fields a real Aadhaar record has -
    # used to come back looking like a normal successful decode.
    result = validate_old_xml_fields({"foo": "bar"}, CURRENT_YEAR)
    assert result["valid"] is False
    assert any("uid" in issue for issue in result["issues"])
    assert any("name" in issue for issue in result["issues"])


def test_old_xml_validation_flags_bad_uid_checksum():
    fields = {"uid": "123412341235", "name": "Test User", "gender": "F", "yob": "2000"}
    result = validate_old_xml_fields(fields, CURRENT_YEAR)
    assert result["valid"] is False
    assert any("checksum" in issue for issue in result["issues"])


def test_old_xml_validation_flags_bad_gender():
    fields = {"uid": SYNTHETIC_UID, "name": "Test User", "gender": "X", "yob": "2000"}
    result = validate_old_xml_fields(fields, CURRENT_YEAR)
    assert result["valid"] is False
    assert any("gender" in issue for issue in result["issues"])


def test_old_xml_validation_flags_implausible_yob():
    fields = {"uid": SYNTHETIC_UID, "name": "Test User", "gender": "F", "yob": "1850"}
    result = validate_old_xml_fields(fields, CURRENT_YEAR)
    assert result["valid"] is False
    assert any("yob" in issue for issue in result["issues"])


def test_parse_unknown_format():
    result = parse_payload("not xml and not all digits")
    assert result["format"] == "unknown"


def test_parse_malformed_xml_does_not_crash():
    # Truncated/corrupted read (e.g. a partial QR decode) used to raise
    # ET.ParseError uncaught - see payload_parser.py's hardening note.
    truncated = '<?xml version="1.0" encoding="UTF-8"?><PrintLetterBarcodeData uid="123" name="Te'
    result = parse_payload(truncated)
    assert result["format"] == "unknown"


def test_parse_non_aadhaar_numeric_qr_does_not_crash():
    # Any digits-only QR (payment ref, coupon, ticket number...) hits this
    # branch, not just real Aadhaar Secure QRs - used to crash with an
    # unhandled zlib error deep inside pyaadhaar.
    not_a_secure_qr = "123456789012345678901234567890"
    result = parse_payload(not_a_secure_qr)
    assert result["format"] == "unknown"


def test_parse_rejects_xml_entity_expansion():
    # A QR is attacker-controlled input. Plain xml.etree.ElementTree expands
    # DTD entities - a small crafted payload can blow up in memory. This
    # must be rejected outright, not parsed (and definitely not expanded).
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE PrintLetterBarcodeData ['
        '<!ENTITY a "AAAAAAAAAAAAAAAA">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
        '<PrintLetterBarcodeData uid="123412341234" name="&c;"/>'
    )
    result = parse_payload(payload)
    assert result["format"] == "unknown"
    assert result["fields"] == {}


def test_load_image_cv2_path():
    # A format cv2 natively decodes (png) - should never fall through to PIL.
    import cv2
    import numpy as np
    import tempfile

    path = tempfile.mktemp(suffix=".png")
    cv2.imwrite(path, np.full((20, 20, 3), 128, dtype="uint8"))
    img = load_image(path)
    assert img.shape == (20, 20, 3)
    os.remove(path)


def test_load_image_pdf_path():
    # UIDAI's own e-Aadhaar download is a PDF - confirmed as a real,
    # previously-unhandled gap (load_image raised FileNotFoundError on an
    # actual PDF before this), not a hypothetical one.
    import numpy as np
    import tempfile
    from PIL import Image as PILImage

    path = tempfile.mktemp(suffix=".pdf")
    PILImage.fromarray(np.full((30, 40, 3), 100, dtype="uint8")).save(path, "PDF")
    img = load_image(path)
    assert img.shape[2] == 3
    assert img.shape[0] > 0 and img.shape[1] > 0
    os.remove(path)


def test_ocr_candidates_from_text_ignores_non_12digit_lines():
    text = "Name: Test User\n3587 0818 3542\nDOB: 13/10/1998\n123\n"
    found = _candidates_from_text(text)
    assert found == {"358708183542"}


def test_ocr_candidates_from_text_does_not_merge_across_lines():
    # PIN code and mobile-number lines sitting next to each other must not
    # get concatenated into a false 12-digit run - each line is checked
    # independently.
    text = "530011\n9180 0607\n"
    found = _candidates_from_text(text)
    assert found == set()


def _build_synthetic_secure_qr_payload(fields: list[str], is_v2: bool) -> str:
    # Mirrors AadhaarSecureQr's real byte layout: 0xFF-delimited fields,
    # gzip-compressed, base10-encoded as one big integer. V2 payloads are
    # distinguished purely by starting with the literal bytes "V2".
    import gzip
    import io

    prefix = b"V2\xff" if is_v2 else b""
    body = prefix + b"\xff".join(f.encode("ISO-8859-1") for f in fields) + b"\xff"
    decompressed = body + b"\x00" * 256  # trailing region signature() slices off

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(decompressed)
    return str(int.from_bytes(buf.getvalue(), "big"))


def test_parse_v1_secure_qr_format():
    # V1 (pre-2022) Secure QR: no "version" field, no trailing
    # "last_4_digits_mobile_no" - see pyaadhaar's own _check_aadhaar_version.
    # Never had a real V1 card to test against; this constructs a payload
    # matching the documented byte layout instead of leaving it untested.
    try:
        import pyaadhaar  # noqa: F401
    except ImportError:
        print("SKIP: test_parse_v1_secure_qr_format (pyaadhaar not installed)")
        return

    v1_fields = ["0", "290120231234567890123", "Test User", "01-01-1990", "M",
                 "S/O Test Father", "Test District", "Test Landmark",
                 "Test House", "Test Location", "123456", "Test PO",
                 "Test State", "Test Street", "Test Subdistrict", "Test VTC"]
    payload = _build_synthetic_secure_qr_payload(v1_fields, is_v2=False)

    result = parse_payload(payload)
    assert result["format"] == "secure_qr"
    assert "version" not in result["fields"]
    assert "last_4_digits_mobile_no" not in result["fields"]
    assert result["fields"]["name"] == "Test User"
    assert result["fields"]["dob"] == "01-01-1990"


def test_extract_dob_prefers_labeled_line():
    # A card can have multiple date-shaped strings (issue date, DOB) - the
    # labeled one must win, not whichever appears first.
    text = "Issue Date: 16/10/2011\nSome Name\nDOB: 15/07/1968\nMALE\n"
    assert _extract_dob(text) == "15-07-1968"


def test_extract_dob_falls_back_to_any_date_shape():
    text = "Some Name\n15/07/1968\nMALE\n"
    assert _extract_dob(text) == "15-07-1968"


def test_extract_gender():
    assert _extract_gender("పురుషుడు/ MALE\n") == "MALE"
    assert _extract_gender("స్త్రీ / FEMALE\n") == "FEMALE"
    assert _extract_gender("no gender line here") is None


def test_extract_pincode_prefers_labeled():
    text = "Some text 123456 more text\nPIN Code: 500072\n"
    assert _extract_pincode(text) == "500072"


def test_extract_pincode_falls_back_to_standalone_6digit():
    text = "Andhra Pradesh - 500072\n"
    assert _extract_pincode(text) == "500072"


def test_extract_address_stops_at_blank_or_keyword_line():
    text = (
        "Address:\n"
        "S/O Test Father, House No 1\n"
        "Test Street, Test City\n"
        "\n"
        "Government of India\n"
    )
    result = _extract_address(text)
    assert result == "S/O Test Father, House No 1, Test Street, Test City"


def test_extract_address_returns_none_without_label():
    text = "S/O Test Father\nNo address label here\n"
    assert _extract_address(text) is None


def test_extract_name_uses_line_before_dob():
    text = "Government of India\nTest User Name\nDOB: 15/07/1968\nMALE\n"
    assert _extract_name(text) == "Test User Name"


def test_extract_name_skips_boilerplate_lines():
    text = "Government of India\nUnique Identification Authority\nTest User Name\nDOB: 15/07/1968\n"
    assert _extract_name(text) == "Test User Name"


def test_aggregate_field_confirms_on_2_of_3_agreement():
    # A real value found consistently across independent preprocessing
    # passes should be trusted; the guard is >= 2 of the (up to 3) passes.
    variants = ["MALE line here", "MALE line here", "totally different noise"]
    result = _aggregate_field(variants, _extract_gender)
    assert result == {"value": "MALE", "agreement_count": 2, "confirmed": True}


def test_aggregate_field_does_not_confirm_single_pass_hit():
    # A one-off misread that only shows up once must NOT be marked
    # confirmed - this is the exact guard that fixed the UID false
    # positive (see uid_ocr_fallback.py), generalized to every field.
    variants = ["FEMALE only here", "no gender line", "no gender line either"]
    result = _aggregate_field(variants, _extract_gender)
    assert result == {"value": "FEMALE", "agreement_count": 1, "confirmed": False}


def test_aggregate_field_all_none_returns_unconfirmed():
    variants = ["no gender", "still no gender", "nope"]
    result = _aggregate_field(variants, _extract_gender)
    assert result == {"value": None, "agreement_count": 0, "confirmed": False}


def test_dob_calendar_check_accepts_real_date():
    result = _dob_calendar_check("15-07-1968", current_year=2026)
    assert result == {"calendar_valid": True, "plausible_year": True}


def test_dob_calendar_check_rejects_impossible_date():
    # 31 February doesn't exist - this is a real structural check, not
    # just agreement-based confidence.
    result = _dob_calendar_check("31-02-1990", current_year=2026)
    assert result["calendar_valid"] is False


def test_dob_calendar_check_flags_implausible_year():
    result = _dob_calendar_check("15-07-1850", current_year=2026)
    assert result["calendar_valid"] is True  # 1850-07-15 is a real date
    assert result["plausible_year"] is False  # just not a plausible DOB


def test_dob_calendar_check_handles_none():
    assert _dob_calendar_check(None, current_year=2026) == {
        "calendar_valid": None,
        "plausible_year": None,
    }


def test_name_similarity_exact_match():
    assert _name_similarity("Konuru Maruthi", "Konuru Maruthi") == 1.0


def test_name_similarity_case_and_whitespace_insensitive():
    assert _name_similarity("  Konuru Maruthi  ", "konuru maruthi") == 1.0


def test_name_similarity_different_names_scores_low():
    assert _name_similarity("Konuru Maruthi Vijaya Saradhi", "Uttam Singh") < 0.5


def test_name_similarity_handles_none():
    assert _name_similarity(None, "Test") == 0.0
    assert _name_similarity("Test", None) == 0.0


def test_dob_check_secure_qr_exact_string_match():
    parsed = {"format": "secure_qr", "fields": {"dob": "15-07-1968"}}
    result = _dob_check(parsed, "15-07-1968")
    assert result["match"] is True


def test_dob_check_secure_qr_mismatch():
    parsed = {"format": "secure_qr", "fields": {"dob": "15-07-1968"}}
    result = _dob_check(parsed, "02-10-2000")
    assert result["match"] is False


def test_dob_check_old_xml_compares_year_only():
    # old-format XML only carries yob (year of birth), never a full date
    parsed = {"format": "old_xml", "fields": {"yob": "1968"}}
    result = _dob_check(parsed, "15-07-1968")
    assert result["match"] is True


def test_dob_check_missing_data_returns_none_not_false():
    parsed = {"format": "secure_qr", "fields": {}}
    result = _dob_check(parsed, None)
    assert result["match"] is None


def test_validation_status_all_match():
    status, validated = _compute_validation_status([True, True, True])
    assert (status, validated) == ("validated", True)


def test_validation_status_one_mismatch():
    status, validated = _compute_validation_status([True, False, True])
    assert (status, validated) == ("not_validated", False)


def test_validation_status_nothing_comparable_is_inconclusive_not_failed():
    # The bug: a card image with no face/demographic text to compare
    # against (e.g. a QR-only back panel) used to report validated=False,
    # identical to an actual mismatch. Nothing was compared here at all.
    status, validated = _compute_validation_status([None, None, None])
    assert (status, validated) == ("inconclusive", None)


def test_validation_status_ignores_unknowns_mixed_with_a_real_match():
    status, validated = _compute_validation_status([True, None, None])
    assert (status, validated) == ("validated", True)


def test_validation_status_ignores_unknowns_mixed_with_a_real_mismatch():
    status, validated = _compute_validation_status([False, None, None])
    assert (status, validated) == ("not_validated", False)


def test_merge_field_prefers_confirmed_over_unconfirmed():
    # Real scenario: DOB only readable (and confirmed) on one page of a
    # 2-page scan, garbage single-pass noise on the other.
    page1 = {"value": None, "agreement_count": 0, "confirmed": False}
    page2 = {"value": "15-07-1968", "agreement_count": 3, "confirmed": True}
    assert _merge_field([page1, page2]) == page2


def test_merge_field_prefers_higher_agreement_among_confirmed():
    page1 = {"value": "A", "agreement_count": 2, "confirmed": True}
    page2 = {"value": "B", "agreement_count": 3, "confirmed": True}
    assert _merge_field([page1, page2]) == page2


def test_merge_field_falls_back_to_any_value_if_none_confirmed():
    page1 = {"value": None, "agreement_count": 0, "confirmed": False}
    page2 = {"value": "maybe", "agreement_count": 1, "confirmed": False}
    assert _merge_field([page1, page2]) == page2


def test_merge_field_all_empty_returns_first():
    empty = {"value": None, "agreement_count": 0, "confirmed": False}
    assert _merge_field([empty, empty]) == empty


def test_confirmed_value_returns_value_when_confirmed():
    field = {"value": "Dhapu Bai", "agreement_count": 3, "confirmed": True}
    assert _confirmed_value(field) == "Dhapu Bai"


def test_confirmed_value_hides_unconfirmed_guess():
    # Real case this covers: a 1-of-3-passes OCR guess ("Fathet" instead
    # of the real "Dhapu Bai") must not be treated as a known value to
    # compare against the QR - it should read as "we don't know", not as
    # a confident wrong answer.
    field = {"value": "Fathet", "agreement_count": 1, "confirmed": False}
    assert _confirmed_value(field) is None


def test_load_image_missing_file_raises():
    try:
        load_image("/definitely/does/not/exist.jpg")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def _build_self_signed_pdf(tmpdir):
    """A PDF signed with a throwaway, non-UIDAI cert generated on the fly -
    enough to exercise pdf_signature_verify.py's mechanics (intact/valid/
    trusted) without needing a real e-Aadhaar sample, which this project
    doesn't have (see pdf_signature_verify.py's module docstring)."""
    import datetime
    import fitz
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    key_path = os.path.join(tmpdir, "key.pem")
    cert_path = os.path.join(tmpdir, "cert.pem")
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    plain_path = os.path.join(tmpdir, "plain.pdf")
    doc = fitz.open()
    doc.new_page()
    doc.save(plain_path)

    signer = signers.SimpleSigner.load(key_path, cert_path)
    signed_path = os.path.join(tmpdir, "signed.pdf")
    with open(plain_path, "rb") as inf, open(signed_path, "wb") as outf:
        w = IncrementalPdfFileWriter(inf)
        signers.sign_pdf(w, signers.PdfSignatureMetadata(field_name="Sig1"), signer=signer, output=outf)
    return plain_path, signed_path


def test_pdf_signature_unsigned_reports_no_signature():
    import tempfile
    from pdf_signature_verify import verify_pdf_signature

    tmpdir = tempfile.mkdtemp()
    plain_path, _ = _build_self_signed_pdf(tmpdir)
    result = verify_pdf_signature(plain_path)
    assert result["has_signature"] is False


def test_pdf_signature_signed_reports_intact_and_untrusted():
    import tempfile
    from pdf_signature_verify import verify_pdf_signature

    tmpdir = tempfile.mkdtemp()
    _, signed_path = _build_self_signed_pdf(tmpdir)
    result = verify_pdf_signature(signed_path)
    assert result["has_signature"] is True
    sig = result["signatures"][0]
    assert sig["intact"] is True
    assert sig["valid"] is True
    # not signed by UIDAI/CCA - must NOT be reported as trusted
    assert sig["trusted"] is False


def test_pdf_signature_tampering_after_signing_breaks_intact():
    import tempfile
    from pdf_signature_verify import verify_pdf_signature

    tmpdir = tempfile.mkdtemp()
    _, signed_path = _build_self_signed_pdf(tmpdir)

    data = bytearray(open(signed_path, "rb").read())
    data[100] ^= 0xFF  # flip a byte inside the signed byte range
    tampered_path = os.path.join(tmpdir, "tampered.pdf")
    with open(tampered_path, "wb") as f:
        f.write(bytes(data))

    result = verify_pdf_signature(tampered_path)
    assert result["signatures"][0]["intact"] is False


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
