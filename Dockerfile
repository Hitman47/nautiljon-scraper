FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    NAUTILJON_OUT_DIR=/data/output \
    NAUTILJON_CHROME_BINARY=/usr/bin/chromium \
    NAUTILJON_CHROMEDRIVER=/usr/bin/chromedriver \
    NAUTILJON_BROWSER_PROFILE=/data/browser-profile

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-driver \
        fonts-liberation \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper_nautiljon.py .

VOLUME ["/data/output", "/data/browser-profile"]

ENTRYPOINT ["xvfb-run", "-a", "--server-args=-screen 0 1365x900x24 -nolisten tcp", "python", "scraper_nautiljon.py"]
CMD ["diff"]
