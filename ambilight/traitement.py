"""Calcul pur (numpy, aucune entrée/sortie) : image décodée -> couleurs des segments.

    poids()          matrice de poids (segments x pixels), fixe tant que les bandes noires ne changent pas
    bandes_noires()  letterbox / pillarbox
    moyennes()       couleur brute de chaque zone
    rendu()          couleurs des LED (vibrance, atténuation peau Oklab, plafond, correction du ruban)
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
    Chaque pixel se partage entre les bords (pas de chevauchement dans les coins) et pèse selon un profil
    gaussien en profondeur (`prof_p = 2`) : le bord compte le plus et la traîne au centre est coupée.
    `crop` = bandes noires (gauche, haut, droite, bas) ignorées. Mise en cache : seules quelques découpes existent."""
    cg, ch_, cd, cb = crop
    x = np.arange(W, dtype=np.float32)[None, :] + 0.5
    y = np.arange(H, dtype=np.float32)[:, None] + 0.5
    x0, x1, y0, y1 = cg, W - cd, ch_, H - cb
    cw, chh = x1 - x0, y1 - y0
    dl, dr, dt, db = x - x0, x1 - x, y - y0, y1 - y
    dedans = ((dl > 0) & (dr > 0) & (dt > 0) & (db > 0)).astype(np.float32)
    Dh, Dv = cfg.prof_h * chh, cfg.prof_v * cw
    prof_p = getattr(cfg, "prof_p", 2.0)
    if prof_p == 1.0:
        ab, at = np.exp(-np.clip(db, 0, None) / Dh), np.exp(-np.clip(dt, 0, None) / Dh)
        al, ar = np.exp(-np.clip(dl, 0, None) / Dv), np.exp(-np.clip(dr, 0, None) / Dv)
    else:
        ab = np.exp(-0.5 * (np.clip(db, 0, None) / Dh) ** prof_p)
        at = np.exp(-0.5 * (np.clip(dt, 0, None) / Dh) ** prof_p)
        al = np.exp(-0.5 * (np.clip(dl, 0, None) / Dv) ** prof_p)
        ar = np.exp(-0.5 * (np.clip(dr, 0, None) / Dv) ** prof_p)
    tot = np.maximum(ab + at + al + ar, 1e-12)
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
    bas_bord = getattr(cfg, "bas_bord", 0.0)
    if bas_bord > 0:
        w_bas = 1.0 + bas_bord * np.exp(-np.clip(db, 0, None) / (0.28 * max(chh, 1.0)))
        lignes.append(w_bas)                        # ambiance globale ancrée sur le bas de l'écran (au-dessus du meuble)
    else:
        lignes.append(np.ones((H, W), np.float32))  # moyenne globale
    m = np.stack([np.broadcast_to(l, (H, W)) * dedans for l in lignes]).reshape(len(lignes), -1).astype(np.float32)
    m[m < 1e-3] = 0
    m.setflags(write=False)                         # partagée par le cache : lecture seule
    return m


@lru_cache(maxsize=8)
def _noyau_voisins(segments):
    """Matrice (N x N) des poids des segments physiquement voisins sur le périmètre (diagonale nulle).
    Ignore les jumeaux superposés (aller-retour en haut) et normalise par densité locale afin qu'un groupe
    de 2-3 segments empilés ne pèse pas plus lourd qu'un segment isolé."""
    sks = np.array([s for s, _ in segments], dtype=np.float32)
    dist = np.abs(((sks[:, None] - sks[None, :] + 2.0) % 4.0) - 2.0)
    dens = np.exp(-0.5 * (dist / 0.08) ** 2).sum(axis=1)
    k = np.exp(-0.5 * (dist / 0.22) ** 2) * (1.0 - np.exp(-0.5 * (dist / 0.08) ** 2))
    np.fill_diagonal(k, 0.0)
    k = k / np.maximum(dens[None, :], 1e-6)
    return k / np.maximum(k.sum(axis=1, keepdims=True), 1e-6)


