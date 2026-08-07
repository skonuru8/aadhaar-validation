"""
Local test UI for validate_card.py - upload an Aadhaar image, see whether
the QR's data is internally consistent with the printed card (fields +
face photo), or an honest "QR not found" notice with unvalidated extracted
details as a fallback.

Everything runs locally against this repo's existing pipeline modules -
no network calls, nothing leaves the machine. Run with:
    python webapp/app.py
then open http://127.0.0.1:5050
"""

import os
import sys
import tempfile
import traceback

from flask import Flask, request, jsonify, render_template

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from validate_card import validate_card  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB, generous for a phone photo

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "aadhaar_validator_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/validate", methods=["POST"])
def validate():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    suffix = os.path.splitext(file.filename)[1] or ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=UPLOAD_DIR)
    os.close(fd)
    file.save(tmp_path)

    try:
        result = validate_card(tmp_path)
        return jsonify(result)
    except FileNotFoundError:
        # image_loader.py raises this specifically when neither cv2 nor
        # PIL/pillow-heif could decode the upload as an image at all - the
        # accurate user-facing message is "not a readable image", not the
        # raw exception (which just shows an internal temp file path).
        return jsonify({
            "error": "Unreadable file",
            "detail": "This file couldn't be read as an image. Make sure it's a JPG, PNG, HEIC, or AVIF photo.",
        }), 400
    except Exception as e:
        return jsonify({
            "error": "Processing failed",
            "detail": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500
    finally:
        os.remove(tmp_path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
