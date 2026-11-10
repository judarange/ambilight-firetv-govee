"""Entrées : image de la Fire TV par ADB, de deux façons au choix (`capture` dans la configuration) :

  raw   `screenrecord --output-format=raw-frames` : images RGB brutes, sans encodeur ni ffmpeg, sans limite de durée (défaut)
  h264  `screenrecord` en h264 -> ffmpeg : repli pour une Fire TV dont le mode brut ne marche pas

Dans les deux cas, quand l'écran est FIXE `screenrecord` se tait : une capture d'écran (PNG, décodée par ffmpeg) prend le relais.
"""
import logging
import subprocess
import time

import numpy as np
from adb_shell import exceptions as adb_exc
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import TcpTimeoutException

from .traitement import W, H, FRAME

log = logging.getLogger("ambilight.capture")

AUD = bytes([0, 0, 0, 1, 9, 0xF0])             # délimiteur d'unité d'accès h264 : force le décodeur à sortir l'image en cours
# panne réseau/ADB : on se reconnecte. TcpTimeoutException (silence de 10 s = écran fixe) n'est PAS une OSError.
ERREURS_ADB = (OSError, TcpTimeoutException, adb_exc.AdbTimeoutError, adb_exc.AdbConnectionError, adb_exc.AdbCommandFailureException,
               adb_exc.InvalidResponseError, adb_exc.InvalidChecksumError, adb_exc.DeviceAuthError)
SESSION_MIN = 5.0                              # une session plus courte est un échec : on attend avant de se reconnecter
ATTENTE_MAX = 10.0


class FluxMort(Exception):
    """Le consommateur du flux (ffmpeg) ne lit plus : rien ne sert de se reconnecter, le programme doit s'arrêter."""


def _signataire(cfg):
    with open(cfg.adb_cle) as f:
        return PythonRSASigner("", f.read())


def lancer_ffmpeg(cfg):
    # -threads 1 en sortie : avec 2 fils ou plus, le codeur rawvideo de ffmpeg garde UNE image en retenue (mesuré : +1 période
    # d'image, soit 33 à 42 ms, et la couleur précédente sur un écran fixe). Sans effet sur une machine à un seul cœur.
    return subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-flags", "low_delay", "-probesize", "32768", "-analyzeduration", "0",
         "-f", "h264", "-i", "pipe:0", "-threads", "1", "-vf", f"scale={W}:{H}:flags=area", "-pix_fmt", "rgb24",
         "-flush_packets", str(cfg.flush), "-f", "rawvideo", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def instantane(cfg):
    """Une capture d'écran (PNG) -> image W x H. Sert quand l'écran est FIXE : screenrecord n'émet rien tant que rien ne bouge."""
    d = AdbDeviceTcp(cfg.fire_ip, cfg.fire_port, default_transport_timeout_s=10)
    try:
        d.connect(rsa_keys=[_signataire(cfg)], auth_timeout_s=10)
        png = d.exec_out("screencap -p", read_timeout_s=10, timeout_s=15, decode=False)
    finally:
        try:
            d.close()
        except Exception:
            pass
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={W}:{H}:flags=area",
                        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], input=png, capture_output=True, timeout=15)
    if len(r.stdout) != FRAME:
        raise RuntimeError("capture illisible")
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3).astype(np.float32)


