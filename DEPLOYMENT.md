# Protocole Portainer Selenium

Le conteneur execute une commande puis s'arrete. Il partage le reseau du Gluetun
existant et utilise Chromium dans un ecran virtuel Xvfb. Aucune fenetre ni action
manuelle n'est necessaire sur le NAS.

## Variables communes

```text
NAUTILJON_IMAGE=ghcr.io/hitman47/nautiljon-scraper:test
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_HOST_BROWSER_PROFILE=/media/nvme0n1p1/AppData/NautiljonScraper/browser-profile
NAUTILJON_BACKEND=selenium
NAUTILJON_BROWSER_HEADLESS=false
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=1g
NAUTILJON_MEMSWAP_LIMIT=1g
NAUTILJON_SHM_SIZE=512m
```

Le profil persistant conserve les cookies et la session. Le bouton de consentement
est clique automatiquement lors de la premiere ouverture.

## 1. Verification du navigateur

```text
NAUTILJON_COMMAND=browser-smoke
```

Resultat obligatoire :

```text
VERDICT NAVIGATEUR: OK
```

Ce test ouvre seulement `about:blank` et ne contacte pas Nautiljon.

## 2. Test Nautiljon Selenium

```text
NAUTILJON_COMMAND=browser-test
NAUTILJON_DIAGNOSE_LETTER=a
```

Le navigateur ouvre `/mangas/`, accepte les cookies, clique sur A, analyse la
premiere page puis ouvre une fiche. Il ne remplace aucun export.

Le seul resultat autorisant la suite est :

```text
VERDICT SELENIUM: PRET POUR DIFF CONTROLE
```

En cas d'echec, les fichiers HTML, PNG et le journal ChromeDriver sont ecrits
dans `output/debug/`.

## 3. Diff controle sur A

Cette etape n'est autorisee qu'apres un `browser-test` reussi :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_LETTERS=a
NAUTILJON_FORCE_SCRAPE=true
NAUTILJON_DROP_MISSING=false
NAUTILJON_RESUME=true
```

Un sous-ensemble termine avec l'etat `PARTIAL` et ne produit jamais de marqueur
mensuel complet.

## 4. Diff mensuel complet

Retirez `NAUTILJON_LETTERS` ou laissez cette variable vide :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_FORCE_SCRAPE=false
NAUTILJON_DROP_MISSING=true
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_DELAY_MIN=2.0
NAUTILJON_DELAY_MAX=5.0
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
```

Le succes exige les 27 lettres, aucune erreur de listing ou de fiche et des
exports finaux valides. Une interruption conserve la page courante et les lettres
deja terminees dans `output/checkpoints/`.

## Import initial

Les CSV par lettre restent dans `output/letters/`. Pour regenerer les JSON :

```text
NAUTILJON_COMMAND=import-csv
```

## RSS

Le RSS reste une commande separee et non exhaustive :

```text
NAUTILJON_COMMAND=discover-rss
NAUTILJON_MERGE_RSS_CANDIDATES=false
```
