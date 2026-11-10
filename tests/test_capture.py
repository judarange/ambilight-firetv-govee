"""flux_adb avec une fausse Fire TV : le silence d'un écran fixe est normal, un ffmpeg mort ne l'est pas."""
import io
import threading

import pytest
from adb_shell.exceptions import TcpTimeoutException

from ambilight import capture
from ambilight.config import Reglages


class FausseFireTv:
    def __init__(self, *a, **k):
        pass

    def connect(self, **k):
        pass

    def close(self):
        pass


def fixer(monkeypatch, flux):
    class D(FausseFireTv):
        def _streaming_command(self, *a):
            return flux()
    monkeypatch.setattr(capture, "AdbDeviceTcp", D)
    monkeypatch.setattr(capture, "_signataire", lambda cfg: None)


def sync():
    return {"lock": threading.Lock(), "t": 0.0, "flushed": True}


def test_ecran_fixe_prend_une_photo_et_continue(monkeypatch):
    stop = threading.Event()
    appels = []

    def silence():
        raise TcpTimeoutException("silence")
        yield

    fixer(monkeypatch, silence)
    capture.flux_adb(Reglages(), io.BytesIO(), stop, sync(), amorce=lambda: (appels.append(1), stop.set()))
    assert appels == [1]                                  # la panne de lecture n'a pas fait tomber le fil


def test_ecran_fixe_une_seule_photo(monkeypatch):
    """Écran fixe sur plusieurs sessions : UNE capture d'écran (elle coûte cher à la Fire TV), pas une à chaque silence."""
    stop, appels, sessions = threading.Event(), [], []

    def silence():
        sessions.append(1)
        if len(sessions) == 3:
            stop.set()
        yield b"\x00"                                     # image d'ouverture de session : pas un mouvement
        raise TcpTimeoutException("silence")

    fixer(monkeypatch, silence)
    monkeypatch.setattr(capture, "SESSION_MIN", 0.0)
    capture.flux_adb(Reglages(), io.BytesIO(), stop, sync(), amorce=lambda: appels.append(1))
    assert len(sessions) == 3 and appels == [1]


def test_mouvement_puis_ecran_fixe_reprend_une_photo(monkeypatch):
    stop, appels, sessions = threading.Event(), [], []

    def flux():
        sessions.append(1)
        if len(sessions) == 3:
            stop.set()
        yield b"\x00"
        raise TcpTimeoutException("silence")

    fixer(monkeypatch, flux)
    monkeypatch.setattr(capture, "SESSION_MIN", 0.0)
    monkeypatch.setattr(capture, "OUVERTURE", -1.0)       # toute donnée compte comme du mouvement
    capture.flux_adb(Reglages(), io.BytesIO(), stop, sync(), amorce=lambda: appels.append(1))
    assert appels == [1, 1]


def test_ffmpeg_mort_arrete_tout(monkeypatch):
    class Mort(io.BytesIO):
        def write(self, b):
            raise BrokenPipeError

    fixer(monkeypatch, lambda: iter([b"\x00\x01"]))
    with pytest.raises(capture.FluxMort):
        capture.flux_adb(Reglages(), Mort(), threading.Event(), sync())


def test_flux_raw_reduit_et_garde_la_derniere_image(monkeypatch):
    """Deux images brutes arrivent d'un coup : seule la plus récente est traitée, réduite par moyenne de blocs à 96x54."""
    import numpy as np
    from ambilight.traitement import H, W

    cfg = Reglages()
    f = cfg.raw_facteur
    octets = W * f * H * f * 3
    stop, recues = threading.Event(), []
    flux = lambda: iter([bytes([40]) * octets + bytes([200]) * octets + bytes([7]) * 100])      # 2 images + le début d'une 3e
    fixer(monkeypatch, flux)
    capture.flux_raw(cfg, lambda img: (recues.append(img), stop.set()), stop)
    assert len(recues) == 1 and recues[0].shape == (H, W, 3)
    assert np.allclose(recues[0], 200.0)                                                         # la plus récente


def test_config_invalide_refusee():
    import pytest
    from ambilight import config
    with pytest.raises(SystemExit):
        config.verifier(Reglages(fire_ip="a", ruban_ip="b", capture="autre"))
    with pytest.raises(SystemExit):
        config.verifier(Reglages(fire_ip="a", ruban_ip="b", hz=0))