_LUMA_REC709 = np.array([[0.2126], [0.7152], [0.0722]], dtype=np.float32)


def affiner_bords(c, cfg):
    """Netteté spatiale non-négative et anti-halo (masque flou sur l'anneau) : sépare luminance Rec.709 et chroma
    pour renforcer les contrastes réels entre zones voisines sans jamais inverser une teinte ni délaver les
    tons pastel voisins d'une couleur pure."""
    n = getattr(cfg, "nettete", 0.0)
    if n <= 0 or len(c) != len(cfg.segments):
        return c
    k = _noyau_voisins(cfg.segments)
    rgb = c[:, :3]
    y = rgb @ _LUMA_REC709
    u = rgb - y
    y_net = np.clip(y + n * (y - k @ y), 0.0, None)
    u_raw = u + n * (u - k @ u)
    u2 = (u * u).sum(axis=1, keepdims=True)
    dot = (u_raw * u).sum(axis=1, keepdims=True)
    s = np.where(u2 > 1e-6, np.clip(dot / np.maximum(u2, 1e-6), 1.0, 1.0 + 1.5 * n), 0.0)
    net = np.clip(y_net + s * u, 0.0, None)
    return np.concatenate([net, c[:, 3:]], axis=1) if c.shape[1] > 3 else net


