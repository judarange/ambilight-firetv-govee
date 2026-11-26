# ambilight-firetv-govee

Live ambilight for an Amazon **Fire TV**: a **Govee** LED strip around the TV follows the edges of the screen, **with no HDMI capture box and no camera**.

```
Fire TV --ADB: screenrecord (h264) --> ffmpeg --> 96x54 --> zone colours --> render --> Govee strip (LAN "razer" mode)
```

* About **26 frames/s** from the Fire TV, one LED frame sent per image received. Latency was measured end to end at roughly **90 ms**
  with a browser clock page shown on the TV (so it includes the browser's own rendering, and the TV's display delay is not counted).
* The colour pipeline is tuned by eye on real content (talk shows, films, anime), not just maths: linear-light averaging, Gaussian edge-weighted
  zones with anti-halo Rec.709 spatial sharpening (`nettete`), perceptual **Oklab** smoothing and LCh vibrance (`vibr_ok`) that keeps whites white
  and skin tones natural (YCbCr + Oklab skin mask), **dominant colour** (18 hue bins) for zones where hues cancel (a purple character in front of a
  pale background stays purple instead of averaging to beige), a pre-gain black-gate toe curve so a video's "black" `(3,12,8)` does not light a
  green LED even at ×3 auto-gain, per-channel LED **gamma** calibration, subtitle-safe black-bar (letterbox) detection, automatic gain for dark
  scenes, a soft ceiling for snow/sunlight, hard cuts on scene changes, flicker hysteresis, and **motion emphasis that adapts**:
  calm on a talk show, full on a fight scene or lightning.
* Pure-numpy core (`ambilight/traitement.py`) with an interactive LED colour calibration CLI (`python -m ambilight.calibrer`) and unit tests that need no hardware.

