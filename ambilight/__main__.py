"""Ambilight en direct : les rubans Govee suivent les bords de l'écran d'une Fire TV.

  Fire TV --adb screenrecord (images brutes, ou h264 + ffmpeg)--> 96x54 rgb --> couleurs de zone --> rendu
    * ruban du tour de l'écran : mode razer, UNE trame UDP de n segments, ~30/s, directement vers le ruban
    * 2e ruban (option)        : trame de n segments en UDP vers Home Assistant (composant govee-ble-lights patché)

Usage : python -m ambilight [-c config.toml] [durée_s]     (durée 0 = sans fin)
Code de sortie 1 si un fil de capture est tombé : le service de contrôle (ambilight.ctl) relance alors le programme.
"""
import argparse
import logging
import math
import signal
import socket
import threading
import time

import numpy as np

from . import config as conf
from .capture import FluxMort, flux_adb, flux_raw, instantane, lancer_ffmpeg, veilleur_aud
from .govee_razer import Razer
from .traitement import FRAME, H, W, accentuer, activite, affiner_bords, bandes_noires, lisser, moyennes, poids, rendu

log = logging.getLogger("ambilight")

REACTIVATION = 2.0          # s : une autre automatisation peut faire quitter le mode razer, on le reprend
BAS_RENVOI = 1.0            # s : le 2e ruban reçoit sa trame au moins chaque seconde (un paquet BLE a pu se perdre)
BAS_GARDE = 2               # le 2e ruban ne bouge que si un canal change d'au moins ça
AGITATION_LISSAGE = 0.1     # part de la nouvelle mesure d'agitation dans la moyenne glissante (par image)
TAU_GAIN_ATTAQUE = 0.15     # s : le gain chute tout de suite sur une scène claire (et ne remonte que lentement : pas de pompage)


class Zones:
    """Dernier résultat de traitement, partagé entre les fils de capture et la boucle d'envoi."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.verrou = threading.Lock()
        self.bord = {"crop": (0, 0, 0, 0), "cand": None, "depuis": 0.0}
        self.resultat = None                         # (zones des segments, moyenne globale), publié d'un bloc
        self.n = 0
        self.nouvelle = threading.Event()            # levé à chaque image : la boucle d'envoi n'attend pas son prochain tic

    def traiter(self, img, t, immediat=False):
        """Image -> couleurs de zone. `t` en secondes monotones. `immediat` : pas d'attente de stabilité pour les bandes noires."""
        cand = bandes_noires(img, self.cfg)
        with self.verrou:
            bord = self.bord
            if cand is not None:                     # bandes noires : prises en compte après bord_duree de stabilité
                if immediat:
                    bord["crop"] = bord["cand"] = cand
                elif cand != bord["cand"]:
                    bord["cand"], bord["depuis"] = cand, t
                elif cand != bord["crop"] and t - bord["depuis"] >= self.cfg.bord_duree:
                    bord["crop"] = cand
            crop = bord["crop"]
        n = len(self.cfg.segments)
        z = moyennes(img, poids(self.cfg, crop), self.cfg)
        with self.verrou:
            self.resultat = (affiner_bords(z[:n], self.cfg), z[n:n + 1])
            self.n += 1
        self.nouvelle.set()

    def lire(self):
        with self.verrou:
            return self.resultat, self.n


def lancer_fil(nom, cible, stop, pannes, *args):
    """Un fil qui tombe arrête tout le programme (au lieu de laisser la boucle renvoyer la dernière couleur à l'infini)."""
    def corps():
        try:
            cible(*args)
        except Exception as e:
            if stop.is_set() and isinstance(e, FluxMort):
                log.debug("fil %s arrêté proprement (%s)", nom, e)
            else:
                log.error("fil %s tombé : %s %s", nom, type(e).__name__, e, exc_info=not isinstance(e, FluxMort))
                pannes.append(nom)
        finally:
            stop.set()
    threading.Thread(target=corps, daemon=True, name=nom).start()


def lecteur(ff, zones, stop):
    buf = bytearray(FRAME)
    vue = memoryview(buf)
    while not stop.is_set():
        n = 0
        while n < FRAME:
            k = ff.stdout.readinto(vue[n:])
            if not k:
                raise FluxMort("ffmpeg : fin du flux")
            n += k
        zones.traiter(np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.float32), time.monotonic())


