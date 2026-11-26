"""Réglages : valeurs par défaut < fichier TOML < variables d'environnement AMBI_<NOM>.

Les réglages d'installation (adresses, position des segments, clé ADB) n'ont pas de valeur par défaut utile :
`verifier()` refuse de démarrer tant qu'ils manquent. Les réglages de rendu ont les valeurs réglées à l'œil.
"""
import dataclasses
import os
import tomllib
from dataclasses import dataclass, fields

# 15 segments répartis régulièrement sur le périmètre : à REMPLACER par la mesure de votre installation (voir README).
SEGMENTS_PAR_DEFAUT = tuple((round((i + 0.5) * 4 / 15, 3), 0.14) for i in range(15))


@dataclass(frozen=True)
class Reglages:
    # ---- installation -------------------------------------------------------------------------------------------
    fire_ip: str = ""                   # Fire TV (débogage ADB réseau activé)
    fire_port: int = 5555
    adb_cle: str = "adbkey"             # clé privée ADB (relative au fichier de configuration) ; ne JAMAIS la versionner
    ruban_ip: str = ""                  # ruban Govee LAN du tour de l'écran (mode razer, UDP 4003)
    ruban_port: int = 4003
    flux_ip: str = ""                   # Home Assistant : trame du 2e ruban (composant govee-ble-lights patché) ; "" = pas de 2e ruban
    flux_port: int = 4011
    bas_segments: int = 15              # nombre de segments du 2e ruban (la trame fait 3 octets par segment)
    ctl_port: int = 8099
    ctl_ecoute: str = "0.0.0.0"
    ctl_autorises: tuple = ("127.0.0.1",)   # adresses autorisées à appeler le service de contrôle (ajouter celle de Home Assistant)
    # (s du centre, écart-type en s) par segment, dans l'ordre du ruban. s : périmètre de l'image, 0 = coin haut-gauche,
    # 0-1 le haut (G->D), 1-2 le côté droit (H->B), 2-3 le bas (D->G), 3-4 le côté gauche (B->H).
    segments: tuple = SEGMENTS_PAR_DEFAUT
    balance: tuple = (1.0, 1.0, 1.0)    # balance des blancs du ruban (R, V, B)
    gamma: tuple = (1.45, 1.45, 1.45)   # courbe gamma LED par canal (R, V, B) : coupe le bleu délavant sans assombrir les tons moyens

    # ---- capture ------------------------------------------------------------------------------------------------
    capture: str = "h264"               # "h264" : encodeur de la Fire TV + ffmpeg (ne la ralentit pas) ; "raw" : images brutes, CHARGE la Fire TV
    raw_facteur: int = 2                # mode raw : l'image demandée fait raw_facteur x 96x54 et est réduite par moyenne de blocs (2 = 192x108)
    taille: str = "320x180"             # mode h264 : taille du flux screenrecord
    debit: int = 1500000
    aud_attente: float = 0.012          # silence du flux (s) après lequel l'image est considérée complète (évite les images H.264 coupées)
    flush: int = 1                      # ffmpeg -flush_packets

    # ---- cadence et lissage -------------------------------------------------------------------------------------
    hz: float = 30.0                    # trames/s vers le ruban du tour
    bas_hz: float = 8.0                 # trames/s vers le 2e ruban (aligné sur HA ips=8, évite le battement)
    tau_haut: float = 0.025             # lissage : montée rapide (un flash doit passer)
    tau_bas: float = 0.06               # lissage : descente plus douce
    tau_flash: float = 0.012            # montée quasi instantanée sur événement local fort (éclair, explosion, laser)
    flash_seuil: float = 40.0           # écart de canal sur un segment (0-255) qui déclenche la réponse flash locale
    lissage_oklab: bool = True          # lissage perceptuel en Oklab (transitions soyeuses sans passage au gris/brun)
    tau_coupure: float = 0.04           # conservé pour compatibilité (le haut saute net sur coupure)
    coupure: float = 25.0               # écart moyen de couleur (0-255) au-delà duquel c'est un changement de plan
    anti_scint: int = 2                 # un segment ne bouge que si un canal change d'au moins ça
    lent: float = 1.0                   # lissage x lent sur les petits changements quand l'image est agitée (voir lent_calme)
    mouvement: float = 2.0              # accentue les changements récents (couleur actuelle - fond lent) pour mieux voir le mouvement ; 0 = aucun
    lent_calme: float = 1.5             # lissage x lent quand l'image est CALME (discussion, plan fixe) ; `lent` s'applique quand elle est agitée
    agitation_min: float = 1.0          # agitation moyenne de l'image (écart de couleur par image, 0-255) sous laquelle l'image est calme : accentuation à 0
    agitation_max: float = 6.0          # ... et au-dessus de laquelle l'effet est complet (intro de Dragon Ball, combat, éclairs)
    tau_fond: float = 0.5               # s : constante de temps du « fond lent » de l'accentuation du mouvement
    evt: float = 8.0                    # un canal qui bouge de moins que ça est « lent » ; au-delà de evt+35, « rapide »

    # ---- zones --------------------------------------------------------------------------------------------------
    prof_h: float = 0.15                # profondeur d'échantillonnage haut/bas (part de la hauteur)
    prof_v: float = 0.11                # idem côtés (part de la largeur)
    prof_p: float = 2.0                 # forme en profondeur : 2 = gaussienne (coupe le centre), 1 = exponentielle
    seg_k: float = 0.60                 # largeur de la fenêtre d'un segment (x l'écart mesuré)
    seg_p: float = 3.0                  # forme de la fenêtre : 2 = gaussienne, plus haut = bords plus nets
    nettete: float = 0.40               # netteté spatiale sur l'anneau (masque flou anti-halo entre segments voisins) ; 0 = aucun
    bas_bord: float = 0.4               # ancrage du 2e ruban vers le tiers inférieur de l'écran (0 = moyenne uniforme)
    bord_noir: int = 12                 # pixel max sous ce seuil = bande noire (letterbox)
    bord_duree: float = 0.8             # une bande noire n'est prise en compte qu'après ce délai (s)

    # ---- couleur ------------------------------------------------------------------------------------------------
    lin: float = 1.8                    # moyenne en lumière ; 2.2 = linéaire strict, 1.0 = moyenne sRGB
    vif: float = 0.0                    # 0 = luminosité fidèle en surface (évite qu'un filet lumineux inonde les voisins), 1 = pondéré vivacité
    dominante: float = 1.0              # zone aux couleurs qui se contredisent : prend la couleur DOMINANTE au lieu de la moyenne (mauve + fond clair = beige) ; 0 = aucune
    vch: float = 0.8                    # part maximale de la teinte « vive » dans la couleur de zone (modulée par la couverture colorée)
    vexp: float = 2.2                   # exposant du poids de vivacité
    plancher_v: float = 0.05            # poids minimal d'un pixel terne (limite le ratio vif/fond)
    sat: float = 1.15                   # conservé pour rétro-compatibilité TOML
    vibr: float = 0.6                   # conservé pour rétro-compatibilité TOML
    vibr_ok: float = 0.70               # vibrance perceptuelle Oklab LCh (préserve la teinte)
    peau: float = 0.85                  # atténuation perceptuelle Oklab + frein vibrance sur les teintes chair
    toe: float = 22.0                   # seuil noir pré-gain pour nettoyer le bruit noir vidéo (même à gain x3) ; 0 = aucun
    presence: float = 26.0              # plancher linéaire minimal pour une couleur franche mais sombre
    jaune_v: float = 1.0                # frein vert orthogonal sur le jaune pur (min(R,V)-B) en linéaire ; 1.0 = neutre
    bleu_v: float = 0.09                # bleu pur des LED qui tire sur le violet : ajout de vert en linéaire
    gain: float = 1.22
    plancher: int = 6                   # sous ce niveau, la LED reste éteinte
    genou: float = 200.0                # plafond de luminosité : compression douce vers lum_max
    lum_max: float = 246.0

    # ---- gain automatique ---------------------------------------------------------------------------------------
    cible_pic: float = 205.0            # le segment le plus clair vise ce niveau
    gain_max: float = 3.0
    tau_pic: float = 3.0                # le gain remonte lentement, chute tout de suite

    # ---- 2e ruban (ambiance : une seule couleur) ----------------------------------------------------------------
    bas_monte: float = 0.5
    bas_descend: float = 1.2
    bas_coupure_tau: float = 0.25       # sur un changement de plan, le bas glisse plus vite vers la nouvelle scène
    bas_gain_max: float = 2.2           # gain auto doux pour le bas (H6125 est ~2x moins lumineux que le H619A)
    bas_gamma: tuple = (1.45, 1.45, 1.45)  # courbe gamma LED dédiée au 2e ruban (R, V, B)
    bas_balance: tuple = ()             # balance dédiée au 2e ruban ; () = repli sur `balance`
    bas_plancher_actif: int = 12        # plancher minimal visible sur H6125 quand la scène n'est pas noire (~4 %)
    bas_flash: float = 70.0             # saut de luminosité en une trame qui déclenche un flash instantané
    pas_bas: int = 2                    # quantification (moins de paquets BLE)


