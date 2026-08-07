"""
Pre-decode quality gate.

Lesson from testing: the Divya and Kartik card photos both failed to decode
no matter how much preprocessing we threw at them, because the QR region was
too few pixels across to begin with (see README "Failure case studies" #1, #2).
Upscaling a low-res crop doesn't add real data back. So instead of attempting
a full decode on every image, find the QR's rough bounding box first (cheap)
and reject anything below a minimum pixel size before spending time on the
expensive decode/preprocessing path.
"""

import cv2

from image_loader import load_image


def qr_bounding_box_side(image_path: str) -> int | None:
    """
    Return the approximate side length (pixels) of the detected QR region,
    or None if no QR-like finder pattern could be located at all.
    """
    img = load_image(image_path)

    height, width = img.shape[:2]
    detector = cv2.QRCodeDetector()
    found, points = detector.detect(img)
    if not found or points is None:
        return None

    quad = points[0]
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]

    # cv2's detector can return a degenerate quad (duplicate/collinear points)
    # instead of raising - we hit this on the Kartik card test. Shoelace area
    # catches it: a real quad has non-trivial area, a degenerate one is ~0.
    area = 0.5 * abs(sum(xs[i] * ys[(i + 1) % 4] - xs[(i + 1) % 4] * ys[i] for i in range(4)))
    if area < 100:
        return None

    # Also seen on the aa_data batch: a quad with a corner at y=-11430 on a
    # 640px-tall image - physically impossible, but shoelace area on such a
    # quad is huge, not near-zero, so the area check above doesn't catch it.
    # A real detection can't have corners outside the image it was found in.
    if any(x < -1 or x > width + 1 or y < -1 or y > height + 1 for x, y in zip(xs, ys)):
        return None

    return int(max(max(xs) - min(xs), max(ys) - min(ys)))


def passes_quality_gate(image_path: str, min_side_px: int = 250) -> dict:
    """
    Gate result. min_side_px=250 is calibrated against exactly one real
    success (342px, the Dhapu Bai card) with margin below it - not a
    validated threshold. Every failure/false-positive case we tested
    returned None (no quad found at all), not "found but small", so this
    graduated cutoff isn't actually doing discriminating work in our
    dataset yet. Recalibrate once you have more than one real success case.
    """
    side = qr_bounding_box_side(image_path)
    if side is None:
        return {"passed": False, "reason": "no_qr_region_detected", "side_px": None}
    if side < min_side_px:
        return {"passed": False, "reason": "qr_region_too_small", "side_px": side}
    return {"passed": True, "reason": None, "side_px": side}
