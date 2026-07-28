# Nautiljon scraper

HTTP scraper for Nautiljon manga data, designed to run without Selenium.

It can import existing CSV exports from `output/letters/`, rebuild per-letter JSON
files, then run a monthly diff that discovers listing entries and refreshes only
new, changed, or stale detail pages.

## Commands

```bash
python scraper_nautiljon.py selftest
python scraper_nautiljon.py import-csv
python scraper_nautiljon.py probe-discovery --letters a --max-pages 1
python scraper_nautiljon.py diff
python scraper_nautiljon.py concat
```

Expected NAS layout:

```text
/media/nvme0n1p1/AppData/NautiljonScraper/output/
├─ letters/
│  ├─ existing CSV files
│  ├─ nautiljon_lettre_A.json
│  └─ ...
├─ exports/
├─ checkpoints/
├─ state/
└─ debug/
```

The initial import reads CSV files directly from `output/letters/`; no separate
input folder is needed.

## Docker

```bash
docker build -t nautiljon-scraper .
docker run --rm -v /media/nvme0n1p1/AppData/NautiljonScraper/output:/data/output nautiljon-scraper import-csv
docker run --rm -v /media/nvme0n1p1/AppData/NautiljonScraper/output:/data/output nautiljon-scraper diff
```

## GHCR

The GitHub Actions workflow publishes:

```text
ghcr.io/hitman47/nautiljon-scraper:latest
```

## Portainer

Use `portainer-stack.yml`, or merge its `nautiljon` service into the same stack as
Bedetheque.

Recommended variables:

```text
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_COMMAND=diff
NAUTILJON_DELAY_MIN=2.0
NAUTILJON_DELAY_MAX=5.0
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_RSS_FALLBACK=true
NAUTILJON_RSS_FEEDS=http://feeds.feedburner.com/nautiljon/NdFI
NAUTILJON_MERGE_RSS_CANDIDATES=false
NAUTILJON_REFRESH_STALE_DAYS=180
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=256m
NAUTILJON_MEMSWAP_LIMIT=256m
```

First run after placing CSV files in `output/letters/`:

```text
NAUTILJON_COMMAND=import-csv
```

Then switch back to:

```text
NAUTILJON_COMMAND=diff
```

For a cautious discovery test:

```text
NAUTILJON_COMMAND=probe-discovery
NAUTILJON_LETTERS=a
```

To discover recent manga candidates from Nautiljon RSS without touching the
letter exports:

```text
NAUTILJON_COMMAND=discover-rss
```

## Notes

- No Selenium or browser is used.
- Missing values are exported as `N/A`.
- CSV files use `;` and `utf-8-sig`.
- Only `yaoi` and `yuri` are filtered.
- If Nautiljon blocks direct HTTP listings, `probe-discovery` will fail cleanly;
  the existing CSV import and concat still work.
- During `diff`, `NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1` stops the run after
  the first fully inaccessible letter. Set it to `0` to disable this guard.
- When listings are blocked, `NAUTILJON_RSS_FALLBACK=true` writes probable new
  manga fiches to `output/discovery/nautiljon_rss_candidates.csv`. They stay out
  of the main export unless `NAUTILJON_MERGE_RSS_CANDIDATES=true`.
