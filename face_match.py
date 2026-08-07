"""
Face match: does the QR-embedded photo show the same person as the photo
printed on the card face?

Uses OpenCV's YuNet (detection) + SFace (128-d embedding, cosine/L2 match) -
both from opencv_zoo, downloaded and checksum-verified against the LFS
pointer's sha256 (see models/). Fully local inference, no network calls
here; nothing is written to disk beyond what the caller explicitly saves.

This is a document-consistency check (do the two photos already present in
this document agree), not identity authentication against any UIDAI system
- same scope boundary as the rest of this pipeline. It also doesn't prove
the card belongs to whoever is presenting it; only that the two embedded
photos are consistent with each other.
"""

import os
import cv2
import numpy as np

_MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
DETECTOR_PATH = os.path.join(_MODELS_DIR, "face_detection_yunet_2023mar.onnx")
RECOGNIZER_PATH = os.path.join(_MODELS_DIR, "face_recognition_sface_2021dec.onnx")

# From opencv_zoo's own SFace docs/benchmarks - cosine similarity above this
# is considered a match. Not independently calibrated here.
COSINE_MATCH_THRESHOLD = 0.363


def _detect_largest_face(img: np.ndarray, detector):
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    # Multiple detections possible (e.g. a stray pattern match) - the real
    # face on an ID photo is reliably the largest box in frame.
    return max(faces, key=lambda f: f[2] * f[3])


def face_embedding(img: np.ndarray, detector=None, recognizer=None) -> np.ndarray | None:
    """Returns a 128-d SFace embedding for the largest detected face, or
    None if no face was found. Pass a pre-created detector/recognizer to
    avoid re-parsing the ONNX models on every call (see compare_faces,
    which creates each model exactly once for both images instead of
    5 times - lesson from an efficiency bug caught after initial delivery:
    each cv2.FaceDetectorYN.create/FaceRecognizerSF.create parses the model
    file fresh, and the original version did this once per image per model
    per call, redundantly, the same class of mistake fixed earlier in
    uid_ocr_fallback.py/card_ocr_fallback.py's duplicate-OCR bug)."""
    if detector is None:
        detector = cv2.FaceDetectorYN.create(DETECTOR_PATH, "", (0, 0))
    if recognizer is None:
        recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER_PATH, "")
    face = _detect_largest_face(img, detector)
    if face is None:
        return None
    aligned = recognizer.alignCrop(img, face)
    return recognizer.feature(aligned)


def compare_faces(img_a: np.ndarray, img_b: np.ndarray) -> dict:
    """Compare the largest face found in each image. Returns whether a face
    was found in each, the cosine similarity if both were found, and a
    match verdict against COSINE_MATCH_THRESHOLD."""
    detector = cv2.FaceDetectorYN.create(DETECTOR_PATH, "", (0, 0))
    recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER_PATH, "")

    feat_a = face_embedding(img_a, detector, recognizer)
    feat_b = face_embedding(img_b, detector, recognizer)

    if feat_a is None or feat_b is None:
        return {
            "face_found_a": feat_a is not None,
            "face_found_b": feat_b is not None,
            "cosine_similarity": None,
            "match": None,
        }

    score = recognizer.match(feat_a, feat_b, cv2.FaceRecognizerSF_FR_COSINE)

    return {
        "face_found_a": True,
        "face_found_b": True,
        "cosine_similarity": float(score),
        "match": bool(score >= COSINE_MATCH_THRESHOLD),
    }