def _convertir(champ, valeur):
    """Valeur brute (texte d'environnement ou TOML) -> type du champ."""
    defaut = champ.default
    if isinstance(defaut, tuple):
        if champ.name in ("gamma", "bas_gamma") and isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
            v = float(valeur)
            return (v, v, v)
        return tuple(tuple(v) if isinstance(v, list) else v for v in valeur)
    return type(defaut)(valeur)


def charger(chemin=None, env=None):
    """Lit le fichier TOML (chemin, sinon $AMBI_CONFIG, sinon ./config.toml s'il existe) puis les variables AMBI_<NOM>."""
    env = os.environ if env is None else env
    chemin = chemin or env.get("AMBI_CONFIG") or ("config.toml" if os.path.exists("config.toml") else None)
    donnees = {}
    if chemin:
        with open(chemin, "rb") as f:
            donnees = tomllib.load(f)
    connus = {c.name: c for c in fields(Reglages)}
    inconnus = set(donnees) - set(connus)
    if inconnus:
        raise ValueError(f"réglage(s) inconnu(s) dans {chemin} : {', '.join(sorted(inconnus))}")
    valeurs = {nom: _convertir(connus[nom], v) for nom, v in donnees.items()}
    for nom, champ in connus.items():
        brut = env.get("AMBI_" + nom.upper())
        if brut not in (None, "") and not isinstance(champ.default, tuple):
            valeurs[nom] = _convertir(champ, brut)
    cfg = Reglages(**valeurs)
    if chemin:                                       # la clé ADB se range à côté du fichier de configuration
        cle = os.path.join(os.path.dirname(os.path.abspath(chemin)), cfg.adb_cle)
        if not os.path.isabs(cfg.adb_cle):
            cfg = dataclasses.replace(cfg, adb_cle=cle)
    return cfg


