"""Calcul pur (numpy, aucune entrée/sortie) : image décodée -> couleurs des segments.

    poids()          matrice de poids (segments x pixels), fixe tant que les bandes noires ne changent pas
    bandes_noires()  letterbox / pillarbox
    moyennes()       couleur brute de chaque zone
    rendu()          couleurs des LED (vibrance, plafond, correction du ruban)
    lisser()         lissage exponentiel selon le temps écoulé
"""
from functools import lru_cache

import numpy as np

W, H = 96, 54                                   # taille décodée
FRAME = W * H * 3


def _sig(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


_SOUS_ECHANTILLON = (np.arange(H)[::2, None] * W + np.arange(W)[None, ::2]).ravel()


@lru_cache(maxsize=8)
def poids(cfg, crop=(0, 0, 0, 0)):
    """Matrice de poids ((segments + 1) x H*W) : un rang par segment du ruban, puis la moyenne globale.
    Chaque pixel se partage entre les bords (pas de chevauchement dans les coins) et pèse en exp(-profondeur/prof) :
    le bord de l'image compte le plus, comme la LED qui est juste à côté.
    `crop` = bandes noires (gauche, haut, droite, bas) ignorées. Mise en cache : seules quelques découpes existent."""
    cg, ch_, cd, cb = crop
    x = np.arange(W, dtype=np.float32)[None, :] + 0.5
    y = np.arange(H, dtype=np.float32)[:, None] + 0.5
    x0, x1, y0, y1 = cg, W - cd, ch_, H - cb
    cw, chh = x1 - x0, y1 - y0
    dl, dr, dt, db = x - x0, x1 - x, y - y0, y1 - y
    dedans = ((dl > 0) & (dr > 0) & (dt > 0) & (db > 0)).astype(np.float32)
    Dh, Dv = cfg.prof_h * chh, cfg.prof_v * cw
    ab, at = np.exp(-np.clip(db, 0, None) / Dh), np.exp(-np.clip(dt, 0, None) / Dh)
    al, ar = np.exp(-np.clip(dl, 0, None) / Dv), np.exp(-np.clip(dr, 0, None) / Dv)
    tot = ab + at + al + ar
    pb, pt, pl, pr = ab / tot, at / tot, al / tot, ar / tot
    ux, uy = np.clip((x - x0) / cw, 0, 1), np.clip((y - y0) / chh, 0, 1)
    aretes = [(at * pt, ux), (ar * pr, 1 + uy), (ab * pb, 3 - ux), (al * pl, 4 - uy)]     # (poids du bord, s du pixel sur ce bord)

    lignes = []
    for sk, sg in cfg.segments:
        w = 0
        for poids_bord, sp in aretes:
            dd = (sp - sk + 2.0) % 4.0 - 2.0        # distance sur le périmètre (boucle)
            w = w + poids_bord * np.exp(-0.5 * np.abs(dd / (sg * cfg.seg_k)) ** cfg.seg_p)
        lignes.append(w)
    lignes.append(np.ones((H, W), np.float32))      # moyenne globale
    m = np.stack([np.broadcast_to(l, (H, W)) * dedans for l in lignes]).reshape(len(lignes), -1).astype(np.float32)
    m[m < 1e-3] = 0
    m.setflags(write=False)                         # partagée par le cache : lecture seule
    return m


def bandes_noires(img, cfg):
    """(gauche, haut, droite, bas) en pixels : bandes noires du letterbox/pillarbox. None si l'image est noire."""
    lignes = img.max((1, 2)) < cfg.bord_noir
    cols = img.max((0, 2)) < cfg.bord_noir
    if lignes.all() or cols.all():
        return None

    def debut(v):
        n = 0
        for e in v:
            if not e:
                break
            n += 1
        return n
    h, b, g, d = debut(lignes), debut(lignes[::-1]), debut(cols), debut(cols[::-1])
    h, b = (h if h >= 2 else 0), (b if b >= 2 else 0)
    g, d = (g if g >= 2 else 0), (d if d >= 2 else 0)
    if h + b > 0.6 * H or g + d > 0.6 * W:
        return None
    return (g, h, d, b)


def moyennes(img, M, cfg):
    """Couleur de chaque zone (n x 4 : R, V, B, cohérence), en deux temps :
    - LUMINOSITÉ : moyenne en lumière (les LED additionnent de la lumière), presque fidèle (vif bas) : une zone SOMBRE reste sombre
      même si quelques pixels y sont colorés (le côté noir d'une affiche ne doit pas s'allumer) ;
    - TEINTE : direction de couleur pondérée par la vivacité (saturation^vexp x clarté), sans la clarté des pixels : un objet franc
      mais sombre (casquette marine devant un ciel pâle) donne sa teinte à la zone au lieu de se noyer dans la lumière du ciel."""
    vif = cfg.vif
    pix = img.reshape(-1, 3)
    mx = pix.max(1); mn = pix.min(1)
    v = cfg.plancher_v + ((mx - mn) / (mx + 1.0)) ** cfg.vexp * (0.1 + 0.9 * mx / 255.0)    # un pixel quasi noir a une saturation sans sens : il pèse peu
    lin = (pix / 255.0) ** cfg.lin                  # moyenne en linéaire, pas sur les valeurs sRGB
    sw = np.maximum(M.sum(1), 1e-6)[:, None]
    sv = np.maximum(M @ v, 1e-6)[:, None]
    m = ((M @ lin) / sw) * (1 - vif) + ((M @ (lin * v[:, None])) / sv) * vif
    res = 255.0 * np.clip(m, 0, 1) ** (1.0 / cfg.lin)
    dir_v = (M @ ((pix / np.maximum(mx, 1.0)[:, None]) * v[:, None])) / sv
    dir_v = dir_v / np.maximum(dir_v.max(1, keepdims=True), 1e-6)
    mxr = res.max(1, keepdims=True)
    dir_m = res / np.maximum(mxr, 1e-6)
    # cohérence des couleurs de la zone : des teintes qui s'annulent (brique orange + veston marine) donnent une moyenne grisâtre dont la
    # « teinte » est du hasard -> on ne fait confiance à la teinte vive, et on ne gonfle pas la couleur, que si les pixels s'accordent
    u = pix - pix.mean(1, keepdims=True)
    coh = np.linalg.norm((M @ u) / sw, axis=1, keepdims=True) / np.maximum((M @ np.linalg.norm(u, axis=1)[:, None]) / sw, 3.0)
    porte = np.clip((coh - 0.4) / 0.35, 0.0, 1.0) * np.clip((mxr - 8.0) / 40.0, 0.0, 1.0)     # zone très sombre : sa teinte vive n'est que du bruit -> moyenne fidèle
    vch = cfg.vch * porte
    direction = (1 - vch) * dir_m + vch * dir_v
    if cfg.dominante > 0 and float((np.clip(2.0 * (1.0 - porte), 0, 1) * np.clip((mxr - 20.0) / 40.0, 0, 1)).max()) > 0.05:      # rien à départager : on saute
        dir_dom, dom_w = _dominante(pix, mx, mn, M, sw, mxr, porte, cfg)
        direction = (1 - dom_w) * direction + dom_w * dir_dom
        porte = np.clip(porte + dom_w, 0.0, 1.0)         # une couleur dominante est cohérente : vibrance et présence permises
    return np.concatenate([mxr * direction, porte], 1)


def _dominante(pix, mx, mn, M, sw, mxr, porte, cfg):
    """Couleur dominante de chaque zone : histogramme de teintes (12 classes de 30 degrés, voisines réunies) pondéré par la SURFACE
    des pixels colorés. Renvoie (direction de la couleur dominante, poids à lui donner). Le poids n'est non nul que pour une zone claire
    dont les teintes se contredisent (porte basse) et où une teinte l'emporte nettement : sinon la moyenne reste la bonne réponse."""
    idx = _SOUS_ECHANTILLON                                                                      # un pixel sur quatre suffit pour une teinte dominante
    pix, mx, mn, M = pix[idx], mx[idx], mn[idx], M[:, idx]
    sw = np.maximum(M.sum(1), 1e-6)[:, None]
    sat = (mx - mn) / (mx + 1.0)
    wp = (np.clip((sat - 0.10) / 0.15, 0.0, 1.0) * (mx > 50)).astype(np.float32)                 # SURFACE des pixels colorés (un mauve pâle qui couvre la zone doit battre un décor saturé)
    d = np.maximum(mx - mn, 1.0)
    r, g, b = pix[:, 0], pix[:, 1], pix[:, 2]
    h = np.where(mx == r, ((g - b) / d) % 6.0, np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0))
    classe = np.floor(h * 2.0).astype(int) % 12
    n = len(mx)
    B = np.zeros((n, 12), np.float32)
    B[np.arange(n), classe] = wp
    SC = M @ np.concatenate([B, (B[:, :, None] * pix[:, None, :]).reshape(n, 36)], 1)           # un seul produit : poids (12) et couleurs (12 x 3) par classe
    S, SC = SC[:, :12], SC[:, 12:].reshape(-1, 12, 3)
    Ss = S + np.roll(S, 1, 1) + np.roll(S, -1, 1)                                                # classe + ses voisines
    gagnante = Ss.argmax(1)
    total = np.maximum(S.sum(1), 1e-6)
    retenu = np.abs((np.arange(12)[None, :] - gagnante[:, None] + 6) % 12 - 6) <= 1             # les 3 classes gagnantes
    somme = (SC * retenu[:, :, None]).sum(1)
    swin = np.maximum(Ss[np.arange(len(gagnante)), gagnante], 1e-6)[:, None]
    dom = somme / swin
    dir_dom = dom / np.maximum(dom.max(1, keepdims=True), 1.0)
    dominance = (swin[:, 0] / total)[:, None]
    couverture = (total / sw[:, 0])[:, None]
    poids_dom = (cfg.dominante * np.clip(2.0 * (1.0 - porte), 0, 1) * np.clip((mxr - 20.0) / 40.0, 0, 1)
                 * np.clip((dominance - 0.4) / 0.3, 0, 1) * np.clip((couverture - 0.04) / 0.08, 0, 1))
    return dir_dom, poids_dom


