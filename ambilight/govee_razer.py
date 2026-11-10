"""Mode « Razer » (DreamView) des Govee LAN : UNE trame UDP règle tous les segments (contre un paquet par couleur en ptReal).
Format (OpenRGB GoveeController) : BB <len_hi> <len_lo> B0 01 <N> <r g b>*N <xor>, len = 2+3N ; activation BB 00 01 B1 01 0A.
Le ruban quitte ce mode s'il ne reçoit rien pendant 1 minute."""
import base64
import json
import socket


def _avec_controle(octets):
    b = bytearray(octets)
    x = 0
    for v in b:
        x ^= v
    b.append(x)
    return bytes(b)


class Razer:
    def __init__(self, ip, port=4003):
        self.adresse = (ip, port)
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _envoyer(self, brut):
        msg = {"msg": {"cmd": "razer", "data": {"pt": base64.b64encode(brut).decode()}}}
        self.s.sendto(json.dumps(msg).encode(), self.adresse)

    def activer(self):
        self._envoyer(bytes([0xBB, 0x00, 0x01, 0xB1, 0x01, 0x0A]))

    def desactiver(self):
        self._envoyer(bytes([0xBB, 0x00, 0x01, 0xB1, 0x00, 0x0B]))

    def image(self, couleurs):
        n = len(couleurs)
        p = bytearray([0xBB, (2 + 3 * n) >> 8, (2 + 3 * n) & 0xFF, 0xB0, 0x01, n])
        for r, g, b in couleurs:
            p += bytes([r, g, b])
        self._envoyer(_avec_controle(p))

    def close(self):
        self.s.close()