def bandes_noires(img, cfg):
    """(gauche, haut, droite, bas) en pixels : bandes noires du letterbox/pillarbox. None si l'image est noire.
    Insensible aux sous-titres centrés en bas, sans rogner un objet centré en haut sur fond sombre."""
    marge = max(4, W // 8)
    bords_lr = np.concatenate([img[:, :marge, :], img[:, W - marge:, :]], axis=1)
    lit = (img.max(2) >= cfg.bord_noir).mean(1)
    plafond_centre = np.where(np.arange(H) < H // 2, 0.10, 0.70)
    lignes = (bords_lr.max((1, 2)) < cfg.bord_noir) & (lit < plafond_centre)
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


def _masque_peau_pixels(pix, mx, sat):
    """Masque doux [0..1] par pixel pour les teintes chair (YCbCr + teinte + saturation).
    Rapide (~0,08 ms) et laisse à 0 les vrais rouges (sabres laser, sang), le feu, les sables/ocres et l'or."""
    r_, g_, b_ = pix[:, 0], pix[:, 1], pix[:, 2]
    t_px = (g_ - b_) / np.maximum(r_ - b_, 1.0)
    cb = 128.0 - 0.168736 * r_ - 0.331264 * g_ + 0.5 * b_
    cr = 128.0 + 0.5 * r_ - 0.418688 * g_ - 0.081312 * b_
    return (
        np.clip((t_px - 0.06) / 0.10, 0.0, 1.0) * np.clip((0.60 - t_px) / 0.12, 0.0, 1.0)
        * ((r_ >= g_) & (g_ >= b_))
        * np.clip((cb - 86.0) / 10.0, 0.0, 1.0) * np.clip((126.0 - cb) / 8.0, 0.0, 1.0)
        * np.clip((cr - 134.0) / 8.0, 0.0, 1.0) * np.clip((184.0 - cr) / 10.0, 0.0, 1.0)
        * np.clip((sat - 0.15) / 0.10, 0.0, 1.0) * np.clip((0.74 - sat) / 0.12, 0.0, 1.0)
        * (mx > 35.0)
    ).astype(np.float32)


def moyennes(img, M, cfg):
    """Couleur de chaque zone (n x 4 : R, V, B, cohérence), en deux temps :
    - LUMINOSITÉ : moyenne en lumière fidèle en surface (vif=0 par défaut) : une queue lumineuse de 5 %
      dans un segment voisin reste sombre au lieu de s'étaler sur tout le bord ;
    - TEINTE : direction de couleur pondérée par la vivacité et la luminance (atténuée sur les pixels chair)
      et modulée par la couverture colorée lumineuse réelle de la zone."""
    vif = cfg.vif
    pix = img.reshape(-1, 3)
    r_, g_, b_ = pix[:, 0], pix[:, 1], pix[:, 2]
    mx = np.maximum(np.maximum(r_, g_), b_)
    mn = np.minimum(np.minimum(r_, g_), b_)
    sat = (mx - mn) / (mx + 1.0)
    peau_px = _masque_peau_pixels(pix, mx, sat)
    lum_v = (mx * (1.0 / 255.0)) ** 1.4
    v = (cfg.plancher_v * lum_v + (sat ** cfg.vexp) * lum_v * (1.0 - 0.85 * peau_px)).astype(np.float32)
    w_col = (np.clip((sat - 0.12) / 0.15, 0.0, 1.0) * lum_v * (1.0 - 0.7 * peau_px)).astype(np.float32)

    # Buffer pré-alloué pour un seul produit matriciel M @ X sur 1 cœur
    X = np.empty((len(pix), 13), np.float32)
    lin = X[:, 0:3]
    np.multiply(pix, 1.0 / 255.0, out=lin)
    np.power(lin, cfg.lin, out=lin)                     # moyenne en linéaire, pas sur les valeurs sRGB
    inv_mx = (v / np.maximum(mx, 1.0))[:, None]
    np.multiply(pix, inv_mx, out=X[:, 3:6])
    u = X[:, 6:9]
    u[:] = pix - ((r_ + g_ + b_) * (1.0 / 3.0))[:, None]
    X[:, 9] = np.sqrt(u[:, 0] ** 2 + u[:, 1] ** 2 + u[:, 2] ** 2)
    X[:, 10] = v
    X[:, 11] = w_col
    X[:, 12] = lum_v

    P = M @ X
    sw = np.maximum(M.sum(1), 1e-6)[:, None]
    sv = np.maximum(P[:, 10:11], 1e-6)
    sl = np.maximum(P[:, 12:13], 1e-6)
    m_lin = P[:, 0:3] / sw
    if vif > 0:
        m = m_lin * (1.0 - vif) + ((M @ (lin * v[:, None])) / sv) * vif
    else:
        m = m_lin
    res = 255.0 * np.clip(m, 0.0, 1.0) ** (1.0 / cfg.lin)
    dir_v = P[:, 3:6] / sv
    dir_v = dir_v / np.maximum(dir_v.max(1, keepdims=True), 1e-6)
    mxr = res.max(1, keepdims=True)
    dir_m = res / np.maximum(mxr, 1e-6)
    # cohérence des couleurs de la zone : des teintes qui s'annulent (brique orange + veston marine) donnent une moyenne grisâtre dont la
    # « teinte » est du hasard -> on ne fait confiance à la teinte vive, et on ne gonfle pas la couleur, que si les pixels s'accordent
    u_moy = P[:, 6:9] / sw
    coh = np.sqrt((u_moy * u_moy).sum(1, keepdims=True)) / np.maximum(P[:, 9:10] / sw, 3.0)
    porte = np.clip((coh - 0.4) / 0.35, 0.0, 1.0) * np.clip((mxr - 8.0) / 40.0, 0.0, 1.0)     # zone très sombre : sa teinte vive n'est que du bruit -> moyenne fidèle
    couv = P[:, 11:12] / sl
    vch_max = min(0.90, cfg.vch + 0.10)
    vch = vch_max * porte * np.clip((couv - 0.05) / 0.15, 0.0, 1.0)
    direction = (1.0 - vch) * dir_m + vch * dir_v
    if cfg.dominante > 0 and float((np.clip(2.0 * (1.0 - porte), 0, 1) * np.clip((mxr - 20.0) / 40.0, 0, 1)).max()) > 0.15:      # rien à départager : on saute
        dir_dom, dom_w = _dominante(pix, mx, mn, peau_px, M, mxr, porte, cfg)
        direction = (1.0 - dom_w) * direction + dom_w * dir_dom
        porte = np.clip(porte + dom_w, 0.0, 1.0)         # une couleur dominante est cohérente : vibrance et présence permises
    return np.concatenate([mxr * direction, porte], 1)


def _dominante(pix, mx, mn, peau_px, M, mxr, porte, cfg):
    """Couleur dominante de chaque zone : histogramme de teintes (18 classes de 20 degrés, 2 voisines = 40°)
    pondéré par la LUMIÈRE colorée (hors chair). Exige une vraie couverture lumineuse de la zone."""
    idx = _SOUS_ECHANTILLON                                                                      # un pixel sur quatre suffit pour une teinte dominante
    pix, mx, mn, peau_s, M = pix[idx], mx[idx], mn[idx], peau_px[idx], M[:, idx]
    lum = (np.minimum(mx * (1.0 / 185.0), 1.0) ** 1.6).astype(np.float32)
    sl = np.maximum(M @ lum, 1e-6)[:, None]
    sat = (mx - mn) / (mx + 1.0)
    wp = (np.clip((sat - 0.10) / 0.15, 0.0, 1.0) * np.clip((mx - 15.0) / 35.0, 0.0, 1.0) * lum * (1.0 - 0.85 * peau_s)).astype(np.float32)
    d = np.maximum(mx - mn, 1.0)
    r, g, b = pix[:, 0], pix[:, 1], pix[:, 2]
    h = np.where(mx == r, ((g - b) / d) % 6.0, np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0))
    classe = np.floor(h * 3.0).astype(int) % 18
    n = len(mx)
    BC = np.zeros((n, 18, 4), np.float32)
    idx_n = np.arange(n)
    BC[idx_n, classe, 0] = wp
    BC[idx_n, classe, 1:] = pix * wp[:, None]
    SC = M @ BC.reshape(n, 72)                                                                   # un seul produit : poids (18) et couleurs (18 x 3)
    SC4 = SC.reshape(-1, 18, 4)
    S, SC = SC4[:, :, 0], SC4[:, :, 1:]
    Ss = S + np.roll(S, -1, 1)                                                                   # paire de classes voisines = 40° (centre 60° exact pour le jaune)
    gagnante = Ss.argmax(1)
    total = np.maximum(S.sum(1), 1e-6)
    cls_idx = np.arange(18)[None, :]
    retenu = (cls_idx == gagnante[:, None]) | (cls_idx == ((gagnante[:, None] + 1) % 18))        # les 2 classes gagnantes
    somme = (SC * retenu[:, :, None]).sum(1)
    swin = np.maximum(Ss[np.arange(len(gagnante)), gagnante], 1e-6)[:, None]
    dom = somme / swin
    dir_dom = dom / np.maximum(dom.max(1, keepdims=True), 1.0)
    dominance = (swin[:, 0] / total)[:, None]
    couverture = (swin[:, 0] / sl[:, 0])[:, None]
    poids_dom = (cfg.dominante * np.clip(2.0 * (1.0 - porte), 0, 1) * np.clip((mxr - 20.0) / 40.0, 0, 1)
                 * np.clip((dominance - 0.4) / 0.3, 0, 1) * np.clip((couverture - 0.18) / 0.24, 0, 1))
    return dir_dom, poids_dom


def _score_peau_oklab(c):
    """Score doux [0..1] de teinte chair en Oklab sur (N, 3) RGB (supporte headroom > 255).
    Sépare les peaux (h in [26..66°], C/L <= 0.22, sat < 0.70) des vrais rouges, du feu, de l'or et du blanc chaud 2700K."""
    sc = np.minimum(1.0, 255.0 / np.maximum(c.max(1, keepdims=True), 1.0))
    cn = c * sc
    lab = _srgb_vers_oklab(cn)
    L, A, B = lab[:, 0:1], lab[:, 1:2], lab[:, 2:3]
    C = np.hypot(A, B)
    h = np.degrees(np.arctan2(B, A)) % 360.0
    dh = np.abs((h - 46.0 + 180.0) % 360.0 - 180.0)
    mx_n = cn.max(1, keepdims=True)
    sat_n = (mx_n - cn.min(1, keepdims=True)) / np.maximum(mx_n, 1.0)
    ws = (
        np.clip((20.0 - dh) / 7.0, 0.0, 1.0)
        * np.clip((C - 0.025) / 0.025, 0.0, 1.0)
        * np.clip((0.145 - C) / 0.035, 0.0, 1.0)
        * np.clip((0.22 - C / np.maximum(L, 1e-4)) / 0.05, 0.0, 1.0)
        * np.clip((L - 0.18) / 0.12, 0.0, 1.0)
        * np.clip((0.70 - sat_n) / 0.12, 0.0, 1.0)
    )
    return ws, sc, L, A, B


def _lin_vers_srgb255(lin):
    """(N, 3) linéaire [0..1] -> sRGB [0..255]."""
    lin = np.clip(lin, 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * (lin ** (1.0 / 2.4)) - 0.055)
    return np.clip(srgb * 255.0, 0.0, 255.0)


def rendu(c, cfg, g=1.0, pas=1, balance=None, gamma=None):
    """Couleurs des LED (n x 3, entiers 0-255) à partir des moyennes de zone.
    g = gain automatique (scènes sombres remontées) ;
    - peau + vibrance Oklab LCh en une passe (préserve la teinte, projection gamut analytique) ;
    - `clair` restreint aux faibles saturations pré-gain (protège les murs blancs chauds sans délaver le jaune) ;
    - `genou`/`lum_max` + `toe` doux en perceptuel, puis courbe `gamma` LED par canal ;
    - `presence`, `jaune_v`, `bleu_v`, `balance` en lumière linéaire LED, puis `plancher` et `pas`."""
    porte = c[:, 3:4] if c.shape[1] > 3 else 1.0
    c_raw = np.clip(c[:, :3], 0.0, None)
    mx_raw = c_raw.max(1, keepdims=True)
    sat_raw = (mx_raw - c_raw.min(1, keepdims=True)) / np.maximum(mx_raw, 1.0)
    c = c_raw * g

    ws, sc, L, A, B = _score_peau_oklab(c)
    cn = c * sc
    mx_n = cn.max(1, keepdims=True)
    sat_n = (mx_n - cn.min(1, keepdims=True)) / np.maximum(mx_n, 1.0)
    att_peau = (1.0 - np.minimum(0.75, 0.65 * cfg.peau) * ws) if cfg.peau > 0 else 1.0

    C = np.hypot(A, B)
    rel_c = C / np.maximum(L, 0.05)
    amont = np.clip((sat_n - 0.025) / 0.04, 0.0, 1.0) * porte * np.clip((mx_n - 15.0) / 55.0, 0.0, 1.0)
    clair = np.clip((mx_raw - 180.0) / 65.0, 0.0, 1.0) * (1.0 - np.clip((sat_raw - 0.18) / 0.15, 0.0, 1.0))
    vibr_ok = getattr(cfg, "vibr_ok", 0.22)
    boost_c = 1.0 + vibr_ok * np.clip((0.55 - rel_c) / 0.42, 0.0, 1.0) * (1.0 - 0.75 * clair) * (1.0 - np.minimum(1.0, cfg.peau) * ws) * amont
    mult = att_peau * boost_c

    # Garde stricte de neutralité sur les gris/blancs purs (évite tout bruit float32 dans Oklab)
    neutre = sat_n < 1e-4
    if not np.all(neutre):
        lin2 = _oklab_vers_lin(np.concatenate([L, A * mult, B * mult], axis=1))
        # Si un canal dépasse la crête linéaire (jaunes/cyans/ciels clairs), re-normaliser vers la crête d'origine
        # au lieu de stopper la vibrance ; ne limiter que le plancher (>= 0) pour garder la teinte Oklab exacte.
        lin1 = np.clip(_oklab_vers_lin(np.concatenate([L, A * att_peau, B * att_peau], axis=1)), 0.0, 1.0)
        peak1 = np.maximum(lin1.max(axis=1, keepdims=True), 1e-6)
        over_hi = (~neutre) & (mult > 1.0) & (lin2.max(axis=1, keepdims=True) > peak1)
        lin2 = np.where(over_hi, lin2 * (peak1 / np.maximum(lin2.max(axis=1, keepdims=True), 1e-6)), lin2)
        under = (~neutre) & (mult > 1.0) & (lin2.min(axis=1, keepdims=True) < 0.0)
        if np.any(under):
            d = lin2 - lin1
            t_lo = np.where(d < -1e-6, -lin1 / np.minimum(d, -1e-6), 1.0)
            t = np.clip(t_lo.min(axis=1, keepdims=True), 0.0, 1.0)
            lin2 = np.where(under, lin1 + t * d, lin2)
        c_ok = _lin_vers_srgb255(lin2) / sc
        c = np.where(neutre, c, c_ok)

    if cfg.peau > 0:
        c = c * (1.0 - 0.15 * np.minimum(1.0, cfg.peau) * ws)

    c = c * cfg.gain
    m = c.max(1, keepdims=True)
    haut = np.maximum(m - cfg.genou, 0.0)
    plafond = np.where(m > cfg.genou, cfg.genou + (cfg.lum_max - cfg.genou) * (1.0 - np.exp(-haut / (cfg.lum_max - cfg.genou))), m)
    c = c * (plafond / np.maximum(m, 1e-6))
    c = c * np.minimum(1.0, 255.0 / np.maximum(c.max(1, keepdims=True), 1e-6))
    c = np.clip(c, 0.0, 255.0)
    if cfg.toe > 0:
        # Écrase aussi le bruit vidéo des noirs (mx_raw <= ~12) avant qu'un gain auto x3 ne l'allume en vert
        c = c * np.minimum(1.0, c.max(1, keepdims=True) / cfg.toe) * np.clip((mx_raw - 10.0) / max(cfg.toe * 0.6, 10.0), 0.0, 1.0)

    gamma_arr = np.array(cfg.gamma if gamma is None else gamma, dtype=np.float32)
    c_lin = 255.0 * ((c / 255.0) ** gamma_arr)

    mx_c = c.max(1, keepdims=True)
    sat_post = (mx_c - c.min(1, keepdims=True)) / np.maximum(mx_c, 1.0)
    mx_lin = c_lin.max(1, keepdims=True)
    voulu_lin = cfg.presence * np.clip((sat_post - 0.30) / 0.30, 0.0, 1.0) * porte * (1.0 - 0.8 * ws)
    c_lin = c_lin * np.where(mx_raw >= 14.0, np.clip(voulu_lin / np.maximum(mx_lin, 1e-3), 1.0, 3.0), 1.0)

    jaune_v = getattr(cfg, "jaune_v", 1.0)
    if jaune_v != 1.0:
        exces_j = np.clip(np.minimum(c_lin[:, 0:1], c_lin[:, 1:2]) - c_lin[:, 2:3], 0.0, None)
        c_lin = np.concatenate([c_lin[:, :1], np.clip(c_lin[:, 1:2] - (1.0 - jaune_v) * exces_j, 0.0, None), c_lin[:, 2:3]], axis=1)

    exces_b = np.clip(c_lin[:, 2:3] - np.maximum(c_lin[:, 0:1], c_lin[:, 1:2]), 0.0, None)
    c_lin = np.concatenate([c_lin[:, :1], c_lin[:, 1:2] + cfg.bleu_v * exces_b, c_lin[:, 2:3]], axis=1)

    balance_arr = np.array(cfg.balance if balance is None else balance, dtype=np.float32)
    c_lin = c_lin * balance_arr
    c_lin = c_lin * np.minimum(1.0, 255.0 / np.maximum(c_lin.max(1, keepdims=True), 1e-6))
    c_lin = np.clip(c_lin, 0.0, 255.0)
    c_lin[c_lin.max(1) < cfg.plancher] = 0.0
    return (np.round(c_lin / pas) * pas).clip(0, 255).astype(int)


def activite(agitation, cfg):
    """0 = image calme, 1 = image très agitée, d'après l'agitation moyenne (écart de couleur par image)."""
    return float(np.clip((agitation - cfg.agitation_min) / max(cfg.agitation_max - cfg.agitation_min, 1e-6), 0.0, 1.0))


def accentuer(c, fond, cfg, activite=1.0):
    """Accentue le mouvement : couleur + mouvement x activité x (couleur - fond lent), sans jamais écrêter
    un canal négatif avant mise à l'échelle (évite la dérive de teinte), avec plafond sat_c et headroom <= 320."""
    force = cfg.mouvement * activite
    if force <= 0:
        return c
    rgb = c[:, :3]
    delta = force * (rgb - fond[:, :3])
    t_neg = np.where(delta < -1e-6, rgb / np.maximum(-delta, 1e-6), 1.0)
    t = np.clip(t_neg.min(axis=1, keepdims=True), 0.0, 1.0)
    haut = np.clip(rgb + t * delta, 0.0, None)
    mx_c = rgb.max(1, keepdims=True)
    sat_c = (mx_c - rgb.min(1, keepdims=True)) / np.maximum(mx_c, 1.0)
    mx_h = haut.max(1, keepdims=True)
    sat_h = (mx_h - haut.min(1, keepdims=True)) / np.maximum(mx_h, 1.0)
    k = np.where(sat_h > np.maximum(sat_c, 1e-3), sat_c / np.maximum(sat_h, 1e-6), 1.0)
    haut = mx_h - (mx_h - haut) * k
    haut = haut * np.minimum(1.0, 320.0 / np.maximum(haut.max(1, keepdims=True), 1e-6))
    return np.concatenate([haut, c[:, 3:]], 1)


def _srgb_vers_oklab(c):
    """(N, 3) RGB [0..255] -> Oklab (L, a, b). Transition perceptuelle sans passage au gris/brun."""
    x = np.clip(c / 255.0, 0.0, 1.0)
    lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    r, g, b = lin[:, 0], lin[:, 1], lin[:, 2]
    l = np.cbrt(np.maximum(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b, 0.0))
    m = np.cbrt(np.maximum(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b, 0.0))
    s = np.cbrt(np.maximum(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b, 0.0))
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return np.stack([L, A, B], axis=1)


def _oklab_vers_lin(lab):
    """Oklab (L, a, b) -> (N, 3) RGB linéaire non écrêté."""
    L, A, B = lab[:, 0], lab[:, 1], lab[:, 2]
    l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
    m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
    s = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return np.stack([r, g, b], axis=1)


def _oklab_vers_srgb(lab):
    """Oklab (L, a, b) -> (N, 3) RGB [0..255]."""
    return _lin_vers_srgb255(_oklab_vers_lin(lab))


def lisser(actuel, cible, dt, tau_haut, tau_bas, cfg, lent=None):
    """Lissage exponentiel dépendant du TEMPS (Hybrid Inertia + Oklab perceptuel) :
    - petits mouvements (< evt) : ralentis par `lent` (fond calme, zéro scintillement) ;
    - mouvements moyens : asymétrique (monte vite, redescend doucement) ;
    - événements locaux forts (> flash_seuil sur un segment) : réponse quasi instantanée (tau_flash) sur CE segment ;
    - si `lissage_oklab` est actif, les canaux RGB sont interpolés en Oklab (pas de passage par le gris/brun)."""
    if actuel is None:
        return cible.copy()
    lent = cfg.lent if lent is None else lent
    rgb_a, rgb_c = actuel[:, :3], cible[:, :3]
    diff_seg = np.abs(rgb_c - rgb_a).max(axis=1, keepdims=True)
    monte = rgb_c.max(axis=1, keepdims=True) > rgb_a.max(axis=1, keepdims=True)
    tau_base = np.where(monte, tau_haut, tau_bas)
    evt = np.clip((diff_seg - cfg.evt) / 35.0, 0.0, 1.0)                       # 0 = petit mouvement, 1 = vrai événement
    tau_seg = tau_base * (1.0 + (lent - 1.0) * (1.0 - evt))
    # Événement local fort (éclair, explosion, sabre laser sur un bord) : ce segment réagit quasi instantanément
    if getattr(cfg, "tau_flash", 0.0) > 0 and tau_haut < 0.2:
        flash = np.clip((diff_seg - cfg.flash_seuil) / 40.0, 0.0, 1.0) * monte
        tau_seg = tau_seg * (1.0 - flash) + min(cfg.tau_flash, tau_haut) * flash
    alpha_seg = 1.0 - np.exp(-dt / np.maximum(tau_seg, 1e-4))
    if getattr(cfg, "lissage_oklab", False):
        echelle = np.maximum(255.0, np.maximum(rgb_a.max(axis=1, keepdims=True), rgb_c.max(axis=1, keepdims=True)))
        k = 255.0 / echelle
        lab_a = _srgb_vers_oklab(rgb_a * k)
        lab_c = _srgb_vers_oklab(rgb_c * k)
        # Séparation perceptuelle L (luminance) vs (a, b) (chrominance) + flashs iso-luminants (bleu -> rouge) :
        # l'œil est plus sensible aux flashs de luminance et aux changements francs de teinte, et plus gêné par
        # le micro-bruit de chrominance sur les fonds calmes.
        dlab = lab_c - lab_a
        de = np.sqrt((dlab ** 2).sum(axis=1, keepdims=True))
        iso_flash = (
            np.clip((de - 0.16) / 0.14, 0.0, 1.0) * (rgb_c.max(axis=1, keepdims=True) > 30.0)
            if getattr(cfg, "tau_flash", 0.0) > 0 and tau_haut < 0.2 else 0.0
        )
        tau_l = tau_seg * (1.0 - iso_flash) + min(getattr(cfg, "tau_flash", tau_haut), tau_haut) * iso_flash
        tau_ab = tau_l * (1.0 + 0.15 * (1.0 - evt) * (1.0 - iso_flash))
        alpha_l = 1.0 - np.exp(-dt / np.maximum(tau_l, 1e-4))
        alpha_ab = 1.0 - np.exp(-dt / np.maximum(tau_ab, 1e-4))
        alpha_lab = np.concatenate([alpha_l, alpha_ab, alpha_ab], axis=1)
        rgb_out = _oklab_vers_srgb(lab_a + alpha_lab * dlab) / k
        alpha_seg = alpha_l
    else:
        tau = np.where(rgb_c > rgb_a, tau_haut, tau_bas)
        evt_c = np.clip((np.abs(rgb_c - rgb_a) - cfg.evt) / 35.0, 0.0, 1.0)
        tau = tau * (1.0 + (lent - 1.0) * (1.0 - evt_c))
        rgb_out = rgb_a + (1.0 - np.exp(-dt / np.maximum(tau, 1e-4))) * (rgb_c - rgb_a)
    if actuel.shape[1] > 3:
        extra = actuel[:, 3:] + alpha_seg * (cible[:, 3:] - actuel[:, 3:])
        return np.concatenate([rgb_out, extra], axis=1)
    return rgb_out
