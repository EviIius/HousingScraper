"""
Housing listing scrapers for Charlotte, NC and surrounding area.

Sources
-------
- Redfin       : GIS CSV endpoint (cookie-warmed, plain requests)
- Estately     : server-rendered HTML, plain requests
- Craigslist   : server-rendered HTML, plain requests
- Zillow       : SeleniumBase UC -> __NEXT_DATA__ JSON
- Realtor.com  : SeleniumBase UC -> JSON-LD CollectionPage
- Apartments   : SeleniumBase UC -> placard DOM (rentals only)

Why SeleniumBase instead of Playwright?
Playwright depends on the compiled `greenlet` extension, whose .pyd is
blocked by Windows Application Control on some installs. Selenium has no
such dependency and SeleniumBase's "UC Mode" handles Cloudflare and
PerimeterX challenges out of the box.
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
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Charlotte, NC area ZIP codes -> Redfin region_ids (region_type=2)
# Discovered by fetching https://www.redfin.com/zipcode/{ZIP} and extracting
# the embedded "region_id=" parameter from the page HTML.
# ---------------------------------------------------------------------------
CHARLOTTE_ZIP_REGIONS: dict[int, int] = {
    28202: 11345, 28203: 11346, 28204: 11347, 28205: 11348,
    28206: 11349, 28207: 11350, 28208: 11351, 28209: 11352,
    28210: 11353, 28211: 11354, 28212: 11355, 28213: 11356,
    28214: 11357, 28215: 11358, 28216: 11359, 28217: 11360,
    28226: 11368, 28227: 11369, 28262: 11393, 28269: 11397,
    28270: 11398, 28273: 11401, 28277: 11404, 28278: 11405,
    28134: 11320, 28105: 11298, 28104: 11297,
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
    "gastonia":     "Gastonia",
    "rock hill":    "Rock Hill",
    "fort mill":    "Fort Mill",
}

SCRAPE_SOURCES: dict[str, str] = {
    "redfin":         "Redfin",
    "zillow":         "Zillow",
    "realtor":        "Realtor.com",
    "craigslist":     "Craigslist",
    "estately":       "Estately",
    "apartments":     "Apartments.com",
    "searchcharlotte": "SearchCharlotte (BHHS)",
    "homes":          "Homes.com",
}

# Curated Charlotte-area neighborhoods -> ZIP groups. Used by the
# Neighborhood filter on the frontend.
CHARLOTTE_NEIGHBORHOODS: dict[str, dict] = {
    "south_end":      {"label": "South End / LoSo",          "zips": ["28203", "28209"]},
    "uptown":         {"label": "Uptown / Center City",       "zips": ["28202"]},
    "dilworth":       {"label": "Dilworth",                   "zips": ["28203"]},
    "noda":           {"label": "NoDa / Plaza Midwood",       "zips": ["28205"]},
    "myers_park":     {"label": "Myers Park",                 "zips": ["28207", "28209"]},
    "elizabeth":      {"label": "Elizabeth / Chantilly",      "zips": ["28204"]},
    "ballantyne":     {"label": "Ballantyne",                 "zips": ["28277", "28226"]},
    "university":     {"label": "University City",            "zips": ["28213", "28262"]},
    "steele_creek":   {"label": "Steele Creek",               "zips": ["28273", "28278"]},
    "cotswold":       {"label": "Cotswold / Eastover",        "zips": ["28211"]},
    "uc_concord":     {"label": "Concord / Harrisburg",       "zips": ["28025", "28027", "28075"]},
    "huntersville":   {"label": "Huntersville / Cornelius",   "zips": ["28078", "28031"]},
    "matthews":       {"label": "Matthews / Mint Hill",       "zips": ["28105", "28104", "28227"]},
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

_DASH = " – "  # en-dash separator used in titles


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

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
    city = (val or "").strip().lower()
    if not city:
        return "charlotte"
    if city in _KNOWN_CITIES:
        return city
    parts = city.split(" ", 1)
    if len(parts) == 2 and parts[1] in _KNOWN_CITIES:
        return parts[1]
    return city


def _norm_beds(val: str) -> str:
    v = (val or "").strip()
    if v and v not in ("—", "N/A"):
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    return ""


def _norm_baths(val: str) -> str:
    v = (val or "").strip()
    if v and v not in ("—", "N/A"):
        try:
            n = float(v)
            return str(int(n)) if n == int(n) else str(n)
        except ValueError:
            return v
    return ""


def _norm_sqft(val: str) -> str:
    v = (val or "").strip().replace(",", "")
    if v and v not in ("—", "N/A"):
        try:
            return str(int(float(v)))
        except ValueError:
            return v
    return ""


def _camel_to_words(s: str) -> str:
    """SingleFamilyResidence -> 'Single Family Residence'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", s or "")


def _price_in_band(price_raw, min_p: int | None, max_p: int | None) -> bool:
    """
    Return True if a listing's price falls within [min_p, max_p].

    Listings with missing/unparseable prices are kept when neither bound is
    set, but dropped as soon as either bound is set (the user explicitly
    asked for a price range, so price-less listings aren't useful).
    """
    if min_p is None and max_p is None:
        return True
    try:
        p = int(float(str(price_raw or "").replace(",", "").replace("$", "")))
    except (ValueError, TypeError):
        return False
    if min_p is not None and p < min_p:
        return False
    if max_p is not None and p > max_p:
        return False
    return True


def _apply_price_filter(rows: list[dict], min_p: int | None, max_p: int | None) -> list[dict]:
    """Filter a list of scraped listings by price (safety net for sources
    where we couldn't push the filter to the source URL)."""
    if min_p is None and max_p is None:
        return rows
    return [r for r in rows if _price_in_band(r.get("price"), min_p, max_p)]


# A progress callback is `cb(step, total, detail)` where step/total describe
# the current source's loop position and `detail` is a short user-facing
# string (e.g., "ZIP 28207", "page 3"). Always called safely.
def _emit_progress(cb, step: int, total: int, detail: str) -> None:
    if cb is None:
        return
    try:
        cb(step, total, detail)
    except Exception:
        pass


_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _extract_zip(*candidates: str) -> str:
    """Return the first 5-digit ZIP found in any of the candidate strings."""
    for c in candidates:
        if not c:
            continue
        m = _ZIP_RE.search(str(c))
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Shared SeleniumBase UC helper
# ---------------------------------------------------------------------------

_SB_RECONNECT = 6


def _open_uc_session():
    """
    Return a SeleniumBase UC context manager.

    Usage:
        with _open_uc_session() as sb:
            sb.uc_open_with_reconnect(url, reconnect_time=_SB_RECONNECT)
            sb.sleep(4)
            html = sb.get_page_source()
    """
    try:
        from seleniumbase import SB
    except ImportError as exc:
        raise RuntimeError(
            "SeleniumBase is not installed. Run: pip install seleniumbase"
        ) from exc

    return SB(
        uc=True,
        headless=True,
        test=False,
        locale="en-US",
        ad_block=True,
        block_images=True,  # ~3x faster page loads; we don't render images
    )


