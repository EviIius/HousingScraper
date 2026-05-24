"""
SQLite persistence layer for housing listings.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "listings.db")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def get_connection(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH) -> None:
    """Create tables and migrate existing databases."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT    NOT NULL,
                price         TEXT,
                location      TEXT,
                bedrooms      TEXT,
                bathrooms     TEXT,
                sqft          TEXT,
                url           TEXT    UNIQUE,
                date_posted   TEXT,
                date_scraped  TEXT,
                source        TEXT,
                city          TEXT,
                zip           TEXT,
                listing_type  TEXT    DEFAULT 'for_sale',
                property_type TEXT,
                created_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS scrape_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                city           TEXT,
                listings_found INTEGER,
                listings_new   INTEGER,
                started_at     TEXT,
                completed_at   TEXT,
                status         TEXT,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS zillow_zip_history (
                zip            TEXT PRIMARY KEY,
                last_scraped   TEXT
            );
            """
        )
        # Migrate older databases that are missing the new columns
        _add_column_if_missing(conn, "listings", "bathrooms",     "TEXT")
        _add_column_if_missing(conn, "listings", "listing_type",  "TEXT DEFAULT 'for_sale'")
        _add_column_if_missing(conn, "listings", "property_type", "TEXT")
        _add_column_if_missing(conn, "listings", "zip",           "TEXT")
        _add_column_if_missing(conn, "listings", "lat",           "REAL")
        _add_column_if_missing(conn, "listings", "lng",           "REAL")
        # Backfill ZIP for any pre-existing rows by parsing it out of location/title
        _backfill_zips(conn)
    logger.info("Database initialised at %s", db_path)


def _backfill_zips(conn) -> None:
    """Fill in `zip` for rows that have it embedded in location/title text."""
    import re
    zip_re = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
    rows = conn.execute(
        "SELECT id, title, location FROM listings WHERE zip IS NULL OR zip = ''"
    ).fetchall()
    if not rows:
        return
    updates: list[tuple[str, int]] = []
    for r in rows:
        for src in (r["location"] or "", r["title"] or ""):
            m = zip_re.search(src)
            if m:
                updates.append((m.group(1), r["id"]))
                break
    if updates:
        conn.executemany("UPDATE listings SET zip = ? WHERE id = ?", updates)
        conn.commit()
        logger.info("Backfilled ZIP on %d listings", len(updates))


def _add_column_if_missing(conn, table: str, column: str, col_def: str) -> None:
    """ALTER TABLE to add column if it does not already exist."""
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        conn.commit()
        logger.info("Migrated %s: added column %s", table, column)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def upsert_listings(listings: list[dict], db_path: str = DB_PATH) -> int:
    """
    Insert listings not yet in the database (keyed on URL).

    Returns the number of *new* rows inserted.
    """
    new_count = 0
    with get_connection(db_path) as conn:
        for listing in listings:
            try:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO listings
                        (title, price, location, bedrooms, bathrooms, sqft, url,
                         date_posted, date_scraped, source, city, zip,
                         listing_type, property_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing.get("title"),
                        listing.get("price"),
                        listing.get("location"),
                        listing.get("bedrooms"),
                        listing.get("bathrooms"),
                        listing.get("sqft"),
                        listing.get("url"),
                        listing.get("date_posted"),
                        listing.get("date_scraped"),
                        listing.get("source"),
                        listing.get("city"),
                        listing.get("zip"),
                        listing.get("listing_type", "for_sale"),
                        listing.get("property_type"),
                    ),
                )
                if cursor.rowcount > 0:
                    new_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Error inserting listing: %s", exc)
        conn.commit()
    return new_count


_SORT_SQL = {
    "price_desc": "CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) DESC",
    "price_asc":  "CASE WHEN price IS NULL OR TRIM(price)='' THEN 999999999 ELSE CAST(REPLACE(price,',','') AS REAL) END ASC",
    "sqft_desc":  "CAST(COALESCE(NULLIF(sqft,''),'0') AS REAL) DESC",
    "sqft_asc":   "CAST(COALESCE(NULLIF(sqft,''),'0') AS REAL) ASC",
    "newest":     "date_scraped DESC",
}


def _filter_clauses(
    city, bedrooms, bathrooms, min_price, max_price,
    listing_type, source, zips,
    property_type=None, min_sqft=None, max_sqft=None, search=None, zip_filter=None,
):
    """Build WHERE clauses + params used by both list and count queries."""
    query  = ""
    params: list = []
    if search:
        query += " AND (LOWER(title) LIKE ? OR LOWER(location) LIKE ? OR zip LIKE ?)"
        term = f"%{search.lower()}%"
        params.extend([term, term, term])
    if zip_filter:
        query += " AND zip = ?"
        params.append(zip_filter.strip())
    if city:
        query += " AND city = ?"
        params.append(city)
    if bedrooms:
        query += " AND CAST(COALESCE(NULLIF(bedrooms,''),'0') AS REAL) >= ?"
        params.append(float(bedrooms))
    if listing_type:
        query += " AND listing_type = ?"
        params.append(listing_type)
    if source:
        query += " AND source = ?"
        params.append(source)
    if zips:
        placeholders = ",".join("?" * len(zips))
        query += f" AND zip IN ({placeholders})"
        params.extend(zips)
    if bathrooms:
        query += " AND CAST(COALESCE(bathrooms,'0') AS REAL) >= ?"
        params.append(float(bathrooms))
    if min_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) >= ?"
        params.append(float(min_price))
    if max_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) <= ?"
        params.append(float(max_price))
    if property_type:
        query += " AND LOWER(COALESCE(property_type,'')) LIKE ?"
        params.append(f"%{property_type.lower()}%")
    if min_sqft:
        query += " AND CAST(COALESCE(NULLIF(sqft,''),'0') AS REAL) >= ?"
        params.append(float(min_sqft))
    if max_sqft:
        query += " AND CAST(COALESCE(NULLIF(sqft,''),'0') AS REAL) <= ?"
        params.append(float(max_sqft))
    return query, params


def get_listings(
    city: str | None = None,
    bedrooms: str | None = None,
    bathrooms: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db_path: str = DB_PATH,
    listing_type: str | None = None,
    source: str | None = None,
    zips: list[str] | None = None,
    property_type: str | None = None,
    min_sqft: str | None = None,
    max_sqft: str | None = None,
    sort_by: str | None = None,
    search: str | None = None,
    zip_filter: str | None = None,
) -> list[dict]:
    """Return listings with optional filters."""
    where, params = _filter_clauses(
        city, bedrooms, bathrooms, min_price, max_price,
        listing_type, source, zips,
        property_type=property_type, min_sqft=min_sqft, max_sqft=max_sqft,
        search=search, zip_filter=zip_filter,
    )
    order = _SORT_SQL.get(sort_by or "", "date_scraped DESC")
    query = f"SELECT * FROM listings WHERE 1=1{where} ORDER BY {order} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_listing_count(
    city: str | None = None,
    bedrooms: str | None = None,
    bathrooms: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    db_path: str = DB_PATH,
    listing_type: str | None = None,
    source: str | None = None,
    zips: list[str] | None = None,
    property_type: str | None = None,
    min_sqft: str | None = None,
    max_sqft: str | None = None,
    search: str | None = None,
    zip_filter: str | None = None,
) -> int:
    """Return total number of listings (optionally filtered)."""
    where, params = _filter_clauses(
        city, bedrooms, bathrooms, min_price, max_price,
        listing_type, source, zips,
        property_type=property_type, min_sqft=min_sqft, max_sqft=max_sqft,
        search=search, zip_filter=zip_filter,
    )
    query = "SELECT COUNT(*) AS count FROM listings WHERE 1=1" + where
    with get_connection(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return row["count"]


def clear_listings(db_path: str = DB_PATH) -> int:
    """Delete all rows from the listings table. Returns number of rows deleted."""
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM listings")
        conn.commit()
        return cur.rowcount


def get_ungeocoded_listings(limit: int = 500, db_path: str = DB_PATH) -> list[dict]:
    """Return listings that have no lat/lng stored yet."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, location, zip FROM listings WHERE lat IS NULL OR lng IS NULL LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_listing_coords(listing_id: int, lat: float, lng: float, db_path: str = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute("UPDATE listings SET lat=?, lng=? WHERE id=?", (lat, lng, listing_id))
        conn.commit()


