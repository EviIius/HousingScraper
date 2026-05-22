import requests

base = {'al':1,'num_homes':5,'page_number':1,'region_id':11404,'region_type':2,'status':9,'v':8,'uipt':'1,2,3,4,5,6,7,8'}
hdrs = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
        'Referer':'https://www.redfin.com/zipcode/28277'}

tests = [
    ('no market',       dict(base)),
    ('market charlotte',dict(base, market='charlotte')),
    ('region_type 6',   dict(base, market='charlotte', region_type=6)),
    ('no status',       {k:v for k,v in base.items() if k != 'status'}),
    ('with sf',         dict(base, market='charlotte', sf='1,2,3,5,6,7')),
    ('al=3',            dict(base, market='charlotte', al=3, sp='true')),
]

for name, params in tests:
    r = requests.get('https://www.redfin.com/stingray/api/gis-csv', params=params, headers=hdrs, timeout=10)
    n = len(r.text.strip().splitlines())
    print(f'{name}: {n} lines | {r.text[:100]}')
