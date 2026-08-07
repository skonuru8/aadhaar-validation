# Aadhaar Document Verification Pipeline — Full Session Progress Log

Everything done in this session, in order, including dead ends, bugs found,
tests run with real numbers, and decisions made and why. Written as a
record to read later, not a polished doc — see `README.md` for the
maintained, current-state documentation of the actual codebase.

---

## Phase 0 — Initial setup

- Project handed over as a small Python pipeline: `payload_parser.py`,
  `pipeline.py`, `qr_decode.py`, `quality_gate.py`, `README.md`,
  `requirements.txt`, `signature_verify.py`, `test_pipeline.py`,
  `verhoeff.py`, plus a `test-data/` folder with 3 real Aadhaar card photos.
- Created a venv, installed `requirements.txt`.
- **Bug hit immediately**: `pyzbar` couldn't find the system `libzbar`
  shared library on macOS ARM/Homebrew. Fixed by `brew install zbar` +
  exporting `DYLD_LIBRARY_PATH=/opt/homebrew/lib`, baked into the venv's
  `activate` script so it's automatic on every `source .venv/bin/activate`.
- Ran `test_pipeline.py` — 5/5 synthetic unit tests passed.
- Ran the pipeline against a blank white image — correctly rejected at the
  quality gate (`no_qr_region_detected`), no crash. Confirmed pipeline runs
  end-to-end before doing anything else.
- Explained the pipeline architecture: `quality_gate.py` (reject QR regions
  too small to decode - based on `cv2.QRCodeDetector` bounding box, not
  blur detection) → `qr_decode.py` (zbar + zxing-cpp cross-validated) →
  `payload_parser.py` (old XML vs Secure QR) → `verhoeff.py` (UID
  checksum) → `signature_verify.py` (not wired in yet at this point).

## Phase 1 — Testing against `test-data/` (3 real cards)