# ---------------------------------------------------------------------------
# Redfin scraper (GIS CSV endpoint, plain requests)
# ---------------------------------------------------------------------------

def scrape_redfin_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 1,
    zip_codes: list[int] | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """
    Scrape Redfin house listings for Charlotte, NC via the GIS CSV endpoint.

    The GIS CSV endpoint doesn't honor min/max_price params reliably, so we
    fetch everything per ZIP and filter client-side at the end.
    """
    status = "9" if listing_type == "for_sale" else "130"
    zips   = zip_codes if zip_codes is not None else list(CHARLOTTE_ZIP_REGIONS.keys())
    now    = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    try:
        warm = session.get(
            "https://www.redfin.com/county/2066/NC/Mecklenburg-County", timeout=20
        )
        logger.info("[redfin] warm-up status %d", warm.status_code)
        time.sleep(random.uniform(2.0, 3.5))
    except Exception as exc:
        logger.warning("[redfin] warm-up failed: %s", exc)

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for zip_idx, zipcode in enumerate(zips, 1):
        _emit_progress(progress_cb, zip_idx, len(zips), f"ZIP {zipcode}")
        region_id = CHARLOTTE_ZIP_REGIONS.get(zipcode)
        if not region_id:
            continue

        for page in range(1, max_pages + 1):
            params = {
                "al": 1, "market": "charlotte", "num_homes": 200,
                "ord": "redfin-recommended-asc", "page_number": page,
                "region_id": region_id, "region_type": 2,
                "status": status, "uipt": "1,2,3,4,5,6,7,8", "v": 8,
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
            time.sleep(random.uniform(1.5, 3.0))

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[redfin] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_redfin_csv(
    raw: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    lines = raw.splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.upper().startswith("SALE TYPE")),
        None,
    )
    if header_idx is None:
        return []

    data_lines = [
        ln for ln in lines[header_idx + 1:]
        if ln.strip() and not ln.startswith('"')
    ]
    if not data_lines:
        return []

    csv_text = lines[header_idx] + "\n" + "\n".join(data_lines)
    reader   = csv.DictReader(io.StringIO(csv_text))

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
        zipcode  = (row.get("ZIP OR POSTAL CODE")   or "").strip()
        price    = (row.get("PRICE")                or "").strip()
        beds     = (row.get("BEDS")                 or "").strip()
        baths    = (row.get("BATHS")                or "").strip()
        sqft_raw = (row.get("SQUARE FEET")          or "").strip()
        url      = (row.get(url_key, "") or "").strip() if url_key else ""
        listed   = (row.get("ORIGINAL DATE LISTED") or "").strip()
        prop_raw = (row.get("PROPERTY TYPE")        or "").strip().lower()
        status   = (row.get("STATUS")               or "").strip().lower()

        if not address or not url or url in seen_urls:
            continue
        if status and status not in ("active", "active contingent", "coming soon"):
            continue

        zip_clean = _extract_zip(zipcode, address)
        seen_urls.add(url)
        out.append({
            "title":         f"{address}{_DASH}{city_raw}, {state} {zip_clean}".rstrip(),
            "price":         price,
            "location":      f"{address}, {city_raw}, {state} {zip_clean}".rstrip(),
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft_raw),
            "url":           url,
            "date_posted":   listed,
            "date_scraped":  scraped_at,
            "source":        "redfin",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_raw,
        })

    return out


# ---------------------------------------------------------------------------
# Realtor.com scraper (SeleniumBase UC + JSON-LD)
# ---------------------------------------------------------------------------

_REALTOR_BASE = "https://www.realtor.com"

# Charlotte metro area slugs — covers the full market, not just city limits.
# One browser session is reused across all cities to avoid repeated cold starts.
_REALTOR_METRO_CITIES = [
    "Charlotte_NC",
    "Matthews_NC",
    "Huntersville_NC",
    "Mint-Hill_NC",
    "Pineville_NC",
    "Concord_NC",
    "Cornelius_NC",
    "Davidson_NC",
    "Weddington_NC",
    "Indian-Trail_NC",
    "Stallings_NC",
    "Gastonia_NC",
]