def rendu(c, cfg, g=1.0, pas=1):
    """Couleurs des LED (n x 3, entiers 0-255) à partir des moyennes de zone.
    g = gain automatique (scènes sombres remontées) ; il multiplie les trois canaux donc garde les teintes.
    - vibrance (valeur HSV constante) : sat' = min(1, sat_max x sat^vibr) -> un ciel pâle devient bleu, une couleur franche bouge peu ;
      un neutre (sat < 0.025 : blanc, gris) ne bouge pas et reste un vrai blanc, tous segments pareils ;
    - présence : une couleur franche mais sombre est remontée (sinon elle disparaît à côté d'un segment clair) ;
    - balance / bleu : corrigent les dominantes des LED elles-mêmes."""
    porte = c[:, 3:4] if c.shape[1] > 3 else 1.0       # cohérence de la zone (voir moyennes) : 0 = teintes qui s'annulent -> ni vibrance ni présence
    c = c[:, :3] * g
    mx = c.max(1, keepdims=True); mn = c.min(1, keepdims=True)
    sat = (mx - mn) / np.maximum(mx, 1.0)                                   # 0 = gris/blanc, 1 = couleur pure
    cible = np.minimum(1.0, cfg.sat * np.maximum(sat, 1e-4) ** cfg.vibr)
    r_, g_, b_ = c[:, 0:1], c[:, 1:2], c[:, 2:3]
    t = (g_ - b_) / np.maximum(r_ - b_, 1e-3)                                # r >= g >= b : teinte = 60 x t degrés
    chair = (np.clip((t - 0.1) / 0.1, 0, 1) * np.clip((0.75 - t) / 0.1, 0, 1)
             * np.clip((sat - 0.12) / 0.1, 0, 1) * np.clip((0.92 - sat) / 0.1, 0, 1)
             * ((r_ >= g_) & (g_ >= b_)))                                     # teintes chair/orange pâle : la vibrance les rendrait orange
    clair = np.clip((mx - 150.0) / 80.0, 0, 1)                               # tons clairs : peu de vibrance (un mur blanc chaud ne doit pas jaunir)
    cible = sat + (cible - sat) * (1.0 - 0.5 * clair) * (1.0 - cfg.peau * chair)
    amont = np.clip((sat - 0.025) / 0.04, 0, 1) * porte * np.clip((mx - 15.0) / 60.0, 0, 1)     # 0 = neutre (ou teintes incohérentes)
    k = (sat + (cible - sat) * amont) / np.maximum(sat, 1e-3)
    c = mx - (mx - c) * np.where(sat > 1e-3, k, 1.0)
    mx = c.max(1, keepdims=True)
    sat = (mx - c.min(1, keepdims=True)) / np.maximum(mx, 1.0)
    voulu = cfg.presence * np.clip((sat - 0.35) / 0.30, 0, 1) * porte
    c = c * np.where(mx >= 12, np.clip(voulu / np.maximum(mx, 1.0), 1.0, 2.5), 1.0)
    c = c * cfg.gain * np.array(cfg.balance, dtype=np.float32)
    exces = np.clip(c[:, 2:3] - np.maximum(c[:, 0:1], c[:, 1:2]), 0, None)  # bleu pur : un peu de vert le ramène du violet au bleu
    c = np.concatenate([c[:, :1], c[:, 1:2] + cfg.bleu_v * exces, c[:, 2:3]], 1)
    m = c.max(1, keepdims=True)
    haut = np.maximum(m - cfg.genou, 0.0)
    plafond = np.where(m > cfg.genou, cfg.genou + (cfg.lum_max - cfg.genou) * (1.0 - np.exp(-haut / (cfg.lum_max - cfg.genou))), m)   # compression douce : tend vers lum_max sans couper
    c = c * (plafond / np.maximum(m, 1e-6))
    c = c * np.minimum(1.0, 255.0 / np.maximum(c.max(1, keepdims=True), 1e-6))     # trop clair : on baisse les trois canaux ENSEMBLE (écrêter un canal change la teinte : crème -> jaune)
    c = np.clip(c, 0, 255)
    if cfg.toe > 0:                                  # une LED est linéaire, une télé écrase les bas niveaux : sans ça, un noir vidéo légèrement teinté s'allume en couleur
        c = c * np.minimum(1.0, c.max(1, keepdims=True) / cfg.toe)
    c[c.max(1) < cfg.plancher] = 0
    return (np.round(c / pas) * pas).clip(0, 255).astype(int)