def verifier(cfg):
    """Refuse de démarrer sans les réglages d'installation indispensables."""
    if cfg.capture not in ("raw", "h264"):
        raise SystemExit(f'capture doit valoir "raw" ou "h264" (reçu : {cfg.capture!r})')
    if cfg.hz <= 0 or cfg.raw_facteur < 1 or cfg.lum_max <= cfg.genou or not cfg.segments:
        raise SystemExit("réglages incohérents : hz > 0, raw_facteur >= 1, lum_max > genou et au moins un segment sont obligatoires")
    if len(cfg.gamma) != 3 or any(v <= 0 for v in cfg.gamma) or len(cfg.bas_gamma) != 3 or any(v <= 0 for v in cfg.bas_gamma):
        raise SystemExit("réglages incohérents : gamma et bas_gamma doivent contenir 3 valeurs > 0")
    if not (0.0 < cfg.jaune_v <= 1.2):
        raise SystemExit(f"jaune_v doit être dans (0, 1.2] (reçu : {cfg.jaune_v!r})")
    if cfg.bas_balance and (len(cfg.bas_balance) != 3 or any(v < 0 for v in cfg.bas_balance)):
        raise SystemExit("réglages incohérents : bas_balance doit être vide ou contenir 3 valeurs >= 0")
    manque = [n for n in ("fire_ip", "ruban_ip") if not getattr(cfg, n)]
    if manque:
        raise SystemExit(f"réglage(s) obligatoire(s) manquant(s) : {', '.join(manque)} (voir config.example.toml)")
    if not os.path.exists(cfg.adb_cle):
        raise SystemExit(f"clé ADB introuvable : {cfg.adb_cle} (voir README, section Installation)")
