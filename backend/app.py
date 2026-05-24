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

from flask import Flask, jsonify, make_response, request, send_from_directory
from flask_cors import CORS

from database import (
    clear_listings,
    get_listing_count,
    get_listings,
    get_property_types,
    get_scrape_history,
    get_zillow_zip_coverage,
    get_zillow_zip_queue,
    init_db,
    log_scrape,
    mark_zillow_zips_scraped,
    upsert_listings,
)
from scraper import (
    CHARLOTTE_AREAS,
    CHARLOTTE_NEIGHBORHOODS,
    CHARLOTTE_ZIP_REGIONS,
    SCRAPE_SOURCES,
    scrape_charlotte_houses,
    scrape_zillow_charlotte,
)

# Fast sources — plain HTTP, no browser needed. Run by default ("all").
_FAST_SOURCES    = ["redfin", "estately", "craigslist", "searchcharlotte"]
# Browser sources — SeleniumBase UC, slower but covers anti-bot sites.
_BROWSER_SOURCES = ["zillow", "realtor", "apartments", "homes"]
# Everything — used when source == "all_full"
_ALL_SOURCES     = _FAST_SOURCES + _BROWSER_SOURCES

# Hard upper cap on pages per scrape — keep large since some sources iterate
# many ZIPs/pages internally and we want as much volume as possible.
_MAX_PAGES_CAP = 30

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
    "progress": {
        "percent": 0,        # 0..100 overall progress across all sources
        "stage":   "",       # current source label
        "step":    0,        # current step within the source
        "total":   0,        # total steps within the source
        "detail":  "",       # short user-facing detail, e.g. "ZIP 28207"
        "source_idx":   0,   # 1-based current source index
        "source_total": 0,   # total sources in this run
    },
}


def _reset_progress() -> None:
    scrape_status["progress"] = {
        "percent": 0, "stage": "", "step": 0, "total": 0, "detail": "",
        "source_idx": 0, "source_total": 0,
    }


def _run_scrape(
    source: str,
    listing_type: str,
    max_pages: int,
    min_price: int | None = None,
    max_price: int | None = None,
) -> None:
    global scrape_status
    started_at = datetime.now(timezone.utc).isoformat()
    type_label = "for sale" if listing_type == "for_sale" else "for rent"
    price_label = ""
    if min_price is not None or max_price is not None:
        lo = f"${min_price:,}" if min_price is not None else "any"
        hi = f"${max_price:,}" if max_price is not None else "any"
        price_label = f" ({lo} – {hi})"

    # "all"      — fast sources only (no browser)
    # "all_full" — fast + browser sources (full coverage, slower)
    if source == "all":
        sources_to_run = _FAST_SOURCES
        label = "All Fast Sources"
    elif source == "all_full":
        sources_to_run = _ALL_SOURCES
        label = "All Sources (incl. browser)"
    else:
        sources_to_run = [source]
        label = SCRAPE_SOURCES.get(source, source)

    with _scrape_lock:
        scrape_status["running"] = True
        scrape_status["message"] = (
            f"Scraping {label} — {type_label} listings in Charlotte{price_label}…"
        )
        _reset_progress()
        scrape_status["progress"]["source_total"] = len(sources_to_run)

    total_found = 0
    total_new   = 0
    errors      = []

    for src_idx, src in enumerate(sources_to_run, 1):
        src_label = SCRAPE_SOURCES.get(src, src)
        with _scrape_lock:
            scrape_status["message"] = (
                f"Scraping {src_label} ({src_idx}/{len(sources_to_run)}) "
                f"— {type_label}{price_label}…"
            )
            scrape_status["progress"].update({
                "stage":      src_label,
                "source_idx": src_idx,
                "step":       0,
                "total":      0,
                "detail":     "starting…",
                # Coarse percent before within-source steps come in
                "percent":    round(((src_idx - 1) / len(sources_to_run)) * 100, 1),
            })

        # Per-source progress callback: blend within-source progress into
        # the overall percent so the bar moves smoothly across all sources.
        def _make_cb(idx=src_idx, total_srcs=len(sources_to_run), label=src_label):
            def _cb(step: int, total: int, detail: str) -> None:
                inner = (step / total) if total > 0 else 0
                overall = ((idx - 1) + inner) / total_srcs
                with _scrape_lock:
                    scrape_status["progress"].update({
                        "stage":   label,
                        "step":    step,
                        "total":   total,
                        "detail":  detail,
                        "percent": round(overall * 100, 1),
                    })
            return _cb

        try:
            # Fast plain-HTTP sources can handle many more pages without
            # slowing the scrape — give them a higher floor.
            _SRC_MIN_PAGES = {"searchcharlotte": 25, "estately": 12, "redfin": 10, "zillow": 2}
            src_max_pages = max(max_pages, _SRC_MIN_PAGES.get(src, 0))

            # Zillow uses ZIP rotation: pick the oldest-scraped ZIPs this run
            # so full coverage builds up across multiple runs without triggering
            # Zillow's IP-level rate limiter on a single session.
            zip_subset = None
            if src == "zillow":
                zip_subset = get_zillow_zip_queue(max_zips=5)
                logger.info("[zillow] ZIP queue for this run: %s", zip_subset)

            listings, succeeded_zips = (
                scrape_zillow_charlotte(
                    listing_type, src_max_pages,
                    min_price=min_price, max_price=max_price,
                    progress_cb=_make_cb(), zip_subset=zip_subset,
                )
                if src == "zillow"
                else (
                    scrape_charlotte_houses(
                        src, listing_type, src_max_pages,
                        min_price=min_price, max_price=max_price,
                        progress_cb=_make_cb(),
                    ),
                    [],
                )
            )

            # Mark only the ZIPs that returned results; blocked ones stay
            # oldest so they get priority on the next run.
            if src == "zillow" and succeeded_zips:
                mark_zillow_zips_scraped(succeeded_zips)

            new_count = upsert_listings(listings)
            completed = datetime.now(timezone.utc).isoformat()
            log_scrape(src, len(listings), new_count, started_at, completed, "success")
            total_found += len(listings)
            total_new   += new_count
            logger.info("[%s] %d listings, %d new", src, len(listings), new_count)
        except Exception as exc:  # noqa: BLE001
            completed = datetime.now(timezone.utc).isoformat()
            log_scrape(src, 0, 0, started_at, completed, "error", str(exc))
            errors.append(f"{src_label}: {exc}")
            logger.error("[%s] scrape failed: %s", src, exc)

    final_completed = datetime.now(timezone.utc).isoformat()
    if errors:
        msg = f"Done with errors. Found {total_found} listings, {total_new} new. Errors: {'; '.join(errors)}"
    else:
        msg = f"Done. Found {total_found} listings, {total_new} new."
    logger.info(msg)
    with _scrape_lock:
        scrape_status["message"]  = msg
        scrape_status["last_run"] = final_completed
        scrape_status["running"]  = False
        scrape_status["progress"].update({
            "percent": 100, "stage": "done", "step": 0, "total": 0, "detail": "",
        })


