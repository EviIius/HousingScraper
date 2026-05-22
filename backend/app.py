"""
Flask REST API – serves both the JSON API and the static frontend files.

Endpoints
---------
GET  /                      → frontend index.html
GET  /api/listings          → paginated listing results
POST /api/scrape            → start a background scrape job
GET  /api/status            → scrape-job status
GET  /api/history           → scrape log (last N runs)
GET  /api/cities            → available Charlotte area filters
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from database import (
    get_listing_count,
    get_listings,
    get_scrape_history,
    init_db,
    log_scrape,
    upsert_listings,
)
from scraper import CHARLOTTE_AREAS, SCRAPE_SOURCES, CHARLOTTE_ZIP_REGIONS, scrape_charlotte_houses

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="/")
CORS(app)

# ---------------------------------------------------------------------------
# Scrape state
# ---------------------------------------------------------------------------

_scrape_lock = threading.Lock()
scrape_status: dict = {
    "running":  False,
    "last_run": None,
    "message":  "No scrape run yet.",
}


def _run_scrape(source: str, listing_type: str, max_pages: int) -> None:
    global scrape_status
    started_at = datetime.now(timezone.utc).isoformat()

    source_label = SCRAPE_SOURCES.get(source, source)
    type_label   = "for sale" if listing_type == "for_sale" else "for rent"

    with _scrape_lock:
        scrape_status["running"] = True
        scrape_status["message"] = (
            f"Scraping {source_label} — {type_label} listings in Charlotte…"
        )

    try:
        listings   = scrape_charlotte_houses(source, listing_type, max_pages)
        new_count  = upsert_listings(listings)
        completed  = datetime.now(timezone.utc).isoformat()
        log_scrape(source, len(listings), new_count, started_at, completed, "success")
        msg = f"Done. Found {len(listings)} listings, {new_count} new."
        logger.info(msg)
        with _scrape_lock:
            scrape_status["message"]  = msg
            scrape_status["last_run"] = completed
    except Exception as exc:  # noqa: BLE001
        completed = datetime.now(timezone.utc).isoformat()
        log_scrape(source, 0, 0, started_at, completed, "error", str(exc))
        msg = f"Scrape failed: {exc}"
        logger.error(msg)
        with _scrape_lock:
            scrape_status["message"] = msg
    finally:
        with _scrape_lock:
            scrape_status["running"] = False


# ---------------------------------------------------------------------------
# Routes – static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ---------------------------------------------------------------------------
# Routes – API
# ---------------------------------------------------------------------------

@app.route("/api/listings", methods=["GET"])
def api_listings():
    city         = request.args.get("city")         or None
    bedrooms     = request.args.get("bedrooms")     or None
    bathrooms    = request.args.get("bathrooms")    or None
    min_price    = request.args.get("min_price")    or None
    max_price    = request.args.get("max_price")    or None
    listing_type = request.args.get("listing_type") or None
    limit        = min(int(request.args.get("limit",  50)), 200)
    offset       = max(int(request.args.get("offset",  0)),   0)

    data  = get_listings(
        city=city, bedrooms=bedrooms, bathrooms=bathrooms,
        min_price=min_price, max_price=max_price,
        limit=limit, offset=offset, listing_type=listing_type,
    )
    total = get_listing_count(
        city=city, bathrooms=bathrooms,
        min_price=min_price, max_price=max_price,
        listing_type=listing_type,
    )

    return jsonify({"listings": data, "total": total, "limit": limit, "offset": offset})


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    with _scrape_lock:
        if scrape_status["running"]:
            return jsonify({"error": "A scrape is already in progress."}), 409

    body         = request.get_json(silent=True) or {}
    source       = body.get("source", "redfin")
    listing_type = body.get("listing_type", "for_sale")
    max_pages    = min(int(body.get("max_pages", 2)), 5)

    if source not in SCRAPE_SOURCES:
        return jsonify({"error": f"Unknown source '{source}'."}), 400
    if listing_type not in ("for_sale", "for_rent"):
        return jsonify({"error": f"Unknown listing_type '{listing_type}'."}), 400

    thread = threading.Thread(
        target=_run_scrape,
        args=(source, listing_type, max_pages),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "message":      f"Scrape started — {SCRAPE_SOURCES[source]}, {listing_type}.",
        "source":       source,
        "listing_type": listing_type,
    })


@app.route("/api/status", methods=["GET"])
def api_status():
    with _scrape_lock:
        return jsonify(dict(scrape_status))


@app.route("/api/history", methods=["GET"])
def api_history():
    return jsonify(get_scrape_history())


@app.route("/api/cities", methods=["GET"])
def api_cities():
    """Return Charlotte-area filter options."""
    cities = [{"value": k, "label": v} for k, v in CHARLOTTE_AREAS.items()]
    return jsonify(cities)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=5000)
