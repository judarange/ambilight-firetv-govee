"""Tests sans matériel : le calcul pur doit rester stable (couleur unie fidèle, noir = noir, bandes noires, lissage)."""
import numpy as np
import pytest

from ambilight.config import Reglages
from ambilight.traitement import H, W, bandes_noires, lisser, moyennes, poids, rendu

CFG = Reglages()
N = len(CFG.segments)


def uni(r, g, b):
    return np.full((H, W, 3), (r, g, b), np.float32)


def test_poids_forme_et_positivite():
    M = poids(CFG)
    assert M.shape == (N + 1, H * W)
    assert (M >= 0).all()
    assert (M.sum(1) > 0).all()                          # aucun segment sans pixels


def test_poids_ignore_les_bandes_noires():
    M = poids(CFG, (0, 7, 0, 7)).reshape(N + 1, H, W)
    assert M[:, :7].sum() == 0 and M[:, -7:].sum() == 0


def test_couleur_unie_fidele():
    z = moyennes(uni(200, 40, 40), poids(CFG), CFG)
    assert np.allclose(z[:, :3], (200, 40, 40), atol=1.5)
    assert np.allclose(z[:, 3], 1.0, atol=1e-3)           # pixels d'accord : teinte de confiance


def test_noir_reste_noir():
    z = moyennes(uni(0, 0, 0), poids(CFG), CFG)
    assert np.isfinite(z).all() and z.max() == 0
    assert rendu(z, CFG).max() == 0


def test_gris_reste_neutre():
    s = rendu(moyennes(uni(128, 128, 128), poids(CFG), CFG), CFG)
    assert (s.max(1) - s.min(1)).max() == 0               # r == g == b


@pytest.mark.parametrize("valeur", [-50.0, 1000.0])
def test_rendu_borne(valeur):
    s = rendu(np.full((N, 4), valeur), CFG)
    assert s.shape == (N, 3) and s.min() >= 0 and s.max() <= 255


def test_bandes_noires_letterbox():
    img = uni(120, 120, 120)
    img[:7] = 0; img[-7:] = 0
    assert bandes_noires(img, CFG) == (0, 7, 0, 7)
    assert bandes_noires(uni(0, 0, 0), CFG) is None
    mince = uni(120, 120, 120); mince[0] = 0
    assert bandes_noires(mince, CFG) == (0, 0, 0, 0)       # une seule ligne : ignorée


def test_lisser():
    cible = np.full((N, 3), 100.0)
    assert lisser(None, cible, 0.03, 0.025, 0.06, CFG) is not cible
    actuel = np.zeros((N, 3))
    assert np.allclose(lisser(actuel, cible, 0.0, 0.025, 0.06, CFG), actuel)
    assert np.allclose(lisser(actuel, cible, 10.0, 0.025, 0.06, CFG), cible, atol=0.1)
    monte = lisser(actuel, cible, 0.03, 0.025, 0.06, CFG)[0, 0]
    descend = 100.0 - lisser(cible, actuel, 0.03, 0.025, 0.06, CFG)[0, 0]
    assert monte > descend                                # monte plus vite qu'elle ne redescend


def test_gros_changement_plus_rapide_que_petit():
    CFG = Reglages(lent=1.5)
    petit = lisser(np.zeros((1, 3)), np.full((1, 3), 5.0), 0.05, 0.025, 0.06, CFG)[0, 0] / 5.0
    gros = lisser(np.zeros((1, 3)), np.full((1, 3), 200.0), 0.05, 0.025, 0.06, CFG)[0, 0] / 200.0
    assert gros > petit


def test_noir_video_legerement_teinte_reste_eteint():
    """Le « noir » d'une vidéo (3,12,8) ne doit pas allumer une LED verte (une LED est linéaire, une télé écrase les bas niveaux)."""
    z = moyennes(uni(3, 12, 8), poids(CFG), CFG)
    assert rendu(z, CFG).max() <= CFG.plancher or rendu(z, CFG).max() < 5


def test_peau_reste_orange_naturel():
    s = rendu(moyennes(uni(110, 61, 35), poids(CFG), CFG), CFG)[0]
    assert s[0] > s[1] > s[2] and s[1] / s[0] > 0.45        # pas de vibrance qui vire à l'orange vif


