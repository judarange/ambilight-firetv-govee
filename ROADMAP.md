# Roadmap

Ideas, not promises. Items are ordered by how much they change the project. Contributions and experiments welcome.

## Done

* Fire TV capture over ADB: raw RGB frames (no encoder, no time limit) with an h264 + ffmpeg fallback.
* Edge-weighted zones, measured per-segment geometry, linear-light averaging, vibrance, skin-tone protection, dominant colour for mixed zones,
  dark-level toe curve, adaptive motion emphasis, auto-gain, flicker hysteresis, black-bar detection.
* Frame-paced output (one LED frame per image received), HTTP on/off service with watchdog, hardened systemd unit, hardware-free test suite.

## Next: take the capture off the Fire TV, and get ahead of the latency

Today the Fire TV does the work: `screenrecord` costs it roughly 10 % of its total CPU (about 40 % of one core with the ADB bridge), on a device that
often has only a few hundred MB of free RAM, and the pipeline has a floor of roughly 80 ms before the first byte reaches the program.

**Idea: read the picture from the media server instead of the screen.** When the content comes from **Jellyfin**, **Plex**, **Emby** or **Kodi**,
the server already has the video. A small worker could:

1. ask the server what is playing and **where** (Jellyfin `Sessions` API `PositionTicks`, Plex `/status/sessions` `viewOffset`, Kodi JSON-RPC `Player.GetProperties`),
2. decode that same file (or a low-resolution proxy) with ffmpeg **ahead of the playback position**,
3. compute the zone colours for the frame the TV is *about to* show and send them to the strip at the right moment.

What it would bring:

* **Zero load on the Fire TV** (no `screenrecord`, no ADB stream): fewer micro-lags on a 1.7 GB device.
* **Negative latency.** Because the colours are known in advance, the program can *delay or advance* them freely: a single `lead_ms` setting compensates the TV's own
  display delay, the strip's radio/firmware delay, and the Wi-Fi jitter, so the light lands **exactly** with the picture instead of 90 ms after it.
  The same offset can be set per strip (a Bluetooth ambiance strip could be advanced to compensate its slower link).
* Works with the TV on **any input** that the server knows about, and keeps working when the screen is static.
* Pre-computation: a whole film could be analysed once, cached, and replayed with no decoding at play time.

Hard parts to solve (honestly):

* **Synchronisation.** Playback position is reported about once per second, so the worker must interpolate with a local clock and re-sync on pause, seek, rate change and buffering.
  A one-off calibration (a clock/flash test pattern, filmed in slow motion) would measure the true offset.
* **What the viewer actually sees:** subtitles burned in by the player, HDR tone-mapping, aspect-ratio changes and in-player zoom are not in the source file.
* **Live TV / IPTV** can only be read ahead as far as the buffer allows.
* **DRM streaming services** (Netflix, Prime, Disney+) are out of reach by design; they stay on the screen-capture path (which already returns black there).
* Server-side decoding costs CPU: a 96×54 proxy decode is cheap, but it should be a single shared worker, not one per player.

Proposed shape: a **source plug-in interface** (`screen` = today's ADB capture, `jellyfin`, `plex`, `kodi`, `v4l2`) that hands the existing, unchanged colour pipeline
a 96×54 RGB image with a timestamp. The pipeline and the outputs would not need to change.

## Other sources

* **HDMI capture card** (V4L2 / USB grabber) as a source: any device, any app, DRM-free if the grabber is not HDCP-limited.
* Other Android TV / Google TV devices via the same ADB path (device-by-device validation of `raw-frames`).
* **Kodi** and a **Home Assistant media-player** based position source for the read-ahead mode above.

## Outputs

* **WLED** (DDP / E1.31 / UDP realtime), Philips Hue Entertainment, other Govee models, generic UDP LED controllers.
* Several strips, each with its own geometry, white balance and delay.
* An optional Home Assistant integration / HACS component replacing the hand-written `rest_command` example (on/off, live settings, status sensor).

## Measuring and calibrating

* **Latency bench:** a reproducible image → LED measurement (slow-motion video of a flashing test pattern and the strip), published with the numbers for each source.
* **`--simulate`:** replay a video or a still, write the per-segment colours as an image/GIF, so anyone can tune the rendering without the hardware.
* **Automatic segment calibration** with a phone camera: light one segment at a time on a black screen, detect where it lies, write `segments` for you.
* Per-device LED gamma / white-balance measurement.

## Quality of the project

* Packaging (PyPI, Docker image), continuous integration, a typed configuration schema with friendlier errors.
* Tests for the control service and the main loop; a recorded-frames regression test for the colour pipeline.
* A demo GIF and an architecture diagram in the README; English log lines and settings glossary.