def get_property_types(db_path: str = DB_PATH) -> list[str]:
    """Return distinct non-empty property_type values from the listings table."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT property_type FROM listings "
            "WHERE property_type IS NOT NULL AND TRIM(property_type) != '' "
            "ORDER BY property_type"
        ).fetchall()
        return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Scrape log
# ---------------------------------------------------------------------------

def log_scrape(
    city: str,
    listings_found: int,
    listings_new: int,
    started_at: str,
    completed_at: str,
    status: str,
    error: str | None = None,
    db_path: str = DB_PATH,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scrape_log
                (city, listings_found, listings_new,
                 started_at, completed_at, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (city, listings_found, listings_new, started_at, completed_at, status, error),
        )
        conn.commit()


def get_scrape_history(limit: int = 10, db_path: str = DB_PATH) -> list[dict]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Zillow ZIP rotation
# ---------------------------------------------------------------------------

def get_zillow_zip_queue(max_zips: int = 8, db_path: str = DB_PATH) -> list[int]:
    """
    Return up to max_zips ZIP codes prioritised for the next Zillow scrape.

    ZIPs that have never been scraped come first (NULL last_scraped), then
    the ones whose last_scraped timestamp is oldest. This ensures every ZIP
    cycles through in roughly equal time regardless of how often scrapes run.
    """
    from scraper import CHARLOTTE_ZIP_REGIONS
    all_zips = [str(z) for z in CHARLOTTE_ZIP_REGIONS.keys()]

    with get_connection(db_path) as conn:
        scraped = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT zip, last_scraped FROM zillow_zip_history"
            ).fetchall()
        }

    # Never-scraped ZIPs sort before any ISO timestamp (empty string < any date)
    sorted_zips = sorted(all_zips, key=lambda z: scraped.get(z) or "")
    return [int(z) for z in sorted_zips[:max_zips]]


def mark_zillow_zips_scraped(zips: list[int], db_path: str = DB_PATH) -> None:
    """Record that these ZIPs were attempted in the current scrape run."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO zillow_zip_history (zip, last_scraped) VALUES (?, ?)",
            [(str(z), now) for z in zips],
        )
        conn.commit()


def get_zillow_zip_coverage(db_path: str = DB_PATH) -> list[dict]:
    """Return last-scraped timestamps for all Zillow ZIPs (for the status API)."""
    from scraper import CHARLOTTE_ZIP_REGIONS
    all_zips = [str(z) for z in CHARLOTTE_ZIP_REGIONS.keys()]

    with get_connection(db_path) as conn:
        scraped = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT zip, last_scraped FROM zillow_zip_history"
            ).fetchall()
        }

    return [
        {"zip": z, "last_scraped": scraped.get(z)}
        for z in all_zips
    ]