# ---------------------------------------------------------------------------
# Routes – static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ---------------------------------------------------------------------------
# Routes – API
# ---------------------------------------------------------------------------

def _resolve_zips(neighborhood: str | None, zips_param: str | None) -> list[str] | None:
    """
    Resolve a ZIP-list filter from either:
      - a neighborhood key (mapped via CHARLOTTE_NEIGHBORHOODS), or
      - a raw comma-separated `zips=28203,28209` query string.
    """
    if neighborhood and neighborhood in CHARLOTTE_NEIGHBORHOODS:
        return list(CHARLOTTE_NEIGHBORHOODS[neighborhood]["zips"])
    if zips_param:
        return [z.strip() for z in zips_param.split(",") if z.strip().isdigit()]
    return None


@app.route("/api/listings", methods=["GET"])
def api_listings():
    city          = request.args.get("city")          or None
    bedrooms      = request.args.get("bedrooms")      or None
    bathrooms     = request.args.get("bathrooms")     or None
    min_price     = request.args.get("min_price")     or None
    max_price     = request.args.get("max_price")     or None
    listing_type  = request.args.get("listing_type")  or None
    source        = request.args.get("source")        or None
    property_type = request.args.get("property_type") or None
    min_sqft      = request.args.get("min_sqft")      or None
    max_sqft      = request.args.get("max_sqft")      or None
    sort_by       = request.args.get("sort_by")       or None
    search        = request.args.get("search")        or None
    zip_filter    = request.args.get("zip_filter")    or None
    zips          = _resolve_zips(request.args.get("neighborhood"), request.args.get("zips"))
    limit         = min(int(request.args.get("limit",  50)), 200)
    offset        = max(int(request.args.get("offset",  0)),   0)

    data  = get_listings(
        city=city, bedrooms=bedrooms, bathrooms=bathrooms,
        min_price=min_price, max_price=max_price,
        limit=limit, offset=offset, listing_type=listing_type, source=source,
        zips=zips, property_type=property_type,
        min_sqft=min_sqft, max_sqft=max_sqft, sort_by=sort_by, search=search,
        zip_filter=zip_filter,
    )
    total = get_listing_count(
        city=city, bedrooms=bedrooms, bathrooms=bathrooms,
        min_price=min_price, max_price=max_price,
        listing_type=listing_type, source=source, zips=zips,
        property_type=property_type, min_sqft=min_sqft, max_sqft=max_sqft,
        search=search, zip_filter=zip_filter,
    )

    return jsonify({"listings": data, "total": total, "limit": limit, "offset": offset})