def boucle_adb(cfg, stop, commande, recevoir, amorce=None, debut_session=None):
    """Se (re)connecte à la Fire TV, exécute `commande` et passe chaque morceau reçu à `recevoir(morceau)`.
    Reconnexion avec attente croissante après un échec rapide ; un silence de 10 s (écran fixe) déclenche `amorce` (capture d'écran).
    Les exceptions de `recevoir` (FluxMort...) ne sont pas des pannes ADB : elles remontent et arrêtent le programme."""
    signer = _signataire(cfg)
    attente = 1.0
    while not stop.is_set():
        debut = time.monotonic()
        if debut_session:
            debut_session()
        d = AdbDeviceTcp(cfg.fire_ip, cfg.fire_port, default_transport_timeout_s=10)
        try:
            d.connect(rsa_keys=[signer], auth_timeout_s=10)
            # NB : API privée d'adb-shell (pas d'équivalent public pour un exec binaire en continu) -> version épinglée dans pyproject
            for morceau in d._streaming_command(b"exec", commande, 10, 10, None):
                if stop.is_set():
                    break
                recevoir(morceau)
        except ERREURS_ADB as e:
            log.warning("adb : %s %s", type(e).__name__, e)
            if amorce and isinstance(e, TcpTimeoutException):    # silence = écran fixe (ou pause) : on prend une photo de l'écran
                try:
                    amorce()
                except Exception as e2:
                    log.warning("capture : %s %s", type(e2).__name__, e2)
        finally:
            try:
                d.close()
            except Exception:
                pass
        if time.monotonic() - debut >= SESSION_MIN:
            attente = 1.0
        else:                                                    # échec rapide : on ne martèle pas la Fire TV
            stop.wait(attente)
            attente = min(attente * 2, ATTENTE_MAX)
        stop.wait(0.2)


def flux_adb(cfg, stdin, stop, sync, amorce=None):
    """Mode h264 : alimente ffmpeg avec le flux de la Fire TV ; se reconnecte quand screenrecord s'arrête (limite de 180 s).
    Lève FluxMort si ffmpeg est mort (le programme s'arrête et le service de contrôle le relance)."""
    cmd = f"screenrecord --output-format=h264 --size {cfg.taille} --bit-rate {cfg.debit} -".encode()
    coupees = 0

    def ecrire(chunk):
        nonlocal coupees
        with sync["lock"]:
            if sync["flushed"] and not chunk.startswith(AUD[:4]):
                coupees += 1                                     # suite d'une image déjà fermée par l'AUD : elle sera mal décodée
                log.warning("image coupée par l'AUD (%d depuis le départ) : si c'est fréquent, augmenter aud_attente", coupees)
            try:
                stdin.write(chunk); stdin.flush()
            except (BrokenPipeError, ValueError) as e:
                raise FluxMort("ffmpeg ne lit plus") from e
            sync["t"] = time.monotonic(); sync["flushed"] = False

    boucle_adb(cfg, stop, cmd, ecrire, amorce)


def flux_raw(cfg, traiter, stop, amorce=None):
    """Mode brut : images RGB (W*facteur x H*facteur) réduites par moyenne de blocs à W x H, puis `traiter(image)`.
    Si plusieurs images attendent (calcul en retard), seule la plus récente est traitée : on ne cumule jamais de retard."""
    f = cfg.raw_facteur
    cmd = f"screenrecord --output-format=raw-frames --size {W * f}x{H * f} -".encode()
    octets = W * f * H * f * 3
    tampon = bytearray()

    def recevoir(morceau):
        tampon.extend(morceau)
        derniere = None
        while len(tampon) >= octets:
            derniere = bytes(tampon[:octets])
            del tampon[:octets]
        if derniere is not None:
            traiter(np.frombuffer(derniere, np.uint8).reshape(H, f, W, f, 3).astype(np.float32).mean((1, 3)))

    boucle_adb(cfg, stop, cmd, recevoir, amorce, debut_session=tampon.clear)    # une session qui reprend ne continue pas au milieu d'une image


def veilleur_aud(ffstdin, stop, sync, attente):
    """Mode h264. screenrecord n'émet une image que si l'écran change, et le décodeur garde la dernière en retenue tant que la
    suivante n'arrive pas. Quand le flux se tait, l'image est complète : un délimiteur (AUD) force sa sortie,
    sinon un écran fixe garde la couleur PRÉCÉDENTE."""
    while not stop.is_set():
        time.sleep(0.005)
        with sync["lock"]:
            if not sync["flushed"] and time.monotonic() - sync["t"] > attente:
                try:
                    ffstdin.write(AUD); ffstdin.flush()
                except (BrokenPipeError, ValueError):
                    pass                                         # flux_adb s'en apercevra à sa prochaine écriture
                sync["flushed"] = True
