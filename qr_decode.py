"""
QR decode with cross-validation.

Lesson from testing: on the Amal card, zbar returned what looked like a hit
("26610569") but its detection rect was width=0, height=0 (a single point)
with quality=1, the lowest score zbar reports - a spurious pattern match, not
a real read (see README "Failure case studies" #3). An unattended pipeline
that trusts any non-empty decoder result will silently accept garbage as
verified data. So: reject degenerate detections, and treat a result as
trustworthy only once a real bounding box is present.
"""

from pyzbar.pyzbar import decode as zbar_decode, ZBarSymbol
import zxingcpp
import cv2

from image_loader import load_image

MIN_RECT_AREA_PX = 2000  # ~45x45px minimum; tune against a larger sample


def _zbar_candidates(img):
    out = []
    # Restrict to QR only - Aadhaar cards print a 1D enrollment/dispatch
    # barcode right next to the QR (see README "Failure case studies" #5).
    # Without this filter zbar happily decodes the 1D barcode instead and
    # hands back its text as if it were the Aadhaar payload.
    for r in zbar_decode(img, symbols=[ZBarSymbol.QRCODE]):
        area = r.rect.width * r.rect.height
        if area < MIN_RECT_AREA_PX:
            continue  # degenerate detection, e.g. the Amal false positive
        out.append(r.data.decode("utf-8", errors="replace"))
    return out


def _zxing_candidates(img):
    out = []
    for r in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode):
        if not r.text:
            continue
        out.append(r.text)
    return out


UPSCALE_FACTOR = 2  # tried 1.5/2/3 against a real dense Secure QR; 1.5 didn't
# help, 2 and 3 both did - 2 is the cheaper of the two that works.

# Fractions of min(height, width) to use as a tile side, largest first (so a
# hit on a coarser tile short-circuits before paying for finer ones).
_TILE_FRACTIONS = (0.5, 0.3, 0.18)
_TILE_OVERLAP = 0.5  # stride = tile_size * (1 - overlap)


def _tiles(img):
    h, w = img.shape[:2]
    min_dim = min(h, w)
    seen_sizes = set()
    for frac in _TILE_FRACTIONS:
        size = int(min_dim * frac)
        if size < 80 or size in seen_sizes:
            continue
        seen_sizes.add(size)
        stride = max(1, int(size * (1 - _TILE_OVERLAP)))
        for y0 in range(0, max(1, h - size + stride), stride):
            for x0 in range(0, max(1, w - size + stride), stride):
                y1, x1 = min(h, y0 + size), min(w, x0 + size)
                if y1 - y0 < size * 0.6 or x1 - x0 < size * 0.6:
                    continue  # tiny sliver tile at the image edge, not worth trying
                yield img[y0:y1, x0:x1]


def _tiled_candidates(img):
    """
    Sweep overlapping tiles at a few scales and try each one (upscaled 2x,
    same as the whole-image fallback) until something decodes.

    Why this exists: a real e-Aadhaar photo (screen capture, 4032x3024) had
    two genuine, undamaged QR codes that neither whole-image decode nor
    whole-image upscale could read - not because the QR was low quality, but
    because it occupied a small fraction of a huge, cluttered frame. Manually
    cropping to roughly the right region fixed it immediately, which is the
    tell: the QR itself was fine, the problem was scale-relative-to-frame.
    Tiling automates exactly what that manual crop did, without needing to
    know where the QR is ahead of time or what orientation the photo was
    taken in - this only assumes the QR isn't rotated more than what zbar/
    zxing already tolerate on their own (arbitrary in-plane rotation - a
    core property of QR finder patterns, not something this code adds).
    """
    for tile in _tiles(img):
        upscaled = cv2.resize(tile, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR,
                               interpolation=cv2.INTER_CUBIC)
        zbar_hits = _zbar_candidates(upscaled)
        zxing_hits = _zxing_candidates(upscaled)
        if zbar_hits or zxing_hits:
            return zbar_hits, zxing_hits
    return [], []


# Degrees to try, coarsest useful step first. Skips multiples of 90 - zbar/
# zxing already handle those via their own finder-pattern detection (see
# _rotated_candidates' docstring for how this was confirmed as a *separate*
# gap from that).
_ANGLE_SWEEP = [a for a in range(-40, 41, 5) if a % 90 != 0]


