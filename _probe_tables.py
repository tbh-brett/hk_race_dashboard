"""Detailed table inspection for HKJC racecard."""
import re, requests
from bs4 import BeautifulSoup

url = "https://racing.hkjc.com/en-us/local/information/racecard"
params = {"racedate": "2026/04/01", "Racecourse": "ST", "RaceNo": "1"}
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
resp = requests.get(url, params=params, headers=headers, timeout=30)
soup = BeautifulSoup(resp.text, "html.parser")

tables = soup.find_all("table")

# Focus on table 4 (MY Race Card) and table 5 (data rows) and table 6 (standby)
for ti in [2, 4, 5, 6]:
    if ti >= len(tables):
        continue
    tbl = tables[ti]
    print(f"\n{'='*80}")
    print(f"TABLE {ti}")
    print(f"{'='*80}")
    # Show table attributes
    print(f"  id={tbl.get('id', '')}, class={tbl.get('class', '')}")
    
    rows = tbl.find_all("tr")
    for ri, row in enumerate(rows):
        cells = row.find_all(["td", "th"])
        cell_texts = []
        for c in cells:
            text = re.sub(r"\s+", " ", c.get_text()).strip()
            colspan = c.get("colspan", 1)
            cell_texts.append(f"[{text[:40]}]" + (f"(cs={colspan})" if str(colspan) != "1" else ""))
        print(f"  Row {ri} ({len(cells)} cells): {' | '.join(cell_texts[:15])}")
        if len(cell_texts) > 15:
            print(f"    ... +{len(cell_texts)-15} more cells")

# Also look for the race header info
print(f"\n{'='*80}")
print("RACE HEADER SEARCH")
print(f"{'='*80}")
# Look for div/span with race class, distance info
for tag in soup.find_all(["div", "span", "td", "th"]):
    text = tag.get_text(strip=True)
    if re.search(r"Class\s+\d.*\d{3,4}[mM]", text) or re.search(r"\d{3,4}[mM].*(?:Turf|All Weather)", text):
        print(f"  <{tag.name} class='{tag.get('class', '')}'> {text[:200]}")
        break

# Look specifically for race info section
for div in soup.find_all("div"):
    text = div.get_text(" ", strip=True)
    if "Class" in text and "1200" in text or "1400" in text or "1650" in text or "AWT" in text:
        if len(text) < 500:
            print(f"  Race info div: {text[:300]}")
            break
