"""Outil CLI de calibration visuelle rapide (~2 minutes) pour les rubans Govee H619A (Razer) et H6125 (BLE).

Appelle directement `traitement.rendu()` avec les réglages ajustés en direct afin de calibrer exactement la chaîne
de production : balance des blancs, gamma par canal, correction du jaune pur (`jaune_v`) et du bleu (`bleu_v`),
séparément pour le ruban du tour (`haut`) et le ruban du meuble (`bas`).

Usage :
    python -m ambilight.calibrer [-c config.toml] [--arreter-ctl]
    python -m ambilight.calibrer -c config.toml --mire jaune_chaud --gamma 2.1,2.25,2.1 --balance 1.0,0.78,0.88
    python -m ambilight.calibrer -c config.toml --simuler --mire balayage
"""
import argparse
import dataclasses
import json
import os
import select
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np

from . import config as conf
from .govee_razer import Razer
from .traitement import rendu

try:
    import termios
    import tty
    _POSIX_TTY = True
except ImportError:
    _POSIX_TTY = False


KEEPALIVE_S = 0.5
REACTIVATION_S = 10.0
BALAYAGE_INTERVALLE_S = 1.5

# (identifiant, libellé, RGB cible ou None pour balayage, conseil de réglage)
MIRES = (
    ("blanc", "Blanc pur (255,255,255)", (255, 255, 255),
     "Régler balance (r/R, g/G, b/B) jusqu'à un blanc neutre identique à l'écran."),
    ("gris", "Gris 50 % (128,128,128)", (128, 128, 128),
     "Vérifier gamma + balance dans les tons moyens (gris neutre sans teinte verte/rose)."),
    ("jaune_pur", "Jaune pur (255,255,0)", (255, 255, 0),
     "Ajuster jaune_v (j/J) si le jaune pur tire encore vers le vert alors que le blanc est neutre."),
    ("jaune_chaud", "Jaune chaud / Or (255,200,0)", (255, 200, 0),
     "Ajuster gamma ([ / ] ou x/y/z) : doit rester doré/chaud, pas citron pâle."),
    ("jaune_pastel", "Jaune pastel (255,255,150)", (255, 255, 150),
     "Ajuster gamma : doit rester un jaune doux lisible, pas blanc crème."),
    ("orange", "Orange vif (255,128,0)", (255, 128, 0),
     "Vérifier gamma R/V : franc et orangé, sans dériver vers le jaune."),
    ("bleu_pur", "Bleu pur (0,0,255)", (0, 0, 255),
     "Ajuster bleu_v (v/V) pour retirer le reflet violet sans verdir le bleu."),
    ("violet", "Violet (140,0,255)", (140, 0, 255),
     "Vérifier gamma + bleu_v : violet profond, pas lavande pâle."),
    ("balayage", "Balayage complet (9 teintes)", None,
     "Vérification globale sur Blanc, Jaune, Or, Pastel, Orange, Rouge, Vert, Cyan, Bleu, Violet."),
)

_ETAPES_BALAYAGE = (
    ("Blanc", (255, 255, 255)),
    ("Jaune pur", (255, 255, 0)),
    ("Or", (255, 200, 0)),
    ("Jaune pastel", (255, 255, 150)),
    ("Orange", (255, 128, 0)),
    ("Rouge", (255, 0, 0)),
    ("Vert", (0, 255, 0)),
    ("Cyan", (0, 255, 255)),
    ("Bleu", (0, 0, 255)),
    ("Violet", (140, 0, 255)),
)


def _parser_triplet(texte, nom):
    """Parse 'v' ou 'r,v,b' en tuple de 3 flottants."""
    morceaux = [m.strip() for m in str(texte).split(",") if m.strip() != ""]
    if len(morceaux) == 1:
        v = float(morceaux[0])
        return (v, v, v)
    if len(morceaux) == 3:
        return tuple(float(m) for m in morceaux)
    raise argparse.ArgumentTypeError(f"{nom} attend 1 ou 3 nombres séparés par des virgules (reçu : {texte!r})")


