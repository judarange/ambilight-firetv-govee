import base64
import json
from functools import reduce

from ambilight.govee_razer import Razer


class FauxSocket:
    def __init__(self):
        self.envois = []

    def sendto(self, data, adresse):
        self.envois.append((json.loads(data)["msg"]["data"]["pt"], adresse))


def octets(r, i=-1):
    return base64.b64decode(r.s.envois[i][0])


def razer():
    r = Razer("192.0.2.1")
    r.s = FauxSocket()
    return r


def test_activation_et_desactivation():
    r = razer()
    r.activer(); r.desactiver()
    assert octets(r, 0) == bytes.fromhex("BB0001B1010A")
    assert octets(r, 1) == bytes.fromhex("BB0001B100" "0B")
    assert r.s.envois[0][1] == ("192.0.2.1", 4003)


def test_image_15_segments():
    r = razer()
    r.image([(i, 2 * i, 3 * i) for i in range(15)])
    b = octets(r)
    assert len(b) == 52 and b[:6] == bytes([0xBB, 0, 47, 0xB0, 1, 15])
    assert reduce(lambda x, y: x ^ y, b) == 0              # octet de contrôle : XOR total nul
