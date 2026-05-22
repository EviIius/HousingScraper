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
            """
        )
        # Migrate older databases that are missing the new columns
        _add_column_if_missing(conn, "listings", "bathrooms",     "TEXT")
        _add_column_if_missing(conn, "listings", "listing_type",  "TEXT DEFAULT 'for_sale'")
        _add_column_if_missing(conn, "listings", "property_type", "TEXT")
    logger.info("Database initialised at %s", db_path)


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
                         date_posted, date_scraped, source, city,
                         listing_type, property_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> list[dict]:
    """Return listings with optional filters."""
    query  = "SELECT * FROM listings WHERE 1=1"
    params: list = []

    if city:
        query += " AND city = ?"
        params.append(city)
    if bedrooms:
        query += " AND bedrooms = ?"
        params.append(bedrooms)
    if listing_type:
        query += " AND listing_type = ?"
        params.append(listing_type)
    if bathrooms:
        query += " AND CAST(COALESCE(bathrooms,'0') AS REAL) >= ?"
        params.append(float(bathrooms))
    if min_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) >= ?"
        params.append(float(min_price))
    if max_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) <= ?"
        params.append(float(max_price))

    query += " ORDER BY date_scraped DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_listing_count(
    city: str | None = None,
    bathrooms: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    db_path: str = DB_PATH,
    listing_type: str | None = None,
) -> int:
    """Return total number of listings (optionally filtered)."""
    query  = "SELECT COUNT(*) AS count FROM listings WHERE 1=1"
    params: list = []

    if city:
        query += " AND city = ?"
        params.append(city)
    if listing_type:
        query += " AND listing_type = ?"
        params.append(listing_type)
    if bathrooms:
        query += " AND CAST(COALESCE(bathrooms,'0') AS REAL) >= ?"
        params.append(float(bathrooms))
    if min_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) >= ?"
        params.append(float(min_price))
    if max_price:
        query += " AND CAST(REPLACE(COALESCE(price,'0'),',','') AS REAL) <= ?"
        params.append(float(max_price))

    with get_connection(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return row["count"]


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
