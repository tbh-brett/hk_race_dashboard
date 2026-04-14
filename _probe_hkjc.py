"""Quick probe to inspect HKJC racecard HTML for embedded data."""
import re, requests

url = "https://racing.hkjc.com/en-us/local/information/racecard"
params = {"racedate": "2026/04/01", "Racecourse": "ST", "RaceNo": "1"}
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
resp = requests.get(url, params=params, headers=headers, timeout=30)
html = resp.text

print(f"Status: {resp.status_code}, HTML length: {len(html)}")

# Look for script tags with horse/race data
scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
print(f"\nTotal script tags: {len(scripts)}")
for i, s in enumerate(scripts):
    keywords = ["horse", "runner", "starter", "racecard", "raceno", "horsename"]
    found = [kw for kw in keywords if kw in s.lower()]
    if found:
        print(f"\n--- Script {i} (len={len(s)}, keywords: {found}) ---")
        print(s[:800])
        print("...")

# Check for window.__ variables
window_vars = re.findall(r"window\.(\w+)\s*=", html)
if window_vars:
    print(f"\nWindow vars: {window_vars}")

# Check for AJAX/fetch URLs in scripts  
for i, s in enumerate(scripts):
    urls = re.findall(r'["\'](/[^"\']*(?:race|card|horse|runner)[^"\']*)["\']', s, re.I)
    if urls:
        print(f"\nScript {i} URLs: {urls[:10]}")

# Check for embedded table
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")
tables = soup.find_all("table")
print(f"\nTables found: {len(tables)}")
for i, t in enumerate(tables):
    rows = t.find_all("tr")
    text = t.get_text(" ", strip=True)[:200]
    print(f"  Table {i}: {len(rows)} rows, text: {text}")

# Check for iframe
iframes = soup.find_all("iframe")
print(f"\nIframes: {len(iframes)}")
for iframe in iframes:
    print(f"  src: {iframe.get('src', 'no-src')}")

# Look for ajaxNav patterns (seen in HKJC pages)
ajax_calls = re.findall(r"ajaxNav\(['\"]([^'\"]+)", html)
if ajax_calls:
    print(f"\najaxNav calls: {ajax_calls[:10]}")

# Check for data loading URL patterns
fetch_urls = re.findall(r'(?:fetch|ajax|get|post)\s*\(\s*["\']([^"\']+)', html, re.I)
if fetch_urls:
    print(f"\nFetch/Ajax URLs: {fetch_urls[:10]}")
