# Nautiljon Scraper

Scraper Selenium des fiches manga Nautiljon. Il reprend le parcours du script PC :
Chromium ouvre l'index manga, accepte les cookies, clique sur une lettre puis
parcourt les pages. Il importe aussi les CSV existants et produit un diff mensuel
avec reprise apres interruption.

## Commandes

```bash
python scraper_nautiljon.py selftest
python scraper_nautiljon.py import-csv
python scraper_nautiljon.py browser-smoke
python scraper_nautiljon.py browser-test
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
|-- exports/       exports consolides finalises
|-- discovery/     candidats RSS non exhaustifs
`-- state/         dernier run et dernier succes complet
```

Le profil Chromium persistant est monte separement dans `/data/browser-profile`.
Il conserve le consentement cookies et la session entre deux executions.

Le fichier `state/last_diff_success.json` n'est ecrit qu'apres un passage sans
erreur sur les 27 lettres et apres validation des exports. Un essai limite ou
interrompu ecrit `state/last_diff_run.json` avec l'etat `PARTIAL` ou `FAILED`.

## Variables principales

```text
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_HOST_BROWSER_PROFILE=/media/nvme0n1p1/AppData/NautiljonScraper/browser-profile
NAUTILJON_COMMAND=browser-test
NAUTILJON_BACKEND=selenium
NAUTILJON_BROWSER_HEADLESS=false
NAUTILJON_BROWSER_ATTACH=true
NAUTILJON_CLOUDFLARE_WAIT_SECONDS=120
NAUTILJON_DELAY_MIN=2.0
NAUTILJON_DELAY_MAX=5.0
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_REFRESH_STALE_DAYS=180
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=1g
NAUTILJON_MEMSWAP_LIMIT=1g
NAUTILJON_SHM_SIZE=512m
```

## Garanties

- `browser-test` ne remplace aucun export et teste un listing puis une fiche.
- Chromium fonctionne en mode graphique dans Xvfb, sans intervention manuelle.
- Le consentement cookies est clique automatiquement et le profil est persistant.
- Les CSV utilisent `;` et `utf-8-sig`.
- Seuls `yaoi` et `yuri` sont exclus.
- Les ecritures finales utilisent des fichiers temporaires puis un remplacement.
- Une erreur de listing ou de detail ne remplace pas le fichier final de la lettre.
- Le RSS reste separe du diff et ne peut pas valider un export mensuel.
- Le conteneur est limite par le compose a 1 CPU et 1 Gio de RAM.
