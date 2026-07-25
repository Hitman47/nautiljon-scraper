FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    NAUTILJON_OUT_DIR=/data/output

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper_nautiljon.py .

VOLUME ["/data/output"]

ENTRYPOINT ["python", "scraper_nautiljon.py"]
CMD ["diff"]