def scrape_realtor_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 3,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """
    Scrape Realtor.com for Charlotte metro listings.

    Iterates over _REALTOR_METRO_CITIES within a single browser session so
    we cover the full metro area — not just the Charlotte city-limits page.
    Price filter is pushed into the URL path so every result is in-band.
    """
    path_prefix = "realestateandhomes-search" if listing_type == "for_sale" else "apartments"
    now = datetime.now(timezone.utc).isoformat()

    if min_price is not None or max_price is not None:
        lo = min_price if min_price is not None else 0
        hi = max_price if max_price is not None else 50_000_000
        price_seg = f"/price-{lo}-{hi}"
    else:
        price_seg = ""

    cities = _REALTOR_METRO_CITIES
    total_steps = len(cities) * max_pages

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()
    step = 0

    try:
        with _open_uc_session() as sb:
            for city_slug in cities:
                for page in range(1, max_pages + 1):
                    step += 1
                    _emit_progress(progress_cb, step, total_steps,
                                   f"{city_slug.replace('_NC','').replace('-',' ')} p{page}")
                    page_seg = "" if page == 1 else f"/pg-{page}"
                    url = f"{_REALTOR_BASE}/{path_prefix}/{city_slug}{price_seg}{page_seg}"
                    try:
                        sb.uc_open_with_reconnect(url, reconnect_time=_SB_RECONNECT)
                        sb.sleep(4)
                    except Exception as exc:
                        logger.warning("[realtor] nav failed %s p%d: %s", city_slug, page, exc)
                        break

                    html = sb.get_page_source()
                    rows = _parse_realtor_html(html, listing_type, now, seen_urls)
                    if not rows:
                        logger.info("[realtor] no listings %s p%d — next city", city_slug, page)
                        break
                    all_listings.extend(rows)
                    logger.info("[realtor] %s p%d: %d listings (total %d)",
                                city_slug, page, len(rows), len(all_listings))
                    time.sleep(random.uniform(2.0, 3.5))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error("[realtor] browser session failed: %s", exc)

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[realtor] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_realtor_html(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    """
    Parse Realtor.com search results.

    Priority: __NEXT_DATA__ (richest — includes baths + proper prop_type)
    → JSON-LD → DOM card fallback.
    """
    rows = _parse_realtor_next_data(html, listing_type, scraped_at, seen_urls)
    if rows:
        return rows
    rows = _parse_realtor_jsonld(html, listing_type, scraped_at, seen_urls)
    if rows:
        return rows
    return _parse_realtor_dom(html, listing_type, scraped_at, seen_urls)


def _parse_realtor_next_data(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    """
    Parse Realtor.com __NEXT_DATA__ JSON blob (Next.js SSR).

    This is the richest data source: includes proper `prop_type` ("condo",
    "single_family", etc.) and full bath counts that JSON-LD omits.
    """
    soup = BeautifulSoup(html, "lxml")
    nd   = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        return []
    try:
        data = json.loads(nd.string)
    except Exception:
        return []

    pp = data.get("props", {}).get("pageProps", {})

    # Try several known paths for the property list
    candidates: list = []
    for path in (
        ["searchResults", "properties"],
        ["properties"],
        ["initialProps", "searchResults", "properties"],
        ["searchResults", "results"],
    ):
        node = pp
        for key in path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, list) and node:
            candidates = node
            break

    if not candidates:
        return []

    _PROP_TYPE_MAP = {
        "single_family":       "single family residence",
        "condos":              "condo",
        "condo":               "condo",
        "condominium":         "condo",
        "townhomes":           "townhouse",
        "townhouse":           "townhouse",
        "townhome":            "townhouse",
        "multi_family":        "multi family",
        "land":                "land",
        "mobile":              "mobile home",
        "manufactured":        "mobile home",
        "apartment":           "apartment",
    }

    out: list[dict] = []
    for item in candidates:
        addr   = item.get("address") or {}
        street = (addr.get("line") or "").strip()
        city_raw  = (addr.get("city") or "Charlotte").strip()
        state     = (addr.get("state_code") or "NC").strip()
        zip_clean = _extract_zip(addr.get("postal_code") or "", street)

        price = str(item.get("list_price") or item.get("price") or "")
        beds  = str(item.get("beds")       or item.get("beds_min")  or "")
        baths = str(
            item.get("baths_consolidated") or
            item.get("baths_full")         or
            item.get("baths")              or
            item.get("baths_min")          or ""
        )
        sqft  = str(item.get("sqft_min") or item.get("sqft") or "")

        prop_raw  = (item.get("prop_type") or item.get("property_type") or "").lower()
        prop_type = _PROP_TYPE_MAP.get(prop_raw, prop_raw.replace("_", " "))

        permalink = item.get("permalink") or ""
        prop_id   = item.get("property_id") or ""
        if permalink:
            url = (permalink if permalink.startswith("http")
                   else f"https://www.realtor.com/realestateandhomes-detail/{permalink}")
        elif prop_id:
            url = f"https://www.realtor.com/realestateandhomes-detail/{prop_id}"
        else:
            url = ""

        if not url or not street or url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "title":         f"{street}{_DASH}{city_raw}, {state} {zip_clean}".rstrip(),
            "price":         price.replace(",", ""),
            "location":      f"{street}, {city_raw}, {state} {zip_clean}".rstrip(),
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft),
            "url":           url,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "realtor",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_type,
        })

    return out


