"""
End-to-end pipeline: quality gate -> decode -> parse -> checksum -> signature.

Old-format cards can't be signature-checked at all (no signature exists).
Secure QR cards can, IF you pass a `uidai_cert_path` - the correct one for
when the QR was generated, since UIDAI rotates its signing certificate.
Passing the wrong vintage produces a False that looks like tampering but
just means "wrong cert," not "forged card" - see signature_verify.py and
README case study #5 before treating a False here as proof of anything.
Default is no cert -> `signed` stays None (verification not attempted),
same as before this was wired in.

`ocr_fallback=True` attempts card_ocr_fallback.py when the QR path fails
entirely (gate reject or decode fail) - OCR the printed UID plus DOB,
gender, pincode, address, and name where readable. Much weaker than a
decoded QR (no signature, no cryptographic cross-check, no photo - see
that file's docstring, especially the name field's caveat), and costs a
few real seconds of Tesseract per image, so it's opt-in rather than the
default path.

`field_validation` is populated for old-format decodes only - see
payload_parser.py's validate_old_xml_fields(). Well-formed XML isn't the
same as plausible Aadhaar data; this catches the gap between the two.

`extract_photo=True` (secure_qr only, opt-in) surfaces the QR's embedded
photo as `photo_jpeg_base64` - see payload_parser.py's parse_payload() for
why this defaults off (it's a face photo, real biometric data).

Failure results carry a `hint` - not a new decode capability (blurred/
degraded QR data is genuinely unrecoverable, see README case studies #1-#3
and #5's dense-QR-decode section), but an honest, actionable "here's likely
why, here's what to try" instead of a bare status code. Distinguishes "no
QR-like pattern found at all" (check the image actually has a QR) from
"a QR was found but couldn't be decoded" (likely blur/resolution - retake,
or fall back to ocr_fallback for a much weaker UID-only check).
"""

from quality_gate import passes_quality_gate
from qr_decode import decode_qr
from payload_parser import parse_payload
from verhoeff import is_valid as verhoeff_valid
from signature_verify import verify_signature
from card_ocr_fallback import card_ocr_fallback


def run(
    image_path: str,
    uidai_cert_path: str | None = None,
    ocr_fallback: bool = False,
    extract_photo: bool = False,
) -> dict:
    gate = passes_quality_gate(image_path)
    if not gate["passed"] and gate["reason"] == "qr_region_too_small":
        result = {
            "status": "rejected_at_quality_gate",
            "detail": gate,
            "hint": (
                f"A QR-like region was found but only ~{gate['side_px']}px across - "
                "too small to reliably decode. Move closer, or use a higher-"
                "resolution photo."
            ),
        }
        if ocr_fallback:
            result["ocr_fallback"] = card_ocr_fallback(image_path)
        return result

    decode = decode_qr(image_path)
    if not decode["decoded"]:
        if gate["side_px"] is None:
            hint = (
                "No QR-like pattern was detected anywhere in the image, even after "
                "checking multiple scales, tiles, and rotations. Confirm the photo "
                "actually contains a visible QR code."
            )
        else:
            hint = (
                "A QR was detected but couldn't be decoded - most likely too "
                "blurred, low-resolution, or damaged at the pixel level, not a "
                "framing/orientation issue (those are handled automatically). "
                "Try a sharper, better-lit photo, or enable ocr_fallback=True for "
                "a much weaker UID-only check that doesn't need the QR at all."
            )
        result = {"status": "decode_failed", "detail": decode, "hint": hint}
        if ocr_fallback:
            result["ocr_fallback"] = card_ocr_fallback(image_path)
        return result

    parsed = parse_payload(decode["data"], extract_photo=extract_photo)
    uid = parsed["fields"].get("uid")

    signed = parsed["signed"]
    if parsed["format"] == "secure_qr" and uidai_cert_path:
        signed = verify_signature(parsed["signed_data"], parsed["signature"], uidai_cert_path)

    result = {
        "status": "decoded",
        "format": parsed["format"],
        "signed": signed,
        "engines_agreed": decode["engines_agreed"],
        "uid_checksum_valid": verhoeff_valid(uid) if uid else None,
        "field_validation": parsed.get("field_validation"),
        "raw_data": decode["data"],
        "fields": parsed["fields"],
    }
    if extract_photo:
        result["photo_jpeg_base64"] = parsed.get("photo_jpeg_base64")
    return result


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) not in (2, 3):
        print("usage: python pipeline.py <image_path> [uidai_cert_path]")
        sys.exit(1)

    cert = sys.argv[2] if len(sys.argv) == 3 else None
    print(json.dumps(run(sys.argv[1], cert), indent=2, ensure_ascii=False))
