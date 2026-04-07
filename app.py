"""app.py — Flask API server for the Telegram Mini App.

Endpoints:
  GET  /api/feed              → paginated free content list
  GET  /api/creators          → all active promoted creators
  GET  /api/content/<id>      → single item (checks PPV purchase)
  POST /api/purchase/verify   → verify Stars payment, return content URL

All routes return JSON. Run: python app.py
"""

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse

from dotenv import load_dotenv
from flask import Flask, jsonify, request, abort
from flask_cors import CORS

import db

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
CORS(app, origins=[
    "https://classy-chaja-7120c1.netlify.app",
    "https://*.telegram.org",
    "http://localhost:5173",   # local dev
])

BOT_TOKEN = os.environ["BOT_TOKEN"]


# ---------------------------------------------------------------------------
# Telegram initData verification
# ---------------------------------------------------------------------------

def _verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData HMAC and return the parsed user dict.

    See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Args:
        init_data: The raw initData string from window.Telegram.WebApp.initData.

    Returns:
        Parsed dict of initData fields if valid, or None if tampered / expired.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    # Build the data-check-string: sorted key=value lines joined by \n
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    # Secret key = HMAC-SHA256(BOT_TOKEN, "WebAppData")
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    # Reject if initData is older than 1 hour
    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > 3600:
        return None

    return parsed


def _get_telegram_user() -> dict | None:
    """Extract and validate the Telegram user from the Authorization header.

    Expects:  Authorization: tma <raw_initData>

    Returns:
        Parsed user dict or None.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        return None

    init_data = auth_header[4:]
    parsed = _verify_init_data(init_data)
    if parsed is None:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None

    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict.

    Args:
        row: A sqlite3.Row instance.

    Returns:
        Dictionary of column → value.
    """
    return dict(row)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/feed", methods=["GET"])
def feed():
    """Return a paginated list of free content items.

    Query params:
        page     (int, default 1)
        per_page (int, default 20, max 50)

    Returns:
        JSON: { items: [...], page: int, per_page: int }
    """
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(50, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return jsonify({"error": "Invalid pagination parameters."}), 400

    rows = db.get_feed_content(page=page, per_page=per_page)
    return jsonify({
        "items":    [_row_to_dict(r) for r in rows],
        "page":     page,
        "per_page": per_page,
    })


@app.route("/api/creators", methods=["GET"])
def creators():
    """Return all active promoted creators.

    Returns:
        JSON: { creators: [...] }
    """
    rows = db.get_active_creators()
    return jsonify({"creators": [_row_to_dict(r) for r in rows]})


@app.route("/api/content/<int:content_id>", methods=["GET"])
def content_item(content_id: int):
    """Return a single content item.

    For PPV items:
      - If the requesting user has purchased it, the real file_url is returned.
      - Otherwise, file_url is omitted and locked=true is set.

    Returns:
        JSON: content dict (file_url omitted when PPV and not purchased)
    """
    row = db.get_content(content_id)
    if row is None:
        return jsonify({"error": "Not found."}), 404

    item = _row_to_dict(row)

    if row["is_ppv"]:
        user = _get_telegram_user()
        if user and db.has_purchased(user["id"], content_id):
            item["locked"] = False
        else:
            item.pop("file_url", None)
            item["locked"] = True

    return jsonify(item)


@app.route("/api/purchase/verify", methods=["POST"])
def purchase_verify():
    """Record a completed Stars purchase and return the content URL.

    Body JSON:
        { "content_id": int, "stars_paid": int }

    Headers:
        Authorization: tma <initData>

    Returns:
        JSON: { "file_url": str } on success
              { "error": str }   on failure
    """
    user = _get_telegram_user()
    if user is None:
        return jsonify({"error": "Unauthorized."}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    content_id = data.get("content_id")
    stars_paid = data.get("stars_paid")

    if not isinstance(content_id, int) or not isinstance(stars_paid, int):
        return jsonify({"error": "content_id and stars_paid must be integers."}), 400

    row = db.get_content(content_id)
    if row is None or not row["is_ppv"]:
        return jsonify({"error": "Content not found or not PPV."}), 404

    if stars_paid < row["ppv_price_stars"]:
        return jsonify({"error": "Insufficient Stars paid."}), 402

    db.record_purchase(user["id"], content_id, stars_paid)
    logger.info("Purchase recorded: user=%d content=%d stars=%d", user["id"], content_id, stars_paid)

    return jsonify({"file_url": row["file_url"]})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Simple health-check endpoint.

    Returns:
        JSON: { "status": "ok" }
    """
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=debug)
