# KodaxWeb

A small local web app that replaces the discontinued Kodak Smart Home app for
Kodak (Hubble / CIN platform) cameras. It talks to the cameras directly on
your network; nothing goes to a cloud, and no Kodak account is needed.

Self-contained: this folder is everything the app needs, ready to live in its
own repository.

## Run

Needs Python 3.11+.

```bash
py -m pip install -r requirements.txt
py server.py
```

Then open <http://localhost:8099> and add your camera by its IP address.

| Option | Default | |
|--------|---------|--|
| `--host` | `127.0.0.1` | Use `0.0.0.0` to reach it from other devices — trusted networks only, there's **no login** and it can change camera Wi-Fi settings |
| `--port` | `8099` | |
| `--debug` | off | Verbose logging |

**Live view needs a secure page.** Browsers only allow the WebCodecs video
decoder on `https://` or `http://localhost`. A plain `http://<server-ip>`
address shows no video, so put the app behind HTTPS (for example a Caddy
reverse proxy) when you run it on a home server.

## Features

| Area | What you can do |
|------|-----------------|
| Live view | ~15 fps 1280×720, smoothed against the camera's Wi-Fi hiccups; on-picture toolbar with night vision (Auto/On/Off), pan & tilt, sound, snapshot and full screen (or double-click the picture) |
| Sound | The camera's microphone (16 kHz). Needs PyAV (`av`) to decode the camera's G.722 audio; without it the button is disabled |
| Pan & tilt | Hold the arrows on the picture (or keyboard arrows). Protected against grinding at the ends: auto-stop 0.9 s after the last command, 3 s cap per press, optional software end-stops. Models 006/008/018 |
| Status | Battery, charging, Wi-Fi network and signal, IP/DNS, firmware, SD card |
| Picture | Resolution, quality (bitrate), 50/60 Hz anti-flicker, image profile, ceiling-mount flip |
| Detection & recording | Motion and sound on/off and sensitivity; paint motion zones over the live picture; save motion clips/snapshots to SD, keeping the latest N clips |
| Lullaby | Tracks 1–5, stop, volume |
| Wi-Fi | Move a working camera to another network |
| Misc | Indicator light, time zone, pan/tilt recalibration |
| Device | Device info, setup log, restart |
| New camera | Pair a camera in hotspot mode with your Wi-Fi (untested end-to-end) |

### Kept on the camera automatically

Each time the app connects it:

- restores your chosen video quality (the camera falls back to 550 kbps after
  power cuts and when its flow control backs off), and
- turns off the camera's background Wi-Fi scanning for a Kodak range extender
  and baby-monitor parent unit, which halves the stream's stalls.

550 kbps is recommended: above it the camera skips more frames, which shows as
smearing until the next full frame.

## Setting up a new camera

Setup needs the RSA *public* key the camera expects, which isn't included in
this repository. It ships inside the official Kodak Smart Home Android app:
an `.apk` is a zip file, and the key is `assets/public.pem` inside it. Copy
that file next to `server.py` as `public.pem` (it's git-ignored). Everything
else in the app works without it.

1. Put the camera in pairing mode (hold the button until the LED flashes). It
   creates a Wi-Fi network called `KodakCam-…`.
2. Connect this PC to that network and open **Set up new camera**.
3. Click **Find camera**, enter your 2.4 GHz Wi-Fi details and send them.
4. Reconnect the PC to your home network, find the camera's IP in your router
   and add it.

Only the local pairing steps are needed; the old cloud registration steps are
skipped.

## Files

| File | |
|------|--|
| `server.py` | aiohttp server: REST API, live-view WebSocket, audio decoding, motor safety |
| `vlvl.py` | Camera stream protocol (VLVL): session, decryption, ACKs, stream modes, command channel |
| `kodak.py` | Camera HTTP API: status and settings |
| `setup_flow.py` | First-time Wi-Fi setup of a camera in pairing mode |
| `ptz_limits.py` | Software end-stops for pan/tilt |
| `public.pem` | RSA public key for camera setup — you supply it, git-ignored (see above) |
| `static/` | The web page |
| `attic/` | Parked features (pan/tilt presets) |
| `cameras.json` | Your cameras and saved settings — created at runtime, git-ignored (holds real IPs/MACs) |

`vlvl.py` started as a copy of the one in the
[KodaxHA](https://github.com/ElliottLW/KodaxHA) Home Assistant integration;
protocol details are documented in that repo's `CAMERA_API.md`.

## Notes

- A camera serves one live session at a time; viewing it elsewhere at the same
  time interrupts one of them.
- The camera skips roughly 1 frame in 25 even on a good link, so expect an
  occasional tiny hitch. Nothing is lost in the app.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE.md): free to use, modify and share for
any non-commercial purpose; selling it or using it commercially isn't permitted.
It comes as is, with no warranty. The licence covers only this project's own
code and docs; see [NOTICE](NOTICE).

## Disclaimer

KodaxWeb is an independent project. It is not affiliated with, endorsed by or
supported by Kodak or the camera's manufacturer. "Kodak" is a trademark of its
owner and is used here only to say which cameras this works with. It talks only
to cameras you own, on your own network.
