"""
House listing scraper for Charlotte, NC area.

Source: Redfin GIS CSV endpoint.
One visit to the Charlotte search page sets the session cookies that
allow the GIS CSV endpoint to return results for individual ZIP codes.
"""

import csv
import io
import json
import logging
import random
import re
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Charlotte, NC area ZIP codes → Redfin region_ids (region_type=2)
# Discovered by fetching https://www.redfin.com/zipcode/{ZIP} and extracting
# the embedded "region_id=" parameter from the page HTML.
# ---------------------------------------------------------------------------
CHARLOTTE_ZIP_REGIONS: dict[int, int] = {
    28202: 11345,   # Uptown / Center City
    28203: 11346,   # South End / Dilworth
    28204: 11347,   # Elizabeth / Myers Park
    28205: 11348,   # Plaza Midwood / NoDa
    28206: 11349,   # North Charlotte
    28207: 11350,   # Myers Park
    28208: 11351,   # West Charlotte
    28209: 11352,   # Sedgefield / Madison Park
    28210: 11353,   # South Charlotte / Carmel
    28211: 11354,   # Cotswold / Eastover
    28212: 11355,   # East Charlotte / Mint Hill
    28213: 11356,   # University City
    28214: 11357,   # Steele Creek / West Charlotte
    28215: 11358,   # Hickory Ridge / East Charlotte
    28216: 11359,   # Coulwood / NW Charlotte
    28217: 11360,   # Westerly Hills / Airport area
    28226: 11368,   # Ballantyne / Pineville
    28227: 11369,   # Matthews / East Charlotte
    28262: 11393,   # University City / NE Charlotte
    28269: 11397,   # Huntersville / N Charlotte
    28270: 11398,   # Matthews
    28273: 11401,   # Steele Creek / SW Charlotte
    28277: 11404,   # Ballantyne
    28278: 11405,   # Lake Wylie / SW Charlotte
    28134: 11320,   # Pineville
    28105: 11298,   # Matthews
    28104: 11297,   # Matthews / Stallings
}

# Friendly area labels for the filter dropdown
CHARLOTTE_AREAS: dict[str, str] = {
    "charlotte":    "Charlotte",
    "matthews":     "Matthews",
    "weddington":   "Weddington",
    "indian trail": "Indian Trail",
    "pineville":    "Pineville",
    "huntersville": "Huntersville",
    "mint hill":    "Mint Hill",
    "cornelius":    "Cornelius",
    "davidson":     "Davidson",
    "concord":      "Concord",
    "harrisburg":   "Harrisburg",
    "stallings":    "Stallings",
}

SCRAPE_SOURCES: dict[str, str] = {
    "redfin":  "Redfin",
    "realtor": "Realtor.com",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}


