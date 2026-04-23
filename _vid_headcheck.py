import urllib.request
urls = [
    'https://racing.hkjc.com/contentAsset/videoplayer_v4/video-player-iframe_v4.html?type=replay-full&date=20250907&no=01&lang=eng&noPTbar=false&noLeading=false&videoParam=PAD',
    'https://racing.hkjc.com/contentAsset/videoplayer_v4/video-player-iframe_v4.html?type=replay-full&date=20260415&no=09&lang=eng&noPTbar=false&noLeading=false&videoParam=PAD',
]
for u in urls:
    try:
        req = urllib.request.Request(u, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            ct = r.headers.get("Content-Type", "?")
            print(f'{r.status} {ct}  ...{u[-70:]}')
    except Exception as e:
        print(f'FAIL {e}  ...{u[-70:]}')
