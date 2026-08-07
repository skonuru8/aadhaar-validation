# Aadhaar Document Validator

A local tool that checks whether an uploaded Aadhaar card (photo or PDF)
looks genuine and internally consistent - without ever sending the data
anywhere. Everything runs on your own machine.

## What it does, in plain terms

You upload a photo or PDF of an Aadhaar card. The app looks at two things
on it and cross-checks them against each other:

1. **The QR code** - every Aadhaar card has one. It secretly contains the
   same name, date of birth, gender, and address that's printed on the
   card, plus (on newer cards) a small photo and a digital signature from
   UIDAI (the government body that issues Aadhaar).
2. **What's printed on the card** - read directly off the image using
   text recognition (OCR) and face detection.

If those two match, the card is marked **Validated**. If they don't
match, or the QR looks tampered with, it's marked **Not Validated**. If
there's nothing to compare (e.g. only the back of the card was uploaded,
which has no photo or printed name), it's marked **Inconclusive** rather
than guessing.

## What "Validated" actually means

It means: *the QR code's hidden data agrees with what's printed on the
card, and where possible, we cryptographically confirmed UIDAI actually
issued that QR code.*

It does **not** mean this Aadhaar number belongs to a real, currently
active person according to UIDAI's live database - that would require
UIDAI's own Authentication API, which needs government registration and,
critically, the actual person's live consent at the moment of checking.
Nothing that inspects an already-uploaded photo can ever provide that. So
this tool answers "is this document self-consistent and, where checkable,
genuinely signed by UIDAI" - not "is this person's ID currently valid."

## What happens step by step

1. **Read the file** - works with JPG/PNG/HEIC photos and PDFs (including
   multi-page scans, front and back, in any order).
2. **Find and decode the QR code** - automatically, at any size, angle,
   or rotation. No manual cropping needed.
3. **If the QR decodes:**
   - Check the Aadhaar number's checksum (catches typos/corruption).
   - Check the QR's digital signature against UIDAI's public certificate,
     where possible - real cryptographic proof, not a guess.
   - Compare the QR's name/DOB/gender/photo against what's printed on the
     card (read via OCR and face matching).
   - If it's a PDF, separately check whether the *PDF file itself* carries
     a government digital signature (a different, independent signature
     from the one inside the QR - useful for older cards whose QR was
     never signed at all).
4. **If the QR can't be read** - shows whatever text could still be read
   directly off the card (name, DOB, etc.), each field clearly labeled as
   either "confirmed" (found consistently across multiple attempts) or
   just "detected" (found once, less certain) - and clearly marked as
   **not validated**, since there's no QR to check it against.

## What it can't do

- Confirm a card against UIDAI's live database (needs government
  registration + the person's real-time consent - not obtainable from an
  uploaded photo, by design).
- Recover data from a QR code that's genuinely too blurry or damaged to
  scan.
- Get the full 12-digit Aadhaar number from a modern "Secure QR" card -
  UIDAI's own format simply never includes it.
- Confirm the signature on a very old (pre-2018) card's QR code - that
  format was never signed by UIDAI in the first place. (A PDF-level
  signature check can sometimes fill this gap - see above.)

## Privacy

No uploaded image, PDF, or extracted personal data ever leaves your
machine. There are no network calls anywhere in this codebase. Uploaded
files are deleted immediately after processing.

## Running it

```
source .venv/bin/activate
python webapp/app.py
```

Then open `http://127.0.0.1:5050` and upload a card.

## Want the full history?

Every decision, bug, dead end, and test from building this is logged in
`PROGRESS.md`.