def scrape_redfin_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 1,
    zip_codes: list[int] | None = None,
) -> list[dict]:
    """
    Scrape Redfin house listings for Charlotte, NC.

    Strategy
    --------
    1. Visit https://www.redfin.com/NC/Charlotte to acquire session cookies —
       this is required for the GIS CSV endpoint to return results.
    2. For each Charlotte ZIP code, call the GIS CSV endpoint with the
       verified region_id.

    Parameters
    ----------
    listing_type : 'for_sale' | 'for_rent'
    max_pages    : pages per ZIP (1 page ≈ 200 listings; usually enough per ZIP)
    zip_codes    : specific ZIPs to query; defaults to all Charlotte ZIPs
    """
    status = "9" if listing_type == "for_sale" else "130"
    zips   = zip_codes if zip_codes is not None else list(CHARLOTTE_ZIP_REGIONS.keys())
    now    = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    # ── Step 1: warm-up visit to set session cookies ────────────────────────
    # Use Mecklenburg County page — stable URL that reliably returns 200.
    try:
        warm = session.get(
            "https://www.redfin.com/county/2066/NC/Mecklenburg-County", timeout=20
        )
        logger.info("[redfin] warm-up status %d", warm.status_code)
        time.sleep(random.uniform(2.0, 3.5))
    except Exception as exc:
        logger.warning("[redfin] warm-up failed: %s", exc)

    # ── Step 2: fetch CSV for each ZIP ──────────────────────────────────────
    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for zipcode in zips:
        region_id = CHARLOTTE_ZIP_REGIONS.get(zipcode)
        if not region_id:
            continue

        for page in range(1, max_pages + 1):
            params = {
                "al":          1,
                "market":      "charlotte",
                "num_homes":   200,
                "ord":         "redfin-recommended-asc",
                "page_number": page,
                "region_id":   region_id,
                "region_type": 2,
                "status":      status,
                "uipt":        "1,2,3,4,5,6,7,8",
                "v":           8,
            }

            try:
                resp = session.get(
                    "https://www.redfin.com/stingray/api/gis-csv",
                    params=params,
                    timeout=20,
                    headers={
                        **_HEADERS,
                        "Referer": f"https://www.redfin.com/zipcode/{zipcode}",
                        "Accept":  "text/csv,*/*",
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.error("[redfin] ZIP %s page %d failed: %s", zipcode, page, exc)
                break

            rows = _parse_redfin_csv(resp.text, listing_type, now, seen_urls)
            if not rows:
                break
            all_listings.extend(rows)
            logger.info("[redfin] ZIP %s page %d: %d listings", zipcode, page, len(rows))

            # Polite delay between ZIP requests
            time.sleep(random.uniform(1.5, 3.0))

    logger.info("[redfin] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_redfin_csv(
    raw: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    """
    Parse Redfin GIS CSV.

    The first line is the header (starts with 'SALE TYPE').
    Any line starting with a double-quote is a Redfin disclaimer and is skipped.
    """
    lines = raw.splitlines()

    # Locate header row
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.upper().startswith("SALE TYPE")),
        None,
    )
    if header_idx is None:
        return []

    # Drop disclaimer lines (wrapped in double quotes)
    data_lines = [
        ln for ln in lines[header_idx + 1:]
        if ln.strip() and not ln.startswith('"')
    ]
    if not data_lines:
        return []

    csv_text = lines[header_idx] + "\n" + "\n".join(data_lines)
    reader   = csv.DictReader(io.StringIO(csv_text))

    # The URL column has a very long name — find it once
    url_key: str | None = None
    out: list[dict] = []

    for row in reader:
        if url_key is None:
            url_key = next(
                (k for k in row if k.strip().upper().startswith("URL")), ""
            )

        address  = (row.get("ADDRESS")              or "").strip()
        city_raw = (row.get("CITY")                 or "").strip()
        state    = (row.get("STATE OR PROVINCE")    or "").strip()
        price    = (row.get("PRICE")                or "").strip()
        beds     = (row.get("BEDS")                 or "").strip()
        baths    = (row.get("BATHS")                or "").strip()
        sqft_raw = (row.get("SQUARE FEET")          or "").strip()
        url      = (row.get(url_key, "") or "").strip() if url_key else ""
        listed   = (row.get("ORIGINAL DATE LISTED") or "").strip()
        prop_raw = (row.get("PROPERTY TYPE")        or "").strip().lower()
        status   = (row.get("STATUS")               or "").strip().lower()

        # Skip non-active or duplicate listings
        if not address or not url:
            continue
        if url in seen_urls:
            continue
        if status and status not in ("active", "active contingent", "coming soon"):
            continue

        seen_urls.add(url)

        out.append({
            "title":         f"{address} – {city_raw}, {state}",
            "price":         price,
            "location":      f"{address}, {city_raw}, {state}",
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft_raw),
            "url":           url,
            "date_posted":   listed,
            "date_scraped":  scraped_at,
            "source":        "redfin",
            "city":          _norm_city(city_raw),
            "listing_type":  listing_type,
            "property_type": prop_raw,
        })

    return out


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

# Known Charlotte-metro city names used to strip builder code prefixes
# (new-construction listings sometimes have a code like "p16mo9 weddington")
_KNOWN_CITIES: set[str] = {
    "charlotte", "matthews", "pineville", "huntersville", "mint hill",
    "cornelius", "davidson", "concord", "harrisburg", "stallings",
    "weddington", "waxhaw", "fort mill", "rock hill", "lake wylie",
    "ballantyne", "belmont", "mooresville", "denver", "iron station",
    "mount holly", "gastonia", "kannapolis", "indian trail", "marvin",
    "monroe", "wesley chapel", "cramerton", "lowell", "bessemer city",
    "uninc",
}


def _norm_city(val: str) -> str:
    """Return a clean lower-case city name, stripping any builder code prefix."""
    city = val.strip().lower()
    if not city:
        return "charlotte"
    if city in _KNOWN_CITIES:
        return city
    # Strip leading code word (e.g. "p16mo9 weddington" → "weddington")
    parts = city.split(" ", 1)
    if len(parts) == 2 and parts[1] in _KNOWN_CITIES:
        return parts[1]
    return city


def _norm_beds(val: str) -> str:
    v = val.strip()
    if v and v not in ("—", "N/A"):
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    return ""


def _norm_baths(val: str) -> str:
    v = val.strip()
    if v and v not in ("—", "N/A"):
        try:
            n = float(v)
            return str(int(n)) if n == int(n) else str(n)
        except ValueError:
            return v
    return ""


def _norm_sqft(val: str) -> str:
    v = val.strip().replace(",", "")
    if v and v not in ("—", "N/A"):
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    return ""


