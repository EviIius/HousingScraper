"""Debug: extract all listing fields from gdpClientCache.property"""
import json
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

gdp_raw = data["props"]["pageProps"]["componentProps"]["gdpClientCache"]
gdp = json.loads(gdp_raw)
first_key = list(gdp.keys())[0]
prop = gdp[first_key]["property"]

# Print all useful fields
fields = ["price", "bedrooms", "bathrooms", "livingArea", "lotSize", "homeType",
          "streetAddress", "city", "state", "zipcode", "latitude", "longitude",
          "datePostedString", "listingTypeName", "description"]
for f in fields:
    print(f"{f}: {prop.get(f)}")
