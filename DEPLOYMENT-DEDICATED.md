# Stack Nautiljon avec VPN dedie

Utiliser `compose.nautiljon-dedicated.yml` et les variables de
`nautiljon-dedicated.env.example` dans un nouveau stack Portainer Docker Standalone.
FlareSolverr reste dans son stack existant sur le reseau externe `torrent_vpn_share`.

## Configuration

- Renseigner `NAUTILJON_NORDVPN_WIREGUARD_PRIVATE_KEY` avec la cle NordVPN existante.
- Generer une cle API locale avec `docker run --rm qmcgaw/gluetun genkey`, puis
  renseigner `GLUETUN_CONTROL_API_KEY`. Ne pas utiliser la cle WireGuard comme cle API.
- Garder des selections de pays disjointes entre les deux VPN : par defaut
  Pays-Bas pour Nautiljon, France pour le VPN torrents existant.
- Conserver le chemin `NAUTILJON_HOST_OUTPUT` actuel pour retrouver exports et checkpoints.
- Le dossier Gluetun est distinct de celui du VPN torrents. Ne pas reutiliser ce dernier.

Le proxy du scraper est fixe a `http://gluetun-nautiljon:8888` dans ce stack pour
eviter qu une ancienne variable pointe encore vers le VPN torrents. Aucun port
du nouveau VPN n est publie sur l hote. L API de controle ecoute sur loopback.
La rotation reste desactivee par defaut (`NAUTILJON_GLUETUN_AUTO_ROTATE=false`).

## Migration

1. Arreter l ancien scraper. Retirer uniquement son service de son ancien stack
   et redeployer celui-ci pour liberer le nom `nautiljon-scraper`. Conserver tous
   les dossiers de donnees et les services torrents/FlareSolverr.
2. Si un Gluetun dedie nomme `Gluetun-Nautiljon_WG` existe deja, ne pas deployer
   une seconde instance du meme nom : migrer sa definition dans ce stack.
3. Creer le nouveau stack avec le Compose complet et ses variables renseignees.
4. Verifier que Gluetun devient healthy et que les IP affichees par le scraper
   et FlareSolverr sont identiques. Le scraper reprend ses checkpoints.

`depends_on` attend la sante de Gluetun et propage les redemarrages explicites
geres par Compose. Ce n est pas un superviseur des actions Docker/Portainer
individuelles ni des crashes. Mettre a jour les deux services ensemble lors du
remplacement du conteneur VPN.

Le mode `monthly` execute un cycle puis termine. Son nom ne programme pas son
lancement chaque mois : conserver le planificateur existant pour lancer le service.

## Verification locale

La configuration exige les deux secrets avant interpolation. Pour verifier la
syntaxe hors production, utiliser des valeurs factices dans ces variables et
`docker compose -f compose.nautiljon-dedicated.yml config --quiet`.