def boucle(cfg, zones, haut, stop, duree):
    """Lisse, rend et envoie jusqu'à `stop` ou la fin de `duree`. Tant que des images arrivent, ce sont elles qui cadencent l'envoi
    (une trame par image, sans attendre un tic) ; écran fixe : `hz` trames par seconde, le lissage finit sa course."""
    nb = cfg.bas_segments
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bas = (cfg.flux_ip, cfg.flux_port) if cfg.flux_ip else None
    lisse_h = lisse_b = fond = None
    agitation, c_image, n_vu = 0.0, None, -1                    # agitation de l'image : écart de couleur moyen par image, lissé sur ~10 images
    pic = pic_bas = 100.0
    prec_c = sortie_h = sortie_b = prec_m = None
    t0 = last = prev = reactive = time.monotonic()
    t_bas = t_bas_envoi = 0.0
    n0 = envois = coupures = 0
    dernier_bas = None
    image = coupure_bas = False
    try:
        while not stop.is_set() and (duree == 0 or time.monotonic() - t0 < duree):
            # Après une image, on laisse 2 périodes à la suivante : un tic glissé juste avant elle lui volerait son `dt`
            # (simulé sur de vraies heures d'arrivée : 50 % du front atteint en 2 ms au lieu de 15, p95 0 au lieu de 31).
            image = zones.nouvelle.wait(max(0.0, prev + (2.0 if image else 1.0) / cfg.hz - time.monotonic()))
            zones.nouvelle.clear()
            now = time.monotonic(); dt = now - prev; prev = now
            resultat, n_images = zones.lire()
            if resultat is None:
                continue
            if now - reactive >= REACTIVATION:
                haut.activer(); reactive = now
            c, moy = resultat
            coupure = prec_c is not None and float(np.abs(c[:, :3] - prec_c[:, :3]).mean()) > cfg.coupure     # changement de plan : on ne traîne pas
            prec_c = c
            fond = c.copy() if fond is None or coupure else fond + (1 - math.exp(-dt / cfg.tau_fond)) * (c - fond)      # fond lent (un changement de plan le remet à zéro)
            if n_images != n_vu:                                 # une nouvelle image est arrivée
                if c_image is not None and not coupure:          # une simple coupure de plan ne doit pas gonfler l'agitation
                    delta_seg = np.abs(c[:, :3] - c_image[:, :3]).mean(axis=1)
                    mesure = 0.6 * float(delta_seg.mean()) + 0.4 * float(np.percentile(delta_seg, 75))
                    agitation += AGITATION_LISSAGE * (mesure - agitation)
                c_image, n_vu = c, n_images
            act = activite(agitation, cfg)
            lent = cfg.lent_calme + (cfg.lent - cfg.lent_calme) * act     # image calme : fond lissé ; image agitée : tout suit
            cible = accentuer(c, fond, cfg, act)
            # Gain auto calculé sur les zones brutes : évite que les pointes transitoires d'accentuer() n'assombrissent les segments calmes
            cmax = float(c[:, :3].max())
            if coupure:                                          # changement de plan : le gain saute aussi (sinon la scène sombre remonte en 5-9 s)
                pic = cmax; coupures += 1; coupure_bas = True
            else:
                pic += (1 - math.exp(-dt / (TAU_GAIN_ATTAQUE if cmax > pic else cfg.tau_pic))) * (cmax - pic)
            g = min(cfg.gain_max, max(1.0, cfg.cible_pic / max(pic, cmax, 8.0)))
            lisse_h = cible.astype(float) if coupure else lisser(lisse_h, cible, dt, cfg.tau_haut, cfg.tau_bas, cfg, lent)   # changement de plan : saut net, aucun fondu
            sortie = rendu(lisse_h, cfg, g)
            if sortie_h is not None and not coupure:             # anti-scintillement (hystérésis) : on ne commet que les vrais changements
                garde = np.abs(sortie - sortie_h).max(1) < cfg.anti_scint
                sortie[garde] = sortie_h[garde]
            sortie_h = sortie
            haut.image([tuple(int(v) for v in x) for x in sortie]); envois += 1

            if bas and now - t_bas >= 1.0 / cfg.bas_hz:          # 2e ruban = AMBIANCE : couleur dominante de toute l'image, douce + gain auto dédié
                moy_max = float(moy[:, :3].max())
                flash = prec_m is not None and moy_max - float(prec_m[:, :3].max()) > cfg.bas_flash
                prec_m = moy
                dt_bas = now - t_bas if t_bas else dt
                coupe_b = coupure or coupure_bas
                coupure_bas = False
                tau_bm = cfg.bas_coupure_tau if coupe_b else cfg.bas_monte
                tau_bd = cfg.bas_coupure_tau if coupe_b else cfg.bas_descend
                lisse_b = lisser(lisse_b, moy, dt_bas, tau_bm, tau_bd, cfg)
                if flash:
                    lisse_b = moy.copy()
                if coupe_b:
                    pic_bas = moy_max
                else:
                    pic_bas += (1 - math.exp(-dt_bas / (TAU_GAIN_ATTAQUE if moy_max > pic_bas else cfg.tau_pic))) * (moy_max - pic_bas)
                g_bas = min(cfg.bas_gain_max, max(1.0, cfg.cible_pic / max(pic_bas, moy_max, 8.0)))
                z = rendu(lisse_b, cfg, g_bas, balance=(cfg.bas_balance or cfg.balance), gamma=cfg.bas_gamma)[0]
                if 0 < z.max() < cfg.bas_plancher_actif:         # H6125 s'éteint sous ~4 % : remonter au plancher visible si scène non noire
                    z = np.clip(np.round(z * (cfg.bas_plancher_actif / float(z.max()))), 0, 255).astype(int)
                if sortie_b is not None and not flash and np.abs(z - sortie_b).max() < BAS_GARDE:
                    z = sortie_b
                sortie_b = z
                q = (np.round(z / cfg.pas_bas) * cfg.pas_bas).clip(0, 255).astype(int)
                trame = bytes(int(v) for _ in range(nb) for v in q)
                if trame != dernier_bas or now - t_bas_envoi >= BAS_RENVOI:
                    sock.sendto(trame, bas); dernier_bas = trame; t_bas_envoi = now
                t_bas = now

            if now - last >= 5:
                log.info("%5.0fs  images %4.1f/s  trames %4.1f/s  gain x%.1f  agitation %.1f (effet %.0f%%)  coupures %d", now - t0,
                         (n_images - n0) / (now - last), envois / (now - last), g, agitation, 100 * act, coupures)
                n0 = n_images; envois = coupures = 0; last = now
    finally:
        if bas:
            try:
                sock.sendto(bytes(3 * nb), bas)                  # 2e ruban au noir : sans source, il resterait sur la dernière couleur
            except OSError:
                pass
        sock.close()