def test_accentuer_le_mouvement():
    from ambilight.traitement import accentuer
    fond = np.full((N, 4), 50.0); c = np.full((N, 4), 90.0); c[:, 3] = 1.0
    a = accentuer(c, fond, CFG)
    assert (a[:, :3] > 90.0).all() and (a[:, 3] == 1.0).all()      # un éclat récent ressort davantage
    assert np.allclose(accentuer(c, c, CFG), c)                    # image stable : rien ne change


def melange(violet, beige, part_violet, graine=0):
    """Image dont `part_violet` des pixels sont violets et le reste beige, mélangés au hasard."""
    rng = np.random.default_rng(graine)
    masque = rng.random((H, W)) < part_violet
    img = np.empty((H, W, 3), np.float32)
    img[masque] = violet; img[~masque] = beige
    return img


def test_zone_mixte_prend_la_couleur_dominante_pas_la_moyenne():
    """Corps mauve (Beerus) devant un décor beige : la zone doit rester mauve, pas devenir beige par moyenne des deux."""
    img = melange((150, 115, 190), (225, 205, 110), 0.65)
    avec = rendu(moyennes(img, poids(CFG), CFG), CFG)[:N]
    sans_cfg = Reglages(dominante=0)
    sans = rendu(moyennes(img, poids(sans_cfg), sans_cfg), sans_cfg)[:N]
    assert (avec[:, 2] > avec[:, 0]).all() and (avec[:, 0] > avec[:, 1]).all()     # bleu > rouge > vert : mauve
    assert (sans[:, 0] > sans[:, 2]).mean() > 0.8                                  # la moyenne simple donnait du beige (rouge > bleu)


def test_dominante_ne_change_pas_une_zone_coherente():
    z0 = moyennes(uni(200, 40, 40), poids(CFG), CFG)
    z1 = moyennes(uni(200, 40, 40), poids(Reglages(dominante=0)), Reglages(dominante=0))
    assert np.allclose(z0, z1, atol=1e-3)


def test_effet_adapte_a_l_agitation():
    from ambilight.traitement import accentuer, activite
    assert activite(0.0, CFG) == 0.0 and activite(100.0, CFG) == 1.0
    assert 0.0 < activite((CFG.agitation_min + CFG.agitation_max) / 2, CFG) < 1.0
    fond = np.full((N, 4), 50.0); c = np.full((N, 4), 90.0)
    assert np.allclose(accentuer(c, fond, CFG, 0.0), c)               # image calme : aucune accentuation
    assert (accentuer(c, fond, CFG, 1.0)[:, :3] > accentuer(c, fond, CFG, 0.5)[:, :3]).all()


def test_bandes_noires_insensible_aux_sous_titres():
    """Des sous-titres blancs centrés dans la bande noire du bas ne doivent pas casser la détection letterbox."""
    img = uni(120, 120, 120)
    img[:7] = 0
    img[-7:] = 0
    img[-5:-2, 25:70] = 255                                           # sous-titre blanc centré
    assert bandes_noires(img, CFG) == (0, 7, 0, 7)


def test_lissage_oklab_flash_isoluminant_et_headroom():
    """Un flash bleu -> rouge à luminosité égale doit réagir vite, et le headroom > 255 doit être préservé."""
    bleu = np.array([[0.0, 90.0, 255.0, 1.0]])
    rouge = np.array([[255.0, 40.0, 0.0, 1.0]])
    out = lisser(bleu, rouge, 0.033, 0.025, 0.06, CFG, lent=1.5)
    assert out[0, 0] > 200.0                                          # saut rapide malgré max(RGB) identique
    hi_a = np.array([[100.0, 50.0, 20.0, 1.0]])
    hi_c = np.array([[340.0, 120.0, 40.0, 1.0]])
    assert lisser(hi_a, hi_c, 0.033, 0.025, 0.06, CFG, lent=1.0)[0, 0] > 255.0


def test_affiner_bords_preserve_blanc_et_renforce_contraste():
    """La netteté spatiale ne touche pas aux scènes uniformes mais renforce un bord contrasté sans valeur négative."""
    from ambilight.traitement import affiner_bords
    blanc = np.full((N, 4), 180.0); blanc[:, 3] = 1.0
    assert np.allclose(affiner_bords(blanc, CFG), blanc, atol=1e-4)
    bord = np.zeros((N, 4)); bord[:, 3] = 1.0
    bord[0, :3] = (200.0, 20.0, 20.0)
    net = affiner_bords(bord, CFG)
    assert net[:, :3].min() >= 0.0
    assert net[0, 0] > bord[0, 0]                                     # segment contrasté renforcé
