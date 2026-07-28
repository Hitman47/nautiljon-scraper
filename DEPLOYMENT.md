# Protocole Portainer FlareSolverr

Le conteneur execute une commande puis s'arrete. Les listings et les fiches sont
charges dans une session FlareSolverr unique, sans fenetre ni intervention
manuelle sur le NAS.

## Variables communes

```text
NAUTILJON_IMAGE=ghcr.io/hitman47/nautiljon-scraper:test
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_BACKEND=flaresolverr
NAUTILJON_FLARESOLVERR_URL=http://192.168.1.30:8191/v1
NAUTILJON_FLARESOLVERR_TIMEOUT_MS=120000
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=1g
NAUTILJON_MEMSWAP_LIMIT=1g
```

## 1. Test FlareSolverr

```text
NAUTILJON_COMMAND=flaresolverr-test
NAUTILJON_DIAGNOSE_LETTER=a
```

Ce test ne modifie aucun export. Il verifie :

1. l'acces a l'API FlareSolverr ;
2. l'IP publique de Gluetun et celle de FlareSolverr ;
3. la premiere page du listing A ;
4. une fiche manga.

Le seul resultat autorisant la suite est :

```text
VERDICT FLARESOLVERR: PRET POUR DIFF CONTROLE
```

Si les IP different, placez FlareSolverr derriere le meme Gluetun :

```yaml
network_mode: "container:GlueTun-Nord_WG"
```

Dans cette configuration, utilisez dans le stack Nautiljon :

```text
NAUTILJON_FLARESOLVERR_URL=http://127.0.0.1:8191/v1
```

FlareSolverr et Nautiljon partagent alors l'espace reseau de Gluetun. Un service
qui utilise `network_mode: container:...` ne doit pas declarer sa propre section
`ports`. Si le port 8191 doit rester accessible depuis le LAN, publiez-le dans le
stack Gluetun.

## 2. Diff controle sur A

Cette etape n'est autorisee qu'apres un `flaresolverr-test` reussi :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_LETTERS=a
NAUTILJON_FORCE_SCRAPE=true
NAUTILJON_DROP_MISSING=false
NAUTILJON_RESUME=true
```

Un sous-ensemble termine avec l'etat `PARTIAL` et ne produit jamais de marqueur
mensuel complet.

## 3. Diff mensuel complet

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

## Selenium

`browser-smoke` et `browser-test` restent disponibles pour le diagnostic, mais
ne sont plus le transport recommande pour le diff.
