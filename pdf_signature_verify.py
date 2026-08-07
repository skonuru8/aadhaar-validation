"""
Verify the digital signature embedded in an e-Aadhaar PDF itself - a
completely different mechanism from signature_verify.py's QR signature
check.

An e-Aadhaar PDF downloaded from UIDAI's site carries its own PDF-level
digital signature (PKCS7/CMS, applied by NIC on UIDAI's behalf), separate
from whatever QR code is printed inside it. This matters for two real
gaps the QR-only check can't cover:
  1. Old-format (pre-2018) Aadhaar QR codes carry no signature at all -
     confirmed by payload_parser.py's old_xml branch never having a
     signature/signed_data field, because the format never allocated one.
     A person with an old card can still download a *current* e-Aadhaar
     PDF, which IS signed - checking the PDF, not the QR, is the only way
     to get a real crypto answer for them.
  2. It's a second, independent proof for anyone, old or new format.

STATUS: mechanism built and self-tested (a locally-generated, deliberately
signed test PDF round-trips through this correctly - see test_pipeline.py),
but NOT proven against a real UIDAI/NIC-signed e-Aadhaar PDF - no real
sample of one exists in this project (only synthetic test PDFs have been
built all session, per PROGRESS.md). Treat "trusted": True from this as
"the mechanism works", not yet as "confirmed against a real government
signature" the way signature_verify.py's QR check is.

Trust root: certs/cca_india_2022_root.cer, the "CCA India 2022" self-
signed root certificate, downloaded directly from cca.gov.in (India's
Controller of Certifying Authorities - the top of India's entire
government PKI trust chain, not UIDAI-specific). The intermediate NIC
sub-CA certificate does NOT need to be separately downloaded - a real
signed PDF carries its own intermediate cert chain embedded in the
signature (that's what "other_certs"/embedded certs are for); only the
root has to come from an independent, out-of-band source, which is why
we fetched it directly from CCA's own site rather than trusting anything
extracted from an uploaded file.

Requires the ORIGINAL, unmodified PDF bytes - a PDF signature covers the
literal byte stream at signing time. Any re-save, flatten, or
rasterize-then-reassemble (which is exactly what image_loader.py's
load_pdf_all_pages() does for QR/OCR) destroys it. This module must run
against the raw uploaded file, before that conversion happens.
"""

import logging
import os

from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator.context import ValidationContext

# An untrusted/unchained signer certificate is an expected, common outcome
# here (e.g. any test cert, or a real signer whose chain we don't have the
# right intermediate for) - pyhanko logs that case as an ERROR-level
# traceback by default, which would spam stderr on every normal call.
# The outcome is still fully reported via the returned "trusted" field.
logging.getLogger("pyhanko_certvalidator").setLevel(logging.CRITICAL)
logging.getLogger("pyhanko").setLevel(logging.CRITICAL)

CCA_ROOT_CERTS = [
    os.path.join(os.path.dirname(__file__), "certs", "cca_india_2022_root.cer"),
    os.path.join(os.path.dirname(__file__), "certs", "cca_india_2022_spl_root.cer"),
]


def _load_trust_roots():
    from asn1crypto import pem, x509

    roots = []
    for path in CCA_ROOT_CERTS:
        with open(path, "rb") as f:
            data = f.read()
        if pem.detect(data):
            _, _, data = pem.unarmor(data)
        roots.append(x509.Certificate.load(data))
    return roots


def verify_pdf_signature(pdf_path: str) -> dict:
    """
    Checks every digital signature embedded in the PDF at pdf_path.

    Returns {"has_signature": bool, "signatures": [...]}. Each signature
    entry reports:
      - intact: the signed byte range hasn't been altered since signing
        (independent of who signed it - catches tampering even without
        trust)
      - valid: the cryptographic signature itself checks out
      - trusted: the signer's certificate chains to CCA India's root -
        this is the "was it really NIC/UIDAI" answer
      - covers_whole_document: False means later revisions were appended
        after signing (normal for some workflows, but means the visible
        content isn't fully covered by this signature)
      - signer: the signing certificate's subject, so a human can see who
        it claims to be even when trust can't be automatically confirmed
    """
    with open(pdf_path, "rb") as f:
        reader = PdfFileReader(f)
        embedded = list(reader.embedded_signatures)

        if not embedded:
            return {"has_signature": False, "signatures": []}

        trust_roots = _load_trust_roots()
        vc = ValidationContext(trust_roots=trust_roots)

        signatures = []
        for sig in embedded:
            try:
                status = validate_pdf_signature(sig, signer_validation_context=vc)
                signatures.append({
                    "field_name": sig.field_name,
                    "intact": status.intact,
                    "valid": status.valid,
                    "trusted": status.trusted,
                    "covers_whole_document": status.coverage.name == "ENTIRE_FILE" if status.coverage else None,
                    "signer": status.signing_cert.subject.human_friendly if status.signing_cert else None,
                    "issuer": status.signing_cert.issuer.human_friendly if status.signing_cert else None,
                    "summary": status.summary(),
                })
            except Exception as e:
                signatures.append({
                    "field_name": sig.field_name,
                    "intact": None,
                    "valid": None,
                    "trusted": None,
                    "error": f"{type(e).__name__}: {e}",
                })

        return {"has_signature": True, "signatures": signatures}
