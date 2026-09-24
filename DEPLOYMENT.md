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
NAUTILJON_IMAGE=ghcr.io/hitman47/nautiljon-scraper:latest
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
NAUTILJON_CPUS=0.50
NAUTILJON_MEM_LIMIT=768m
NAUTILJON_MEMSWAP_LIMIT=768m
NAUTILJON_SHM_SIZE=256m
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
NAUTILJON_DETAIL_WINDOW_SIZE=5
NAUTILJON_DETAIL_WINDOW_SECONDS=900
NAUTILJON_LETTER_PAUSE_MIN=20
NAUTILJON_LETTER_PAUSE_MAX=45
NAUTILJON_FAILURE_PAUSE_MIN=120
NAUTILJON_FAILURE_PAUSE_MAX=300
NAUTILJON_BLOCK_COOLDOWN_HOURS=24
NAUTILJON_PAGE_FAILURE_RETRIES=1
NAUTILJON_ABORT_AFTER_DETAIL_FAILURES=2
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_MAX_CONFIRMED_MISSING_RATIO=0.35
NAUTILJON_COVERAGE_CONFIRMATION_MIN_SECONDS=3600
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
NAUTILJON_DETAIL_WINDOW_SIZE=5
NAUTILJON_DETAIL_WINDOW_SECONDS=900
NAUTILJON_LETTER_PAUSE_MIN=20
NAUTILJON_LETTER_PAUSE_MAX=45
NAUTILJON_FAILURE_PAUSE_MIN=120
NAUTILJON_FAILURE_PAUSE_MAX=300
NAUTILJON_BLOCK_COOLDOWN_HOURS=24
NAUTILJON_PAGE_FAILURE_RETRIES=1
NAUTILJON_ABORT_AFTER_DETAIL_FAILURES=2
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
NAUTILJON_MAX_MISSING_RATIO=0.15
NAUTILJON_MAX_CONFIRMED_MISSING_RATIO=0.35
NAUTILJON_COVERAGE_CONFIRMATION_MIN_SECONDS=3600
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
disparaissent du listing, la premiere observation bloque la finalisation. La
suppression n'est acceptee qu'apres un second listing complet, espace d'au moins
une heure, qui retrouve exactement les memes absences. Un ecart superieur a 35 %
reste toujours bloque.
Une page indiquant que l'IP est interdite pour abus provoque egalement un arret
immediat, sans trois nouvelles tentatives, avec conservation du checkpoint.
Un challenge Cloudflare non resolu est traite comme un blocage. Le fichier
`output/state/access_cooldown.json` interdit alors un nouveau diff pendant 24
heures sur l'IP publique concernee, y compris avec
`NAUTILJON_FORCE_SCRAPE=true`. Apres un changement de sortie Gluetun, le scraper
compare les IP de Gluetun et FlareSolverr puis leve automatiquement la
quarantaine. Les anciens marqueurs sans IP effectuent un unique canari de
migration avant d'etre leves.
Si le canari affiche une case interactive « Verifiez que vous etes humain »,
considerez l'IP comme bloquee. Le projet n'automatise pas le clic et ne tente
pas de contourner les CAPTCHA : attendez la quarantaine ou changez proprement
l'IP de sortie, puis relancez uniquement `flaresolverr-test`.
Si FlareSolverr ne renvoie plus l'index alphabetique, le HTML et ses metadonnees
sont sauvegardes dans `output/debug/flaresolverr_mangas_index_missing_*`. Ne
supprimez pas les exports : l'URL exacte d'une page en cours reste dans le
checkpoint et est prioritaire lors de la reprise.

La cadence par defaut reste prudente sans pauses disproportionnees. Les
navigations sont sequentielles, 4 a 7 secondes les separent, une pause de 45 a
90 secondes intervient toutes les 80 requetes et 20 a 45 secondes separent les
lettres. Les fiches detail attendent 10 a 15 secondes apres chaque lecture et
font une pause de 45 a 75 secondes toutes les 15 fiches. Elles sont en plus
limitees a 5 visites par fenetre glissante de 15 minutes; cette fenetre est
persistee dans `output/state/detail_rate_limit.json` et survit aux redemarrages.
Une valeur `N/A` issue
du listing n'est jamais consideree comme un changement, tandis qu'une nouvelle
valeur exploitable (par exemple un nombre de tomes different) reste detectee.
Une variation de note est actualisee depuis le listing sans ouvrir la fiche et
les champs detail ne peuvent pas ecraser les valeurs fiables du listing.
Les transitions de presentation `0 -> -` et `7 -> 7 (En cours)` sont ignorees.
Un champ de listing modifie est sauvegarde directement; seul un vrai changement
du nombre de tomes impose une nouvelle lecture de la fiche.
Une page en erreur n'est tentee qu'une fois et deux erreurs de fiches consecutives
interrompent la lettre.

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

## Limites du conteneur FlareSolverr

Les limites de `portainer-stack.yml` ne s'appliquent qu'au scraper. Le Chromium
qui traite les pages se trouve dans le conteneur FlareSolverr. Ajoutez dans le
service FlareSolverr de `search-stack`, si sa charge partagee le permet :

```yaml
cpus: "0.75"
mem_limit: 768m
memswap_limit: 768m
shm_size: 256m
pids_limit: 256
```

N'executez pas d'autres travaux FlareSolverr concurrents pendant le diff
Nautiljon. Une session unique est conservee afin de reutiliser les cookies et
de ne pas resoudre le challenge a chaque page.
