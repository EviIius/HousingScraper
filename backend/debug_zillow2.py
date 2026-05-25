"""Debug: dig into gdpClientCache from a Zillow detail page."""
import json, re, sys
from seleniumbase import SB
from bs4 import BeautifulSoup

URL = "https://www.zillow.com/homedetails/1315-East-Blvd-UNIT-719-Charlotte-NC-28203/80453756_zpid/"

with SB(uc=True, headless=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(URL, reconnect_time=6)
    sb.sleep(5)
    html = sb.get_page_source()

soup = BeautifulSoup(html, "lxml")
nd = soup.find("script", id="__NEXT_DATA__")
data = json.loads(nd.string)

gdp = data["props"]["pageProps"]["componentProps"]["gdpClientCache"]
print("gdpClientCache type:", type(gdp))
print("gdpClientCache keys (first 5):", list(gdp.keys())[:5] if isinstance(gdp, dict) else "NOT A DICT - is", type(gdp))

if isinstance(gdp, str):
    gdp = json.loads(gdp)
    print("Parsed string. Keys:", list(gdp.keys())[:5])

# Find homeInfo
def find_key(d, target, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == target:
                print(f"  Found '{target}' at {path}.{k}")
                print(f"  Value: {json.dumps(v, indent=2)[:500]}")
                return v
            r = find_key(v, target, f"{path}.{k}")
            if r is not None:
                return r
    elif isinstance(d, list):
        for i, item in enumerate(d):
            r = find_key(item, target, f"{path}[{i}]")
            if r is not None:
                return r
    return None

print("\n--- Looking for homeInfo ---")
find_key(gdp, "homeInfo")

print("\n--- Looking for price ---")
find_key(gdp, "price")

print("\n--- Looking for bedrooms ---")
find_key(gdp, "bedrooms")