# ---------------------------------------------------------------------------
# Realtor.com scraper
# ---------------------------------------------------------------------------

_REALTOR_HEADERS = {
    **_HEADERS,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Upgrade-Insecure-Requests": "1",
}

_REALTOR_BASE = "https://www.realtor.com"


def scrape_realtor_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 3,
) -> list[dict]:
    """
    Scrape Realtor.com for Charlotte, NC listings using __NEXT_DATA__ JSON.

    Realtor.com embeds full listing data in a <script id="__NEXT_DATA__"> tag.
    If the site returns 429 (rate limited) the function returns an empty list
    and logs a warning rather than raising.
    """
    search_slug = "Charlotte_NC" if listing_type == "for_sale" else "Charlotte_NC"
    path_prefix = "realestateandhomes-search" if listing_type == "for_sale" else "apartments"
    now = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update(_REALTOR_HEADERS)

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for page in range(1, max_pages + 1):
        url = f"{_REALTOR_BASE}/{path_prefix}/{search_slug}/pg-{page}"
        try:
            resp = session.get(url, timeout=25)
        except Exception as exc:
            logger.warning("[realtor] request failed page %d: %s", page, exc)
            break

        if resp.status_code == 429:
            logger.warning(
                "[realtor] rate-limited (429) on page %d — "
                "Realtor.com is blocking automated requests. "
                "Try again later or use a different source.",
                page,
            )
            break
        if resp.status_code != 200:
            logger.warning("[realtor] unexpected status %d on page %d", resp.status_code, page)
            break

        # Extract __NEXT_DATA__ JSON
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not m:
            logger.warning("[realtor] no __NEXT_DATA__ found on page %d", page)
            break

        try:
            data = json.loads(m.group(1))
        except Exception:
            logger.warning("[realtor] failed to parse __NEXT_DATA__ on page %d", page)
            break

        rows = _parse_realtor_next_data(data, listing_type, now, seen_urls)
        if not rows:
            logger.info("[realtor] no listings on page %d — stopping", page)
            break

        all_listings.extend(rows)
        logger.info("[realtor] page %d: %d listings", page, len(rows))
        time.sleep(random.uniform(3.0, 5.0))

    logger.info("[realtor] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_realtor_next_data(
    data: dict,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    """Extract listings from Realtor.com __NEXT_DATA__ JSON."""
    # Common paths for listing results
    page_props = data.get("props", {}).get("pageProps", {})

    # Try several known JSON paths used by Realtor.com
    results: list = []
    for path in [
        ["searchResults", "home_search", "results"],
        ["searchResults", "results"],
        ["properties"],
        ["homes"],
    ]:
        node = page_props
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
                break
        if isinstance(node, list) and node:
            results = node
            break

    out: list[dict] = []
    for item in results:
        loc    = item.get("location", {}) or {}
        addr   = loc.get("address", {}) or {}
        desc   = item.get("description", {}) or {}

        address  = addr.get("line", "")
        city_raw = addr.get("city", "")
        state    = addr.get("state_code", "NC")
        price    = str(item.get("list_price", item.get("price", "")) or "")
        beds     = str(desc.get("beds", "") or "")
        baths    = str(desc.get("baths_consolidated", desc.get("baths", "")) or "")
        sqft     = str(desc.get("sqft", "") or "")
        prop_raw = (desc.get("type", "") or "").lower()
        listed   = item.get("list_date", "")
        status   = (item.get("status", "") or "").lower()
        permalink = item.get("permalink", "")
        url      = f"{_REALTOR_BASE}{permalink}" if permalink else ""

        if not address or not url:
            continue
        if url in seen_urls:
            continue
        if status and status not in ("for_sale", "active", "for_rent", ""):
            continue

        seen_urls.add(url)
        out.append({
            "title":         f"{address} \u2013 {city_raw}, {state}",
            "price":         price,
            "location":      f"{address}, {city_raw}, {state}",
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft),
            "url":           url,
            "date_posted":   listed,
            "date_scraped":  scraped_at,
            "source":        "realtor",
            "city":          _norm_city(city_raw),
            "listing_type":  listing_type,
            "property_type": prop_raw,
        })

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_charlotte_houses(
    source: str = "redfin",
    listing_type: str = "for_sale",
    max_pages: int = 1,
) -> list[dict]:
    """
    Main entry point for scraping Charlotte, NC house listings.

    Parameters
    ----------
    source       : 'redfin' | 'realtor'
    listing_type : 'for_sale' | 'for_rent'
    max_pages    : pages to fetch (Redfin: per ZIP; Realtor.com: search pages)
    """
    if source == "realtor":
        return scrape_realtor_charlotte(listing_type, max_pages)
    return scrape_redfin_charlotte(listing_type, max_pages)
