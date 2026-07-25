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

## Same Gluetun as Bedetheque

The service uses:

```yaml
network_mode: "container:${GLUETUN_CONTAINER:-GlueTun-Nord_WG}"
```

No `ports:` or `networks:` should be added to this service.
