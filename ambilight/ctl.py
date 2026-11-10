"""Service de contrôle de l'ambilight (HTTP).

    POST /on      lance `python -m ambilight`
    POST /off     l'arrête et rend le ruban à son mode normal (désactive le mode razer)
    GET  /status  {"on": bool, "pid": int|null, "depuis": epoch|null, "redemarrages": int}

Seules les adresses de `ctl_autorises` sont acceptées (filtre par adresse source, pas d'authentification : à réserver à un réseau
de confiance). Le service démarre éteint et relance le programme s'il tombe pendant qu'il doit tourner.
Usage : python -m ambilight.ctl [-c config.toml]
"""
import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as conf
from .govee_razer import Razer

log = logging.getLogger("ambilight.ctl")

RELANCES_MAX = 10                       # par fenêtre de FENETRE secondes, après quoi on abandonne
FENETRE = 600


class Controleur:
    def __init__(self, cfg, chemin_config):
        self.cfg = cfg
        self.commande = [sys.executable, "-u", "-m", "ambilight"] + (["-c", chemin_config] if chemin_config else [])
        self.verrou = threading.Lock()
        self.on = False
        self.proc = None
        self.depuis = None
        self.redemarrages = 0

    def _lancer(self):
        self.proc = subprocess.Popen(self.commande)
        self.depuis = self.depuis or time.time()

    def _arreter(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.proc = None
        self.depuis = None
        self._rendre_le_ruban()

    def _rendre_le_ruban(self):         # SIGTERM saute le « finally » du programme : on rend le ruban nous-mêmes
        try:
            r = Razer(self.cfg.ruban_ip, self.cfg.ruban_port)
            r.desactiver(); r.close()
        except OSError as e:
            log.warning("ruban non rendu : %s", e)

    def demarrer(self):
        with self.verrou:
            if not self.on:
                self.on = True
                self._lancer()

    def arreter(self):
        with self.verrou:
            self.on = False
            self._arreter()

    def statut(self):
        with self.verrou:
            p = self.proc
            return {"on": self.on, "pid": p.pid if p and p.poll() is None else None,
                    "depuis": self.depuis, "redemarrages": self.redemarrages}

    def veiller(self):
        """Si le programme meurt alors qu'il doit tourner, on le relance (RELANCES_MAX fois par tranche de FENETRE s)."""
        recents = []
        while True:
            time.sleep(3)
            with self.verrou:
                p = self.proc
                if not (self.on and p and p.poll() is not None):
                    continue
                recents = [t for t in recents if time.time() - t < FENETRE] + [time.time()]
                if len(recents) > RELANCES_MAX:
                    log.error("trop de redémarrages : arrêt")
                    self.on = False; self.proc = None; self.depuis = None
                    self._rendre_le_ruban()
                    continue
                self.redemarrages += 1
                log.warning("programme tombé (code %s) : relance", p.returncode)
                try:
                    self._lancer()
                except OSError as e:                 # sans ça, le fil mourrait en silence et /status annoncerait « on » pour rien
                    log.error("relance impossible : %s", e)
                    self.on = False; self.proc = None; self.depuis = None


def gestionnaire(ctl):
    autorises = set(ctl.cfg.ctl_autorises)

    class Gestionnaire(BaseHTTPRequestHandler):
        def _repondre(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def _autorise(self):
            if self.client_address[0] not in autorises:
                self._repondre(403, {"erreur": "interdit"}); return False
            return True

        def do_GET(self):
            if not self._autorise():
                return
            if self.path == "/status":
                self._repondre(200, ctl.statut())
            else:
                self._repondre(404, {"erreur": "inconnu"})

        def do_POST(self):
            if not self._autorise():
                return
            if self.path == "/on":
                ctl.demarrer()
            elif self.path == "/off":
                ctl.arreter()
            else:
                self._repondre(404, {"erreur": "inconnu"}); return
            self._repondre(200, ctl.statut())

        def log_message(self, fmt, *a):
            log.info("%s %s", self.client_address[0], fmt % a)

    return Gestionnaire


def cli(argv=None):
    p = argparse.ArgumentParser(prog="ambilight-ctl")
    p.add_argument("-c", "--config")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = conf.charger(a.config)
    conf.verifier(cfg)
    ctl = Controleur(cfg, a.config)

    def fin(*_):                         # SIGTERM / Ctrl-C : arrêt propre de l'enfant, jamais d'orphelin qui rallume le ruban
        ctl.arreter()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, fin)
    signal.signal(signal.SIGINT, fin)
    threading.Thread(target=ctl.veiller, daemon=True).start()
    ThreadingHTTPServer((cfg.ctl_ecoute, cfg.ctl_port), gestionnaire(ctl)).serve_forever()


if __name__ == "__main__":
    cli()