Ran all 3 real card images through the pipeline:
- `cda059353e798192917fd975f9c413cc.jpg` (Uttam Singh's card) → `decode_failed`
- `pawan-kotiya-...avif` (Dhapu Bai's card) → `rejected_at_quality_gate`, `qr_region_too_small` (side_px=342)
- `tabrej-alam-...avif` (Firdos Alam's card) → `rejected_at_quality_gate`, `no_qr_region_detected`

**Bug found**: `min_side_px=400` threshold in `quality_gate.py` rejected
the one card that should have decoded (342px). README's own documented
"Dhapu Bai" case. Fixed: threshold lowered to 250 (measured against the
one real success case, with margin).

After the fix: pawan-kotiya (Dhapu Bai) now decoded successfully as
`old_xml` format, checksum valid. tabrej-alam still rejected
(`no_qr_region_detected`) - this was actually a SEPARATE bug caught later.

## Phase 2 — aa_data batch dataset (2646-image Roboflow entity-detection dataset)

User provided a large dataset (`aa_data/`, 2646 images across
train/valid/test splits, meant for name/DOB/address bounding-box training,
not QR legibility - 640x640 stretch-resized).

**First 600-image sample run**: 576 `decode_failed`, 20
`rejected_at_quality_gate`, 3 `decoded`, 1 exception (Secure QR needing
`pyaadhaar`, expected).

Investigated why 552/576 failures had literally no detectable QR quad at
all (cv2's own detector). Tested crop+upscale+preprocessing recovery
against the 23 that DID have a detected quad but still failed - **0/23
recovered**. Confirmed this dataset's failures are genuine information
loss (stretch resize + small scale + JPEG compression), not a fixable
preprocessing gap. Decided NOT to add more preprocessing complexity for
zero measured benefit.

Found the 3 successful decodes: 1 real old-format Aadhaar (checksum
valid), 2 decoded to `WWW.EVOLIS.COM` (a card-printer vendor's own QR
baked into a stock/template image in the dataset - correctly classified
`format: "unknown"`, not misparsed as Aadhaar - proved `payload_parser.py`'s
fallback branch works correctly on real non-Aadhaar data).

**Bug found**: one image's `qr_bounding_box_side()` returned an
impossible value (`side_px=11866` on a 640px-tall image) - cv2's detector
returned a quad with a corner at y=-11430, physically outside the image.
The existing shoelace-area check didn't catch it because a wildly
out-of-bounds quad still computes a large area, not near-zero. **Fixed**:
`quality_gate.py` now also rejects any quad with corners outside the
image's own dimensions.

**Bug found**: `pipeline.py` hard-rejected on `no_qr_region_detected` -
but this was proven wrong by tabrej-alam's card (cv2's detector missed it,
zbar decoded it fine directly, once tested standalone). **Fixed**:
`pipeline.py` now only hard-rejects on the measured `qr_region_too_small`,
letting `no_qr_region_detected` images continue to the real decode
attempt.

## Phase 3 — Real user photo #1: `PHOTO-2026-08-06-17-44-06.jpg`

First real photo the user uploaded (from their own device, not the
dataset). Decoded successfully but the payload (`KC385448081FL`) didn't
look like real Aadhaar data.

**Bug found (serious)**: this card prints a 1D linear barcode
(enrollment/dispatch reference) right next to the 2D QR. `qr_decode.py`
had no symbology filter, so zbar decoded the 1D barcode instead and
returned its text as if it were the Aadhaar payload - `engines_agreed:
True` even, since zxing's default scan found the same wrong barcode.
**Fixed**: restricted both engines to QR-only symbology
(`ZBarSymbol.QRCODE` for zbar, `BarcodeFormat.QRCode` for zxing-cpp).

After that fix, the real QR still failed to decode - even at native
resolution, both engines came back empty, despite the QR being visually
sharp. Root cause: a 3186-digit numeric payload = a high module-count QR,
under-sampled at native resolution. **Fixed**: added a 2x cubic upscale
fallback pass to `qr_decode.py`, retried only when the native-resolution
decode comes back empty.

With both fixes, the Secure QR decoded and parsed cleanly via
`pyaadhaar` (installed as an optional dependency, per the project's
existing docstring convention): name, DOB, full address, last-4 UID
digits, all matching the printed card exactly.

**Privacy directive from user**: don't share scanned personal data beyond
this session, don't call any network unless explicitly asked. Confirmed
via `grep` that no module in the codebase makes any network calls.
Cleaned up scratch image files from `/tmp`.

Also found this QR embeds a photo (`pyaadhaar`'s `isImage()` → `True`) -
`payload_parser.py` didn't extract it at the time (only called
`.decodeddata()`). Noted, extracted later.

## Phase 4 — Face match module (`face_match.py`)

Built from scratch: YuNet (face detection) + SFace (128-d embedding,
cosine similarity) from `opencv/opencv_zoo`. Downloaded both ONNX models
via GitHub's LFS media endpoint (had to route around a git-lfs-pointer
gotcha - `raw.githubusercontent.com` serves LFS pointer text files, not
the binary; `media.githubusercontent.com/media/...` serves the actual
binary). Verified both downloads against the LFS pointer's own SHA-256
hash before trusting them.

Tested against real data: extracted the QR's embedded photo (front photo,
`PHOTO-2026-08-06-19-31-46.jpg`, provided by the user separately) vs the
photo printed on the card face. **Result: 0.80 cosine similarity, correct
match** (real threshold from opencv_zoo docs is 0.363).

## Phase 5 — Signature verification: first cert-hunting round

Attempted to fetch UIDAI's public cert for Secure QR signature
verification. `uidai.gov.in` was unreachable from the sandbox across every
path tried: direct `curl`, `WebFetch`, and a real Chrome browser session
(navigation silently bounced back to a blank tab). Confirmed this wasn't
worth retrying further.

User opened the UIDAI cert page in their own Firefox (Chrome failed for
them too, Firefox worked) and shared a screenshot, then pasted the full
table contents as text. Identified the correct table: **"Secure QR Code"**
specifically (not "Offline e-KYC", not "UIDAI Digital Signature" - that
one signs API-response XML, a different artifact, confirmed later by
reading UIDAI's own Auth API spec in Phase 15).

User downloaded and shared 2 certs from that table:
- `uidai_12_06_18_cer.cer` (DS UIDAI cert 7, expired Jun 2021)
- `uidai_prod_cdup.cer` (DS UIDAI cert 4, expired Jun 2020)

Both genuinely UIDAI-issued (verified subject). **Both failed** against
the real card's signature - expected, since the card's QR is clearly
newer than either cert's vintage.

Tried one more speculative candidate (`uidai_auth_sign_prod_2026.cer`,
the "UIDAI Digital Signature" table's latest) despite the category
mismatch - also failed.

**7 certs tried total, all failed** (see Phase 15 for the full list and
the eventual breakthrough). Documented as an exhausted-for-now search,
not chased further blind at the time.

## Phase 6 — OCR fallback for UID (`uid_ocr_fallback.py`)

Built when it became clear that ~96% of the aa_data dataset (and some real
photos) never produce a decodable QR. First version: single-pass Tesseract
OCR, regex-extract 12-digit lines, Verhoeff-check them.

**Real false positive found** (not hypothetical): against a second real
photo of a card (`PHOTO-2026-08-06-19-31-46.jpg` / a harder companion
photo), the only checksum-valid candidate returned was `436813101998` -
**not the card's actual UID**. Its tail (`13101998`) matched the printed
DOB (13/10/1998) with separators stripped - Tesseract had misread nearby
text and stitched it into something that coincidentally passed Verhoeff.

**Lesson**: passing the checksum is necessary but not sufficient - it
filters typos, not "some other printed number that happens to also be a
valid UID shape."

**Fixed**: run OCR against 3 independently-preprocessed variants (raw
grayscale, 2x upscale, Otsu threshold), only trust a checksum-valid
candidate if the same 12 digits appear in 2+ of the 3 passes. Verified:
on the clear photo, the real UID now confirms at 3/3 agreement while
noise candidates score 1/3 and get excluded; on the harder photo, it
correctly confirms *nothing* rather than confidently reporting the wrong
number.

## Phase 7 — Hardening pass (crash bugs, no card involved)

Prompted by wanting to test "any image, any state" robustness rather than
just happy-path real cards. Found via direct testing, not assumed:

1. **Crash on non-Aadhaar numeric QR**: any digits-only QR (payment
   reference, coupon, ticket number - all common, all valid "QR code"
   symbology) got routed into `AadhaarSecureQr()` and crashed with an
   unhandled zlib error. **Fixed**: caught, falls back to `format:
   "unknown"`.
2. **Crash on malformed/truncated XML**: a partial/corrupted decode
   raised `ET.ParseError` uncaught. **Fixed**: same fallback.
3. **Real security vulnerability, more serious**: old-format XML comes
   straight from an attacker-controllable QR code. Plain
   `xml.etree.ElementTree` is documented as unsafe against entity
   expansion ("billion laughs"). **Proved it's real**: a 64-byte entity
   definition with 3 nesting levels expanded to 6400 bytes (100x) using
   stdlib ElementTree. **Fixed**: switched to `defusedxml`, which rejects
   DOCTYPE/ENTITY declarations outright.

Ran the full 2646-image dataset again after these fixes (parallel this
time isn't used yet - this was still sequential): **0 crashes across
2646 images**, confirming the hardening held at scale. Same 22
decoded / 102 rejected / 2522 failed split as before (proving the
fixes didn't change legitimate-decode behavior, only crash-safety).

## Phase 8 — "Upload it any way" round (rotation, HEIC, scale)

User asked: any size, any orientation, rotated any way - do we handle it?
Answer at the time: no, several real gaps. Fixed what was fixable.

**HEIC crash**: every real photo uploaded this session came from an
iPhone as HEIC. `cv2.imread` returns `None` for it. Every prior real-photo
test in this session had needed a MANUAL `sips` conversion outside the
pipeline first - flagged as the same class of problem as manual QR
cropping. **Fixed**: built `image_loader.py`, tries `cv2.imread` first,
falls back to PIL extended by `pillow-heif`. Every module that called
`cv2.imread` directly was migrated to use this instead.

**Small-QR-in-large-frame**: a real e-Aadhaar screen-photo
(`PHOTO-2026-08-06-19-31-46.jpg`'s sibling shots) had genuine, undamaged
QR codes that neither whole-image decode nor whole-image upscale could
read, because the QR occupied a tiny fraction of a huge frame. Manually
cropping fixed it immediately - the tell that this was a scale problem,
not a quality problem. **Fixed**: added a tiled sweep (`_tiled_candidates`
in `qr_decode.py`) - overlapping tiles at multiple scales, each tried with
its own 2x upscale. Verified with a fair synthetic test: a known-good QR
pasted onto a blank 4000x3200 canvas at 0.15% of frame area, unknown
position - recovered correctly in ~1 second.

**Rotation (90/180/270)**: tested directly - decoded payload byte-for-byte
identical at all 4 angles. Already worked, confirmed not assumed.

**Arbitrary skew**: first two attempts at testing this were flawed
(discovered after the fact) - one tested a synthetic image rotated then
rotated back (double interpolation, not representative), one tested a QR
that was already marginal even unrotated. Rebuilt a fair test: one real,
comfortably-decodable QR, skewed exactly once by 15 degrees from its
original full photo. Result: the tiled sweep ALONE recovered it, no
explicit rotation correction needed - its per-tile upscale gave zbar
enough resolution to use its own existing rotation tolerance. Added an
explicit rotation-sweep pass (`_rotated_candidates`) as a backstop anyway
for cases tiling's grid can't fully contain, verified to run without
regressing anything, but documented honestly as a backstop, not the
proven fix - tiling is.

Ran the full dataset again with rotation-sweep added: **identical decode
counts to before** (22/102/2522) - expected, since none of this dataset's
failures were rotation-related; the added pass only cost more processing
time on the same eventual failures (165s → ~932s → 1238s across this
session's cumulative hardening, for the same 22 successes - a real,
stated tradeoff, not hidden).

## Phase 9 — Full card OCR fallback (`card_ocr_fallback.py`)

User asked to build OCR extraction for the REST of the card (name, DOB,
gender, address), not just UID, robust to rotation. Built using
Tesseract's own OSD (orientation/script detection) to correct rotation
before OCR - the purpose-built tool for this, since OCR (unlike QR) needs
roughly-upright text.

Tested against 3 real cards: Konuru's front photo, the always-QR-dead
Uttam Singh jpg, and a harder/degraded photo. **On the always-QR-dead
card**: recovered full name, DOB, gender, and UID, all correct - the
first time any usable data came out of that specific image all session.

**Regression caught during the build**: the first version of this module
duplicated a WEAKER, single-pass UID check instead of reusing the
already-proven cross-pass-agreement logic from `uid_ocr_fallback.py`, and
immediately reproduced the exact same false positive
(`436813101998`) on the harder photo. **Fixed**: refactored
`uid_ocr_fallback.py` to expose `uid_candidates_from_texts()` (operates on
already-OCR'd text) so both modules share one implementation.

**Efficiency bug also caught and fixed**: the first version ran OCR on 3
preprocessing variants twice (once for UID via a separate function call,
once for the other fields) - redundant Tesseract calls on equivalent
variants. Fixed by sharing the OCR pass.

## Phase 10 — Validation added to every OCR field (not just UID)

User asked for validation + quality improvement on the OCR numbers.
Explained upfront: real validation via cross-pass agreement would LOWER
raw counts (filtering weak single-pass hits), not raise them - that's
what quality-over-quantity looks like.

Generalized the cross-pass-agreement guard to every field
(`_aggregate_field()` in `card_ocr_fallback.py`): each field now reports
`{"value", "agreement_count", "confirmed"}`. DOB additionally got a real,
independent structural check (`_dob_calendar_check`) - rejects impossible
dates (31 Feb) and implausible years, same principle as
`validate_old_xml_fields()`'s year check.

**Proof it works**: tested against the harder card - DOB/gender/pincode
all correctly `confirmed: true` at 3/3 agreement (matches ground truth),
while the garbage name (`"Las"`) and garbage address (`"ee"`) both
correctly came back `confirmed: false` at only 1/3 agreement. Same image,
same OCR run - the field-level confidence now distinguishes real signal
from noise.

Ran the FULL 2646-image dataset with this validated version (parallel,
9 workers, 4.6 minutes - faster than the earlier 6.2-minute unvalidated
run despite doing more work per field, because of the dedup fix).
Results, found vs confirmed:

| field | found (best-of-3) | confirmed (2+/3 agree) |
|---|---|---|
| gender | 1205 (45.5%) | 886 (33.5%) |
| UID | 1418 (53.6%) | 1096 (41.4%) |
| name | 946 (35.8%) | 315 (11.9%) |
| DOB | 626 (23.7%) | 315 (11.9%) |
| pincode | 120 (4.5%) | 56 (2.1%) |
| address | 33 (1.2%) | 7 (0.3%) |

"Found" counts actually went UP vs the earlier single-pass run (best-of-3
recovers cases where only one variant succeeded) while confirmed counts
are the honestly-lower trustworthy tier. 58.2% of images got at least one
confirmed field.

## Phase 11 — Parallel batch testing

User asked to test faster via parallelism. Used Python
`multiprocessing.Pool` (9 workers, matched to available cores). Full
2646-image dataset: **231 seconds vs 1238 seconds sequential (5.4x
speedup)**, with results matching the sequential run almost exactly
(1-image discrepancy, a borderline threshold case, not a correctness
issue).

## Phase 12 — Bug audit (found on request, not offered proactively)

User asked to check for bugs "seen but avoided" plus find new ones. Found
and fixed 3 real bugs, each confirmed by direct test before being called
a bug:

1. **`face_match.py` created each ONNX model up to 5 times per call**
   (`FaceDetectorYN` x2, `FaceRecognizerSF` x3) - same class of mistake
   already caught once in the OCR path, missed here. **Fixed**: create
   once, reuse. Verified identical similarity scores before/after
   (0.8002 / 0.0979 unchanged) - confirmed real speedup (~71ms/call), not
   a behavior change.
2. **Rotated single-image uploads silently broke face matching.** Real,
   not theoretical - tested directly: a 90-degree-rotated photo of a real
   face scored `face_found: False` against its own upright version, not
   just lower similarity. `card_ocr_fallback.py` already corrected
   orientation for OCR; `validate_card.py` (built in the next phase,
   see below) never did the same for face matching. **Fixed**: extracted
   `card_fields_from_image()` (image-array core) so orientation gets
   corrected once and shared between OCR and face match.
3. **"Not Validated" didn't distinguish "nothing to compare" from
   "compared and failed."** Found by testing a real card's QR-only back
   panel (no face/text on that side) - every check came back unknown, yet
   the code showed a red "Not Validated" claiming fields "don't match."
   Nothing was ever compared. **Fixed**: added a 3rd status
   (`validated`/`not_validated`/`inconclusive`) via
   `_compute_validation_status()`.

(Note: items 2 and 3 above reference `validate_card.py`, which by this
point in the actual session timeline had already been built - see next
section. Documented here in the audit-phase grouping to match how the
user's "find bugs" request surfaced them.)

## Phase 13 — `validate_card.py`: the real "is this genuine" feature

User clarified what "validation" should mean when a QR exists: cross-check
the QR's data against what's printed on the card (text + face photo), not
(only) UIDAI signature verification, which was known to be blocked.

Built `validate_card.py`:
- Cross-checks QR-decoded name/DOB/gender against OCR'd card text
  (name via `difflib` similarity, DOB/gender exact-ish match accounting
  for old-format-only-has-year)
- Compares the QR's embedded photo against the card's printed face via
  `face_match.py`
- UID Verhoeff checksum
- 3-state overall verdict (see Phase 12, item 3)

**Tested both directions on real data**, not just the happy path:
- Real match: took the real working Secure QR crop + the real front
  photo (same actual person, captured as 2 separate photos this session)
  and composited them into one image (had to save as lossless PNG - JPEG
  recompression degraded this specific "marginal" QR below decodability,
  a repeat of an earlier-documented lesson). Result: `validated: True`,
  all fields match, 80% face similarity.
- Real mismatch (simulated tampering): pasted the SAME real, genuine QR
  onto Uttam Singh's card/photo. Result: `validated: False`, name/DOB/face
  all correctly flagged mismatched, only gender coincidentally matched.

## Phase 14 — Web UI (`webapp/`)

Built a local Flask app (`webapp/app.py` + `templates/index.html`) per
user's explicit choice (local web app over React Native, which the
UI-design skill defaults to but doesn't fit a local Python pipeline).
Single-image upload, calls `validate_card.py`, renders 3 states
(Validated/Not Validated/Inconclusive/QR-not-found) with a QR-vs-card
comparison table.

**Real bug found via actual browser testing** (not assumed to work):
mismatch rows (✗ icons) weren't rendering at all. Root cause: the
`matchIcon()` JS function was extracting bare `<path>` elements out of
complete SVG strings via regex, discarding `viewBox`/`fill`/`stroke`
attributes needed for the icon to actually render. **Fixed**: switched to
injecting a CSS class into the complete, original SVG string instead of
rebuilding from extracted fragments.

Tested 3 core scenarios live in Chrome via the `claude-in-chrome` browser
tool (not just curl/direct calls): real match (green, all checks pass),
real mismatch (red, correct X marks after the icon fix), QR-dead card
(amber "QR not found", OCR-extracted fields shown with confidence
badges).

Also tested and fixed: a garbage-file upload (`.txt`) was handled without
crashing (correct 500 response) but showed a raw technical error message
with an internal temp file path - improved to a clean, human-readable
message.

## Phase 15 — Real bug found via the QR-only back-panel test (3rd validation state)

Uploaded the real back-panel photo (QR decodes, no face/name/DOB printed
on that side) through the actual browser. Got a misleading red "Not
Validated" claiming fields "don't match" - but NOTHING was ever compared
(every check was `None`/unknown). This is the bug described in Phase 12
item 3 - documented here again because this is literally when/how it was
found: real browser testing, not code review.

## Phase 16 — PDF upload support

User asked: what about PDFs (e-Aadhaar downloads, or scanned cards saved
as PDF)? Answer at the time: not handled at all, confirmed by building an
actual test PDF and watching `load_image()` raise `FileNotFoundError`.

**Fixed**: added PyMuPDF as a third tier in `image_loader.py` (chosen
over `pdf2image` specifically because it has no system dependency -
`pdf2image` needs `poppler` installed separately). Renders at 300 DPI so
an embedded QR doesn't lose resolution before reaching decode.

Tested: a plain photo saved as PDF loaded correctly. A real decodable QR
card saved as PDF decoded correctly end-to-end through `validate_card()`
AND the actual browser UI. One composite test DID fail as a PDF - traced
to that composite's QR already being the known "marginal" one from Phase
3 (needs upscale even pristine) that PDF's JPEG re-encoding pushed past
recovery - confirmed this wasn't a new PDF bug by testing a robust
(non-marginal) QR through the same PDF path, which decoded cleanly.

**Known limitation stated at the time**: only page 1 of a PDF was
rendered.

## Phase 17 — Multi-page PDF support

User asked: what about a 2-page PDF (front page, back page - a real,
common scan shape)? Confirmed as a real gap the same way as before: built
an actual 2-page test PDF (page 1: face+name+DOB, no QR; page 2: QR, no
face) and watched the QR come back "missing" because only page 1 was ever
loaded.

**Fixed properly, not patched narrowly**:
- `decode_qr_from_image()` extracted as an array-based core in
  `qr_decode.py` (mirrors the pattern already used for
  `uid_candidates_from_texts()`/`card_fields_from_image()`)
- `validate_card.py` now tries QR decode against every page (first page
  that decodes wins)
- OCR fields merge across pages (`_merge_field()`) - a confirmed value
  from any page wins over an unconfirmed one from another
- Face matching tries every page, uses whichever one actually has a face
- `image_loader.py` got a new `load_all_pages()` uniform entry point (a
  normal image becomes a 1-element list, a PDF becomes every page)

Verified 3 ways: the real 2-page PDF through both direct function call
AND the actual browser UI (pincode from page 2 now correctly shows up
alongside name/DOB/gender/UID from page 1, all `confirmed: true`); an
isolated per-page decode test with a robust QR to rule out the
Phase-16 marginal-QR issue as a confound; full regression against every
existing single-image test case (identical results before/after).

**User then asked**: does page ORDER matter (front-first vs back-first)?
Tested directly: built the same 2-page PDF with pages reversed, got
byte-identical results, through BOTH direct function call and (when asked
"did you test it with claude in chrome?" - honestly answered no at first
for the reversed-order case, then actually did it) the real browser
upload. Confirmed order-independent, verified not assumed, in both
directions.

## Phase 18 — Reading UIDAI's real Authentication API spec

User referenced a real UIDAI PDF
(`Aadhaar_Authentication_API-2.5_Revision-1_of_January_2022.pdf`) sitting
in their Downloads folder, asking whether it means we CAN access Aadhaar
data via an API, contradicting what was said earlier. Read the full
34-page document (not skimmed).

**Confirmed, with more precision than before**: live access needs
AUA/ASA registration (explicit in the doc's own terminology section),
production access is private-network-only, every request needs signed
XML + license keys. There IS a documented dev/test path (public URL,
default "public" AUA code) - a nuance not mentioned before.

**More important finding**: even fully registered, this specific API
**never returns Aadhaar data** - stated twice in the document itself
(section 2.3's highlighted callout, section 3.4's opening line): "only
responds with yes/no... no Personal Identity Information (PII) is
returned as part of the response." It's a match-confirmation oracle, not
a data-access API, by UIDAI's own explicit design.

Follow-up questions answered from the same source, precisely: what to
send (UID + demographic/biometric data you already have, encrypted,
signed), what you get back (`ret="y"/"n"` + numeric error code + an
info/audit bitmap - never the actual stored values).

**"We already have this data, right?" - answered honestly**: yes for the
demographic fields, but that's the one requirement out of five that's
already satisfied. Still missing: AUA/ASA registration + license keys,
registered private-network access, a digitally-signed request (needs an
org-bound X.509 cert), and - the one that can't be solved by more
engineering - live, per-transaction consent from the actual person, which
doesn't exist for a dataset of scraped strangers' photos.

**"What can we get now vs what can't we get" - final synthesis**: laid
out cleanly what's achievable fully locally (UID, demographic fields,
internal QR-vs-card consistency, embedded photo, structural
validation) vs what requires UIDAI and is blocked (cryptographic proof of
a specific QR's authenticity without the right cert; live database
confirmation, blocked by registration+network+consent; full UID from
Secure QR, which isn't a permission problem - the data simply isn't in
the payload).

## Phase 19 — Checking real UID coverage across the dataset

User asked: do we get UIDs in ALL images? Pulled exact numbers from
already-stored batch results rather than re-running anything:
- QR decode path: 22/2646 decoded at all; only 4 exposed a FULL UID
  (old-format only - Secure QR never exposes it)
- OCR fallback: 1096/2646 confirmed (41.4%), 1418/2646 checksum-valid at
  any confidence (53.6%)
- **No — far from all images.** At best ~54%, worst-case ~1228 images
  with no extractable UID at all through either path.

## Phase 20 — Second cert-hunting round: the breakthrough

User referenced the UIDAI cert page again ("do you still have that
image... we have so many certificates in that site"). Didn't have the
screenshot file itself (screenshots aren't saved to disk), but had the
exact table text from when the user pasted it earlier in the same
conversation. Enumerated what had been tried (7 certs) vs what was left
untested by table: **Secure QR Code (2/2, exhausted), Production Public
Key (1/6, rest confirmed irrelevant), UIDAI Digital Signature (1/5, rest
confirmed irrelevant - signs API responses, not the QR), Offline e-KYC
(1/5, 4 untested and the only remaining plausible category), Staging/
PreProduction (non-production, low value)**.

User downloaded ~10 more certs (Offline e-KYC + Production Public Key
categories) and shared them. Tested all 15 certs total (7 old + 8 new)
against the real card's signature.

**BREAKTHROUGH: `uidai_offline_publickey_26022021.cer` → `True`.** First
genuine signature match in the project's entire history. Verified this
wasn't a fluke before trusting it:
- Reproducible across 3 repeated runs
- Correctly flips to `False` when the signature bytes are deliberately
  tampered with
- Correctly flips to `False` when the signed data is deliberately
  tampered with
- Confirmed through `pipeline.py`'s actual CLI entry point too, not just
  internal function calls

This also resolved a genuinely uncertain question: the match came from an
**"Offline e-KYC"** cert, not the dedicated "Secure QR Code" table (both
of which had already failed) - meaning UIDAI's Secure QR and Offline
e-KYC signing shared the same key during this cert's validity window
(Feb 2021 - Feb 2024), despite the page listing them separately.

**Answered the user's PDF question honestly**: the Auth API spec (Phase
18) does NOT document how to use Offline e-KYC certs - that's covered by
a different UIDAI document not provided. This was found empirically, by
testing everything reachable, not by following a documented procedure.

## Phase 21 — Wiring real signature verification into the product

Updated `signature_verify.py`'s status from "written but never exercised"
to "proven against a real payload," with the full story documented.

Wired into `validate_card.py` **deliberately asymmetrically**: a `True`
from the signature check is unambiguous cryptographic proof and counts as
strong evidence toward "Validated." A `False` does NOT get treated as
equivalent to a failed demographic check - it's far more likely to mean
"wrong cert vintage" (UIDAI rotates signing keys) than "card is fake."
Feeding a `False` in as if it were a real mismatch would systematically
bias every card signed by a different-vintage key toward false negatives -
worse than not checking at all. Implemented via `_check_signature()` and
a new `signature_evidence` variable that's only non-None when the check
actually succeeds.

Bundled the proven-working cert as `DEFAULT_UIDAI_CERT_PATH`, wired as
the default parameter so the webapp benefits automatically with no
`app.py` changes needed.

**Found and fixed a real accuracy bug while wiring the frontend**: the
"Validated" banner's description text was a hardcoded string claiming
"name, DOB, gender, and face photo all check out" regardless of which
checks actually ran - inaccurate for e.g. the back-panel case where only
the signature contributed. **Fixed**: banner text is now built dynamically
from which checks actually passed/failed.

**Tested live through the actual browser**, 3 real scenarios:
1. Konuru's real back-panel photo (only comparable data is the signature)
   → "Validated - Confirmed via: UIDAI signature." (accurate, not
   overclaiming fields that were never compared)
2. The real front+back composite (all fields comparable) → "Validated -
   Confirmed via: UIDAI signature, name, DOB, gender, face photo." (all 5
   checks correctly listed)
3. **The most valuable test of the whole project**: re-ran the simulated
   tampering case (Konuru's real, genuinely-signed QR pasted onto Uttam
   Singh's card/photo) with the signature check now live. Signature
   correctly reports "cryptographically confirmed" (true - it IS a real,
   unmodified, validly-signed QR). Overall verdict correctly stays
   **"Not Validated"** because name/DOB/face don't match what it's now
   attached to. A signature-only check would have wrongly called this
   genuine - proof the layered design catches something a single check
   would miss, demonstrated against a real adversarial case, not just
   asserted.

51/51 unit tests passing at this point, full regression clean.

## Phase 22 — This file

User asked to close the shell (Flask server stopped, confirmed via `ps`)
and write this complete progress log.

---

## Full list of files built/modified this session

- `image_loader.py` - built. HEIC support, then PDF support
  (`load_pdf_all_pages`, `load_all_pages`), the shared image-loading layer
  every other module uses.
- `quality_gate.py` - threshold fix (400→250), out-of-bounds quad bug fix.
- `qr_decode.py` - QR-only symbology restriction, 2x upscale fallback,
  tiled sweep, rotation sweep, `decode_qr_from_image()` array-based core.
- `payload_parser.py` - non-Aadhaar-QR crash fix, malformed-XML crash fix,
  `defusedxml` security fix, `validate_old_xml_fields()`, photo
  extraction (`extract_photo` param).
- `pipeline.py` - gate-blocking fix, signature verification wiring,
  OCR fallback wiring, `hint` field on failures.
- `verhoeff.py` - unchanged, used throughout.
- `signature_verify.py` - unchanged code, but status updated from
  "never tested" to "proven against real data."
- `uid_ocr_fallback.py` - built. Cross-pass-agreement false-positive fix,
  `uid_candidates_from_texts()` extraction for sharing with
  `card_ocr_fallback.py`.
- `card_ocr_fallback.py` - built. Orientation correction via Tesseract
  OSD, per-field cross-pass agreement, DOB calendar validation,
  `card_fields_from_image()` array-based core.
- `face_match.py` - built. Model-loading dedup fix (5x → 2x per call).
- `validate_card.py` - built, then rewritten for multi-page support, then
  extended with real signature verification. 3-state validation status
  fix. The main orchestrator tying everything together.
- `webapp/app.py` - built. Friendly error messages for unreadable files.
- `webapp/templates/index.html` - built. SVG icon rendering bug fix,
  dynamic banner text fix, signature row added.
- `test_pipeline.py` - grew from 5 to 51 tests across the session, every
  new module/bugfix got real unit test coverage.
- `README.md` - grown continuously throughout, documenting every finding,
  case study, and bug as it was found - the maintained current-state doc.
- `certs/` - 15 real UIDAI certificate files collected and tested;
  `uidai_offline_publickey_26022021.cer` is the one that works.
- `requirements.txt` - grew from 4 to 8 packages
  (opencv-python-headless, pyzbar, zxing-cpp, cryptography, defusedxml,
  pillow-heif, pytesseract, pymupdf). `pyaadhaar` and `flask` stayed
  optional/undeclared per the project's existing convention.

## Known open limitations, stated plainly (not hidden)

- Genuinely blurred/degraded QR data cannot be recovered - proved with 3
  independent decode engines (zbar, zxing, WeChatQRCode+super-resolution)
  all failing on the same real degraded photo.
- Full 12-digit UID is never available from a Secure QR - not a
  permission problem, UIDAI's spec simply never puts it in the payload.
- Signature verification only has ONE proven-working cert
  (`uidai_offline_publickey_26022021.cer`, valid Feb 2021-Feb 2024) - a
  `False` result against a different card's QR is far more likely to be
  "wrong cert vintage" than "card is fake," and is deliberately not
  treated as proof of anything by the code.
- `card_ocr_fallback.py`'s `name` field is the weakest by design - no
  label exists on real cards, it's a positional heuristic.
- `card_ocr_fallback.py`'s `address` extraction only works when an
  explicit "Address:" label is present in English - 2 of 3 real test
  cards print the address block unlabeled, not handled.
- PDF multi-page support has only been tested with 2-page scans - a 3+
  page scan should work the same way (iterates every page) but hasn't
  been built a test for.
- Actual UIDAI database confirmation (the live Authentication API) is
  blocked structurally, not technically - needs org registration, private
  network access, and live per-transaction consent from the actual
  person, none of which apply to bulk/dataset validation.
