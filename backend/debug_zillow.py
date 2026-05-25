"""Debug: dump __NEXT_DATA__ keys from a Zillow detail page."""
import json, re, sys
from seleniumbase import SB
from bs4 import BeautifulSoup

URL = "https://www.zillow.com/homedetails/1315-East-Blvd-UNIT-719-Charlotte-NC-28203/80453756_zpid/"

with SB(uc=True, headless=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(URL, reconnect_time=6)
    sb.sleep(5)
    html = sb.get_page_source()

print("Blocked:", "Access to this page has been denied" in html)
print("Has __NEXT_DATA__:", "__NEXT_DATA__" in html)

soup = BeautifulSoup(html, "lxml")
nd = soup.find("script", id="__NEXT_DATA__")
if nd and nd.string:
    data = json.loads(nd.string)
    raw = nd.string

    # Check regex hits
    for name, pat in [
        ("price", r'"price"\s*:\s*(\d{4,9})'),
        ("bedrooms", r'"bedrooms"\s*:\s*([\d.]+)'),
        ("bathrooms", r'"bathrooms"\s*:\s*([\d.]+)'),
        ("livingArea", r'"livingArea"\s*:\s*([\d.]+)'),
        ("zipcode", r'"zipcode"\s*:\s*"(\d{5})"'),
        ("streetAddress", r'"streetAddress"\s*:\s*"([^"]+)"'),
        ("homeType", r'"homeType"\s*:\s*"([^"]+)"'),
    ]:
        m = re.search(pat, raw)
        print(f"  {name}: {m.group(1) if m else 'NOT FOUND'}")

    # Show top-level keys
    def show_keys(d, prefix="", depth=0):
        if depth > 3: return
        if isinstance(d, dict):
            for k, v in list(d.items())[:20]:
                print(f"{'  '*depth}{prefix}{k}")
                if isinstance(v, (dict, list)):
                    show_keys(v, "", depth+1)
        elif isinstance(d, list) and d:
            show_keys(d[0], "[0].", depth)

    print("\n--- __NEXT_DATA__ structure (first 3 levels) ---")
    show_keys(data)
else:
    print("No __NEXT_DATA__ found")
    # Save page for inspection
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Page saved to debug_page.html")