> Language: the code, comments, configuration keys, `--help` and log lines are in **French** (the author's language). The README is in English.
> A glossary of the main settings is in `ambilight/config.py` (each field has a comment).

## What you need

| | |
|---|---|
| Fire TV | Fire Stick 4K or similar, **ADB debugging over network** enabled |
| Strip | a Govee strip with **LAN control** and the **razer / DreamView** mode (tested: **H619A**) |
| A small always-on machine | Linux, Python ≥ 3.11, `ffmpeg` (decodes the h264 stream and the static-screen screenshot) |
| Optional | Home Assistant, to switch it on/off |

Limits you should know about:
* Protected apps (Netflix, Prime, Disney+) return a **black screen** to `screenrecord`: the strip stays dark there.
* A fixed screen makes `screenrecord` go silent: one screenshot takes over, and the first image after a pause can be a few seconds late.
* The default capture is `h264` (the Fire TV's hardware encoder; it reconnects every 180 s and shows an occasional decoding glitch).
  `capture = "raw"` (raw RGB frames, no ffmpeg) also works but **slows the Fire TV down a lot**: measured on a Fire Stick 4K Max, the
  home menu went from 24 ms to 200 ms per frame (`dumpsys gfxinfo`), while h264 was indistinguishable from ambilight off. Not recommended.

## Install

```bash
sudo apt install ffmpeg python3-venv
python3 -m venv .venv && . .venv/bin/activate
pip install .                                   # numpy + adb-shell
python -c "from adb_shell.auth.keygen import keygen; keygen('adbkey')"     # creates adbkey / adbkey.pub (keep adbkey SECRET)
cp config.example.toml config.toml              # set fire_ip, ruban_ip, ctl_autorises, segments
python -m ambilight -c config.toml 30           # 30 s test; the TV asks once to accept the key: tick "always allow"
```

The private key `adbkey` gives a **full shell on your Fire TV**: it is git-ignored, keep it `chmod 600`.

On the Fire TV: *Settings → My Fire TV → Developer options → ADB debugging*.
On the strip: Govee app → device settings → **LAN Control**.

## Calibrate the segments (the part that makes it precise)

The strip is rarely a clean loop around the screen (too long, a fold-back, a start in a corner). Instead of assuming an order, **measure where each segment really is**:

1. Show a black image on the TV and film the room with any camera (a phone is fine).
2. Light **one segment at a time** in white and note where it lights. (Any Govee LAN client works; the razer frame in `ambilight/govee_razer.py` can do it:
   `Razer(ip).image([(0,0,0)]*i + [(255,255,255)] + [(0,0,0)]*(14-i))`.)
3. Write its position as a perimeter coordinate `s`: `0` = top-left corner, `0–1` top (left→right), `1–2` right side (down), `2–3` bottom (right→left), `3–4` left side (up).
   The second number is how wide the segment spreads (same unit).
4. Put the pairs in `segments = [[s, width], …]` of `config.toml` (one per segment, in strip order).

`config.example.toml` shows a real, measured H619A that is too long for the TV and folds back along the top.

## Calibrate the LED colours (gamma & white balance)

LED strips are PWM-linear while TVs are sRGB, and each strip has its own colour cast. An interactive CLI lets you tune `gamma`, `balance`, `jaune_v` and `bleu_v` live on 9 test patterns (white, 50 % grey, pure yellow, warm gold, pastel yellow, orange, pure blue, violet, full sweep) using the exact production `rendu()` pipeline:

```bash
python -m ambilight.calibrer -c config.toml --arreter-ctl   # pauses ambilight.ctl, restores it on exit, prints TOML
python -m ambilight.calibrer -c config.toml --simuler       # dry run without hardware
```

Press `1`–`9` to switch patterns, `r/R g/G b/B` for white balance, `[ / ]` (or `x/X y/Y z/Z`) for gamma, `j/J` for yellow green-brake, `v/V` for blue correction, `Tab` to switch strips, and `p` or `q` to print the ready-to-paste `config.toml` block.

## Run it as a service (with an HTTP switch)

`ambilight.ctl` starts/stops the program and restarts it if it dies:

```bash
python -m ambilight.ctl -c config.toml          # POST /on, POST /off, GET /status on port 8099
```

`deploy/ambilight-ctl.service` is a hardened systemd unit. **It assumes** a system user `ambilight`, the code in `/opt/ambilight`, a virtualenv in
`/opt/ambilight/venv` and `config.toml` + `adbkey` in `/opt/ambilight` (readable by that user only): adapt the paths to your setup.
`homeassistant/exemple.yaml` wires an on/off switch in Home Assistant.

Security model: **access is filtered by source IP** (`ctl_autorises`), there is no authentication. Keep it on a trusted network. Anyone who can spoof an allowed address
on your LAN can switch the strip on or off, nothing more.

## Tuning

Every setting is in `ambilight/config.py` (documented), overridable in `config.toml` or, for scalar settings, with `AMBI_<NAME>` environment variables
(e.g. `AMBI_HZ=20`; `segments`, `balance` and `ctl_autorises` can only be set in the file).
The most useful knobs:

| setting | effect |
|---|---|
| `mouvement`, `agitation_min`, `agitation_max` | strength of the motion emphasis, and the image agitation between which it fades in (talk show → fight scene) |
| `lent_calme`, `lent` | smoothing of small changes on a calm / an agitated image |
| `lissage_oklab`, `tau_flash`, `coupure` | perceptual Oklab smoothing, fast local flash response, and scene-cut threshold (instant jump on main strip) |
| `dominante` | 1 = a zone whose hues cancel takes its dominant colour instead of the average; 0 = off |
| `vibr_ok`, `peau` | Oklab LCh vibrance (hue-preserving) and skin-tone protection |
| `gamma`, `balance`, `jaune_v`, `bleu_v` | per-channel LED gamma, white balance, pure-yellow green brake, and pure-blue tint correction (tunable via `ambilight.calibrer`) |
| `toe`, `presence` | pre-gain black-gate crush of dark video noise and linear floor for saturated dark colours |
| `seg_k`, `seg_p`, `nettete` | how tightly a segment samples its zone and anti-halo spatial edge sharpening |
| `genou`, `lum_max` | soft brightness ceiling for bright scenes |
| `aud_attente` | h264 silence delay (s) before injecting an AUD flush delimiter (increase if logs warn of cut frames) |

The program logs, every 5 s, the image rate, the frame rate, the auto-gain, and the image agitation with the resulting motion effect.

## A second strip

The repo does not drive a second strip. `flux_ip` / `flux_port` make the program send a UDP frame (15 × RGB = 45 bytes: one ambiance colour for the whole picture)
to any listener you write; the author's own consumer is a patched copy of a Bluetooth Home Assistant integration and is not published here.

## Roadmap

See [ROADMAP.md](ROADMAP.md): reading the picture from Jellyfin / Plex / Kodi ahead of playback (no load on the Fire TV, latency compensated), HDMI capture, more outputs.

## Tests

```bash
pip install '.[test]' && pytest
```

## Credits

* The *razer* frame format comes from the [OpenRGB GoveeController](https://gitlab.com/CalcProgrammer1/OpenRGB).
* Ideas borrowed from [HyperHDR](https://github.com/awawa-dev/HyperHDR): linear-light calibration, hybrid smoothing, flicker filtering.

Not affiliated with Govee or Amazon. Use at your own risk.
