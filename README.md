# Nautiljon Scraper

Scraper HTTP des fiches manga Nautiljon, sans Selenium. Il importe les CSV
existants, maintient des fichiers par lettre et peut produire un diff mensuel
avec reprise apres interruption.

## Commandes

```bash
python scraper_nautiljon.py selftest
python scraper_nautiljon.py import-csv
python scraper_nautiljon.py diagnose
python scraper_nautiljon.py probe-discovery --letters a --max-pages 1
python scraper_nautiljon.py diff
python scraper_nautiljon.py discover-rss
python scraper_nautiljon.py concat
```

Consultez [DEPLOYMENT.md](DEPLOYMENT.md) pour le protocole Portainer complet.

## Images GHCR

```text
ghcr.io/hitman47/nautiljon-scraper:latest  # production, branche main
ghcr.io/hitman47/nautiljon-scraper:test    # validation, branches codex/*
```

## Organisation des donnees

```text
output/
|-- letters/       CSV et JSON finalises par lettre
|-- checkpoints/   reprise de la page et de la lettre en cours
|-- exports/       exports consolides finalises
|-- discovery/     candidats RSS non exhaustifs
`-- state/         dernier run et dernier succes complet
```

Le fichier `state/last_diff_success.json` n'est ecrit qu'apres un passage sans
erreur sur les 27 lettres et apres validation des exports. Un essai limite ou
interrompu ecrit `state/last_diff_run.json` avec l'etat `PARTIAL` ou `FAILED`.

## Variables principales

```text
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_COMMAND=diagnose
NAUTILJON_DELAY_MIN=2.0
NAUTILJON_DELAY_MAX=5.0
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_REFRESH_STALE_DAYS=180
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=256m
NAUTILJON_MEMSWAP_LIMIT=256m
```

## Garanties

- `diagnose` ne cree et ne modifie aucun fichier.
- Les CSV utilisent `;` et `utf-8-sig`.
- Seuls `yaoi` et `yuri` sont exclus.
- Les ecritures finales utilisent des fichiers temporaires puis un remplacement.
- Une erreur de listing ou de detail ne remplace pas le fichier final de la lettre.
- Le RSS reste separe du diff et ne peut pas valider un export mensuel.
- Le conteneur est limite par le compose a 1 CPU et 256 Mio de RAM.
