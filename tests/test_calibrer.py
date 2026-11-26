"""Tests sans matériel pour l'outil CLI de calibration visuelle (ambilight.calibrer)."""
import argparse

import pytest

from ambilight import calibrer
from ambilight.config import Reglages


def test_parser_triplet_et_mire():
    assert calibrer._parser_triplet("1.5", "--gamma") == (1.5, 1.5, 1.5)
    assert calibrer._parser_triplet("1.4, 1.5, 1.6", "--gamma") == (1.4, 1.5, 1.6)
    with pytest.raises(argparse.ArgumentTypeError):
        calibrer._parser_triplet("1.4,1.5", "--gamma")

    assert calibrer._trouver_mire("1") == 0
    assert calibrer._trouver_mire("jaune_pur") == 2
    with pytest.raises(argparse.ArgumentTypeError):
        calibrer._trouver_mire("inconnue")


def test_couleur_mire_fixe_et_balayage():
    lib, rgb = calibrer.couleur_mire(0, 0.0)
    assert rgb == (255, 255, 255) and "Blanc" in lib
    idx_bal = calibrer._trouver_mire("balayage")
    _, rgb0 = calibrer.couleur_mire(idx_bal, 0.0)
    _, rgb1 = calibrer.couleur_mire(idx_bal, calibrer.BALAYAGE_INTERVALLE_S + 0.1)
    assert rgb0 != rgb1


def test_calculer_sorties_et_formater_toml():
    cfg = Reglages(gamma=(1.45, 1.45, 1.45), bas_gamma=(1.50, 1.50, 1.50), pas_bas=2)
    out_h, out_b = calibrer.calculer_sorties((255, 255, 255), cfg)
    assert out_h[0] == out_h[1] == out_h[2] > 200          # blanc pur neutre, plafonné par genou/lum_max + gamma
    assert out_b[0] == out_b[1] == out_b[2] > 200
    bloc = calibrer.formater_toml(cfg)
    assert "gamma = [1.45, 1.45, 1.45]" in bloc
    assert "bas_gamma = [1.50, 1.50, 1.50]" in bloc
    assert "jaune_v = 1.00" in bloc


def test_touches_interactives():
    cfg0 = Reglages()
    idx, rub, cfg, act = calibrer._appliquer_touche("3", 0, "haut", cfg0, cfg0)
    assert idx == 2 and act == "maj"
    idx, rub, cfg, act = calibrer._appliquer_touche("]", idx, rub, cfg, cfg0)
    assert cfg.gamma == tuple(round(v + 0.05, 4) for v in cfg0.gamma)
    idx, rub, cfg, act = calibrer._appliquer_touche("\t", idx, rub, cfg, cfg0)
    assert rub == "bas"
    idx, rub, cfg, act = calibrer._appliquer_touche("r", idx, rub, cfg, cfg0)
    assert cfg.bas_balance[0] < cfg0.balance[0]
    idx, rub, cfg, act = calibrer._appliquer_touche("0", idx, rub, cfg, cfg0)
    assert cfg == cfg0
    _, _, _, act = calibrer._appliquer_touche("q", idx, rub, cfg, cfg0)
    assert act == "quitter"


def test_cli_mode_simuler(capsys):
    rc = calibrer.cli(["--simuler", "--mire", "jaune_chaud", "--gamma", "1.45"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Jaune chaud" in out
    assert "gamma = [1.45, 1.45, 1.45]" in out