def _rotated_candidates(img):
    """
    Sweep candidate rotation angles - a backstop for arbitrary in-plane skew
    (a photo taken at an angle) that _tiled_candidates doesn't reach.

    Lesson from testing: a first attempt at reproducing "skewed QR fails"
    turned out to be testing the wrong thing twice - once against a
    synthetic image that had been rotated, then rotated back (two
    interpolation passes compounding blur, not representative of a real
    single-skew photo), and once against a QR that was already marginal
    even at 0 degrees (needed a 2x upscale just to decode unrotated - see
    UPSCALE_FACTOR's history). Neither result was trustworthy evidence
    about rotation handling itself.

    Built a fair test instead: one real, comfortably-decodable QR (no
    upscale needed even at 0 degrees), skewed exactly once by 15 degrees
    from its original full photo. Result: _tiled_candidates alone recovered
    it, with no rotation correction at all - tiling's per-tile 2x upscale
    gave zbar enough effective resolution to use its own existing rotation
    tolerance, which is better than the earlier (confounded) tests
    suggested. So arbitrary skew combined with reasonable resolution is
    already handled upstream of this function.

    This pass still exists as defense in depth for cases tiling can't
    reach - e.g. a QR that fills most of the frame, where no small tile
    fully contains it at every angle a skewed grid might present. Verified
    to run correctly (no crash, no regression) but its case wasn't
    isolated as the deciding factor in any real recovery yet - treat it as
    a backstop, not the proven fix for skew; _tiled_candidates is.

    Pads to a diagonal-sized canvas before rotating so corner content isn't
    clipped, mirroring how a real photo taken at an angle would frame the
    document - we don't know the true skew ahead of time, only that this
    pass didn't run until whole-image and tiled attempts already failed.
    """
    h, w = img.shape[:2]
    diag = int((h**2 + w**2) ** 0.5)
    pad_y, pad_x = (diag - h) // 2, (diag - w) // 2
    padded = cv2.copyMakeBorder(img, pad_y, pad_y, pad_x, pad_x,
                                 cv2.BORDER_CONSTANT, value=(255, 255, 255))
    ph, pw = padded.shape[:2]
    center = (pw / 2, ph / 2)

    for angle in _ANGLE_SWEEP:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(padded, m, (pw, ph), borderValue=(255, 255, 255))
        zbar_hits = _zbar_candidates(rotated)
        zxing_hits = _zxing_candidates(rotated)
        if zbar_hits or zxing_hits:
            return zbar_hits, zxing_hits
    return [], []


def decode_qr_from_image(img) -> dict:
    """
    Core decode logic, taking an already-loaded image array. decode_qr() is
    a thin path-based wrapper around this - split out so a caller already
    holding multiple in-memory pages (a multi-page PDF - see
    validate_card.py) can try each one without a redundant disk round-trip,
    same pattern as uid_candidates_from_texts()/card_fields_from_image().

    Four passes, cheapest first - each only runs if the previous one came
    back empty:
      1. Whole image, native resolution.
      2. Whole image, 2x upscale (fixes dense/high-module QRs that are sharp
         but under-sampled at native res - see UPSCALE_FACTOR's history).
      3. Tiled sweep at multiple scales (fixes a real QR that's fine in
         isolation but tiny relative to a large, cluttered frame - see
         _tiled_candidates. Also confirmed this handles moderate arbitrary
         skew, e.g. a photo taken at an angle, when combined with its own
         per-tile upscale - see that function's docstring).
      4. Rotation sweep (backstop for skew tiling can't reach - see
         _rotated_candidates for why this is a backstop, not the proven
         fix). Most expensive pass, tried last; only pays its cost when
         the other three all failed.
    """
    zbar_hits = _zbar_candidates(img)
    zxing_hits = _zxing_candidates(img)

    if not zbar_hits and not zxing_hits:
        upscaled = cv2.resize(img, None, fx=UPSCALE_FACTOR, fy=UPSCALE_FACTOR,
                               interpolation=cv2.INTER_CUBIC)
        zbar_hits = _zbar_candidates(upscaled)
        zxing_hits = _zxing_candidates(upscaled)

    if not zbar_hits and not zxing_hits:
        zbar_hits, zxing_hits = _tiled_candidates(img)

    if not zbar_hits and not zxing_hits:
        zbar_hits, zxing_hits = _rotated_candidates(img)

    if not zbar_hits and not zxing_hits:
        return {"decoded": False, "data": None, "engines_agreed": False}

    data = zbar_hits[0] if zbar_hits else zxing_hits[0]
    agreed = bool(zbar_hits and zxing_hits and zbar_hits[0] == zxing_hits[0])

    return {"decoded": True, "data": data, "engines_agreed": agreed}


def decode_qr(image_path: str) -> dict:
    """
    Decode a QR code from an image, cross-checked across two independent
    decoding engines (zbar and zxing-cpp). Only returns a result when at
    least one engine produces a non-degenerate detection; flags whether the
    two engines agreed, since agreement is the strongest confidence signal
    we have without a signature check. See decode_qr_from_image() for the
    actual pass-by-pass logic.
    """
    return decode_qr_from_image(load_image(image_path))