def _trouver_mire(identifiant):
    """Retourne l'index [0..len(MIRES)-1] à partir d'un nom ou d'un numéro 1-based."""
    cle = str(identifiant).strip().lower()
    if cle.isdigit():
        idx = int(cle) - 1
        if 0 <= idx < len(MIRES):
            return idx
    for i, (nom, *_reste) in enumerate(MIRES):
        if nom == cle:
            return i
    valides = ", ".join(nom for nom, *_ in MIRES)
    raise argparse.ArgumentTypeError(f"mire inconnue : {identifiant!r} (valides : 1..{len(MIRES)} ou {valides})")


def _tuple3(val, defaut=(1.0, 1.0, 1.0)):
    if isinstance(val, (int, float)):
        return (float(val), float(val), float(val))
    if val and len(val) == 3:
        return tuple(float(x) for x in val)
    return tuple(float(x) for x in defaut)


def couleur_mire(idx_mire, t_ecoule=0.0):
    """Retourne (sous_libelle, (r, g, b)) pour la mire donnée à l'instant `t_ecoule`."""
    _nom, libelle, rgb, _conseil = MIRES[idx_mire]
    if rgb is not None:
        return libelle, rgb
    etape = int(t_ecoule // BALAYAGE_INTERVALLE_S) % len(_ETAPES_BALAYAGE)
    nom_etape, rgb_etape = _ETAPES_BALAYAGE[etape]
    return f"{libelle} — {nom_etape} {rgb_etape}", rgb_etape


def _appeler_rendu(rgb, cfg, balance, gamma, pas=1):
    """Appelle `traitement.rendu()` avec compatibilité stricte (signature avec ou sans kwargs balance/gamma)."""
    zone = np.array([[float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0]], dtype=np.float32)
    try:
        out = rendu(zone, cfg, g=1.0, pas=pas, balance=balance, gamma=gamma)[0]
    except TypeError:
        cfg_tmp = dataclasses.replace(cfg, balance=balance, gamma=gamma)
        out = rendu(zone, cfg_tmp, g=1.0, pas=pas)[0]
    return tuple(int(v) for v in out)


def calculer_sorties(rgb, cfg):
    """Calcule les octets envoyés aux deux rubans via `traitement.rendu()`."""
    gamma_h = _tuple3(getattr(cfg, "gamma", (2.2, 2.2, 2.2)), (2.2, 2.2, 2.2))
    bal_h = _tuple3(cfg.balance)
    out_h = _appeler_rendu(rgb, cfg, balance=bal_h, gamma=gamma_h, pas=1)

    bas_bal_cfg = getattr(cfg, "bas_balance", ())
    bal_b = _tuple3(bas_bal_cfg) if bas_bal_cfg else bal_h
    gamma_b = _tuple3(getattr(cfg, "bas_gamma", gamma_h), gamma_h)
    z_b = np.array(_appeler_rendu(rgb, cfg, balance=bal_b, gamma=gamma_b, pas=1), dtype=int)
    if 0 < z_b.max() < cfg.bas_plancher_actif:
        z_b = np.clip(np.round(z_b * (cfg.bas_plancher_actif / float(z_b.max()))), 0, 255).astype(int)
    pas_b = max(1, int(cfg.pas_bas))
    q_b = (np.round(z_b / pas_b) * pas_b).clip(0, 255).astype(int)
    return out_h, tuple(int(v) for v in q_b)


def formater_toml(cfg):
    """Produit le bloc TOML exact à coller dans `config.toml`."""
    gamma_h = _tuple3(getattr(cfg, "gamma", (2.2, 2.2, 2.2)), (2.2, 2.2, 2.2))
    bal_h = _tuple3(cfg.balance)
    gamma_b = _tuple3(getattr(cfg, "bas_gamma", gamma_h), gamma_h)
    bas_bal_cfg = getattr(cfg, "bas_balance", ())
    bal_b = _tuple3(bas_bal_cfg) if bas_bal_cfg else bal_h
    jaune_v = float(getattr(cfg, "jaune_v", 1.0))
    bleu_v = float(cfg.bleu_v)
    toe = float(cfg.toe)
    return "\n".join([
        "# ---- Calibration LED (généré par ambilight.calibrer) ----",
        f"gamma = [{gamma_h[0]:.2f}, {gamma_h[1]:.2f}, {gamma_h[2]:.2f}]",
        f"balance = [{bal_h[0]:.2f}, {bal_h[1]:.2f}, {bal_h[2]:.2f}]",
        f"jaune_v = {jaune_v:.2f}",
        f"bleu_v = {bleu_v:.2f}",
        f"toe = {toe:.1f}",
        f"bas_gamma = [{gamma_b[0]:.2f}, {gamma_b[1]:.2f}, {gamma_b[2]:.2f}]",
        f"bas_balance = [{bal_b[0]:.2f}, {bal_b[1]:.2f}, {bal_b[2]:.2f}]",
        f"pas_bas = {int(cfg.pas_bas)}",
    ])


def _ctl_requete(port, methode, chemin):
    url = f"http://127.0.0.1:{port}{chemin}"
    req = urllib.request.Request(url, method=methode)
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        return json.loads(resp.read().decode())


def _verifier_ctl(cfg, arreter):
    """Vérifie le service `ambilight.ctl` local ; l'arrête si demandé et indique s'il faut le relancer."""
    try:
        st = _ctl_requete(cfg.ctl_port, "GET", "/status")
    except (OSError, urllib.error.URLError, ValueError):
        return False
    if not st.get("on"):
        return False
    if arreter:
        _ctl_requete(cfg.ctl_port, "POST", "/off")
        time.sleep(0.3)
        return True
    print(
        f"ATTENTION : ambilight.ctl est actif sur 127.0.0.1:{cfg.ctl_port} (PID {st.get('pid')}).\n"
        "Relancez avec `--arreter-ctl` ou mettez Ambilight à OFF dans Home Assistant pour éviter les conflits.",
        file=sys.stderr,
    )
    return False


class Diffuseur:
    """Envoie en boucle (keepalive 0,5 s) les couleurs de mire vers le H619A et/ou le H6125."""

    def __init__(self, cfg, ruban, simuler=False):
        self.cfg = cfg
        self.ruban = ruban
        self.simuler = simuler
        self.haut = None
        self.sock = None
        self.bas_addr = (cfg.flux_ip, cfg.flux_port) if cfg.flux_ip else None
        self._verrou = threading.Lock()
        self._couleur_h = (0, 0, 0)
        self._couleur_b = (0, 0, 0)
        self._stop = threading.Event()
        self._thread = None
        self._dernier_act = 0.0

    def ouvrir(self):
        if self.simuler:
            return
        if self.ruban in ("haut", "les-deux") and self.cfg.ruban_ip:
            self.haut = Razer(self.cfg.ruban_ip, self.cfg.ruban_port)
            self.haut.activer()
            self._dernier_act = time.monotonic()
        if self.ruban in ("bas", "les-deux") and self.bas_addr:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._boucle, daemon=True, name="calibrer-keepalive")
        self._thread.start()

    def definir(self, ruban, out_h, out_b):
        with self._verrou:
            self.ruban = ruban
            self._couleur_h = out_h if ruban in ("haut", "les-deux") else (0, 0, 0)
            self._couleur_b = out_b if ruban in ("bas", "les-deux") else (0, 0, 0)
        self._envoyer_maintenant()

    def _envoyer_maintenant(self):
        if self.simuler:
            return
        with self._verrou:
            ch = self._couleur_h
            cb = self._couleur_b
        now = time.monotonic()
        if self.haut:
            if now - self._dernier_act >= REACTIVATION_S:
                self.haut.activer()
                self._dernier_act = now
            self.haut.image([ch] * len(self.cfg.segments))
        if self.sock and self.bas_addr:
            trame = bytes(int(v) for _ in range(self.cfg.bas_segments) for v in cb)
            self.sock.sendto(trame, self.bas_addr)

    def _boucle(self):
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self._envoyer_maintenant()
            except OSError:
                pass

    def fermer(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.sock and self.bas_addr:
            try:
                self.sock.sendto(bytes(3 * self.cfg.bas_segments), self.bas_addr)
            except OSError:
                pass
            self.sock.close()
        if self.haut:
            try:
                self.haut.desactiver()
            except OSError:
                pass
            self.haut.close()


def _clamped(val, lo, hi):
    return round(min(hi, max(lo, val)), 4)


def _maj_triplet(t, idx, delta, lo, hi):
    lst = list(t)
    if idx is None:
        lst = [_clamped(v + delta, lo, hi) for v in lst]
    else:
        lst[idx] = _clamped(lst[idx] + delta, lo, hi)
    return tuple(lst)


def _afficher_etat(idx_mire, ruban, cfg, rgb_cible, out_h, out_b, libelle_mire):
    gamma_h = _tuple3(getattr(cfg, "gamma", (2.2, 2.2, 2.2)), (2.2, 2.2, 2.2))
    bal_h = _tuple3(cfg.balance)
    gamma_b = _tuple3(getattr(cfg, "bas_gamma", gamma_h), gamma_h)
    bas_bal_cfg = getattr(cfg, "bas_balance", ())
    bal_b = _tuple3(bas_bal_cfg) if bas_bal_cfg else bal_h
    conseil = MIRES[idx_mire][3]
    lignes = [
        "\033[H\033[J" if sys.stdout.isatty() else "",
        f"=== Calibration Ambilight ({idx_mire + 1}/{len(MIRES)}) — Ruban actif : {ruban.upper()} ===",
        f"Mire     : {libelle_mire}",
        f"Conseil  : {conseil}",
        f"Sortie   : H619A={out_h}   H6125={out_b}   (cible {rgb_cible})",
        "",
        f"H619A (haut) : gamma={list(round(x, 2) for x in gamma_h)}  balance={list(round(x, 2) for x in bal_h)}",
        f"H6125 (bas)  : bas_gamma={list(round(x, 2) for x in gamma_b)}  bas_balance={list(round(x, 2) for x in bal_b)}",
        f"Communs      : jaune_v={getattr(cfg, 'jaune_v', 1.0):.2f}  bleu_v={cfg.bleu_v:.2f}  toe={cfg.toe:.1f}",
        "",
        "Touches : [1..9] mire  [n/Espace] suivante  [Tab] haut/bas/les-deux",
        "          [r/R g/G b/B] balance ±0.02  [[ / ]] gamma ±0.05  [x/X y/Y z/Z] gamma R/V/B ±0.05",
        "          [j/J] jaune_v ±0.02  [v/V] bleu_v ±0.01  [0] reset  [p] TOML  [q] quitter",
    ]
    sys.stdout.write("\n".join(lignes) + "\n")
    sys.stdout.flush()


def _appliquer_touche(ch, idx_mire, ruban, cfg, cfg_init):
    """Applique une touche interactive et retourne (idx_mire, ruban, cfg, action)."""
    if ch in ("q", "Q", "\x03"):
        return idx_mire, ruban, cfg, "quitter"
    if ch == "p":
        return idx_mire, ruban, cfg, "imprimer"
    if ch in "123456789":
        return int(ch) - 1, ruban, cfg, "maj"
    if ch in ("n", " ", "\r", "\n"):
        return (idx_mire + 1) % len(MIRES), ruban, cfg, "maj"
    if ch == "\t":
        ordre = ("haut", "bas", "les-deux")
        return idx_mire, ordre[(ordre.index(ruban) + 1) % len(ordre)], cfg, "maj"
    if ch == "0":
        return idx_mire, ruban, cfg_init, "maj"

    cible_bas = ruban == "bas"
    bal_courante = _tuple3(getattr(cfg, "bas_balance", ()) or cfg.balance) if cible_bas else _tuple3(cfg.balance)
    gam_courant = _tuple3(getattr(cfg, "bas_gamma", cfg.gamma)) if cible_bas else _tuple3(cfg.gamma)

    bal_touches = {"r": (0, -0.02), "R": (0, +0.02), "g": (1, -0.02), "G": (1, +0.02), "b": (2, -0.02), "B": (2, +0.02)}
    gam_touches = {"[": (None, -0.05), "]": (None, +0.05), "x": (0, -0.05), "X": (0, +0.05),
                   "y": (1, -0.05), "Y": (1, +0.05), "z": (2, -0.05), "Z": (2, +0.05)}

    if ch in bal_touches:
        i, d = bal_touches[ch]
        nouv = _maj_triplet(bal_courante, i, d, 0.20, 1.50)
        cfg = dataclasses.replace(cfg, bas_balance=nouv) if cible_bas else dataclasses.replace(cfg, balance=nouv)
        return idx_mire, ruban, cfg, "maj"
    if ch in gam_touches:
        i, d = gam_touches[ch]
        nouv = _maj_triplet(gam_courant, i, d, 1.00, 3.50)
        cfg = dataclasses.replace(cfg, bas_gamma=nouv) if cible_bas else dataclasses.replace(cfg, gamma=nouv)
        return idx_mire, ruban, cfg, "maj"
    if ch in ("j", "J") and hasattr(cfg, "jaune_v"):
        d = -0.02 if ch == "j" else +0.02
        return idx_mire, ruban, dataclasses.replace(cfg, jaune_v=_clamped(cfg.jaune_v + d, 0.50, 1.15)), "maj"
    if ch in ("v", "V"):
        d = -0.01 if ch == "v" else +0.01
        return idx_mire, ruban, dataclasses.replace(cfg, bleu_v=_clamped(cfg.bleu_v + d, 0.00, 0.50)), "maj"
    return idx_mire, ruban, cfg, None


def _appliquer_overrides(cfg, args):
    """Applique les options CLI (--gamma, --balance, etc.) à la config chargée."""
    modifs = {}
    if args.gamma is not None:
        modifs["gamma"] = args.gamma
    if args.balance is not None:
        modifs["balance"] = args.balance
    if args.bas_gamma is not None and hasattr(cfg, "bas_gamma"):
        modifs["bas_gamma"] = args.bas_gamma
    if args.bas_balance is not None and hasattr(cfg, "bas_balance"):
        modifs["bas_balance"] = args.bas_balance
    if args.jaune_v is not None and hasattr(cfg, "jaune_v"):
        modifs["jaune_v"] = args.jaune_v
    if args.bleu_v is not None:
        modifs["bleu_v"] = args.bleu_v
    return dataclasses.replace(cfg, **modifs) if modifs else cfg


def _verifier_cible_reseau(cfg, ruban, simuler):
    if simuler:
        return
    if ruban in ("haut", "les-deux") and not cfg.ruban_ip:
        raise SystemExit("ruban_ip manquant dans la configuration pour piloter le H619A (utilisez --simuler sinon)")
    if ruban in ("bas", "les-deux") and not cfg.flux_ip:
        if ruban == "bas":
            raise SystemExit("flux_ip manquant dans la configuration pour piloter le H6125")


def construire_parser():
    p = argparse.ArgumentParser(prog="ambilight.calibrer", description="Calibration visuelle rapide H619A & H6125")
    p.add_argument("-c", "--config", help="fichier config.toml (sinon $AMBI_CONFIG ou ./config.toml)")
    p.add_argument("--ruban", choices=("haut", "bas", "les-deux"), default="les-deux", help="ruban ciblé (défaut : les-deux)")
    p.add_argument("--mire", type=_trouver_mire, default=0, help="mire initiale (1..9 ou nom : blanc, jaune_pur, jaune_chaud, ...)")
    p.add_argument("--gamma", type=lambda s: _parser_triplet(s, "--gamma"), help="gamma H619A : v ou r,v,b")
    p.add_argument("--balance", type=lambda s: _parser_triplet(s, "--balance"), help="balance H619A : r,v,b")
    p.add_argument("--bas-gamma", type=lambda s: _parser_triplet(s, "--bas-gamma"), help="gamma H6125 : v ou r,v,b")
    p.add_argument("--bas-balance", type=lambda s: _parser_triplet(s, "--bas-balance"), help="balance H6125 : r,v,b")
    p.add_argument("--jaune-v", type=float, help="frein vert sur le jaune pur (ex. 0.90)")
    p.add_argument("--bleu-v", type=float, help="ajout vert sur le bleu pur (ex. 0.05)")
    p.add_argument("--duree", type=float, default=0.0, help="durée fixe en secondes (mode non-interactif ; 0 = interactif)")
    p.add_argument("--arreter-ctl", action="store_true", help="met ambilight.ctl sur /off pendant la calibration puis /on à la fin")
    p.add_argument("--simuler", action="store_true", help="n'envoie rien sur le réseau ; calcule et affiche les sorties + TOML")
    return p


def executer(args):
    cfg_init = conf.charger(args.config)
    cfg = _appliquer_overrides(cfg_init, args)
    ruban = args.ruban
    idx_mire = args.mire
    _verifier_cible_reseau(cfg, ruban, args.simuler)

    if args.simuler and args.duree <= 0:
        # Mode vérification rapide sans matériel : calcule toutes les mires (ou la mire demandée) et imprime le TOML
        indices = [idx_mire] if idx_mire != 0 or any(
            getattr(args, k) is not None for k in ("gamma", "balance", "bas_gamma", "bas_balance", "jaune_v", "bleu_v")
        ) else range(len(MIRES))
        for i in indices:
            lib, rgb = couleur_mire(i, 0.0)
            out_h, out_b = calculer_sorties(rgb, cfg)
            print(f"[{i + 1}/{len(MIRES)}] {lib:30s} -> H619A={out_h}  H6125={out_b}")
        print("\n" + formater_toml(cfg))
        return 0

    ctl_etait_actif = False if args.simuler else _verifier_ctl(cfg, args.arreter_ctl)
    diffuseur = Diffuseur(cfg, ruban, simuler=args.simuler)
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    diffuseur.ouvrir()
    t0 = time.monotonic()
    ancien_tty = None
    interactif = args.duree <= 0 and _POSIX_TTY and sys.stdin.isatty()
    try:
        if interactif:
            fd = sys.stdin.fileno()
            ancien_tty = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        derniere_etape = None
        besoin_affichage = True
        while not stop.is_set():
            ecoule = time.monotonic() - t0
            if args.duree > 0 and ecoule >= args.duree:
                break
            lib, rgb = couleur_mire(idx_mire, ecoule)
            etape_courante = (idx_mire, rgb)
            if besoin_affichage or etape_courante != derniere_etape:
                out_h, out_b = calculer_sorties(rgb, cfg)
                diffuseur.definir(ruban, out_h, out_b)
                _afficher_etat(idx_mire, ruban, cfg, rgb, out_h, out_b, lib)
                derniere_etape = etape_courante
                besoin_affichage = False
            if interactif:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.25)
                if rlist:
                    ch = sys.stdin.read(1)
                    idx_mire, ruban, cfg, action = _appliquer_touche(ch, idx_mire, ruban, cfg, cfg_init)
                    if action == "quitter":
                        break
                    if action == "imprimer":
                        print("\n" + formater_toml(cfg) + "\n")
                    elif action == "maj":
                        besoin_affichage = True
            else:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if ancien_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, ancien_tty)
        diffuseur.fermer()
        if ctl_etait_actif:
            try:
                _ctl_requete(cfg.ctl_port, "POST", "/on")
            except (OSError, urllib.error.URLError):
                pass
        print("\n" + formater_toml(cfg))
    return 0


def cli(argv=None):
    args = construire_parser().parse_args(argv)
    return executer(args)


if __name__ == "__main__":
    raise SystemExit(cli())
