import pytest

from ambilight import config


def test_defauts_sans_fichier():
    cfg = config.charger(env={})
    assert cfg.hz == 30.0 and len(cfg.segments) == 15


def test_toml_puis_env(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('fire_ip = "192.0.2.5"\nhz = 20\nsegments = [[0.5, 0.1], [1.5, 0.2]]\n')
    cfg = config.charger(str(f), env={"AMBI_HZ": "25"})
    assert cfg.fire_ip == "192.0.2.5" and cfg.hz == 25.0 and cfg.segments == ((0.5, 0.1), (1.5, 0.2))
    assert cfg.adb_cle == str(tmp_path / "adbkey")         # relative au fichier de configuration
    hash(cfg)                                              # utilisable comme clé du cache de poids()


def test_reglage_inconnu_refuse(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("fautedefrappe = 1\n")
    with pytest.raises(ValueError):
        config.charger(str(f), env={})


def test_installation_obligatoire():
    with pytest.raises(SystemExit):
        config.verifier(config.charger(env={}))