@app.route("/api/export.csv", methods=["GET"])
def api_export_csv():
    """Export all matching listings (respecting current filters) as a CSV download."""
    import csv as csv_mod
    import io

    city          = request.args.get("city")          or None
    bedrooms      = request.args.get("bedrooms")      or None
    bathrooms     = request.args.get("bathrooms")     or None
    min_price     = request.args.get("min_price")     or None
    max_price     = request.args.get("max_price")     or None
    listing_type  = request.args.get("listing_type")  or None
    source        = request.args.get("source")        or None
    property_type = request.args.get("property_type") or None
    min_sqft      = request.args.get("min_sqft")      or None
    max_sqft      = request.args.get("max_sqft")      or None
    sort_by       = request.args.get("sort_by")       or None
    zips          = _resolve_zips(request.args.get("neighborhood"), request.args.get("zips"))

    rows = get_listings(
        city=city, bedrooms=bedrooms, bathrooms=bathrooms,
        min_price=min_price, max_price=max_price,
        limit=5000, offset=0, listing_type=listing_type, source=source,
        zips=zips, property_type=property_type,
        min_sqft=min_sqft, max_sqft=max_sqft, sort_by=sort_by,
    )

    COLS = [
        "title", "price", "location", "bedrooms", "bathrooms", "sqft",
        "property_type", "listing_type", "source", "city", "zip",
        "date_posted", "url",
    ]

    output = io.StringIO()
    writer = csv_mod.DictWriter(output, fieldnames=COLS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = 'attachment; filename="charlotte_listings.csv"'
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response


@app.route("/api/property-types", methods=["GET"])
def api_property_types():
    """Return distinct property types present in the database."""
    return jsonify(get_property_types())


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    with _scrape_lock:
        if scrape_status["running"]:
            return jsonify({"error": "A scrape is already in progress."}), 409

    body         = request.get_json(silent=True) or {}
    source       = body.get("source", "all_full")
    listing_type = body.get("listing_type", "for_sale")
    max_pages    = min(int(body.get("max_pages", 5)), _MAX_PAGES_CAP)

    # Optional price filter — pushed down to source URLs where supported.
    def _parse_price(v):
        if v in (None, "", "null"):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    min_price = _parse_price(body.get("min_price"))
    max_price = _parse_price(body.get("max_price"))

    if source not in ("all", "all_full") and source not in SCRAPE_SOURCES:
        return jsonify({"error": f"Unknown source '{source}'."}), 400
    if listing_type not in ("for_sale", "for_rent"):
        return jsonify({"error": f"Unknown listing_type '{listing_type}'."}), 400

    source_label = {
        "all":      "All Fast Sources",
        "all_full": "All Sources (incl. browser)",
    }.get(source) or SCRAPE_SOURCES.get(source, source)

    thread = threading.Thread(
        target=_run_scrape,
        args=(source, listing_type, max_pages, min_price, max_price),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "message":      f"Scrape started — {source_label}, {listing_type}.",
        "source":       source,
        "listing_type": listing_type,
        "min_price":    min_price,
        "max_price":    max_price,
    })


@app.route("/api/status", methods=["GET"])
def api_status():
    with _scrape_lock:
        return jsonify(dict(scrape_status))


@app.route("/api/listings", methods=["DELETE"])
def api_clear_listings():
    deleted = clear_listings()
    return jsonify({"deleted": deleted})


@app.route("/api/history", methods=["GET"])
def api_history():
    return jsonify(get_scrape_history())


@app.route("/api/cities", methods=["GET"])
def api_cities():
    """Return Charlotte-area filter options."""
    cities = [{"value": k, "label": v} for k, v in CHARLOTTE_AREAS.items()]
    return jsonify(cities)


@app.route("/api/neighborhoods", methods=["GET"])
def api_neighborhoods():
    """Return curated Charlotte neighborhood -> ZIP-list filter options."""
    return jsonify([
        {"value": k, "label": v["label"], "zips": v["zips"]}
        for k, v in CHARLOTTE_NEIGHBORHOODS.items()
    ])


@app.route("/api/zillow-zips", methods=["GET"])
def api_zillow_zips():
    """Return Zillow ZIP coverage — when each ZIP was last successfully scraped."""
    return jsonify(get_zillow_zip_coverage())


@app.route("/api/geocode-status", methods=["GET"])
def api_geocode_status():
    from database import get_ungeocoded_listings
    import sqlite3
    with get_connection() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        coded   = conn.execute("SELECT COUNT(*) FROM listings WHERE lat IS NOT NULL AND lng IS NOT NULL").fetchone()[0]
    return jsonify({"total": total, "geocoded": coded, "pending": total - coded})


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=False, host="0.0.0.0", port=5002)
