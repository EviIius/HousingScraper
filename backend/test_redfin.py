import requests, io, csv

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.redfin.com/county/2066/NC/Mecklenburg-County',
})

# Warm up
r = session.get('https://www.redfin.com/county/2066/NC/Mecklenburg-County', timeout=10)
print('County page status:', r.status_code, '| URL:', r.url[:80])

params = {
    'al': 1,
    'market': 'charlotte',
    'num_homes': 10,
    'ord': 'redfin-recommended-asc',
    'page_number': 1,
    'region_id': 2066,
    'region_type': 5,
    'sf': '1,2,3,5,6,7',
    'status': 9,
    'uipt': '1,2,3,4,5,6,7,8',
    'v': 8,
}

r2 = session.get('https://www.redfin.com/stingray/api/gis-csv', params=params, timeout=15)
print('GIS CSV status:', r2.status_code)
lines = r2.text.strip().splitlines()
print('Lines returned:', len(lines))
if lines:
    print('Header:', lines[0][:120])
if len(lines) > 1:
    print('Second line:', lines[1][:200])
    reader = csv.DictReader(io.StringIO(r2.text))
    for i, row in enumerate(reader):
        addr = row.get('ADDRESS', '')
        city = row.get('CITY', '')
        state = row.get('STATE OR PROVINCE', '')
        price = row.get('PRICE', '')
        beds = row.get('BEDS', '')
        baths = row.get('BATHS', '')
        if addr:
            print(f'  {i}: {addr}, {city}, {state} | ${price} | {beds}bd {baths}ba')
        if i >= 4:
            break
