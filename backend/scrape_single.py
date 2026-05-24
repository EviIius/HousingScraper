"""
One-off: scrape a single Zillow detail page and upsert it into the database.
Usage:  python scrape_single.py <zillow_url>
"""
import json
import re
import sys
import time
import random
from datetime import datetime, timezone

from database import upsert_listings, get_connection

TARGET_URL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "https://www.zillow.com/homedetails/1315-East-Blvd-UNIT-719-Charlotte-NC-28203/80453756_zpid/"
)

_PTYPE_MAP = {
    "condo":         "condo",
    "condominium":   "condo",
    "townhouse":     "townhouse",
    "townhome":      "townhouse",
    "single_family": "single family",
    "single family": "single family",
    "multi_family":  "multi family",
    "apartment":     "apartment",
    "lot":           "land",
    "land":          "land",
}


def _norm_ptype(raw: str) -> str:
    key = raw.lower().replace("_", " ").strip()
    return _PTYPE_MAP.get(key, key)


def run():
    from seleniumbase import SB
    from bs4 import BeautifulSoup
    now = datetime.now(timezone.utc).isoformat()

    print(f"Opening: {TARGET_URL}")
    for attempt in range(1, 4):
        try:
            with SB(uc=True, headless=True, locale_code="en") as sb:
                sb.uc_open_with_reconnect(TARGET_URL, reconnect_time=6)
                sb.sleep(5 if attempt == 1 else 10)
                html = sb.get_page_source()
        except Exception as exc:
            print(f"  Attempt {attempt} session error: {exc}")
            continue

        if "Access to this page has been denied" in html or "__NEXT_DATA__" not in html:
            print(f"  Attempt {attempt} blocked, retrying…")
            time.sleep(random.uniform(10, 18))
            continue

        soup = BeautifulSoup(html, "lxml")
        nd = soup.find("script", id="__NEXT_DATA__")
        if not nd or not nd.string:
            print("  No __NEXT_DATA__ found, retrying…")
            continue

        try:
            data    = json.loads(nd.string)
            gdp_raw = data["props"]["pageProps"]["componentProps"]["gdpClientCache"]
            gdp     = json.loads(gdp_raw)
            first_key = list(gdp.keys())[0]
            prop = gdp[first_key]["property"]
        except Exception as exc:
            print(f"  Failed to parse gdpClientCache: {exc}")
            continue

        price    = str(prop.get("price") or "")
        beds     = str(prop.get("bedrooms") or "")
        baths    = str(prop.get("bathrooms") or "")
        sqft     = str(prop.get("livingArea") or "")
        city     = str(prop.get("city") or "Charlotte")
        zip_code = str(prop.get("zipcode") or "28203")
        street   = str(prop.get("streetAddress") or "")
        ptype    = _norm_ptype(str(prop.get("homeType") or ""))
        lat      = prop.get("latitude")
        lng      = prop.get("longitude")

        location = f"{street}, {city}, NC {zip_code}"
        url_norm = TARGET_URL.rstrip("/") + "/"

        listing = {
            "title":         location,
            "price":         price,
            "location":      location,
            "bedrooms":      beds,
            "bathrooms":     baths,
            "sqft":          sqft,
            "url":           url_norm,
            "date_posted":   str(prop.get("datePostedString") or ""),
            "date_scraped":  now,
            "source":        "zillow",
            "city":          city,
            "zip":           zip_code,
            "listing_type":  "for_sale",
            "property_type": ptype,
        }

        print("  Extracted listing:")
        for k, v in listing.items():
            if k != "url":
                print(f"    {k}: {v}")

        new = upsert_listings([listing])
        if new:
            print(f"\nInserted into database (1 new row).")
        else:
            # Exists — patch with fresh data (upsert_listings uses INSERT OR IGNORE)
            with get_connection() as conn:
                conn.execute(
                    """UPDATE listings
                       SET price=?, bedrooms=?, bathrooms=?, sqft=?,
                           city=?, zip=?, property_type=?, date_scraped=?,
                           lat=?, lng=?
                       WHERE url=?""",
                    (price, beds, baths, sqft, city, zip_code, ptype, now,
                     lat, lng, url_norm),
                )
                conn.commit()
            print(f"\nUpdated existing row in database.")

        print(f"\nDone — {street}, {city} NC {zip_code} | ${price} | {beds}bd/{baths}ba | {sqft} sqft | {ptype}")
        return

    print("Failed to scrape after 3 attempts.")
    sys.exit(1)


if __name__ == "__main__":
    run()
