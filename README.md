# Nautiljon Scraper

Scraper prudent des fiches manga Nautiljon. FlareSolverr est le transport
principal et Selenium/Chromium reste disponible pour le diagnostic. Aucun
transport ne garantit le passage de Cloudflare : un challenge non resolu arrete
immediatement le run et place le scraper en quarantaine. Le scraper importe les
CSV existants et produit un diff mensuel avec reprise apres interruption.

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
Le fichier [portainer.env.example](portainer.env.example) contient le profil
Portainer de production pret a copier.

## Images GHCR

```text
ghcr.io/hitman47/nautiljon-scraper:latest  # production, branche main
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
NAUTILJON_DELAY_MIN=4.0
NAUTILJON_DELAY_MAX=7.0
NAUTILJON_BATCH_SIZE=80
NAUTILJON_BATCH_PAUSE_MIN=45
NAUTILJON_BATCH_PAUSE_MAX=90
NAUTILJON_DETAIL_DELAY_MIN=10
NAUTILJON_DETAIL_DELAY_MAX=15
NAUTILJON_DETAIL_BATCH_SIZE=15
NAUTILJON_DETAIL_BATCH_PAUSE_MIN=45
NAUTILJON_DETAIL_BATCH_PAUSE_MAX=75
NAUTILJON_LETTER_PAUSE_MIN=20
NAUTILJON_LETTER_PAUSE_MAX=45
NAUTILJON_FAILURE_PAUSE_MIN=120
NAUTILJON_FAILURE_PAUSE_MAX=300
NAUTILJON_BLOCK_COOLDOWN_HOURS=24
NAUTILJON_PAGE_FAILURE_RETRIES=1
NAUTILJON_ABORT_AFTER_DETAIL_FAILURES=2
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_MAX_CONFIRMED_MISSING_RATIO=0.35
NAUTILJON_COVERAGE_CONFIRMATION_MIN_SECONDS=3600
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_REFRESH_STALE_DAYS=30
NAUTILJON_CPUS=0.50
NAUTILJON_MEM_LIMIT=768m
NAUTILJON_MEMSWAP_LIMIT=768m
NAUTILJON_SHM_SIZE=256m
```

## Garanties

- `flaresolverr-test` compare les IP puis teste un listing et une fiche sans exporter.
- Un diff est refuse si FlareSolverr et Gluetun n'utilisent pas la meme IP publique.
- Une IP explicitement interdite par Nautiljon arrete le diff sans nouvelle tentative,
  y compris si le blocage apparait pendant une fiche detail.
- Un challenge Cloudflare non resolu a le meme effet et interdit tout nouveau diff
  pendant 24 heures sur la meme IP. La quarantaine est levee automatiquement si
  Gluetun et FlareSolverr confirment une nouvelle IP publique. `--force` ne la contourne pas.
- Une page FlareSolverr sans index alphabetique est conservee dans
  `output/debug/flaresolverr_mangas_index_missing_*.html` puis traitee comme un
  blocage. Une reprise utilise d'abord l'URL exacte stockee dans son checkpoint.
- Une case interactive « Verifiez que vous etes humain » est un verdict de blocage :
  le scraper ne tente ni de la cliquer ni de contourner un CAPTCHA. Changez
  d'IP de sortie ou attendez la fin de la quarantaine avant un nouveau canari.
- Les acces Nautiljon sont strictement sequentiels : 4 a 7 secondes entre deux
  navigations, 45 a 90 secondes toutes les 80 requetes et 20 a 45 secondes entre lettres.
  Les fiches detail, plus sensibles, attendent 10 a 15 secondes et font une pause
  de 45 a 75 secondes toutes les 15 fiches.
- Une valeur absente (`N/A`) dans un listing ne remplace jamais une valeur connue
  et ne declenche pas de consultation de fiche. Une vraie nouvelle valeur reste
  comparee normalement, notamment pour detecter un changement du nombre de tomes.
- Une page inaccessible n'est pas rechargee en boucle. Deux echecs de fiches
  consecutifs interrompent la lettre en conservant son checkpoint.
- Selenium reste disponible avec `browser-test` pour le diagnostic.
- Les CSV utilisent `;` et `utf-8-sig`.
- Seuls `yaoi` et `yuri` sont exclus.
- Les ecritures finales utilisent des fichiers temporaires puis un remplacement.
- Une erreur de listing ou de detail ne remplace pas le fichier final de la lettre.
- Le lien de pagination exact est suivi et conserve dans le checkpoint.
- Une progression reste reprenable si le delai de rafraichissement, le seuil de
  couverture ou le mode controle/final change. Un checkpoint inutilisable est
  archive dans `output/checkpoints/archive/` avant tout remplacement.
- Une disparition superieure a 15 % de la base bloque le premier listing. Elle
  n'est acceptee qu'apres un second listing complet, espace d'au moins une heure,
  qui retrouve exactement les memes absences. Au-dela de 35 %, la finalisation
  reste toujours bloquee.
- Un controle avec `NAUTILJON_DROP_MISSING=false` ecrit dans `output/control/`.
- Un lancement des 27 lettres avec `NAUTILJON_DROP_MISSING=false` est refuse :
  le mode controle doit toujours utiliser une liste explicite de lettres.
- Un lancement global avec `NAUTILJON_FORCE_SCRAPE=true` est egalement refuse.
  Le mode force exige une liste explicite et limitee de lettres.
- `NAUTILJON_REFRESH_STALE_DAYS` ne recharge que les fiches dont `Nb volumes VF`
  est indique `En cours`. Les series VF terminees ne sont pas revisitees par
  anciennete. Les colonnes `dernier_tome_vf_*` et `prochain_tome_vf_*` viennent
  directement de la fiche serie, sans ouvrir les fiches des tomes.
- Un diff complet reutilise les lettres validees depuis moins de 30 jours si `FORCE=false`.
- Le RSS reste separe du diff et ne peut pas valider un export mensuel.
- Le conteneur est limite par le compose a 0,5 CPU, 768 Mio de RAM, 256 Mio de
  memoire partagee et 256 processus. Le navigateur reste mono-session.

Les limites ci-dessus concernent le conteneur du scraper. Lorsque le backend
`flaresolverr` est utilise, le conteneur FlareSolverr execute son propre Chromium
et doit recevoir des limites equivalentes dans son stack (`cpus: 0.75`,
`mem_limit: 768m`, `memswap_limit: 768m`, `shm_size: 256m`). Ne lancez pas de
requete FlareSolverr concurrente pendant le diff Nautiljon.