def main(cfg, duree):
    stop = threading.Event()
    pannes = []
    zones = Zones(cfg)
    amorce = lambda: zones.traiter(instantane(cfg), time.monotonic(), True)
    ff = None
    if cfg.capture == "raw":                                     # images brutes : ni ffmpeg, ni délimiteur AUD, ni limite de 180 s
        lancer_fil("adb", flux_raw, stop, pannes, cfg, lambda img: zones.traiter(img, time.monotonic()), stop, amorce)
    else:
        ff = lancer_ffmpeg(cfg)
        sync = {"lock": threading.Lock(), "t": 0.0, "flushed": True}
        lancer_fil("adb", flux_adb, stop, pannes, cfg, ff.stdin, stop, sync, amorce)
        lancer_fil("aud", veilleur_aud, stop, pannes, ff.stdin, stop, sync, cfg.aud_attente)
        lancer_fil("lecteur", lecteur, stop, pannes, ff, zones, stop)

    def premiere_image():                                        # écran fixe dès le départ : screenrecord ne dira rien
        try:
            amorce()
        except Exception as e:
            log.warning("capture initiale : %s %s", type(e).__name__, e)
    threading.Thread(target=premiere_image, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())    # arrêt propre : le « finally » rend le ruban et éteint le 2e
    haut = Razer(cfg.ruban_ip, cfg.ruban_port)
    haut.activer()
    try:
        boucle(cfg, zones, haut, stop, duree)
    finally:
        stop.set()
        if ff:
            ff.kill(); ff.wait()
        try:
            haut.desactiver()
        except OSError:
            pass
        haut.close()
    return 1 if pannes else 0


def cli(argv=None):
    p = argparse.ArgumentParser(prog="ambilight", description=__doc__.split("\n\n")[0])
    p.add_argument("-c", "--config", help="fichier de configuration TOML (sinon $AMBI_CONFIG ou ./config.toml)")
    p.add_argument("duree", nargs="?", type=float, default=0, help="durée en secondes (0 = sans fin)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = conf.charger(a.config)
    conf.verifier(cfg)
    return main(cfg, a.duree)


if __name__ == "__main__":
    raise SystemExit(cli())
