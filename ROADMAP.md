# Roadmap

Ideas, not promises.

* **Read the picture from the media server** (Jellyfin, Plex, Kodi) instead of the Fire TV screen: the worker asks the server what is playing and where, decodes that
  file slightly **ahead** of the playback position and sends the colours for the frame the TV is about to show. No load on the Fire TV, and a single `lead_ms` setting can
  compensate the TV's display delay and the strip's own delay, so the light lands with the picture instead of after it. The hard parts are synchronisation (position is reported
  about once a second), subtitles and HDR that are not in the source file, and live TV (only as far as the buffer allows). DRM streaming services stay on screen capture.
* **HDMI capture card** as another source.
* **More outputs**: WLED, Philips Hue, other Govee models, several strips with their own geometry and delay.
* **Tools**: a reproducible latency bench, a `--simulate` mode that renders the segment colours of a video without hardware, automatic segment calibration with a phone camera.
