"""
Parse decoded QR text into fields.

Lesson from testing: the Dhapu Bai card's QR decoded to plain XML
(`<PrintLetterBarcodeData uid="..." name="..." .../>`) - this is the OLD,
pre-2018 Aadhaar QR format. It carries no digital signature at all, which is
exactly why UIDAI introduced the newer Secure QR: the old format can be
edited and re-rendered into a fresh QR with nothing to prove tampering.
See README "Failure case studies" #4 for the full walkthrough.

The numeric-payload (Secure QR) branch delegates to `pyaadhaar`, which does
the documented byte-unpacking - reimplementing that ourselves would be
untested code pretending to be tested code. Now confirmed against one real
V2 Secure QR (README case study #5): decode+parse match the printed card
exactly. NOT yet tested: a real V1 (pre-2022, non-"V2"-prefixed) Secure QR -
pyaadhaar claims to support both, but we've only ever had a V2 sample to
check against. Install pyaadhaar if you need this path: pip install pyaadhaar

Lesson from hardening (no card involved - this is a "what if the QR isn't
Aadhaar at all" bug): both branches below used to assume that *looking*
right (starts with "<?xml", or is all-digits) meant it would *parse*
successfully. A non-Aadhaar numeric QR (a payment reference, coupon, ticket
number - all common, and all "QR code" symbology so our qr_decode.py
wouldn't filter them out) crashed with an unhandled zlib error deep inside
pyaadhaar. A truncated/corrupted XML read crashed with ET.ParseError. Both
now fail into `format: "unknown"` instead of taking the whole pipeline down -
this is exactly the kind of untrusted-external-data boundary the project's
own "validate at system boundaries" principle is about.

Second hardening finding, more serious: plain `xml.etree.ElementTree` is
documented as unsafe against maliciously crafted XML (entity expansion /
"billion laughs" DoS, external entity access) - and this XML comes straight
from a QR code, which is about as attacker-controlled as input gets. Proved
it: a 64-byte entity definition with 3 nesting levels expanded to 6400 bytes
(100x) using stdlib ElementTree; real nesting depth could exhaust memory
from a QR-sized payload. Switched to `defusedxml`, purpose-built for this -
it rejects DOCTYPE/ENTITY declarations outright instead of expanding them.
"""

import base64
import io

import defusedxml.ElementTree as ET
import defusedxml.common

from verhoeff import is_valid as verhoeff_valid

# Attributes seen on every real old-format card we've decoded (Dhapu Bai,
# Firdos Alam) - both used "yob", not "dob", for date of birth.
OLD_XML_REQUIRED_FIELDS = ("uid", "name", "gender", "yob")
OLD_XML_VALID_GENDERS = {"M", "F", "T"}  # T = transgender, per UIDAI spec
OLD_XML_MIN_YEAR = 1900


def validate_old_xml_fields(fields: dict, current_year: int) -> dict:
    """
    parse_payload() only checks that the XML is well-formed - it says
    nothing about whether the *content* looks like a real Aadhaar record.
    `<PrintLetterBarcodeData foo="bar"/>` parses cleanly and used to come
    back indistinguishable from a real decode. This checks the parsed
    fields are structurally complete and individually plausible - still not
    proof of authenticity (old format has no signature to check against,
    see this file's module docstring), just a sanity floor before the UID
    checksum step downstream is even worth running.
    """
    issues = []

    for required in OLD_XML_REQUIRED_FIELDS:
        if not fields.get(required):
            issues.append(f"missing required field: {required}")

    uid = fields.get("uid")
    if uid is not None:
        if not (len(uid) == 12 and uid.isdigit()):
            issues.append(f"uid is not 12 digits: {uid!r}")
        elif not verhoeff_valid(uid):
            issues.append("uid fails Verhoeff checksum")

    gender = fields.get("gender")
    if gender is not None and gender not in OLD_XML_VALID_GENDERS:
        issues.append(f"gender not one of {sorted(OLD_XML_VALID_GENDERS)}: {gender!r}")

    yob = fields.get("yob")
    if yob is not None:
        if not (len(yob) == 4 and yob.isdigit()):
            issues.append(f"yob is not a 4-digit year: {yob!r}")
        elif not (OLD_XML_MIN_YEAR <= int(yob) <= current_year):
            issues.append(f"yob outside plausible range ({OLD_XML_MIN_YEAR}-{current_year}): {yob}")

    pincode = fields.get("pc")
    if pincode is not None and not (len(pincode) == 6 and pincode.isdigit()):
        issues.append(f"pincode (pc) is not 6 digits: {pincode!r}")

    return {"valid": len(issues) == 0, "issues": issues}


