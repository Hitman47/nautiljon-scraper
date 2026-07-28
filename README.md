# Nautiljon Scraper

Scraper des fiches manga Nautiljon utilisant FlareSolverr pour franchir la
verification Cloudflare sans intervention manuelle. Selenium reste disponible
comme outil de diagnostic. Le scraper importe les CSV existants et produit un
diff mensuel avec reprise apres interruption.

## Commandes

```bash
python scraper_nautiljon.py selftest
python scraper_nautiljon.py import-csv
python scraper_nautiljon.py browser-smoke
python scraper_nautiljon.py browser-test
python scraper_nautiljon.py flaresolverr-test
python scraper_nautiljon.py diagnose
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
|-- control/       resultats des tests sans remplacement des lettres finales
|-- letter-cache/  lettres validees reutilisables pendant 30 jours
|-- exports/       exports consolides finalises
|-- discovery/     candidats RSS non exhaustifs
`-- state/         dernier run et dernier succes complet
```

Le scraper reutilise le conteneur `flaresolverr` du stack de recherche. Gluetun
et FlareSolverr sont joignables sur le reseau externe `torrent_vpn_share`. La
session reservee a Nautiljon utilise le proxy HTTP de Gluetun, sans modifier le
comportement des autres clients de FlareSolverr. Elle est reutilisee pendant
toute une execution puis fermee proprement.

Le fichier `state/last_diff_success.json` n'est ecrit qu'apres un passage sans
erreur sur les 27 lettres et apres validation des exports. Un essai limite ou
interrompu ecrit `state/last_diff_run.json` avec l'etat `PARTIAL` ou `FAILED`.

## Variables principales

```text
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_HOST_BROWSER_PROFILE=/media/nvme0n1p1/AppData/NautiljonScraper/browser-profile
NAUTILJON_COMMAND=flaresolverr-test
NAUTILJON_BACKEND=flaresolverr
NAUTILJON_FLARESOLVERR_URL=http://flaresolverr:8191/v1
NAUTILJON_FLARESOLVERR_PROXY_URL=http://gluetun-nord:8888
NAUTILJON_FLARESOLVERR_PROXY_USERNAME=
NAUTILJON_FLARESOLVERR_PROXY_PASSWORD=
NAUTILJON_FLARESOLVERR_TIMEOUT_MS=120000
NAUTILJON_FLARESOLVERR_STARTUP_ATTEMPTS=30
NAUTILJON_FLARESOLVERR_STARTUP_DELAY=2
NAUTILJON_DELAY_MIN=2.0
NAUTILJON_DELAY_MAX=5.0
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_REFRESH_STALE_DAYS=180
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=1g
NAUTILJON_MEMSWAP_LIMIT=1g
NAUTILJON_SHM_SIZE=512m
```

## Garanties

- `flaresolverr-test` compare les IP puis teste un listing et une fiche sans exporter.
- Un diff est refuse si FlareSolverr et Gluetun n'utilisent pas la meme IP publique.
- Une IP explicitement interdite par Nautiljon arrete le diff sans nouvelle tentative.
- Selenium reste disponible avec `browser-test` pour le diagnostic.
- Les CSV utilisent `;` et `utf-8-sig`.
- Seuls `yaoi` et `yuri` sont exclus.
- Les ecritures finales utilisent des fichiers temporaires puis un remplacement.
- Une erreur de listing ou de detail ne remplace pas le fichier final de la lettre.
- Le lien de pagination exact est suivi et conserve dans le checkpoint.
- Une disparition superieure a 15 % de la base bloque la finalisation.
- Un controle avec `NAUTILJON_DROP_MISSING=false` ecrit dans `output/control/`.
- Un diff complet reutilise les lettres validees depuis moins de 30 jours si `FORCE=false`.
- Le RSS reste separe du diff et ne peut pas valider un export mensuel.
- Le conteneur est limite par le compose a 1 CPU et 1 Gio de RAM.
