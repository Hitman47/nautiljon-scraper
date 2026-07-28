# Protocole Portainer

Le scraper est un conteneur ponctuel : il execute une commande, puis s'arrete.
Dans Portainer, changez `NAUTILJON_COMMAND`, mettez a jour la stack et consultez
les logs du conteneur `nautiljon-scraper`.

## 1. Repertoire de test

Les premiers essais ne doivent pas modifier la production. Creez :

```text
/media/nvme0n1p1/AppData/NautiljonScraper/test-output/letters/
```

Copiez-y les CSV par lettre, puis utilisez ces variables de stack :

```text
NAUTILJON_IMAGE=ghcr.io/hitman47/nautiljon-scraper:test
NAUTILJON_HOST_OUTPUT=/media/nvme0n1p1/AppData/NautiljonScraper/test-output
GLUETUN_CONTAINER=GlueTun-Nord_WG
```

## 2. Import de reference

```text
NAUTILJON_COMMAND=import-csv
```

Le conteneur doit sortir avec le code `0`. Les logs doivent annoncer le nombre
d'URL uniques et ne contenir aucune erreur `CSV illisible`.

## 3. Diagnostic obligatoire

```text
NAUTILJON_COMMAND=diagnose
NAUTILJON_DIAGNOSE_LETTER=a
NAUTILJON_DIAGNOSE_DETAIL_URL=https://www.nautiljon.com/mangas/one+piece.html
```

Cette commande ne modifie aucun fichier. Elle teste l'IP de sortie, `robots.txt`,
le sitemap, quatre routes de listing, une fiche connue et le RSS.

Le seul resultat autorisant la suite est :

```text
VERDICT: PRET POUR UN DIFF CONTROLE
```

`VERDICT: BLOQUE - NE PAS LANCER LE DIFF` et un code de sortie `1` signifient
que Cloudflare ou le reseau bloque encore le scraper.

## 4. Diff controle

Cette etape n'est autorisee que si le diagnostic est vert :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_LETTERS=a
NAUTILJON_FORCE_SCRAPE=true
NAUTILJON_DROP_MISSING=false
```

Un sous-ensemble de lettres se termine volontairement avec l'etat `PARTIAL`.
Il ne produit jamais de marqueur mensuel ni d'export presente comme complet.

## 5. Diff mensuel complet

Retirez `NAUTILJON_LETTERS` ou laissez cette variable vide, puis utilisez :

```text
NAUTILJON_COMMAND=diff
NAUTILJON_FORCE_SCRAPE=false
NAUTILJON_DROP_MISSING=true
NAUTILJON_RESUME=true
NAUTILJON_FLUSH_EVERY=25
NAUTILJON_MIN_DAYS_BETWEEN_DIFF_EXPORTS=30
NAUTILJON_ABORT_AFTER_LISTING_FAILURES=1
```

Un succes exige les 27 lettres, des fichiers finaux valides, aucune erreur de
listing ou de detail et quatre fichiers d'export non vides. Il ecrit :

```text
output/state/last_diff_run.json
output/state/last_diff_success.json
```

Un echec ou une interruption conserve les checkpoints et ecrit seulement
`last_diff_run.json` avec l'etat `FAILED` ou `PARTIAL`. Le redemarrage reprend la
lettre et la page en cours, puis ignore les lettres deja finalisees.

## 6. RSS separe

```text
NAUTILJON_COMMAND=discover-rss
NAUTILJON_MERGE_RSS_CANDIDATES=false
```

Le RSS produit uniquement `output/discovery/nautiljon_rss_candidates.*`. Il
n'est pas exhaustif et ne fait jamais partie du verdict du diff mensuel.

## Reseau et ressources

Le service partage la pile reseau de Gluetun :

```yaml
network_mode: "container:${GLUETUN_CONTAINER:-GlueTun-Nord_WG}"
```

Il reste limite a 1 CPU, 256 Mio de RAM et 256 Mio de memoire totale avec swap.
