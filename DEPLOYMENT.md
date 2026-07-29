# Protocole Portainer FlareSolverr

Le conteneur execute une commande puis s'arrete. Les listings et les fiches sont
charges dans une session FlareSolverr unique, sans fenetre ni intervention
manuelle sur le NAS.

Le scraper reutilise le conteneur `flaresolverr` existant dans `search-stack`.
Il ne lance aucune seconde instance. Sa session FlareSolverr recoit un proxy
dedie vers Gluetun, sans changer les sessions de Prowlarr ou des autres clients.

## 1. Reseau partage existant

Gluetun est deja rattache au reseau externe avec l'alias suivant :

```yaml
services:
  gluetun:
    networks:
      torrent_vpn_share:
        aliases:
          - gluetun-nord

networks:
  torrent_vpn_share:
    external: true
```

Le proxy et le mode stealth sont deja actifs dans Gluetun. Le port `8888` n'a
pas besoin d'etre publie sur le LAN.

Le service `flaresolverr` de `search-stack` doit conserver `media_net` et etre
egalement rattache a `torrent_vpn_share`.

## Variables communes

```text
NAUTILJON_IMAGE=ghcr.io/hitman47/nautiljon-scraper:test
GLUETUN_CONTAINER=GlueTun-Nord_WG
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/output
NAUTILJON_BACKEND=flaresolverr
NAUTILJON_FLARESOLVERR_URL=http://flaresolverr:8191/v1
NAUTILJON_FLARESOLVERR_PROXY_URL=http://gluetun-nord:8888
NAUTILJON_FLARESOLVERR_PROXY_USERNAME=
NAUTILJON_FLARESOLVERR_PROXY_PASSWORD=
NAUTILJON_FLARESOLVERR_TIMEOUT_MS=120000
NAUTILJON_FLARESOLVERR_STARTUP_ATTEMPTS=30
NAUTILJON_FLARESOLVERR_STARTUP_DELAY=2
NAUTILJON_CPUS=1.0
NAUTILJON_MEM_LIMIT=1g
NAUTILJON_MEMSWAP_LIMIT=1g
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_REFRESH_STALE_DAYS=30
```

Si le proxy Gluetun est protege par `HTTPPROXY_USER` et
`HTTPPROXY_PASSWORD`, recopiez les memes valeurs dans les deux variables
`NAUTILJON_FLARESOLVERR_PROXY_*` correspondantes.

## 2. Test FlareSolverr

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

Le scraper partage le namespace reseau de Gluetun :

```yaml
network_mode: "container:GlueTun-Nord_WG"
```

Ce namespace est rattache a `torrent_vpn_share`, ce qui rend l'API existante accessible
par son nom Docker :

```text
NAUTILJON_FLARESOLVERR_URL=http://flaresolverr:8191/v1
```

La session Chromium creee dans FlareSolverr utilise quant a elle
`http://gluetun-nord:8888`. Son trafic Nautiljon ressort donc par le VPN. Le
test refuse le diff si son IP publique differe de celle du scraper derriere
Gluetun.

## 3. Diff controle sur A

Cette etape n'est autorisee qu'apres un `flaresolverr-test` reussi :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_LETTERS=a
NAUTILJON_FORCE_SCRAPE=true
NAUTILJON_DROP_MISSING=false
NAUTILJON_RESUME=true
```

Un sous-ensemble termine avec l'etat `PARTIAL` et ne produit jamais de marqueur
mensuel complet. Son resultat est ecrit dans `output/control/`; les fichiers
finaux de `output/letters/` restent inchanges. La lettre validee est aussi copiee
dans `output/letter-cache/` avec un marqueur date.

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
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_REFRESH_STALE_DAYS=30
```

Les trois reglages importants doivent etre modifies ensemble apres un controle :
`NAUTILJON_LETTERS=` (vide), `NAUTILJON_FORCE_SCRAPE=false` et
`NAUTILJON_DROP_MISSING=true`. Le scraper refuse maintenant les 27 lettres si
le mode controle `DROP_MISSING=false` est reste actif.
Il refuse aussi un lancement global si `NAUTILJON_FORCE_SCRAPE=true` est reste
actif ; le mode force est reserve a une liste explicite de lettres.

Le succes exige les 27 lettres, aucune erreur de listing ou de fiche et des
exports finaux valides. Une interruption conserve la page courante et les lettres
deja terminees dans `output/checkpoints/`.

Le scraper suit le lien de pagination exact fourni par Nautiljon et enregistre
ce lien dans le checkpoint. Si plus de 15 % des fiches historiques d'une lettre
disparaissent du listing, la lettre n'est pas remplacee et le diff s'arrete.
Une page indiquant que l'IP est interdite pour abus provoque egalement un arret
immediat, sans trois nouvelles tentatives, avec conservation du checkpoint.

Avec `NAUTILJON_FORCE_SCRAPE=false`, une lettre validee depuis moins de
`NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS` jours est reutilisee sans requete et
promue dans `output/letters/`. Une lettre expiree ou dont le cache est invalide
est automatiquement rescrapee. `NAUTILJON_FORCE_SCRAPE=true` ignore ce cache.

Apres l'ajout des colonnes de parutions VF, les caches crees par une ancienne
version sont volontairement ignores une fois. Un checkpoint de lettre en cours
reste reprenable et ne complete que les fiches VF `En cours` deja parcourues.
Les reglages de delai, de couverture et de conservation des fiches absentes ne
rendent plus un checkpoint incompatible. Tout checkpoint réellement inutilisable
est copie dans `output/checkpoints/archive/` avant son remplacement.

## Import initial

Les CSV par lettre restent dans `output/letters/`. Pour regenerer les JSON :

```text
NAUTILJON_COMMAND=import-csv
```

## Selenium

`browser-smoke` et `browser-test` restent disponibles pour le diagnostic, mais
ne sont plus le transport recommande pour le diff.
