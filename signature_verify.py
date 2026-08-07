"""
Verify a Secure QR / Offline e-KYC XML signature against UIDAI's public cert.

STATUS: proven against a real payload. `certs/uidai_offline_publickey_
26022021.cer` (genuinely UIDAI-issued, subject "DS Unique Identification
Authority of India 05", valid Feb 2021 - Feb 2024) returns True against a
real card's real Secure QR signature - the first confirmed match after
trying 15 certs across every category on UIDAI's certificate page. Verified
this wasn't a fluke: reproducible across repeated runs, and correctly flips
to False when either the signature bytes or the signed data are
deliberately tampered with (proves the check discriminates, isn't a no-op).

This also resolved something genuinely uncertain before: this is an
"Offline e-KYC" cert, not one of the 2 certs UIDAI's page dedicates to
"Secure QR Code" specifically (both of which failed) - meaning UIDAI's
Secure QR and Offline e-KYC signing evidently shared the same key during
this cert's validity window, despite the page listing them under separate
headings. Not documented anywhere we have access to (the Authentication
API spec - the one UIDAI PDF we've read in full - covers a different cert
category entirely: Section 4.1 uses "Production Public Key" certs to
encrypt live API requests, not to verify anything). This was found
empirically, by testing every real cert reachable, not by following a
documented procedure - because no procedure for this specific case exists
in the documentation we have.

Caveat that still applies: a cert's validity window tells you when THAT
KEY was current, not when a given QR was generated. A True here means "this
exact data was signed by whoever held this key" - strong evidence, but
pair it with other context (e.g. the card's own issue date) before treating
match/no-match as the full story for a different card's QR.

To get more/newer certificates:
  https://uidai.gov.in/en/916-developer-section/data-and-downloads-section/19388-uidai-certificate-details-2.html
  (try "Offline e-KYC public key certificates" first, based on the above -
  not "Secure QR Code", despite that section's name)
"""

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature


def verify_signature(signed_bytes: bytes, signature: bytes, cert_path: str) -> bool:
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    public_key = cert.public_key()

    try:
        public_key.verify(
            signature,
            signed_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False
