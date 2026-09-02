# HKJC Racing Dashboard — production image
#
# Python 3.12 is a hard floor: dashboard.py uses PEP 701 f-strings
# (backslash escapes inside the expression part, ~30 sites) which are a
# SyntaxError on 3.11 and earlier. Do not "simplify" this to 3.11.
# The upper bound matters too — the sqlite fast paths in dashboard.py carry
# comments about segfaults under 3.14 / pandas 3.0, so we pin 3.12 exactly.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    TZ=Asia/Hong_Kong

WORKDIR /app

# System deps: libgl1/libglib2.0-0 for cv2 (rapidocr), fonts for the PDF
# builder + matplotlib (noto-cjk covers the CJK horse names), tzdata so HKT
# conversions resolve, curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        fonts-liberation \
        fonts-noto-cjk \
        tzdata \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so the layer caches across code-only deploys.
COPY requirements.txt constraints.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt -c constraints.txt

# Chromium for the racecard scraper's JS-rendered fallback. Baked into the
# image so scrape_hkjc_racecard.py never has to shell out to
# `playwright install` at request time on a running machine.
RUN playwright install --with-deps chromium

COPY . /app
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["streamlit", "run", "dashboard.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
