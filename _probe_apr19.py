import requests, re
from bs4 import BeautifulSoup
r = requests.get(
    'https://racing.hkjc.com/racing/information/English/Racing/ResultsAll.aspx',
    params={'racedate': '2026/04/19'}, timeout=30,
)
soup = BeautifulSoup(r.text, 'html.parser')
links = soup.select("a[href*='localresults?racedate=']")
nums = set()
for l in links:
    m = re.search(r'Racecourse=([A-Z0-9]+)&RaceNo=(\d+)', l.get('href', ''))
    if m and m.group(1) in ('ST', 'HV'):
        nums.add(int(m.group(2)))
print('races on page:', sorted(nums))
matches = re.findall(r'RaceNo=(\d+)', r.text)
print('all RaceNo hits (incl simulcast):', sorted(set(int(x) for x in matches)))
print('status:', r.status_code, 'page len:', len(r.text))
