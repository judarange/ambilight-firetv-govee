# ambilight-firetv-govee

Live ambilight for an Amazon **Fire TV**: a **Govee** LED strip around the TV follows the edges of the screen, **with no HDMI capture box and no camera**.

```
Fire TV --ADB: screenrecord raw-frames (RGB, no encoder)--> 96x54 --> zone colours --> render --> Govee strip (LAN "razer" mode)
```

* About **26 frames/s** from the Fire TV, one LED frame sent per image received. Latency was measured end to end at roughly **90 ms**
  with a browser clock page shown on the TV (so it includes the browser's own rendering, and the TV's display delay is not counted).
* The colour pipeline is tuned by eye on real content (talk shows, films, anime), not just maths: linear-light averaging, edge-weighted zones,
  vibrance that keeps whites white and skin tones natural, **dominant colour** for zones where hues cancel (a purple character in front of a
  pale background stays purple instead of averaging to beige), a toe curve so a video's "black" `(3,12,8)` does not light a green LED, black-bar
  (letterbox) detection, automatic gain for dark scenes, a soft ceiling for snow/sunlight, flicker hysteresis, and **motion emphasis that adapts**:
  calm on a talk show, full on a fight scene or lightning.
* Pure-numpy core (`ambilight/traitement.py`) with unit tests that need no hardware.

> Language: the code, comments, configuration keys, `--help` and log lines are in **French** (the author's language). The README is in English.
> A glossary of the main settings is in `ambilight/config.py` (each field has a comment).

## What you need

| | |
|---|---|
| Fire TV | Fire Stick 4K or similar, **ADB debugging over network** enabled |
| Strip | a Govee strip with **LAN control** and the **razer / DreamView** mode (tested: **H619A**) |
| A small always-on machine | Linux, Python ≥ 3.11, `ffmpeg` (used for the static-screen screenshot fallback, and for `capture = "h264"`) |
| Optional | Home Assistant, to switch it on/off |

Limits you should know about:
* Protected apps (Netflix, Prime, Disney+) return a **black screen** to `screenrecord`: the strip stays dark there.
* A fixed screen makes `screenrecord` go silent: a periodic screenshot takes over, and the first image after a pause can be a few seconds late.
* The raw mode (`capture = "raw"`, default) was validated on a Fire Stick 4K. If it does not work on your device, set `capture = "h264"`
  (older path: h264 stream decoded by ffmpeg; it reconnects every 180 s and shows an occasional decoding glitch).

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
| `dominante` | 1 = a zone whose hues cancel takes its dominant colour instead of the average; 0 = off |
| `toe` | crush of very dark levels (a video "black" is rarely 0); 0 = off |
| `seg_k`, `seg_p` | how tightly a segment samples its zone (less spill into neighbours) |
| `genou`, `lum_max` | soft brightness ceiling for bright scenes |
| `balance` | white balance of the strip |

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