def parse_payload(data: str, current_year: int = 2026, extract_photo: bool = False) -> dict:
    """
    Returns:
      {"format": "old_xml", "fields": {...}, "signed": False, "field_validation": {...}}
      {"format": "secure_qr", "fields": {...}, "signed": None}  # not verified here
      {"format": "unknown", "fields": {}, "signed": False}

    `current_year` bounds the plausible-yob check for old_xml - pass the
    real current year in production; the default here is just this
    project's build date, not a live clock (nothing in this codebase
    reads the system clock).

    `extract_photo` (secure_qr only, opt-in, default off): a Secure QR can
    embed a small compressed photo - `pyaadhaar` exposes it via `.image()`
    but the pipeline never called that method, so the photo silently existed
    in every Secure QR decode without ever being surfaced. It's a face
    photo - real biometric data - so this stays opt-in rather than
    materializing into every casual decode by default. When on, the result
    carries a top-level `photo_jpeg_base64` key (base64 string if a photo
    was present, None if the QR had `email_mobile_status` shaped without
    room for one) - a sibling of `signature`/`signed_data`, not folded into
    `fields`, so default output shape/size is unchanged when this is off.
    """
    stripped = data.strip()

    if stripped.startswith("<?xml") or "PrintLetterBarcodeData" in stripped:
        try:
            root = ET.fromstring(stripped)
            fields = dict(root.attrib)
            return {
                "format": "old_xml",
                "fields": fields,
                "signed": False,
                "field_validation": validate_old_xml_fields(fields, current_year),
            }
        except (ET.ParseError, defusedxml.common.DefusedXmlException):
            return {"format": "unknown", "fields": {}, "signed": False}

    if stripped.isdigit():
        try:
            from pyaadhaar.decode import AadhaarSecureQr
        except ImportError as e:
            raise ImportError(
                "Numeric payload looks like a Secure QR. "
                "Install pyaadhaar to parse it: pip install pyaadhaar"
            ) from e
        try:
            aadhaar = AadhaarSecureQr(int(stripped))
            fields = aadhaar.decodeddata()
            # signature/signed_data are the raw bytes signature_verify.py
            # needs - not user-facing fields, so callers must pop them
            # before serializing this dict (they're bytes, not JSON-safe).
            result = {
                "format": "secure_qr",
                "fields": fields,
                "signed": None,
                "signature": aadhaar.signature(),
                "signed_data": aadhaar.signedData(),
            }
            if extract_photo:
                photo_b64 = None
                if aadhaar.isImage():
                    buf = io.BytesIO()
                    aadhaar.image().convert("RGB").save(buf, format="JPEG")
                    photo_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                result["photo_jpeg_base64"] = photo_b64
            return result
        except Exception:
            # A digits-only QR that isn't actually an Aadhaar Secure QR
            # (payment ref, coupon, ticket number...) fails somewhere inside
            # pyaadhaar's zlib/byte-unpacking - not our bug to fix, but not
            # a reason to crash the whole pipeline over a QR that was never
            # Aadhaar data in the first place.
            return {"format": "unknown", "fields": {}, "signed": False}

    return {"format": "unknown", "fields": {}, "signed": False}