def activite(agitation, cfg):
    """0 = image calme, 1 = image très agitée, d'après l'agitation moyenne (écart de couleur par image)."""
    return float(np.clip((agitation - cfg.agitation_min) / max(cfg.agitation_max - cfg.agitation_min, 1e-6), 0.0, 1.0))


def accentuer(c, fond, cfg, activite=1.0):
    """Accentue le mouvement : couleur + mouvement x activité x (couleur - fond lent). Ne touche pas la colonne de cohérence.
    L'activité (0 calme, 1 agité) règle l'intensité : une émission de discussion reste calme, une intro de combat éclate."""
    force = cfg.mouvement * activite
    if force <= 0:
        return c
    haut = np.clip(c[:, :3] + force * (c[:, :3] - fond[:, :3]), 0, None)
    return np.concatenate([haut, c[:, 3:]], 1)


def lisser(actuel, cible, dt, tau_haut, tau_bas, cfg, lent=None):
    """Lissage exponentiel dépendant du TEMPS, asymétrique par canal : monte vite, redescend doucement.
    Les petits mouvements sont ralentis (fond calme), les gros (éclair, flamme) gardent le lissage rapide."""
    if actuel is None:
        return cible.copy()
    tau = np.where(cible > actuel, tau_haut, tau_bas)
    evt = np.clip((np.abs(cible - actuel) - cfg.evt) / 35.0, 0.0, 1.0)         # 0 = petit mouvement, 1 = vrai événement
    lent = cfg.lent if lent is None else lent
    tau = tau * (1.0 + (lent - 1.0) * (1.0 - evt))
    return actuel + (1.0 - np.exp(-dt / tau)) * (cible - actuel)