def _parse_realtor_jsonld(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            main = entry.get("mainEntity")
            if isinstance(main, dict) and main.get("@type") == "ItemList":
                items.extend(main.get("itemListElement", []))

    out: list[dict] = []
    for it in items:
        url   = (it.get("url") or "").strip()
        name  = (it.get("name") or "").strip()
        offer = it.get("offers") or {}
        ent   = it.get("mainEntity") or {}
        addr  = ent.get("address") or {}
        floor = ent.get("floorSize") or {}

        if not url or not name or url in seen_urls:
            continue

        prop_type_raw = ent.get("@type") or ""
        if isinstance(prop_type_raw, list):
            prop_type_raw = prop_type_raw[0] if prop_type_raw else ""
        prop_type = _camel_to_words(prop_type_raw).lower()
        # schema.org uses "Apartment" for both apartments and condos;
        # look for "condo" in adjacent text to correct the type.
        if prop_type in ("apartment", ""):
            hint = (name + " " + str(offer.get("description") or "")).lower()
            if "condo" in hint:
                prop_type = "condo"
            elif "townhome" in hint or "townhouse" in hint:
                prop_type = "townhouse"

        street   = (addr.get("streetAddress") or "").strip()
        city_raw = (addr.get("addressLocality") or "Charlotte").strip()
        state    = (addr.get("addressRegion") or "NC").strip()
        zip_clean = _extract_zip(addr.get("postalCode"), name, url)
        full_addr = street or name

        seen_urls.add(url)
        out.append({
            "title":         f"{full_addr}{_DASH}{city_raw}, {state} {zip_clean}".rstrip(),
            "price":         str(offer.get("price") or ""),
            "location":      f"{full_addr}, {city_raw}, {state} {zip_clean}".rstrip(),
            "bedrooms":      _norm_beds(str(ent.get("numberOfBedrooms") or "")),
            "bathrooms":     _norm_baths(str(
                ent.get("numberOfBathroomsTotal") or
                ent.get("numberOfFullBathrooms")  or
                ent.get("numberOfBathrooms")      or ""
            )),
            "sqft":          _norm_sqft(str(floor.get("value") or "")),
            "url":           url,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "realtor",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_type,
        })

    return out


def _parse_realtor_dom(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    """
    Fallback DOM parser for Realtor.com — scrapes listing cards directly.
    Handles the `data-testid="property-card"` pattern used in current builds.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []

    cards = (
        soup.select('[data-testid="property-card"]')
        or soup.select('[class*="PropertyCard"]')
        or soup.select('[class*="property-card"]')
    )

    for card in cards:
        # URL
        a_tag = card.find("a", href=True)
        if not a_tag:
            continue
        href = a_tag["href"].strip()
        if not href.startswith("http"):
            href = _REALTOR_BASE + href
        if href in seen_urls:
            continue

        # Price
        price_el = (
            card.select_one('[data-testid="pc-price"]')
            or card.select_one('[class*="Price"]')
            or card.find(class_=re.compile(r"price", re.I))
        )
        price_raw = ""
        if price_el:
            pm = re.search(r"\$([\d,]+)", price_el.get_text())
            price_raw = pm.group(1).replace(",", "") if pm else ""

        # Address
        addr_el = (
            card.select_one('[data-testid="card-address-1"]')
            or card.select_one('[data-testid="card-address"]')
            or card.find(class_=re.compile(r"address", re.I))
        )
        addr2_el = card.select_one('[data-testid="card-address-2"]')
        street   = addr_el.get_text(strip=True) if addr_el else ""
        city_state_zip = addr2_el.get_text(strip=True) if addr2_el else ""

        # Parse "Charlotte, NC 28203" or "Charlotte, NC"
        csz_parts = city_state_zip.replace(",", " ").split()
        city_raw  = csz_parts[0] if csz_parts else "Charlotte"
        zip_clean = _extract_zip(city_state_zip, href)

        # Beds / baths / sqft
        beds = baths = sqft = ""
        for li in card.select('[data-testid*="bed"], [data-testid*="bath"], [data-testid*="sqft"]'):
            t  = li.get("data-testid", "")
            v  = re.search(r"[\d,.]+", li.get_text())
            if not v:
                continue
            if "bed" in t:
                beds = v.group()
            elif "bath" in t:
                baths = v.group()
            elif "sqft" in t:
                sqft = v.group().replace(",", "")

        # Property type
        prop_el = card.find(class_=re.compile(r"(property.?type|prop.?type)", re.I))
        prop_type = prop_el.get_text(strip=True).lower() if prop_el else ""

        full_addr = f"{street}, {city_state_zip}".strip(", ")
        seen_urls.add(href)
        out.append({
            "title":         full_addr or street,
            "price":         price_raw,
            "location":      full_addr,
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft),
            "url":           href,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "realtor",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_type,
        })

    return out

    return out


# ---------------------------------------------------------------------------
# Zillow scraper (SeleniumBase UC + __NEXT_DATA__)
# ---------------------------------------------------------------------------

_ZILLOW_BASE = "https://www.zillow.com"


def scrape_zillow_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 1,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
    zip_subset: list[int] | None = None,
) -> tuple[list[dict], list[int]]:
    """
    Scrape Zillow for Charlotte, NC listings.

    Parameters
    ----------
    zip_subset : list[int] | None
        If given, only scrape these ZIPs (used by the rotation system so each
        run covers a manageable slice rather than all 27 ZIPs at once, which
        triggers Zillow's IP-level rate limiter after the first ZIP).

    Returns
    -------
    (listings, succeeded_zips)
        succeeded_zips contains only the ZIPs that returned at least one
        page of real results — blocked ZIPs are excluded so the caller can
        decide whether to mark them as scraped or leave them at the front of
        the queue for the next run.
    """
    path_prefix = "homes/for_rent" if listing_type == "for_rent" else "homes"
    now = datetime.now(timezone.utc).isoformat()
    zips = zip_subset if zip_subset is not None else list(CHARLOTTE_ZIP_REGIONS.keys())

    if min_price is not None or max_price is not None:
        lo = min_price if min_price is not None else 0
        hi = max_price if max_price is not None else 50_000_000
        price_seg = f"{lo}-{hi}_price/"
    else:
        price_seg = ""

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()
    succeeded_zips: list[int] = []

    def _scrape_page(zipcode: int, page: int) -> tuple[list[dict], bool]:
        """Fetch a single page in a fresh browser session. Returns (rows, was_blocked)."""
        page_suffix = "" if page == 1 else f"{page}_p/"
        # Sort newest-first so freshly listed properties always appear on page 1
        url = f"{_ZILLOW_BASE}/{path_prefix}/{zipcode}_rb/{price_seg}days_sort/{page_suffix}"
        for attempt in range(1, 3):
            try:
                with _open_uc_session() as sb:
                    try:
                        sb.uc_open_with_reconnect(url, reconnect_time=_SB_RECONNECT)
                        sb.sleep(3 if attempt == 1 else 8)
                    except Exception as exc:
                        logger.warning("[zillow] ZIP %s p%d attempt %d nav failed: %s", zipcode, page, attempt, exc)
                        return [], True
                    html = sb.get_page_source()
            except RuntimeError:
                raise
            except Exception as exc:
                logger.error("[zillow] ZIP %s p%d session error: %s", zipcode, page, exc)
                return [], True

            if "Access to this page has been denied" in html or "__NEXT_DATA__" not in html:
                logger.warning("[zillow] ZIP %s p%d attempt %d bot-blocked; retrying…", zipcode, page, attempt)
                time.sleep(random.uniform(10.0, 16.0))
                continue

            rows = _parse_zillow_next_data(html, listing_type, now, seen_urls)
            logger.info("[zillow] ZIP %s p%d/%d: %d listings", zipcode, page, max_pages, len(rows))
            return rows, False

        logger.warning("[zillow] ZIP %s p%d blocked on all attempts", zipcode, page)
        return [], True

    # Each page gets a completely fresh browser session — fresh fingerprint
    # means Zillow sees each page request as a new visitor, not a paginating bot.
    for idx, zipcode in enumerate(zips, 1):
        zip_listings: list[dict] = []
        zip_succeeded = False

        for page in range(1, max_pages + 1):
            _emit_progress(progress_cb, idx, len(zips), f"ZIP {zipcode} p{page}/{max_pages}")
            rows, blocked = _scrape_page(zipcode, page)

            if blocked:
                if page == 1:
                    logger.warning("[zillow] ZIP %s p1 blocked — skipping ZIP", zipcode)
                else:
                    logger.warning("[zillow] ZIP %s blocked at p%d — stopping", zipcode, page)
                break

            zip_succeeded = True
            zip_listings.extend(rows)

            if len(rows) == 0:
                logger.info("[zillow] ZIP %s p%d empty — end of listings", zipcode, page)
                break

            # Pause between page sessions so Zillow doesn't correlate them by timing
            if page < max_pages:
                time.sleep(random.uniform(4.0, 8.0))

        if zip_succeeded:
            all_listings.extend(zip_listings)
            succeeded_zips.append(zipcode)

        # Pause between ZIPs
        if idx < len(zips):
            time.sleep(random.uniform(3.0, 6.0))

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[zillow] done: %d listings from %d/%d ZIPs succeeded",
                len(all_listings), len(succeeded_zips), len(zips))
    return all_listings, succeeded_zips


def _parse_zillow_next_data(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    nd   = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        return []
    try:
        data = json.loads(nd.string)
    except Exception:
        return []

    # Locate the list of results — known path on current Zillow.
    results: list = []
    try:
        sps = data["props"]["pageProps"]["searchPageState"]
        for cat in ("cat1", "cat2"):
            node = sps.get(cat, {}).get("searchResults", {})
            for key in ("listResults", "mapResults"):
                v = node.get(key)
                if isinstance(v, list) and v:
                    results = v
                    break
            if results:
                break
    except (KeyError, TypeError):
        pass

    out: list[dict] = []
    for item in results:
        address  = (item.get("addressStreet") or "").strip()
        city_raw = (item.get("addressCity")   or "").strip()
        state    = (item.get("addressState")  or "NC").strip()
        zip_clean = _extract_zip(item.get("addressZipcode"), item.get("address"))
        price    = str(item.get("unformattedPrice") or item.get("price") or "")
        beds     = str(item.get("beds")  or "")
        baths    = str(item.get("baths") or "")
        sqft     = str(item.get("area")  or "")
        detail   = (item.get("detailUrl") or "").strip()
        prop_raw = (item.get("propertyType") or item.get("hdpData", {})
                                                   .get("homeInfo", {})
                                                   .get("homeType")
                                                   or "").lower().replace("_", " ")

        if not address or not detail:
            continue
        url = detail if detail.startswith("http") else f"{_ZILLOW_BASE}{detail}"
        if url in seen_urls:
            continue

        status = (item.get("statusType") or "").upper()
        if status in ("RECENTLY_SOLD", "OTHER"):
            continue

        seen_urls.add(url)
        out.append({
            "title":         f"{address}{_DASH}{city_raw}, {state} {zip_clean}".rstrip(),
            "price":         price,
            "location":      f"{address}, {city_raw}, {state} {zip_clean}".rstrip(),
            "bedrooms":      _norm_beds(beds),
            "bathrooms":     _norm_baths(baths),
            "sqft":          _norm_sqft(sqft),
            "url":           url,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "zillow",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_raw,
        })

    return out


# ---------------------------------------------------------------------------
# Apartments.com scraper (SeleniumBase UC + DOM placards)
# ---------------------------------------------------------------------------

_APTS_BASE = "https://www.apartments.com"


def scrape_apartments_charlotte(
    listing_type: str = "for_rent",
    max_pages: int = 3,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """
    Scrape Apartments.com for Charlotte, NC rentals.

    Apartments.com only carries rentals — `for_sale` returns an empty list.
    Uses SeleniumBase UC to load each search page, then parses each
    `<article class="placard">` block in the rendered DOM.
    """
    if listing_type != "for_rent":
        logger.info("[apartments] only supports for_rent; skipping for_sale request")
        return []

    now = datetime.now(timezone.utc).isoformat()
    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    try:
        with _open_uc_session() as sb:
            for pg in range(1, max_pages + 1):
                _emit_progress(progress_cb, pg, max_pages, f"page {pg}")
                path = "charlotte-nc/" if pg == 1 else f"charlotte-nc/{pg}/"
                url  = f"{_APTS_BASE}/{path}"
                try:
                    sb.uc_open_with_reconnect(url, reconnect_time=_SB_RECONNECT)
                    sb.sleep(4)
                except Exception as exc:
                    logger.warning("[apartments] navigation failed page %d: %s", pg, exc)
                    break

                html = sb.get_page_source()
                rows = _parse_apartments_placards(html, now, seen_urls)
                if not rows:
                    logger.info("[apartments] no placards on page %d — stopping", pg)
                    break
                all_listings.extend(rows)
                logger.info("[apartments] page %d: %d listings", pg, len(rows))
                time.sleep(random.uniform(2.0, 3.5))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error("[apartments] browser session failed: %s", exc)

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[apartments] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_apartments_placards(
    html: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    placards = soup.select("article.placard")
    out: list[dict] = []

    for p in placards:
        url      = (p.get("data-url") or "").strip()
        if not url or url in seen_urls:
            continue

        title_el = p.select_one(".js-placardTitle, .property-title")
        addr_el  = p.select_one(".property-address")
        title    = title_el.get_text(strip=True) if title_el else ""
        addr_txt = addr_el.get_text(strip=True) if addr_el else (p.get("data-streetaddress") or "")

        # Pull minimum price + bedroom count out of the .rentRollup table
        prices: list[int] = []
        beds_list: list[int] = []
        for box in p.select(".bedRentBox"):
            bed_txt   = (box.select_one(".bedTextBox") or "").get_text(strip=True) if box.select_one(".bedTextBox") else ""
            price_txt = (box.select_one(".priceTextBox") or "").get_text(strip=True) if box.select_one(".priceTextBox") else ""
            pm = re.search(r"\$([\d,]+)", price_txt)
            if pm:
                try:
                    prices.append(int(pm.group(1).replace(",", "")))
                except ValueError:
                    pass
            if bed_txt:
                low = bed_txt.lower()
                if "studio" in low:
                    beds_list.append(0)
                else:
                    bm = re.match(r"(\d+)", low)
                    if bm:
                        beds_list.append(int(bm.group(1)))

        price = str(min(prices)) if prices else ""
        beds  = str(min(beds_list)) if beds_list else ""

        # Address parts: "1100 Falls Creek Ln, Charlotte, NC 28209"
        parts = [s.strip() for s in addr_txt.split(",")]
        city_raw = parts[-2] if len(parts) >= 3 else "Charlotte"
        state_zip = parts[-1] if len(parts) >= 3 else "NC"
        state = state_zip.split(" ")[0] if state_zip else "NC"
        zip_clean = _extract_zip(state_zip, addr_txt)
        street = parts[0] if parts else addr_txt

        display_title = title or street
        seen_urls.add(url)
        out.append({
            "title":         f"{display_title}{_DASH}{city_raw}, {state} {zip_clean}".rstrip(),
            "price":         price,
            "location":      f"{street}, {city_raw}, {state} {zip_clean}".rstrip(),
            "bedrooms":      beds,
            "bathrooms":     "",
            "sqft":          "",
            "url":           url,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "apartments",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  "for_rent",
            "property_type": "apartment",
        })

    return out


# ---------------------------------------------------------------------------
# Homes.com scraper (SeleniumBase UC + __NEXT_DATA__ / DOM)
# ---------------------------------------------------------------------------
#
# Homes.com is owned by CoStar and has Cloudflare protection, so we use
# SeleniumBase UC (same as Zillow/Realtor).  We target the condo/townhome
# search URL per-ZIP and use fresh browser sessions to avoid bot blocks.
#
# Default ZIP set focuses on South End and adjacent Charlotte neighborhoods:
#   28203 – South End / LoSo / Dilworth (core)
#   28202 – Uptown / 4th Ward (luxury condos)
#   28209 – South Dilworth / Myers Park corridor
#   28208 – Wilmore / Seversville (adjacent west)
#   28204 – Elizabeth / Midtown (adjacent east)
#   28207 – Myers Park / Eastover
#   28206 – Optimist Park / NoDa south
#   28205 – Plaza Midwood / NoDa

_HOMES_BASE = "https://www.homes.com"

# City-level search URLs — Homes.com does not support per-ZIP search pages.
# Condos + townhomes are scraped separately then merged.
_HOMES_SEARCH_PATHS = ["condos", "townhomes"]


def scrape_homes_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 2,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
    zip_subset: list[int] | None = None,  # unused — kept for API compat
) -> list[dict]:
    """
    Scrape Homes.com (CoStar) for Charlotte metro condo/townhome listings.

    Only scrapes for-sale — Apartments.com already covers rentals.
    Uses city-level URLs (homes.com/charlotte-nc/condos/ and /townhomes/)
    since Homes.com doesn't support per-ZIP search pages.
    """
    if listing_type != "for_sale":
        logger.info("[homes] only supports for_sale; skipping for_rent")
        return []

    now = datetime.now(timezone.utc).isoformat()

    price_qs = ""
    if min_price is not None or max_price is not None:
        parts = []
        if min_price is not None:
            parts.append(f"minprice={min_price}")
        if max_price is not None:
            parts.append(f"maxprice={max_price}")
        price_qs = "?" + "&".join(parts)

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    total_steps = len(_HOMES_SEARCH_PATHS) * max_pages
    step = 0

    for prop_path in _HOMES_SEARCH_PATHS:
        prop_type_hint = "condo" if prop_path == "condos" else "townhouse"
        try:
            with _open_uc_session() as sb:
                for page in range(1, max_pages + 1):
                    step += 1
                    _emit_progress(progress_cb, step, total_steps,
                                   f"{prop_path} p{page}")
                    page_seg = "" if page == 1 else f"p{page}/"
                    url = (
                        f"{_HOMES_BASE}/charlotte-nc/{prop_path}/"
                        f"{page_seg}{price_qs}"
                    )
                    try:
                        sb.uc_open_with_reconnect(url, reconnect_time=_SB_RECONNECT)
                        sb.sleep(4)
                    except Exception as exc:
                        logger.warning("[homes] %s p%d nav failed: %s",
                                       prop_path, page, exc)
                        break

                    html = sb.get_page_source()
                    if "Access denied" in html or "challenge" in html[:1000].lower():
                        logger.warning("[homes] %s p%d bot-blocked", prop_path, page)
                        break

                    rows = _parse_homes_dom(html, listing_type, now, seen_urls,
                                            prop_type_hint)
                    if not rows:
                        logger.info("[homes] %s p%d: no listings — stopping",
                                    prop_path, page)
                        break
                    all_listings.extend(rows)
                    logger.info("[homes] %s p%d: %d listings (total %d)",
                                prop_path, page, len(rows), len(all_listings))
                    time.sleep(random.uniform(2.0, 3.5))
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("[homes] %s session error: %s", prop_path, exc)

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[homes] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_homes_dom(
    html: str,
    listing_type: str,
    scraped_at: str,
    seen_urls: set[str],
    prop_type_hint: str = "condo",
) -> list[dict]:
    """Parse Homes.com search result page using article.search-placard cards."""
    soup  = BeautifulSoup(html, "lxml")
    cards = soup.select("article.search-placard")

    out: list[dict] = []
    for card in cards:
        # URL + address from first image anchor aria-label
        a = card.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if not href.startswith("http"):
            href = _HOMES_BASE + href
        if href in seen_urls:
            continue

        # Address from aria-label: "3109 Marlborough Rd, Charlotte, NC 28208"
        addr_raw = a.get("aria-label", "") or a.get("title", "")
        m_addr = re.match(
            r"^(.+?),\s*([^,]+),\s*NC\s+(\d{5})", addr_raw
        )
        if m_addr:
            street    = m_addr.group(1).strip()
            city_raw  = m_addr.group(2).strip()
            zip_clean = m_addr.group(3)
        else:
            # Fallback: parse card text
            txt_full = card.get_text(" ", strip=True)
            m2 = re.search(r"(\d{5})", txt_full)
            zip_clean = m2.group(1) if m2 else ""
            street, city_raw = addr_raw.split(",")[0].strip(), "Charlotte"

        # Price
        price_el  = card.select_one(".price-container")
        price_raw = ""
        if price_el:
            pm = re.search(r"\$([\d,]+)", price_el.get_text())
            price_raw = pm.group(1).replace(",", "") if pm else ""

        # Beds / baths / sqft from .detailed-info-container
        info_el  = card.select_one(".detailed-info-container")
        info_txt = info_el.get_text(" ", strip=True) if info_el else card.get_text(" ", strip=True)
        beds_m = re.search(r"(\d+)\s*Bed", info_txt, re.I)
        bath_m = re.search(r"([\d.]+)\s*Bath", info_txt, re.I)
        sqft_m = re.search(r"([\d,]+)\s*Sq\s*Ft", info_txt, re.I)

        # Property type: inherit hint (condos page → condo, townhomes page → townhouse)
        # Override if text explicitly mentions townhome/townhouse
        full_txt  = card.get_text(" ", strip=True)
        prop_type = "townhouse" if re.search(r"townhome|townhouse", full_txt, re.I) else prop_type_hint

        if not street:
            continue
        seen_urls.add(href)
        out.append({
            "title":         f"{street}{_DASH}{city_raw}, NC {zip_clean}".rstrip(),
            "price":         price_raw,
            "location":      f"{street}, {city_raw}, NC {zip_clean}".rstrip(),
            "bedrooms":      _norm_beds(beds_m.group(1) if beds_m else ""),
            "bathrooms":     _norm_baths(bath_m.group(1) if bath_m else ""),
            "sqft":          _norm_sqft(sqft_m.group(1).replace(",", "") if sqft_m else ""),
            "url":           href,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "homes",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  listing_type,
            "property_type": prop_type,
        })

    return out


# ---------------------------------------------------------------------------
# Estately scraper (plain requests + BS4 — no bot protection)
# ---------------------------------------------------------------------------

_ESTATELY_BASE = "https://www.estately.com"


def scrape_estately_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 5,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """Scrape Estately for Charlotte, NC listings (server-rendered HTML)."""
    base_url = f"{_ESTATELY_BASE}/NC/Charlotte"
    qs_parts: list[str] = []
    if listing_type == "for_rent":
        qs_parts.append("only_rent=true")
    if min_price is not None:
        qs_parts.append(f"min_price={min_price}")
    if max_price is not None:
        qs_parts.append(f"max_price={max_price}")
    if qs_parts:
        base_url += "?" + "&".join(qs_parts)
        page_sep = "&"
    else:
        page_sep = "?"

    now = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": _ESTATELY_BASE,
    })

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for pg in range(1, max_pages + 1):
        _emit_progress(progress_cb, pg, max_pages, f"page {pg}")
        url = base_url if pg == 1 else f"{base_url}{page_sep}page={pg}"
        try:
            resp = session.get(url, timeout=25)
        except Exception as exc:
            logger.error("[estately] request failed page %d: %s", pg, exc)
            break

        if resp.status_code != 200:
            logger.warning("[estately] status %d on page %d", resp.status_code, pg)
            break

        soup = BeautifulSoup(resp.text, "lxml")
        wrappers = soup.find_all(class_="full-height-padded-wrapper")
        if not wrappers:
            logger.info("[estately] no listing wrappers on page %d — stopping", pg)
            break

        page_count = 0
        for item in wrappers:
            addr_link = item.select_one("h2.result-address a")
            if not addr_link:
                continue
            address_text = addr_link.get_text(strip=True)
            href = addr_link.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = _ESTATELY_BASE + href
            if href in seen_urls:
                continue

            small_el = item.select_one("h2.result-address small")
            prop_raw = small_el.get_text(strip=True).lower() if small_el else ""
            prop_type = re.sub(r"\s*for\s+(sale|rent)$", "", prop_raw).strip()

            price_el = item.select_one("p.result-price strong")
            price_raw = (
                price_el.get_text(strip=True).replace("$", "").replace(",", "")
                if price_el else ""
            )

            beds = baths = sqft = ""
            for li in item.select("div.result-basics li"):
                bold = li.find("b")
                if not bold:
                    continue
                num  = bold.get_text(strip=True)
                rest = li.get_text(strip=True).replace(num, "", 1).strip().lower()
                if "bed" in rest:
                    beds = num
                elif "bath" in rest:
                    baths = num
                elif "sqft" in rest and "lot" not in rest and not sqft:
                    sqft = num.replace(",", "")

            parts    = [p.strip() for p in address_text.split(",")]
            city_raw = parts[-2] if len(parts) >= 3 else "Charlotte"
            zip_clean = _extract_zip(parts[-1] if parts else "", address_text, href)

            seen_urls.add(href)
            all_listings.append({
                "title":         address_text,
                "price":         price_raw,
                "location":      address_text,
                "bedrooms":      _norm_beds(beds),
                "bathrooms":     _norm_baths(baths),
                "sqft":          _norm_sqft(sqft),
                "url":           href,
                "date_posted":   "",
                "date_scraped":  now,
                "source":        "estately",
                "city":          _norm_city(city_raw),
                "zip":           zip_clean,
                "listing_type":  listing_type,
                "property_type": prop_type,
            })
            page_count += 1

        logger.info("[estately] page %d: %d listings", pg, page_count)
        time.sleep(random.uniform(2.0, 3.5))

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[estately] total scraped: %d listings", len(all_listings))
    return all_listings


# ---------------------------------------------------------------------------
# Craigslist scraper (plain requests + BS4)
# ---------------------------------------------------------------------------

_CL_BASE = "https://charlotte.craigslist.org"


def scrape_craigslist_charlotte(
    listing_type: str = "for_sale",
    max_pages: int = 3,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """Scrape Craigslist Charlotte for housing listings."""
    search_path = "/search/apa" if listing_type == "for_rent" else "/search/rea"
    now = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    price_qs = ""
    if min_price is not None:
        price_qs += f"&min_price={min_price}"
    if max_price is not None:
        price_qs += f"&max_price={max_price}"

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for pg in range(max_pages):
        _emit_progress(progress_cb, pg + 1, max_pages, f"page {pg + 1}")
        url = f"{_CL_BASE}{search_path}?s={pg * 120}{price_qs}"
        try:
            resp = session.get(url, timeout=20)
        except Exception as exc:
            logger.error("[craigslist] request failed page %d: %s", pg + 1, exc)
            break

        if resp.status_code != 200:
            logger.warning("[craigslist] status %d on page %d", resp.status_code, pg + 1)
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # JSON-LD blocks supplement HTML with structured address / bed / bath
        ld_by_title: dict[str, dict] = {}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if data.get("@type") == "ItemList":
                    for element in data.get("itemListElement", []):
                        item = element.get("item", element)
                        name = (item.get("name") or "").strip()
                        if name:
                            ld_by_title[name.lower()[:80]] = item
                    break
            except Exception:
                pass

        results = (
            soup.select("li.cl-static-search-result")
            or soup.select("li.cl-search-result")
            or soup.select("li.result-row")
        )

        if not results:
            logger.info("[craigslist] no results on page %d — stopping", pg + 1)
            break

        page_count = 0
        for row in results:
            link = row.find("a")
            if not link:
                continue

            title_div = row.select_one(".title")
            title = (
                title_div.get_text(strip=True)
                or row.get("title", "")
                or link.get_text(strip=True)
            ).strip()

            href = link.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = _CL_BASE + href
            if href in seen_urls:
                continue

            price_el = row.select_one(".price") or row.select_one(".result-price")
            price_raw = price_el.get_text(strip=True).replace("$", "").replace(",", "") if price_el else ""

            housing_el = row.select_one(".housing") or row.select_one(".result-housing")
            beds, baths = _parse_cl_housing(housing_el.get_text() if housing_el else "")

            hood_el = (
                row.select_one(".location")
                or row.select_one(".result-hood")
                or row.select_one(".hood")
            )
            hood = hood_el.get_text(strip=True).strip("() ") if hood_el else ""

            date_el = row.select_one("time")
            date_posted = (date_el.get("datetime") or "")[:10] if date_el else ""

            ld = ld_by_title.get(title.lower()[:80], {})
            addr_obj  = ld.get("address") or {}
            locality  = addr_obj.get("addressLocality", "") if isinstance(addr_obj, dict) else ""
            region    = addr_obj.get("addressRegion", "NC")  if isinstance(addr_obj, dict) else "NC"
            if not beds:
                beds  = str(ld.get("numberOfBedrooms",  "")) if ld.get("numberOfBedrooms")  else beds
            if not baths:
                baths = str(ld.get("numberOfBathroomsTotal", "")) if ld.get("numberOfBathroomsTotal") else baths

            city_raw  = locality or hood or "Charlotte"
            city_norm = _norm_city(city_raw)
            zip_clean = _extract_zip(
                addr_obj.get("postalCode") if isinstance(addr_obj, dict) else "",
                title, hood,
            )

            seen_urls.add(href)
            all_listings.append({
                "title":         title,
                "price":         price_raw,
                "location":      f"{city_raw}, {region}" + (f" {zip_clean}" if zip_clean else ""),
                "bedrooms":      _norm_beds(str(beds)),
                "bathrooms":     _norm_baths(str(baths)),
                "sqft":          "",
                "url":           href,
                "date_posted":   date_posted,
                "date_scraped":  now,
                "source":        "craigslist",
                "city":          city_norm,
                "zip":           zip_clean,
                "listing_type":  listing_type,
                "property_type": (ld.get("@type") or "").lower(),
            })
            page_count += 1

        logger.info("[craigslist] page %d: %d listings", pg + 1, page_count)
        time.sleep(random.uniform(2.0, 3.5))

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[craigslist] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_cl_housing(text: str) -> tuple[str, str]:
    """Parse Craigslist housing blurb '3br 2ba' -> ('3', '2')."""
    beds  = ""
    baths = ""
    bed_m  = re.search(r"(\d+)\s*[Bb][Rr]", text)
    bath_m = re.search(r"(\d+(?:\.\d)?)\s*[Bb][Aa]", text)
    if bed_m:
        beds  = bed_m.group(1)
    if bath_m:
        baths = bath_m.group(1)
    return beds, baths


# ---------------------------------------------------------------------------
# SearchCharlotte.com (Berkshire Hathaway / BoomTown IDX) scraper
# ---------------------------------------------------------------------------
#
# Server-rendered HTML, no bot protection. 10 listings per page, paginate
# with ?pageIndex=N. 27k+ active listings, so even max_pages=30 only pulls
# ~300 of them. URL format already contains the ZIP code:
#   /homes/{street-slug}/{city}/NC/{zip}/{mls_id}/

_SC_BASE = "https://www.searchcharlotte.com"
_SC_RESULTS = f"{_SC_BASE}/results-gallery/?status=A"
_SC_URL_RE = re.compile(
    r"/homes/([^/]+)/([^/]+)/([A-Z]{2})/(\d{5})/(\d+)/?"
)


def scrape_searchcharlotte(
    listing_type: str = "for_sale",
    max_pages: int = 5,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
) -> list[dict]:
    """
    Scrape SearchCharlotte.com (BHHS Carolinas' IDX) for active listings.

    Server-rendered HTML, no bot protection. Pushes price filter to the
    URL when given (BoomTown convention: &priceMin=...&priceMax=...).
    """
    if listing_type != "for_sale":
        logger.info("[searchcharlotte] only supports for_sale; skipping for_rent")
        return []

    now = datetime.now(timezone.utc).isoformat()
    session = requests.Session()
    session.headers.update({
        **_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    price_qs = ""
    if min_price is not None:
        price_qs += f"&priceMin={min_price}"
    if max_price is not None:
        price_qs += f"&priceMax={max_price}"

    all_listings: list[dict] = []
    seen_urls:    set[str]   = set()

    for pg in range(1, max_pages + 1):
        _emit_progress(progress_cb, pg, max_pages, f"page {pg}")
        url = _SC_RESULTS + price_qs + (f"&pageIndex={pg}" if pg > 1 else "")
        try:
            resp = session.get(url, timeout=20)
        except Exception as exc:
            logger.error("[searchcharlotte] request failed page %d: %s", pg, exc)
            break
        if resp.status_code != 200:
            logger.warning("[searchcharlotte] status %d on page %d", resp.status_code, pg)
            break

        rows = _parse_searchcharlotte_cards(resp.text, now, seen_urls)
        if not rows:
            logger.info("[searchcharlotte] no cards on page %d — stopping", pg)
            break
        all_listings.extend(rows)
        logger.info("[searchcharlotte] page %d: %d listings", pg, len(rows))
        time.sleep(random.uniform(1.5, 3.0))

    all_listings = _apply_price_filter(all_listings, min_price, max_price)
    logger.info("[searchcharlotte] total scraped: %d listings", len(all_listings))
    return all_listings


def _parse_searchcharlotte_cards(
    html: str,
    scraped_at: str,
    seen_urls: set[str],
) -> list[dict]:
    soup  = BeautifulSoup(html, "lxml")
    cards = soup.select(".bt-listing-teaser")
    out: list[dict] = []

    for c in cards:
        link = c.find("a", href=lambda h: bool(h and "/homes/" in h))
        if not link:
            continue
        url = link.get("href", "").strip()
        if not url or url in seen_urls:
            continue

        m = _SC_URL_RE.search(url)
        if m:
            street_slug, city_slug, state, zip_clean, _mls = m.groups()
            street   = street_slug.replace("-", " ")
            city_raw = city_slug.replace("-", " ")
        else:
            street, city_raw, state, zip_clean = "", "Charlotte", "NC", ""

        # The .bt-cover__wrapper alt has the cleaner address text
        cover = c.find(class_="bt-cover__wrapper")
        addr_text = (cover.get("alt") if cover else "") or f"{street}, {city_raw}, {state} {zip_clean}"

        price_el = c.select_one(".listing-card__price")
        price_raw = ""
        if price_el:
            pm = re.search(r"\$([\d,]+)", price_el.get_text())
            if pm:
                price_raw = pm.group(1).replace(",", "")

        # Meta text contains "2 beds 2 baths 1,309 sqft"
        meta_text = c.get_text(" ", strip=True)
        beds_m = re.search(r"(\d+)\s+beds?", meta_text, re.I)
        bath_m = re.search(r"(\d+(?:\.\d)?)\s+baths?", meta_text, re.I)
        sqft_m = re.search(r"([\d,]+)\s+sqft", meta_text, re.I)

        # Property type isn't always exposed on the card; infer from MLS context
        prop_type = ""
        if "Condo" in meta_text:
            prop_type = "condo"
        elif "Townhouse" in meta_text or "Townhome" in meta_text:
            prop_type = "townhouse"
        elif sqft_m:
            prop_type = "single family residence"

        seen_urls.add(url)
        out.append({
            "title":         addr_text,
            "price":         price_raw,
            "location":      addr_text,
            "bedrooms":      _norm_beds(beds_m.group(1)) if beds_m else "",
            "bathrooms":     _norm_baths(bath_m.group(1)) if bath_m else "",
            "sqft":          _norm_sqft(sqft_m.group(1)) if sqft_m else "",
            "url":           url,
            "date_posted":   "",
            "date_scraped":  scraped_at,
            "source":        "searchcharlotte",
            "city":          _norm_city(city_raw),
            "zip":           zip_clean,
            "listing_type":  "for_sale",
            "property_type": prop_type,
        })

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_charlotte_houses(
    source: str = "redfin",
    listing_type: str = "for_sale",
    max_pages: int = 1,
    min_price: int | None = None,
    max_price: int | None = None,
    progress_cb=None,
    zip_subset: list[int] | None = None,
) -> list[dict]:
    """
    Main entry point for scraping Charlotte, NC housing listings.

    zip_subset is forwarded to the Zillow scraper only (rotation system).
    The Zillow scraper returns (listings, succeeded_zips); this wrapper
    strips the second element so callers get a plain list[dict].
    """
    kw = {"min_price": min_price, "max_price": max_price, "progress_cb": progress_cb}
    if source == "zillow":
        listings, _succeeded = scrape_zillow_charlotte(
            listing_type, max_pages, **kw, zip_subset=zip_subset
        )
        return listings
    if source == "realtor":
        return scrape_realtor_charlotte(listing_type, max_pages, **kw)
    if source == "craigslist":
        return scrape_craigslist_charlotte(listing_type, max_pages, **kw)
    if source == "estately":
        return scrape_estately_charlotte(listing_type, max_pages, **kw)
    if source == "apartments":
        return scrape_apartments_charlotte(listing_type, max_pages, **kw)
    if source == "searchcharlotte":
        return scrape_searchcharlotte(listing_type, max_pages, **kw)
    if source == "homes":
        return scrape_homes_charlotte(listing_type, max_pages, **kw)
    return scrape_redfin_charlotte(listing_type, max_pages, **kw)
