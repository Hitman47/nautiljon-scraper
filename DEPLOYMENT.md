# Deployment

## Initial import

Put your existing CSV files in:

```text
/media/nvme0n1p1/AppData/NautiljonScraper/output/letters/
```

Deploy the Portainer service with:

```text
NAUTILJON_COMMAND=import-csv
```

This creates per-letter JSON files beside the CSV files and writes a consolidated
export under `output/exports/`.

## Discovery probe

Before enabling the monthly diff, test whether Nautiljon listings are reachable
without Selenium:

```text
NAUTILJON_COMMAND=probe-discovery
NAUTILJON_LETTERS=a
```

Expected result: logs show at least one discovered listing page and a non-zero
number of rows.

## Monthly diff

Use:

```text
NAUTILJON_COMMAND=diff
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_RSS_FALLBACK=true
NAUTILJON_RSS_FEEDS=http://feeds.feedburner.com/nautiljon/NdFI
NAUTILJON_MERGE_RSS_CANDIDATES=false
NAUTILJON_REFRESH_STALE_DAYS=180
```

The diff:

- reads existing per-letter JSON files,
- discovers listing entries for each letter,
- adds new fiches,
- refreshes changed listing entries,
- refreshes stale detail pages,
- writes updated letter JSON/CSV,
- writes a final export,
- writes `output/state/last_diff_success.json`.

Set `NAUTILJON_FORCE_SCRAPE=true` to ignore the 30-day protection once.
Set `NAUTILJON_ABORT_AFTER_LISTING_FAILURES=0` only if you want the diff to
continue through all letters even when listings are blocked.

If listings are blocked, RSS fallback still writes probable new manga fiches to
`output/discovery/nautiljon_rss_candidates.csv`. Keep
`NAUTILJON_MERGE_RSS_CANDIDATES=false` unless you accept incomplete, unverified
rows in the per-letter exports.

## Same Gluetun as Bedetheque

The service uses:

```yaml
network_mode: "container:${GLUETUN_CONTAINER:-GlueTun-Nord_WG}"
```

No `ports:` or `networks:` should be added to this service.
