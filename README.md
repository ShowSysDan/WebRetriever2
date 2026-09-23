# NDI Streamer

[![Version](https://img.shields.io/badge/version-1.10.0-blue.svg)]()
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)]()
[![License](https://img.shields.io/badge/license-MIT-gray.svg)]()

A self-hosted Flask application that captures webpages, images, or text via headless Chromium — plus webcams, video files, and signage playlists decoded and composited natively, with no browser involved — and outputs them as NDI video streams on your network.

---

## Features

- **Multiple NDI output instances** — each with its own stream name, resolution, and capture rate
- **Six source types** — webpage URL, uploaded image, custom styled text, a connected webcam, an uploaded video file, or a scheduled signage playlist
- **Digital signage playlists** — turn an output into a signage player: stills and videos play in order with real crossfade transitions (for videos the fade starts before the file ends), all rendered in the worker and streamed as one rock-steady NDI source
- **Content scheduling** — per item or per group: go-live date/time, expiry date/time, and a daily time-of-day window (e.g. breakfast menu 07:00–11:00, overnight loop 22:00–06:00); out-of-window content is skipped automatically and the playlist updates live, no restarts
- **Content groups** — bundle items (e.g. 10 uploaded slides) into one unit that is ordered, scheduled, transitioned, and deleted together
- **PowerPoint / PDF decks** — upload a .ppt/.pptx/.odp/.pdf straight into a playlist; every slide is rasterized to an image and arrives as a ready-made group (requires LibreOffice + poppler-utils)
- **Impression counters** — every item counts how many times it went on air
- **Live signage control** — see what's playing and what's next, and skip ahead with one click or a bare URL (`/api/instances/<id-or-name>/signage/skip`)
- **Upload progress** — per-file progress readout with % uploaded, server-processing state, and clear error messages
- **4K-safe video pipeline** — workers decode with all cores (and the box's hardware decoder when present), and every uploaded video is checked against the ideal playback format (H.264/yuv420p MP4 within the output size): anything else is transcoded once, in the background, into a light playback copy; already-perfect files are marked playback-ready untouched. Originals always kept (`VIDEO_OPTIMIZE=oversized` limits conversion to 4K-class files, `off` disables)
- **Overview stream** — one switch on the Overview tab turns on a built-in multiview: every output in one real-time 1080p30 NDI source (`MACHINE (Overview)`), grouped under a header per source type with each output's name under its tile. Tiles appear, disappear and show STOPPED live as outputs are added, disabled or stopped — no restart. See [Overview Stream](#overview-stream)
- **Live preview popups** — pop any output into its own confidence-monitor window (click an output card's live screen on the Overview, or **⧉ Popup Preview** on the Signage tab): an MJPEG stream that automatically switches the worker to larger, faster preview frames (854px @ ~4fps) while the window is open, with live state, and now/next for signage
- **Video playback as NDI** — upload a video (mp4, mov, mkv, webm…) and play it out as an NDI source: play once or loop, hold the last or first frame while stopped, optional autoplay on start
- **Show-control friendly playback API** — trigger video play/stop/load with a plain GET or POST URL on the same port as the web UI (works from Companion, Crestron, QLab, or a browser bookmark), addressing instances by id or by name
- **Instant video switching & cueing** — swap the video playing on a running output with a single URL (hot-swap inside the worker, the NDI stream never drops), or pre-load ("cue") the next video on its first frame so the play cue fires with zero latency
- **Permanent media IDs** — every uploaded file gets a `uid` that is never reused or shifted, even after deletes, so controller cues keyed on it stay correct forever; media addressable by id, uid, or filename
- **Webcam detection** — auto-detects all connected V4L2 cameras (Linux) and streams them as NDI, bypassing the browser entirely for full camera-native frame rates (30–60fps)
- **Stable camera identity** — webcams are bound by udev stable ID (USB serial via `/dev/v4l/by-id`, physical port via `/dev/v4l/by-path` as fallback), so each camera keeps its correct NDI output across unplugs, replugs, and reboots even when `/dev/videoN` numbers shuffle
- **Custom NDI naming** — per-instance stream name; sources appear as `MACHINE (Instance Name)`, where `MACHINE` is the computer's OS hostname (e.g. `PRODUCTION (Lower Third)` — see [NDI source naming](#ndi-source-naming))
- **Decoupled FPS** — capture at any rate (e.g. 15fps for a weather radar), NDI always outputs at the global rate (60fps) by duplicating frames
- **Auto-refresh** — per-instance configurable interval to reload content (e.g. refresh a weather page every 30 minutes)
- **Media library** — upload, manage, and assign images and videos to instances and signage playlists via a built-in file manager, with the media drive's free space shown alongside
- **Live dashboard** — the Overview tab shows every output as a live card (with program/preview tally frames), receiver and bandwidth totals, a CPU graph (core average + per-core load), and what's on air in signage
- **Crash recovery** — every output runs in its own process, so one crashing or hanging never takes down the others; a self-healing watchdog restarts only the failed one
- **Syslog integration** — structured event logging for all instance lifecycle events, settings changes, and media operations
- **Systemd service** — runs on boot, restarts on failure, production-ready
- **Database portable** — SQLite by default, one-line swap to PostgreSQL

---

## Table of Contents

1. [Requirements](#requirements)
2. [Quick Start (Linux)](#quick-start-linux)
3. [Quick Start (Windows)](#quick-start-windows)
4. [Installation (Linux)](#installation-linux)
5. [Installation (Windows)](#installation-windows)
6. [Configuration](#configuration)
7. [Running as a Service (Linux)](#running-as-a-service-linux)
8. [Running as a Service (Windows)](#running-as-a-service-windows)
9. [NDI SDK Installation](#ndi-sdk-installation)
10. [Switching to PostgreSQL](#switching-to-postgresql)
11. [Syslog Configuration](#syslog-configuration)
12. [Architecture](#architecture)
13. [24/7 Production Reliability](#247-production-reliability)
14. [Digital Signage](#digital-signage)
    - [Overview Stream](#overview-stream)
15. [API Reference](#api-reference)
16. [Performance Notes](#performance-notes)
17. [Versioning](#versioning)
18. [Development](#development)
19. [Troubleshooting](#troubleshooting)

---

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| **OS** | Ubuntu 22.04+ / Windows 10+ | Linux recommended for production; Windows fully supported |
| **Python** | 3.10+ | 3.11 or 3.12 recommended. Windows: [python.org](https://www.python.org/downloads/) (check "Add to PATH") |
| **NDI SDK** | 5.x or 6.x | Free runtime from [ndi.video](https://ndi.video/tools/ndi-sdk/) |
| **Chromium** | (auto-installed) | Managed by Playwright |
| **Git** | Any | Windows: [git-scm.com](https://git-scm.com/download/win) |
| **LibreOffice + poppler-utils** | Any recent | *Optional* — only needed to upload PowerPoint/PDF decks into signage playlists (`sudo apt install libreoffice-impress poppler-utils`) |

### System packages (Debian 11/12, Ubuntu 22.04+)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git rsync curl ca-certificates
```

On Debian 11 (bullseye) the default Python is 3.9, which is too old. Use the
`python3.11` backport (`sudo apt install -y python3.11 python3.11-venv`) or
upgrade to Debian 12 (bookworm), which ships Python 3.11 out of the box.

Playwright will download Chromium inside the venv, but Chromium needs system
shared libraries (libnss3, libatk-bridge, libasound2, libxkbcommon, fonts,
etc.). These are installed in the next section via `playwright install-deps`.

### Windows prerequisites

1. **Python 3.10+** — download from [python.org](https://www.python.org/downloads/). During install, check **"Add Python to PATH"** and **"Install pip"**.
2. **Git** — download from [git-scm.com](https://git-scm.com/download/win).
3. **NDI SDK** — download and run the Windows installer from [ndi.video/tools](https://ndi.video/tools/ndi-sdk/). This installs the runtime DLLs and adds them to the system PATH automatically.

Verify in PowerShell or Command Prompt:

```powershell
python --version   # should show 3.10+
git --version
pip --version
```

---

## Quick Start (Linux)

```bash
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
chmod +x setup.sh
./setup.sh
source venv/bin/activate
python run.py
```

Open **http://localhost:5000** in your browser.

---

## Quick Start (Windows)

```powershell
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
python -m venv venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process   # if needed
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
python run.py
```

Open **http://localhost:5000** in your browser.

---

## Installation (Linux)

There are two paths:

- **A. Scripted install** — good for a developer workstation or when you just
  want to try it out. Clones anywhere, runs `setup.sh`, optionally registers
  the systemd service.
- **B. Manual production install** — recommended for a dedicated Debian server
  that will run 24/7. Each step is explicit so you know exactly what landed
  where, the venv is created as the service user, and the service runs under
  its own account with a hardened unit file.

### A. Scripted install

```bash
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
chmod +x setup.sh
./setup.sh
```

The setup script will:
- Detect and validate your Python version (3.10+ required)
- Create a virtual environment in `./venv`
- Install all Python dependencies
- Install Playwright and headless Chromium (with `sudo playwright install-deps`)
- Check for the NDI SDK runtime (warns if missing)
- Create `.env` from `.env.example`
- Initialize the SQLite database
- Optionally install as a systemd service (requires `sudo`)

Then edit `.env` (at minimum change `SECRET_KEY`) and start with
`source venv/bin/activate && python run.py`.

### B. Manual production install (Debian 12)

The service will run as a dedicated `ndi-streamer` user with its home at
`/opt/ndi-streamer`. Run the following as a user with `sudo` access.

#### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git rsync curl ca-certificates
```

#### 2. Clone into /opt and create the service user

```bash
sudo git clone https://github.com/showsysdan/webretriever2.git /opt/ndi-streamer
sudo useradd --system --home /opt/ndi-streamer --shell /usr/sbin/nologin ndi-streamer
sudo chown -R ndi-streamer:ndi-streamer /opt/ndi-streamer
```

#### 3. Create the venv and install Python dependencies

Do this **as the service user** so the venv is owned correctly from the start:

```bash
sudo -u ndi-streamer python3 -m venv /opt/ndi-streamer/venv
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/pip install --upgrade pip wheel
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/pip install -r /opt/ndi-streamer/requirements.txt
```

#### 4. Install Playwright + Chromium system libraries

Playwright downloads Chromium into the user's cache (under
`/opt/ndi-streamer/.cache/ms-playwright/` for the service user). The system
libraries Chromium links against (`libnss3`, `libatk-bridge2.0-0`, `libasound2`,
`libxkbcommon0`, fonts, etc.) must be installed as root via `apt`.

```bash
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/playwright install chromium
sudo /opt/ndi-streamer/venv/bin/playwright install-deps chromium
```

Verify:

```bash
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/python -c \
  "from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); b=pw.chromium.launch(); print('Chromium OK', b.version); b.close(); pw.stop()"
```

#### 5. Install the NDI SDK

See the [NDI SDK Installation](#ndi-sdk-installation) section for the full
recipe (download, accept EULA, install to `/usr/local/ndi/`, register with
`ld.so.conf.d`, install `ndi-python` into the venv).

Short version — after the SDK is installed system-wide:

```bash
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/pip install ndi-python
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/python -c \
  "import NDIlib as n; n.initialize(); print('NDI ready'); n.destroy()"
```

If you skip this step the app still runs; it just falls back to "dummy mode"
(Chromium captures frames but no NDI streams go out).

#### 6. Configure `.env`

```bash
sudo -u ndi-streamer cp /opt/ndi-streamer/.env.example /opt/ndi-streamer/.env
sudo -u ndi-streamer nano /opt/ndi-streamer/.env
```

Required for production:

- Set `SECRET_KEY` to a long random string
  (`python3 -c 'import secrets; print(secrets.token_hex(32))'`).
- Keep `FLASK_ENV=production` (the default). `development` turns on the
  Werkzeug debugger and reloader, and is ignored unless `FLASK_HOST` is a
  loopback address — the debugger console is remote code execution for
  anyone who can reach it.
- If you are fronting the app with nginx on the same box, set
  `FLASK_HOST=127.0.0.1` so Flask only listens on loopback. If clients will
  hit `:5000` directly on the LAN, leave it at the default `0.0.0.0` and rely
  on the firewall (see [Firewall](#firewall)).

#### 7. Initialize the database

```bash
cd /opt/ndi-streamer
sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/python -c "from app import create_app; create_app()"
```

#### 8. Install the systemd service

```bash
sudo cp /opt/ndi-streamer/ndi-streamer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ndi-streamer
sudo systemctl status ndi-streamer
sudo journalctl -u ndi-streamer -f
```

The unit file already runs under the `ndi-streamer` user, restarts on crash,
and applies the systemd hardening described in
[Running as a Service (Linux)](#running-as-a-service-linux).

---

## Installation (Windows)

### 1. Clone the repository

```powershell
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
```

### 2. Create virtual environment and install dependencies

```powershell
python -m venv venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process   # if activation fails
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

#### Optional: NDI output and PostgreSQL

```powershell
pip install ndi-python        # Requires NDI SDK installed; app runs in dummy mode without it
pip install psycopg2-binary   # Only if using PostgreSQL instead of SQLite
```

### 3. Install Playwright browser

```powershell
playwright install chromium
```

### 4. Create environment file

```powershell
copy .env.example .env
```

Edit `.env` with Notepad or your preferred editor:

```powershell
notepad .env
```

At minimum, change `SECRET_KEY`. On Windows, the syslog settings can be left disabled (Windows uses the Event Log instead — see [Running as a Service (Windows)](#running-as-a-service-windows)).

### 5. Initialize the database

```powershell
python -c "from app import create_app; create_app()"
```

### 6. Start the application

```powershell
venv\Scripts\activate
python run.py
```

Open **http://localhost:5000** in your browser.

### Windows-specific notes

- **NDI SDK**: The Windows NDI installer adds DLLs to `C:\Program Files\NDI\NDI 6 Runtime\` and registers them in the system PATH automatically. No `LD_LIBRARY_PATH` equivalent is needed.
- **Syslog**: Windows doesn't have `/dev/log`. Leave `SYSLOG_ENABLED=false` (the default) and use Windows Event Log via the service wrapper, or point `SYSLOG_ADDRESS` to a remote syslog server (`192.168.1.100:514`).
- **`multiprocessing`**: Python on Windows uses `spawn` instead of `fork` for new processes. The app handles this correctly — all worker arguments are picklable. Startup time per instance is slightly slower (~1-2s extra) but runtime performance is identical.
- **File paths**: SQLite paths in `.env` work as-is (`sqlite:///ndi_streamer.db` creates the file in the project directory). Upload paths use `os.path.join` internally so forward/back slashes both work.

---

## Configuration

All configuration is in `.env`. Copy from the template:

```bash
cp .env.example .env
```

### Database

```env
# SQLite (default — zero setup)
DATABASE_URL=sqlite:///ndi_streamer.db

# PostgreSQL (production)
# DATABASE_URL=postgresql://user:pass@localhost:5432/ndi_streamer
```

### NDI Defaults

```env
# Stored as a global setting, but NOT applied to NDI output (see below).
NDI_HOSTNAME=NDI-STREAMER

# Global NDI output frame rate (all senders output at this rate)
NDI_OUTPUT_FPS=60
```

#### NDI source naming

NDI sources always appear on the network as `MACHINE (Source Name)`. The app
controls only the part in parentheses — the instance name. The NDI SDK fills in
`MACHINE` itself from the computer's operating-system hostname, and offers no
way to override it, so `NDI_HOSTNAME` currently has no effect on what
receivers see.

To change the machine part, rename the computer and restart the service:

```bash
# Linux
sudo hostnamectl set-hostname PRODUCTION
sudo systemctl restart ndi-streamer
```

On Windows, rename the PC (Settings → System → About → Rename this PC) and
reboot. Avoid giving two machines on the same network the same hostname —
receivers can't tell their sources apart, and mDNS/DHCP/DNS will conflict.

### Flask

```env
SECRET_KEY=change-me-to-a-random-string
FLASK_ENV=production          # "development" only takes effect on a loopback FLASK_HOST
FLASK_HOST=0.0.0.0            # bind address. Set to 127.0.0.1 when behind nginx.
FLASK_PORT=5000
ALLOWED_HOSTS=                # optional, e.g. ndi-server,ndi-server.local,10.0.0.5
```

> **Security:** the app has no built-in authentication. Anyone who can reach
> `FLASK_HOST:FLASK_PORT` can create, start, stop, and delete instances.
> Restrict access with a firewall (see [Firewall](#firewall)), put it behind
> nginx with basic auth (see [Reverse proxy with TLS](#reverse-proxy-with-tls)),
> or set `FLASK_HOST=127.0.0.1` so it's only reachable from the local machine.

> **Built-in browser protections** (no configuration needed): state-changing
> requests (`POST`/`PUT`/`DELETE`) that a browser labels as coming from
> another site (`Sec-Fetch-Site: cross-site`/`same-site`, or a foreign
> `Origin`) are refused with 403, so a web page elsewhere can't use a LAN
> browser to stop outputs or upload files. Show controllers (Companion,
> Crestron, QLab, curl) don't send those headers and are unaffected, and the
> `GET` cue URLs stay open by design. Uploaded media is served with
> `Content-Security-Policy: sandbox` and a server-derived content type, and
> webpage sources must be `http://` or `https://` (no `file://`).
>
> **`ALLOWED_HOSTS`** (optional) blocks DNS-rebinding attacks: when set, the
> app answers only requests whose `Host` is in the list (plus `localhost` /
> `127.0.0.1`). List every name and IP people and controllers use to reach
> the box — anything else gets 403.

> **SECRET_KEY:** defaults to `dev-secret-key` if unset. The app logs a warning
> on startup when it sees the default. Always generate a fresh one for
> production: `python3 -c 'import secrets; print(secrets.token_hex(32))'`.

### Uploads

```env
UPLOAD_FOLDER=app/uploads
MAX_UPLOAD_SIZE_MB=500
MAX_IMAGE_MEGAPIXELS=100      # larger images are rejected (8K UHD is ~33 MP)
MAX_DECK_PAGES=300            # PDF/PowerPoint decks are cut at this many pages
```

The media library accepts images (`png jpg jpeg gif bmp webp svg tiff`) and
videos (`mp4 mov m4v mkv webm avi mpg mpeg`). The default size cap is 500 MB
to leave room for video files; tune `MAX_UPLOAD_SIZE_MB` to taste. The pixel
and page caps stop a small crafted file from making the server or a signage
worker decode gigabytes.

### Video optimization

```env
VIDEO_OPTIMIZE=all            # all | oversized | off
VIDEO_TARGET_WIDTH=1920
VIDEO_TARGET_HEIGHT=1080
VIDEO_CRF=20                  # x264 quality of the one-time encode
VIDEO_PRESET=veryfast         # x264 speed of the one-time encode
```

Needs ffmpeg. `all` converts every video that isn't already H.264/yuv420p
within the target size into a light playback copy (originals are kept);
`oversized` converts only videos larger than the target; `off` never
transcodes.

### Signage and runtime files

```env
PRESENTATION_RENDER_DPI=150   # PowerPoint/PDF rasterizing (≈2000×1125 per 16:9 slide)
SIGNAGE_STILL_CACHE_MB=256    # RAM for decoded stills, per signage worker
BROWSER_RECYCLE_HOURS=4       # full Chromium restart interval per browser instance
# PREVIEW_FOLDER=/dev/shm/webretriever2_previews
# SIGNAGE_RUNTIME_FOLDER=/dev/shm/webretriever2_runtime
```

Preview JPEGs and the signage now-playing status are rewritten constantly,
so they default to tmpfs (`/dev/shm`) where it exists and fall back to the
app folder elsewhere (e.g. Windows).

### Overview stream

```env
OVERVIEW_WIDTH=1920           # canvas size of the built-in multiview
OVERVIEW_HEIGHT=1080
OVERVIEW_FPS=30               # its own frame rate (not the global output FPS)
OVERVIEW_BANDWIDTH=highest    # highest = full-quality real-time tiles; lowest = NDI preview streams (much cheaper)
```

See [Overview Stream](#overview-stream) for how it works and what it costs.

### Syslog

```env
LOG_LEVEL=INFO
SYSLOG_ENABLED=false
SYSLOG_ADDRESS=/dev/log
SYSLOG_FACILITY=local0
SYSLOG_TAG=ndi-streamer
```

---

## Running as a Service (Linux)

The setup script can install NDI Streamer as a systemd service:

```bash
sudo ./setup.sh
# Answer "y" when prompted to install as a service
```

This installs the app to `/opt/ndi-streamer` and creates a dedicated system user.

### Manual service management

```bash
sudo systemctl start ndi-streamer
sudo systemctl stop ndi-streamer
sudo systemctl restart ndi-streamer
sudo systemctl status ndi-streamer

# View logs
sudo journalctl -u ndi-streamer -f

# Enable/disable auto-start on boot
sudo systemctl enable ndi-streamer
sudo systemctl disable ndi-streamer
```

### Service features

- **Auto-start** on boot
- **Auto-restart** on crash (5 attempts within 60 seconds)
- **Dedicated system user** (`ndi-streamer`) with `/usr/sbin/nologin`
- **Journal logging** — all output captured by systemd journal

### systemd hardening

The shipped unit file applies the following restrictions. Confirm they are
active on your box with `systemctl show ndi-streamer | grep -E "Protect|Restrict|UMask|LimitNOFILE|MemoryMax"`:

| Directive | Effect |
|-----------|--------|
| `NoNewPrivileges=true` | Process can't gain privileges via setuid binaries |
| `ProtectSystem=strict` | `/usr`, `/boot`, `/etc` are read-only to the service |
| `ReadWritePaths=/opt/ndi-streamer` | Only the app dir is writable |
| `ProtectHome=true` | `/home`, `/root`, `/run/user` are inaccessible |
| `PrivateTmp=true` | Service sees its own private `/tmp` |
| `ProtectKernelTunables=true` | `/proc/sys`, `/sys` are read-only |
| `ProtectKernelModules=true` | Can't load kernel modules |
| `ProtectKernelLogs=true` | `dmesg` is hidden |
| `ProtectControlGroups=true` | Cgroup hierarchy is read-only |
| `ProtectClock=true` | Can't change the system clock |
| `ProtectHostname=true` | Can't change the hostname |
| `RestrictNamespaces=true` | No new user/mount/network/pid namespaces |
| `RestrictSUIDSGID=true` | Can't create setuid/setgid binaries |
| `LockPersonality=true` | `personality()` locked |
| `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK` | Only the socket families the app needs (NDI mDNS needs AF_NETLINK on Linux) |
| `UMask=0077` | New files default to owner-only perms |
| `LimitNOFILE=65536` | Plenty of fds for many instances |
| `MemoryMax=12G` | Cgroup-enforced cap; raise for big multi-instance setups |
| `TasksMax=16384` | Caps total threads/processes |

Advanced hardening not enabled by default (enable cautiously — Playwright and
Chromium use a wide syscall surface):

- `SystemCallFilter=@system-service` — can break Chromium's sandboxed renderer
  launch. Test on a staging box first.
- `IPAddressAllow=`/`IPAddressDeny=` — works but you'd need to whitelist NDI
  multicast + every NDI receiver.

### Log retention

systemd journal auto-rotates, but on a long-running box the defaults can grow
large. Cap the journal at 500 MB and keep two weeks of history:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/ndi-streamer.conf <<'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=2week
EOF
sudo systemctl restart systemd-journald
```

---

## Firewall

The app has no authentication. Your firewall is the first line of defense.

### Required ports

| Port | Proto | Purpose | Required |
|------|-------|---------|----------|
| 5000 | TCP | Web UI + REST API | Only if you access it from another machine. **Do not expose to the internet.** |
| 5353 | UDP | mDNS — NDI source discovery | Required on any subnet where NDI receivers live |
| 5960–5969 | TCP | NDI video streams (per-source, range grows with sender count) | Required for all NDI receivers |

### ufw example

Allow only a trusted admin subnet (`10.0.0.0/24`) to hit the web UI, and
allow NDI traffic anywhere on the LAN:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 10.0.0.0/24 to any port 5000 proto tcp comment 'NDI Streamer admin UI'
sudo ufw allow 5353/udp comment 'mDNS for NDI discovery'
sudo ufw allow 5960:5969/tcp comment 'NDI video streams'
sudo ufw enable
sudo ufw status verbose
```

If clients are on another VLAN, you may need to open additional NDI TCP ports;
NDI allocates sequentially starting at 5960 per sender. For many instances,
open `5960:5999/tcp` or a wider range.

---

## Reverse proxy with TLS

Putting nginx in front gives you HTTPS **and** an authentication layer the app
doesn't provide out of the box. Do this whenever the server is reachable from
anywhere outside a trusted LAN.

1. Set `FLASK_HOST=127.0.0.1` in `/opt/ndi-streamer/.env` and restart the
   service — the app will now only listen on loopback.

2. Install nginx and create a basic-auth password file:

   ```bash
   sudo apt install -y nginx apache2-utils
   sudo htpasswd -c /etc/nginx/.htpasswd-ndi admin
   ```

3. Create `/etc/nginx/sites-available/ndi-streamer`:

   ```nginx
   server {
       listen 80;
       server_name ndi.example.com;
       return 301 https://$host$request_uri;
   }

   server {
       listen 443 ssl http2;
       server_name ndi.example.com;

       # Certs: use certbot (Let's Encrypt) or drop your own in.
       ssl_certificate     /etc/letsencrypt/live/ndi.example.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/ndi.example.com/privkey.pem;

       client_max_body_size 64M;   # must be >= MAX_UPLOAD_SIZE_MB in .env

       auth_basic           "NDI Streamer";
       auth_basic_user_file /etc/nginx/.htpasswd-ndi;

       location / {
           proxy_pass         http://127.0.0.1:5000;
           proxy_http_version 1.1;
           proxy_set_header   Host              $host;
           proxy_set_header   X-Real-IP         $remote_addr;
           proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
           proxy_set_header   X-Forwarded-Proto $scheme;
           proxy_read_timeout 120s;
       }
   }
   ```

4. Enable it and issue a certificate:

   ```bash
   sudo ln -s /etc/nginx/sites-available/ndi-streamer /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo apt install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d ndi.example.com
   ```

5. Tighten the firewall so only `443/tcp` (and `80/tcp` for the ACME redirect)
   is reachable for the web UI — port `5000` should no longer be in your ufw
   ruleset since Flask is only on loopback now.

---

## Monitoring

```bash
# Service state + recent logs
sudo systemctl status ndi-streamer
sudo journalctl -u ndi-streamer --since "1 hour ago"
sudo journalctl -u ndi-streamer -f

# See the parent + all worker child processes
pgrep -af 'run.py|ndi_worker'
ps --ppid "$(pgrep -f run.py | head -n1)" -o pid,rss,cmd

# Per-instance heartbeat + health from the API
curl -s http://127.0.0.1:5000/api/health | python3 -m json.tool

# Who's pulling each source: connected NDI receiver count + tally per instance
curl -s http://127.0.0.1:5000/api/instances | python3 -c \
  "import sys,json; [print(i['name'], i.get('ndi')) for i in json.load(sys.stdin)]"

# Inspect actual NDI TCP connections at the socket level (receiver IPs);
# NDI listens on 5960+ (one port per sender, plus discovery on 5353/5959)
sudo ss -tnp | grep -E ':59[6-9][0-9]'
```

A simple periodic memory snapshot (expect RSS to stay flat between 4h browser
recycles):

```bash
watch -n 30 'ps -o pid,rss,cmd -p $(pgrep -f ndi_worker | tr "\n" ",") 2>/dev/null'
```

---

## Running as a Service (Windows)

On Windows, you can run NDI Streamer as a background service using **NSSM** (Non-Sucking Service Manager) or as a scheduled task.

### Option A: NSSM (recommended)

[NSSM](https://nssm.cc/) wraps any executable as a Windows service with restart-on-failure, logging, and boot-start.

1. **Download NSSM** from [nssm.cc/download](https://nssm.cc/download) and extract `nssm.exe` somewhere on your PATH (e.g. `C:\Tools\`).

2. **Install the service** (run PowerShell as Administrator):

```powershell
nssm install NDIStreamer "C:\path\to\ndi-streamer\venv\Scripts\python.exe" "C:\path\to\ndi-streamer\run.py"
nssm set NDIStreamer AppDirectory "C:\path\to\ndi-streamer"
nssm set NDIStreamer DisplayName "NDI Streamer"
nssm set NDIStreamer Description "Streams webpages, images, and text as NDI sources"
nssm set NDIStreamer AppStdout "C:\path\to\ndi-streamer\logs\service.log"
nssm set NDIStreamer AppStderr "C:\path\to\ndi-streamer\logs\service.log"
nssm set NDIStreamer AppRotateFiles 1
nssm set NDIStreamer AppRotateBytes 10485760
nssm set NDIStreamer AppRestartDelay 5000
nssm set NDIStreamer Start SERVICE_AUTO_START
```

3. **Manage the service:**

```powershell
nssm start NDIStreamer
nssm stop NDIStreamer
nssm restart NDIStreamer
nssm status NDIStreamer

# Edit configuration GUI
nssm edit NDIStreamer

# Remove service
nssm remove NDIStreamer confirm
```

### Option B: Task Scheduler

For simpler setups, create a scheduled task that runs at startup:

1. Open **Task Scheduler** → Create Task
2. **General**: Name it "NDI Streamer", check "Run whether user is logged on or not"
3. **Trigger**: "At startup"
4. **Action**: Start a program
   - Program: `C:\path\to\ndi-streamer\venv\Scripts\python.exe`
   - Arguments: `run.py`
   - Start in: `C:\path\to\ndi-streamer`
5. **Settings**: Check "If the task fails, restart every 1 minute", up to 5 times

### Windows service notes

- NSSM handles restart-on-crash automatically (comparable to systemd's `Restart=always`)
- Log rotation is built into NSSM (`AppRotateFiles` / `AppRotateBytes`)
- The internal watchdog (heartbeat + crash recovery) works identically on Windows — it's Python-level, not OS-level
- For remote syslog on Windows, set `SYSLOG_ENABLED=true` and point `SYSLOG_ADDRESS` to your syslog server (`192.168.1.100:514`). Local `/dev/log` does not exist on Windows.

---

## NDI SDK Installation

The NDI SDK runtime is required for actual NDI output. Without it, the app runs in "dummy mode" (Playwright captures but no NDI streams are sent).

### Linux

#### Automatic detection

The setup script checks these paths for `libndi.so`:
- `/usr/share/ndi/lib`
- `/usr/local/lib`
- `/usr/lib`
- `/opt/ndi/lib`

#### Manual installation (Debian/Ubuntu)

1. **Download** the NDI SDK for Linux from
   [ndi.video/tools/ndi-sdk](https://ndi.video/tools/ndi-sdk/) (the download
   is gated by an email signup). You'll get either a `.tar.gz` or a
   self-extracting `.sh` — both end up as a shell installer.

2. **Run the installer.** It will prompt you to accept the EULA and extract
   into the current directory:

   ```bash
   chmod +x Install_NDI_SDK_v6_Linux.sh
   ./Install_NDI_SDK_v6_Linux.sh
   ```

   This extracts a directory like `NDI SDK for Linux/`. The runtime shared
   library lives at `NDI SDK for Linux/lib/x86_64-linux-gnu/libndi.so.*`.

3. **Install the runtime system-wide.** Copy the library and headers into
   `/usr/local/`:

   ```bash
   cd "NDI SDK for Linux"
   sudo mkdir -p /usr/local/ndi/lib /usr/local/ndi/include
   sudo cp -r lib/x86_64-linux-gnu/* /usr/local/ndi/lib/
   sudo cp -r include/* /usr/local/ndi/include/
   ```

4. **Register the library path with the dynamic linker.** This is cleaner
   than `LD_LIBRARY_PATH` because it works for every process on the machine,
   including systemd services, without needing to export an env var:

   ```bash
   echo '/usr/local/ndi/lib' | sudo tee /etc/ld.so.conf.d/ndi.conf
   sudo ldconfig
   ```

   Verify the linker can find it:

   ```bash
   ldconfig -p | grep libndi
   # Should print something like:
   # libndi.so.6 (libc6,x86-64) => /usr/local/ndi/lib/libndi.so.6
   ```

5. **Install the Python bindings** into the venv:

   ```bash
   sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/pip install ndi-python
   ```

   Verify:

   ```bash
   sudo -u ndi-streamer /opt/ndi-streamer/venv/bin/python -c \
     "import NDIlib as n; assert n.initialize(); print('NDI ready'); n.destroy()"
   ```

**Alternative** — if you prefer `LD_LIBRARY_PATH` (e.g. to keep the SDK in a
user-writable directory), create `/etc/profile.d/ndi.sh` with
`export LD_LIBRARY_PATH=/usr/local/ndi/lib:$LD_LIBRARY_PATH` and also set it
in `ndi-streamer.service` via an `Environment=` line. The `ld.so.conf.d`
approach above is recommended because it removes the env var from the equation.

### Windows

1. **Download** the NDI SDK from [ndi.video/tools/ndi-sdk](https://ndi.video/tools/ndi-sdk/) — choose the Windows installer.

2. **Run the installer.** It installs the NDI runtime DLLs (typically to `C:\Program Files\NDI\NDI 6 Runtime\`) and adds them to the system PATH automatically.

3. **Verify** — open a new PowerShell window:
   ```powershell
   where.exe ndi-*
   # Or check the DLL exists:
   dir "C:\Program Files\NDI\NDI*\*ndi*"
   ```

4. **Reboot** or log out and back in if the PATH change isn't picked up.

5. **Install the Python bindings** (inside your activated venv):
   ```powershell
   # PyPI source tarball is missing pybind11 — build from GitHub instead:
   git clone --recursive https://github.com/buresu/ndi-python.git
   cd ndi-python
   $env:CMAKE_ARGS="-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
   pip install .
   cd ..
   ```
   Requires CMake and Visual Studio Build Tools (C++ workload). If the build fails, the app still runs in dummy mode (captures work, no NDI output).

---

## Switching to PostgreSQL

1. **Install PostgreSQL:**
   ```bash
   sudo apt install postgresql postgresql-client
   ```

2. **Create a database:**
   ```bash
   sudo -u postgres createuser ndi_streamer
   sudo -u postgres createdb -O ndi_streamer ndi_streamer
   sudo -u postgres psql -c "ALTER USER ndi_streamer PASSWORD 'your_password';"
   ```

3. **Update `.env`:**
   ```env
   DATABASE_URL=postgresql://ndi_streamer:your_password@localhost:5432/ndi_streamer
   ```

4. **Run migrations:**
   ```bash
   source venv/bin/activate
   flask db upgrade
   ```

`psycopg2-binary` is already included in the requirements.

---

## Syslog Configuration

NDI Streamer can forward structured event logs to syslog for centralized monitoring.

### Enable syslog

```env
SYSLOG_ENABLED=true
SYSLOG_ADDRESS=/dev/log        # local syslog (Linux only)
SYSLOG_FACILITY=local0
SYSLOG_TAG=ndi-streamer
```

> **Windows note:** `/dev/log` does not exist on Windows. Leave `SYSLOG_ENABLED=false` (the default) or point `SYSLOG_ADDRESS` to a remote syslog server (e.g. `192.168.1.100:514`). See [Running as a Service (Windows)](#running-as-a-service-windows) for details.

### Remote syslog

```env
SYSLOG_ADDRESS=192.168.1.100:514
```

### Tracked events

| Event | Description |
|-------|-------------|
| `INSTANCE_STARTED` | Worker process launched |
| `INSTANCE_STOPPED` | Worker process stopped |
| `INSTANCE_CRASHED` | Worker died unexpectedly |
| `INSTANCE_UNHEALTHY` | Worker hung (heartbeat stale) |
| `INSTANCE_RESTARTED` | Watchdog restarted a crashed/hung worker |
| `INSTANCE_REFRESHED` | Manual or auto content reload |
| `INSTANCE_CREATED` | New instance added |
| `INSTANCE_UPDATED` | Instance config changed |
| `INSTANCE_DELETED` | Instance removed |
| `ALL_STARTED` | Global start triggered |
| `ALL_STOPPED` | Global stop triggered |
| `SETTINGS_CHANGED` | Global settings modified |
| `MEDIA_UPLOADED` | File uploaded to library |
| `MEDIA_DELETED` | File removed from library |
| `MEDIA_IN_USE_STOPPED` | Running instances stopped because their media was deleted |
| `MEDIA_OPTIMIZED` | Background video optimization finished |
| `VIDEO_PLAY` / `VIDEO_STOP` / `VIDEO_LOAD` | Video playback cue received |
| `VIDEO_COMMAND` | Other video control command |
| `SIGNAGE_SKIP` | Signage skipped to the next item |
| `SIGNAGE_COMMAND` | Other signage control command |
| `SIGNAGE_DECK_UPLOADED` | PowerPoint/PDF deck converted into a slide group |
| `SIGNAGE_ITEMS_ADDED` / `SIGNAGE_ITEMS_UPDATED` / `SIGNAGE_ITEMS_DELETED` | Playlist items changed (bulk) |
| `SIGNAGE_ITEM_DELETED` | Single playlist item removed |
| `SIGNAGE_GROUP_CREATED` / `SIGNAGE_GROUP_DELETED` | Content group added or removed |
| `OVERVIEW_STARTED` / `OVERVIEW_STOPPED` | Overview stream switched on or off |
| `INSTANCE_RESTART_FAILED` / `INSTANCE_KILL_FAILED` | Watchdog couldn't respawn or kill a worker (retried) |
| `WATCHDOG_ERROR` / `WATCHDOG_REARMED` | Watchdog recovered from an internal error / was restarted by a status poll |

### Example syslog output

```
Apr  9 14:23:01 prod-server ndi-streamer: [INFO] ndi_streamer.events - [INSTANCE_STARTED] id=3 name='PRODUCTION (Weather Radar)' pid=48291
Apr  9 14:53:01 prod-server ndi-streamer: [INFO] ndi_streamer.events - [INSTANCE_REFRESHED] id=3 name='Weather Radar'
Apr  9 15:01:44 prod-server ndi-streamer: [WARNING] ndi_streamer.events - [INSTANCE_CRASHED] id=3 — restarting
```

---

## Architecture

```
┌──────────────────────────────────────────┐
│              Flask App (:5000)            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ REST API │ │ Media    │ │ Web UI   │  │
│  │ /api/*   │ │ Uploads  │ │ SPA      │  │
│  └────┬─────┘ └──────────┘ └──────────┘  │
│       │                                   │
│  ┌────▼─────────────────────────────────┐ │
│  │         SQLAlchemy ORM               │ │
│  │    SQLite ◄──────► PostgreSQL        │ │
│  └────┬─────────────────────────────────┘ │
│       │                                   │
│  ┌────▼─────────────────────────────────┐ │
│  │       Worker Manager + Watchdog      │ │
│  └────┬──────────────┬──────────────────┘ │
└───────┼──────────────┼────────────────────┘
        │              │          ← one separate process per instance
 ┌──────▼──────┐ ┌─────▼───────────────────┐
 │ Browser     │ │ Native worker           │
 │ worker      │ │ (webcam / video /       │
 │ (webpage /  │ │  signage)               │
 │ image/text) │ │                         │
 │ Playwright +│ │ OpenCV/FFmpeg decode,   │
 │ headless    │ │ numpy letterbox +       │
 │ Chromium    │ │ crossfade compositing   │
 │ screenshots │ │ — no browser            │
 │      │      │ │            │            │
 │  BGRX frame │ │       BGRX frame        │
 │      ▼      │ │            ▼            │
 │  NDI sender │ │       NDI sender        │
 └──────┬──────┘ └────────────┬────────────┘
        ▼                     ▼
  NDI "MACHINE (Instance Name)" on the network
```

- Each instance runs in an isolated **process** (not thread) — a crash in one does not affect others
- The **watchdog thread** monitors processes every 5 seconds and auto-restarts any that crash or hang; it is self-healing (see [Output Isolation & Watchdog Self-Healing](#output-isolation--watchdog-self-healing))
- **Browser workers** (webpage, image, text) render in headless Chromium and capture JPEG screenshots at the **instance capture FPS**; NDI sends at the **global output FPS** by duplicating frames
- **Native workers** never launch a browser: webcams are grabbed via V4L2/OpenCV, and video files and signage playlists are decoded with OpenCV/FFmpeg. Signage stills and video frames are letterboxed onto in-memory canvases, and crossfades are alpha-blended per output frame (`cv2.addWeighted`) straight into the NDI frame buffer — frame timing is set by the worker, not a browser compositor
- Signage playlists can therefore only contain stills and video files, not live webpages
- **Auto-refresh** and **browser recycling** apply to browser workers only
- The **Overview stream** is one more native worker that only *receives*: it pulls every output back over NDI and composites a grid (see [Overview Stream](#overview-stream)). It cannot take an output down, and an output dying only changes its tile

---

## 24/7 Production Reliability

NDI Streamer is designed to run unattended for weeks or months. Three systems work together to keep instances healthy:

### Browser Recycling

Chromium is not designed to run indefinitely. Over hours and days, each headless browser process accumulates leaked DOM nodes, JS heap growth, and internal caches that cannot be garbage collected. Left unchecked, a single instance can grow from ~200MB to 500MB+ over a few days.

NDI Streamer solves this by periodically tearing down the **entire browser process** (not just reloading the page) and launching a fresh one. During the ~1–2 second recycle window, the last captured frame continues being sent to NDI so receivers see no interruption.

```env
# Recycle every 4 hours (default). Lower for complex pages, raise for static content.
BROWSER_RECYCLE_HOURS=4
```

What happens during a recycle:
1. Current page, browser context, and browser process are closed
2. Python's garbage collector runs to reclaim freed memory
3. A new Chromium process launches and loads the content
4. Frame output continues uninterrupted (last frame is re-sent)

### Pre-Allocated Frame Buffers

The original implementation created a new PIL Image and numpy array for every screenshot — at 30fps that's 30 allocations and deallocations per second. Python's memory allocator doesn't reliably return large allocations to the OS, causing heap fragmentation and steady memory growth over days.

Now, a single BGRX frame buffer is allocated at worker startup and **reused for every capture**. Screenshot data is decoded and channel-swapped directly into this fixed buffer. This keeps per-worker memory flat and predictable:

| Resolution | Buffer Size | Total per instance (approx.) |
|-----------|-------------|------------------------------|
| 1280×720 | 3.5 MB | ~150–200 MB |
| 1920×1080 | 7.9 MB | ~200–300 MB |
| 3840×2160 | 31.6 MB | ~350–500 MB |

The "total per instance" includes the Chromium process, Python interpreter, and frame buffer.

### Heartbeat & Hang Detection

A crashed worker is easy to detect (process is dead). A **hung** worker is harder — the process is alive, CPU is consumed, but no new frames are being produced. Common causes:

- A webpage runs an infinite JavaScript loop
- Playwright blocks waiting for a network response that never arrives
- Chromium's renderer process deadlocks internally

Each worker writes `time.monotonic()` into a shared `multiprocessing.Value` after every successful frame send. The parent watchdog checks this value every 5 seconds. If it hasn't updated in 30 seconds, the worker is considered hung:

1. The process is asked to stop gracefully (SIGTERM), which closes the browser and NDI sender cleanly
2. If it doesn't die, its **entire process group** is force-killed — each worker
   is its own process-group leader, so the kill takes the Playwright driver and
   the whole Chromium process tree with it. Nothing gets orphaned, even when the
   worker is wedged hard enough to ignore SIGTERM
3. A new worker is launched with the same configuration
4. `INSTANCE_UNHEALTHY` and `INSTANCE_RESTARTED` events are logged to syslog

```
Apr 10 03:42:15 prod ndi-streamer: [WARNING] [INSTANCE_UNHEALTHY] id=3 reason=hung (heartbeat stale 34s)
Apr 10 03:42:17 prod ndi-streamer: [INFO] [INSTANCE_RESTARTED] id=3 reason=hung new_pid=51203
```

### Restart Backoff

A worker that dies instantly every time it starts (missing NDI runtime, corrupt
media file, a page Chromium can't load) would otherwise respawn — and relaunch
Chromium — every 5 seconds forever. The watchdog rate-limits restarts per
instance with exponential backoff: 5s → 10s → 20s → 40s … capped at 5 minutes.
A worker that then stays up for 2+ minutes is considered recovered and the
backoff resets. Repeated failures log `INSTANCE_RESTART_BACKOFF` warnings to
syslog so external monitoring can catch chronic flappers.

### Output Isolation & Watchdog Self-Healing

Every output is its own OS process with its own NDI sender, frame buffer and
browser/decoder. When one output crashes or hangs, the other outputs keep
streaming and only that output is restarted. What all outputs share is the
main app process and its single watchdog thread, so the watchdog is built so
that it can't fail silently:

- **Each instance is checked and restarted on its own.** An error while
  checking or restarting one output (a failed fork under memory pressure,
  a kill that throws) is logged and the watchdog carries on with the rest.
- **A failed respawn retries automatically.** If a new worker can't be
  started, the output stays tracked with no process. The next pass sees it
  as crashed and tries again under the normal restart backoff.
  Logged as `INSTANCE_RESTART_FAILED` (and `INSTANCE_KILL_FAILED` if killing
  the old worker failed).
- **The watchdog supervises itself.** An unexpected error anywhere in a pass
  is logged (`WATCHDOG_ERROR`) and the watchdog resumes after 5 seconds
  instead of stopping.
- **Re-armed from polling.** `/api/system` (polled every 2s by the
  dashboard), `/api/status` and `/api/health` check that the watchdog thread
  is alive while outputs are running, and restart it if it isn't
  (`WATCHDOG_REARMED`). Before, a dead watchdog stayed dead until someone
  pressed Start on an output.
- `/api/status` and `/api/system` return a `watchdog` object
  (`running`, `rearmed`, `tracked_instances`, `last_check_age_s`). If
  `last_check_age_s` goes well past 5s, the watchdog is stuck — for example
  a single thread busy killing several hung workers one after another, each
  of which can take up to ~8s.

```
Apr 10 03:42:15 prod ndi-streamer: [ERROR] [INSTANCE_RESTART_FAILED] id=3 reason=crashed error=OSError(12, 'Cannot allocate memory') (will retry)
Apr 10 03:42:25 prod ndi-streamer: [INFO] [INSTANCE_RESTARTED] id=3 reason=crashed new_pid=51244
```

### Sizing Guide

For planning server resources:

| Instances | Resolution | Capture FPS | RAM (approx.) | CPU Cores |
|-----------|-----------|-------------|---------------|-----------|
| 1–3 | 1080p | 30 | 2–4 GB | 2–4 |
| 5–10 | 1080p | 30 | 4–8 GB | 4–8 |
| 10–20 | 1080p | 15 | 6–12 GB | 8–16 |
| 5–10 | 720p | 60 | 3–6 GB | 4–8 |

Tips:
- Static content (images, text) → set capture FPS to 5–15 to cut CPU by 50–80%
- If pages are simple HTML, RAM usage stays at the lower end
- Each worker is a separate process and scales across CPU cores
- Monitor memory over 24h to establish baseline: `watch -n 60 'ps aux | grep ndi-worker'`

### Health Monitoring

The `/api/health` endpoint returns per-instance health data:

```json
[
  {
    "id": 3,
    "name": "Weather Radar",
    "running": true,
    "health": {
      "alive": true,
      "pid": 48291,
      "heartbeat_age_s": 1.2,
      "healthy": true
    }
  }
]
```

You can poll this from external monitoring (Prometheus, Nagios, etc.):

```bash
# Simple health check script
curl -s http://localhost:5000/api/health | python3 -c "
import sys, json
data = json.load(sys.stdin)
unhealthy = [d for d in data if d.get('running') and not d.get('health',{}).get('healthy')]
if unhealthy:
    print(f'CRITICAL: {len(unhealthy)} unhealthy instances')
    sys.exit(2)
print(f'OK: {len([d for d in data if d.get(\"running\")])} instances healthy')
"
```

---

## Digital Signage

A **signage** instance turns an NDI output into a self-contained signage
player: a playlist of stills and videos that plays in order, with crossfade
transitions, per-item scheduling, grouping, and impression counting. The
whole thing is rendered inside the worker process (OpenCV, no browser) and
streamed as a single uninterrupted NDI source.

### Getting started

1. Create an instance with source type **Signage Playlist** (or use the
   *+ New Signage Instance* button on the **Signage** tab).
2. Open the **Signage** tab, pick the instance, and drop content onto the
   upload zone — images, videos, or whole PowerPoint/PDF decks.
3. Start the instance. The live panel shows what's on air, what's next, a
   countdown, and a **Skip Next** button.

### Playback model

- Items play top-to-bottom and loop. Drag the `⠿` handle to reorder — when
  the dragged row is part of the current selection, the whole selection
  moves together as a block, keeping its order. Drop items onto a group
  header to add them to that group.
- **Duration** — stills default to the instance's *Default Still Duration*;
  videos default to their own file length. An explicit duration on a video
  either cuts it short or holds its last frame to fill the slot.
- **Crossfade** — each item's crossfade is its *outgoing* transition into
  the next item (`0` = hard cut). The anchors differ by content type:
  - **Video**: the fade starts `crossfade` seconds *before the file ends*,
    so the picture is still moving as it dissolves away.
  - **Still**: `duration` is time on screen *before* the outgoing fade
    begins — a 5s still with a 3s fade does a 3s fade-in, holds ~2s clean,
    then fades out over 3s.
  The incoming item (and its impression count) starts the moment it first
  becomes visible.
- **Bulk editing** — *Select All* (or tick individual rows), then *Edit
  Selected* applies duration, crossfade, schedule windows, or
  enabled/disabled to every selected item in one shot; *Group Selected* and
  *Delete Selected* work the same way. Rows are tinted by schedule state:
  red-ish = expired or disabled, grey = upcoming (not started yet).
- The header shows the **server's date & time** — the clock every schedule
  runs on — so clock drift is visible at a glance.
- If nothing is eligible to play (everything expired / outside its daily
  window / disabled), the output fades to black and re-checks twice a second.

### Scheduling

Every item and every group can carry:

| Setting | Meaning |
|---------|---------|
| **Start showing** | Date + time the content goes live |
| **Expire** | Date + time it stops playing |
| **Daily from / until** | Time-of-day window, every day (`07:00–11:00`; `22:00–06:00` wraps past midnight) |
| **Enabled** | Master switch — off means skipped |

Group settings apply to all items inside: the date windows *intersect*
(an item plays only when both its own and its group's window are open), the
daily window and duration/crossfade act as inheritable defaults an item can
override, and a disabled group silences all its items. Schedule changes
apply to a running player within a second — the playlist is hot-reloaded
into the worker without dropping the NDI stream.

All schedule times are the **server's local wall-clock**.

### Groups

Select items with their checkboxes and hit **Group Selected** (or create an
empty group and drag items in). A group occupies one slot in the top-level
order and its items play consecutively — order, schedule, transition, enable,
and delete them as a single unit. Deleting a group asks whether to delete or
keep its items; *ungrouping* returns them to the top level.

### PowerPoint / PDF decks

Uploading a `.ppt`, `.pptx`, `.odp`, or `.pdf` through the Signage tab
rasterizes every slide/page to a PNG (via LibreOffice + `pdftoppm`) and
appends them as a group named after the file. Requires:

```bash
sudo apt install -y libreoffice-impress poppler-utils
```

Render resolution is controlled by `PRESENTATION_RENDER_DPI` in `.env`
(default 150 — a 16:9 slide comes out around 2000×1125).

### Impressions

Every time an item goes on air the worker logs an impression; counts appear
next to each item in the playlist (👁). Counting survives worker restarts —
impressions are appended to a log the API folds into the database.

### Show-control URLs

Like the video API, the signage endpoints accept `GET` as well as `POST`, so
anything that can hit a URL (Companion, Crestron, a browser bookmark) can
drive them, addressing the instance by numeric id or by name:

```bash
# What's playing / what's next
curl http://<host>:5000/api/instances/Lobby%20Signage/signage/status
# → {"id":1,"name":"Lobby Signage","running":true,"status":{
#      "current":{"id":12,"name":"promo.mp4","kind":"video",...},
#      "remaining_s":8.5,
#      "next":{"id":13,"name":"menu.png","kind":"image",...}}}

# Crossfade to the next item right now
curl http://<host>:5000/api/instances/1/signage/skip
```


## Overview Stream

The **Overview stream** is a built-in multiview. It shows every output in
one real-time NDI source, `MACHINE (Overview)`, for a confidence monitor, a
switcher multiviewer input, or a producer's screen.

Switch it on with the **Overview stream** toggle on the Overview tab (the
switch is remembered across restarts). The tile shows a live thumbnail;
click it to open a full-size preview window.

### Layout

- Tiles are grouped under a header per source type, in this order: Images,
  Video, Signage, Webpage, Text, Webcam. Empty sections are left out.
- Each tile has the output's name underneath. Tiles are 16:9 and all the
  same size: the largest size that fits everything on the canvas. Sections
  flow left to right and wrap like lines of text. Sources with a different
  aspect ratio are letterboxed.
- **The layout follows your outputs live.** Adding, renaming, deleting,
  starting or stopping an output updates the Overview within half a second,
  with no restart and no drop in the stream.
  - **Stopped** outputs keep their tile, which reads **STOPPED**.
  - **Disabled** outputs are removed from the Overview.
  - An output that crashes shows **NO SIGNAL** until the watchdog brings it
    back. **CONNECTING…** means the output is starting but hasn't sent a
    frame yet.

### How it works

- It is a worker process like any output, with its own heartbeat, watchdog
  restarts and preview. **Stop All** leaves it running (it has its own
  toggle); switching it off stops only the Overview.
- It only **receives**. There is one NDI receiver thread per tile, at full
  quality by default. The compositor sends the newest frame of every tile
  at `OVERVIEW_FPS` (30), so tiles are real time with at most one output
  frame of latency.
- **It connects locally, without NDI discovery.** Every worker publishes
  its NDI sender port. The Overview connects straight to
  `127.0.0.1:<port>`, and checks that the port still belongs to the right
  worker process, so a stale entry can never show the wrong output. If that
  gives no video, it falls back to connecting by NDI name.
- The name "Overview" is reserved: an output can't be named "Overview",
  since two NDI sources with one name would be indistinguishable.

### Cost

Receiving at full quality means decoding every output's NDI stream. Also,
while the Overview is on, every output has at least one receiver, so each
output encodes its NDI stream even if nothing else is watching it. Measured
on a 4-core VM with 7 outputs at 720p60 (two videos, two images, signage,
a webpage and text):

| Setting | Overview process CPU | Overview delivered | Outputs |
|---------|---------------------|--------------------|---------|
| `OVERVIEW_BANDWIDTH=highest`, 30fps | ~120% (1.2 cores) | 24fps (box saturated) | dropped from 59 to ~50fps |
| same, with 3 outputs running | — | 30fps steady | — |
| `OVERVIEW_BANDWIDTH=lowest` (at 60fps) | ~100% | 50fps | ~56fps |

In short, budget about 0.15–0.2 core per 720p60 output at full quality, on
top of the outputs themselves. The 4-core test box was simply too small
for 7 outputs, 3 Chromium browsers and a full-quality Overview. On a box
without headroom, `OVERVIEW_BANDWIDTH=lowest` uses NDI's low-bandwidth
preview streams: much cheaper to decode, and fine for a monitor wall.

Each output's receiver list shows the Overview as a connection from
`127.0.0.1`.

---

## API Reference

### Global Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/settings` | Get global settings |
| `PUT` | `/api/settings` | Update hostname / output FPS (1–120) |

### Global Controls

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/start-all` | Start all enabled instances |
| `POST` | `/api/stop-all` | Stop all running instances (the Overview stream keeps running) |

### Overview Stream

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/overview` | State: `enabled`, `running`, `healthy`, `ndi_source`, `width`/`height`/`fps`, `bandwidth`, `tiles` (enabled outputs shown) |
| `POST` | `/api/overview` | `{"enabled": true\|false}` — switch it on/off (remembered across restarts). 409 if an output is already named "Overview" |
| `GET`/`POST` | `/api/overview/on` · `/api/overview/off` | Plain-URL switches for show controllers |
| `GET` | `/api/instances/0/preview` | Latest Overview thumbnail |
| `GET` | `/api/overview/preview/stream` | Live MJPEG of the Overview (the popup at `/preview/overview` uses it) |

### Instances

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/instances` | List all instances — running ones include an `ndi` object with `receivers` (connected NDI receiver count, `null` if the SDK can't report it) and `on_program` / `on_preview` tally |
| `POST` | `/api/instances` | Create instance |
| `GET` | `/api/instances/:id` | Get instance |
| `PUT` | `/api/instances/:id` | Update instance |
| `DELETE` | `/api/instances/:id` | Delete instance |
| `POST` | `/api/instances/:id/start` | Start instance |
| `POST` | `/api/instances/:id/stop` | Stop instance |
| `POST` | `/api/instances/:id/refresh` | Reload content |
| `GET` | `/api/instances/:id/preview` | Latest preview JPEG (list thumbnail) |
| `GET` | `/api/instances/:ref/preview/stream` | Live MJPEG preview stream (`:ref` = id or name) — boosts the worker to 854px @ ~4fps while connected; also embeddable in any `<img>` tag |

The built-in popup viewer at `/preview/:id` wraps the stream with the
instance's name, live/stopped state, auto-reconnect, and — for signage —
a now-playing / up-next footer. Open it from the UI (click an output card's
screen on the Overview, or ⧉ Popup Preview on the Signage tab) or bookmark
the URL directly.

### Media Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/media` | List all uploaded files |
| `POST` | `/api/media` | Upload a file (multipart) |
| `GET` | `/api/media/:id` | Get file metadata |
| `GET` | `/api/media/:id/file` | Serve the playback file (optimized copy when one exists; `?original=1` for the untouched upload) |
| `GET` | `/api/media/:id/thumb` | Poster thumbnail JPEG (generated server-side, cached) |
| `GET` | `/api/media/:id/download` | Download the original upload as an attachment; `?optimized=1` downloads the transcoded copy |
| `POST` | `/api/media/:id/optimize` | Queue a video for background optimization (202; 501 when ffmpeg is missing) |
| `DELETE` | `/api/media/:id` | Delete file (unlinks from instances, removes the optimized copy too) |

Each entry in the listing carries its **origin** — `library` (Media Library
upload), `signage` (Signage tab upload), or `deck` (a slide rasterized from
an uploaded presentation, with `origin_name` naming the source deck) — plus
`signage_usage`: every playlist appearance with the instance and, when the
item sits in one, the signage group. The Media Library tab uses these for
its filter bar (type / source / usage, including "in a signage group" and
"unused") and a per-deck view of converted slides.

Every uploaded file gets a permanent `uid` (short random token, e.g.
`9f3c21ab`) alongside its numeric `id`. Numeric ids of existing files never
change, but SQLite can hand a deleted file's id to the next upload — the
`uid` is assigned once and **never reused**, so show-controller cues keyed on
it can't silently point at the wrong file. Playback endpoints accept media by
id, uid, or original filename.

### Webcams

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/webcams` | Detect connected V4L2 capture devices (Linux) |

### Video Playback Control

Video-file instances expose playback control on the same port as the web UI,
designed for show controllers (Companion, Crestron, QLab network cues) and
plain browser bookmarks. `:ref` is the instance **id or name** (URL-encode
spaces in names), `:media` is a media file's **numeric id, permanent uid, or
original filename**, and both `GET` and `POST` are accepted so any device
that can fire a URL can drive playback:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`/`POST` | `/api/instances/:ref/video/play` | Play the assigned video from the first frame (auto-starts the NDI output if needed) |
| `GET`/`POST` | `/api/instances/:ref/video/play/:media` | Switch to a different video and play it immediately (hot-swap, no restart) |
| `GET`/`POST` | `/api/instances/:ref/video/load` | Cue the instance's assigned video (or `?media=`) on its first frame |
| `GET`/`POST` | `/api/instances/:ref/video/load/:media` | Cue a video: load it and hold on its first frame, so a later `play` starts instantly |
| `GET`/`POST` | `/api/instances/:ref/video/stop` | Stop playback and hold the configured frame |
| `GET` | `/api/instances/:ref/video/status` | Playback state + loop/hold/autoplay + loaded media |

`play`, `load`, and `stop` all accept optional query parameters:

- `?media=` — alternative to the path form for selecting the video (`play`/`load`)
- `?hold=first|last` — set which frame stays on air when stopped or when a
  play-once video ends; persisted to the instance, so it also survives restarts

```bash
# Fire the walk-in video from anything that can hit a URL
curl "http://10.0.0.5:5000/api/instances/Walk-In%20Video/video/play"

# Switch the same output to a different video and play it — media addressed
# by numeric id, permanent uid, or original filename
curl "http://10.0.0.5:5000/api/instances/3/video/play/9f3c21ab"
curl "http://10.0.0.5:5000/api/instances/3/video/play/sponsor-reel.mp4"

# Pre-load ("cue") the next video: it appears frozen on its first frame,
# and the next /video/play fires with zero load latency
curl "http://10.0.0.5:5000/api/instances/3/video/load/walkout.mp4"

# Stop and hold — override the held frame per command
curl "http://10.0.0.5:5000/api/instances/3/video/stop?hold=first"

# Check state
curl "http://10.0.0.5:5000/api/instances/3/video/status"
# → {"id":3,"name":"Walk-In Video","running":true,"video_state":"playing",
#    "video_loop":false,"video_hold":"last","video_autoplay":false,
#    "media":{"id":7,"uid":"9f3c21ab","original_name":"walkout.mp4",...}}
```

Behavior notes:

- **`play` always restarts from the first frame** — it doubles as a restart button.
- **`play`/`load` on a stopped instance auto-start the NDI output first**, so one
  URL is all a controller needs.
- **Switching videos is a hot-swap inside the running worker** — no process
  restart, no NDI sender teardown. The new file is opened and verified before
  it replaces the old one; the previous frame stays on air until the new
  file's first frame lands, so receivers never see a drop or a blank frame.
  If the new file can't be read, the command is rejected and the current
  video keeps playing/holding untouched.
- **`load` is the fast path for tight cues**: decode of the first frame,
  letterbox setup, and the file handle are all ready before the play cue, so
  `play` after a `load` is effectively instantaneous.
- A media switch is persisted to the instance, so a later restart (or watchdog
  recovery) resumes with the file that was last loaded.
- **The NDI stream never goes blank**: before playback and while stopped the
  held frame keeps streaming at the global output FPS.
- Per-instance settings (in the instance editor or via `PUT /api/instances/:id`):
  - `video_loop` — `false` = play once then stop, `true` = loop forever
  - `video_hold` — `"last"` or `"first"`: which frame stays on air when stopped
    or after a play-once video ends (also settable per command via `?hold=`)
  - `video_autoplay` — start playing as soon as the instance starts
- Frames advance at the file's native frame rate (letterboxed to the instance
  resolution); NDI output stays at the global output FPS. Audio is not output —
  playback is video-only.

### Signage

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/instances/:id/signage` | Full playlist: groups, items (with impression counts), defaults |
| `POST` | `/api/instances/:id/signage/items` | Append library files — `{"media_file_ids":[..], "group_id": optional}` |
| `PUT` | `/api/signage/items/:id` | Update an item — `duration_s`, `crossfade_s`, `start_at`, `end_at`, `daily_start`, `daily_end`, `enabled`, `group_id` (null clears any of them) |
| `DELETE` | `/api/signage/items/:id` | Remove an item from the playlist |
| `POST` | `/api/instances/:id/signage/items/update` | Batch edit — `{"item_ids":[..], "set":{fields}}` (same fields as item PUT; null clears) |
| `POST` | `/api/instances/:id/signage/items/delete` | Batch remove — `{"item_ids":[..]}` |
| `POST` | `/api/instances/:id/signage/groups` | Create group — `{"name": str, "item_ids": optional}` |
| `PUT` | `/api/signage/groups/:id` | Update group (same fields as items, plus `name`) |
| `DELETE` | `/api/signage/groups/:id` | Delete group + its items; `?keep_items=1` ungroups instead |
| `POST` | `/api/instances/:id/signage/reorder` | Persist a full ordering — `{"order":[{"type":"item"\|"group","id":n},..], "group_items":{"<gid>":[item ids]}}` |
| `POST` | `/api/instances/:id/signage/upload` | Upload straight into the playlist (multipart); ppt/pptx/odp/pdf become a group of slide images |
| `GET` | `/api/instances/:ref/signage/status` | Now playing / up next / seconds remaining (`:ref` = id or name) |
| `GET` | `/api/instances/:ref/signage/events` | Real-time now-playing stream (Server-Sent Events) — pushes the status payload on every change; usable from any `EventSource` client |
| `GET`/`POST` | `/api/instances/:ref/signage/skip` | Crossfade to the next item now |

Schedule datetimes use the HTML `datetime-local` format (`2026-10-01T07:00`)
and are interpreted in the server's local timezone; daily windows are `HH:MM`
strings. Playlist mutations apply to a running player within a second via a
live reload — the NDI stream never drops.

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Running count, totals, and watchdog state (`watchdog`: `running`, `rearmed`, `tracked_instances`, `last_check_age_s`) |
| `GET` | `/api/health` | Per-instance health with heartbeat age (also re-arms the watchdog if its thread has died) |
| `GET` | `/api/receivers` | Fleet-wide receiver view: every connection across all running instances, summed TCP bandwidth (`total_tcp_mbps`), unique receiver / connection counts, and total NIC egress (`egress_mbps`, loopback excluded) |
| `GET` | `/api/system` | Host health: CPU average across all cores (`cpu.avg`), per-core load (`cpu.per_core`), ~5 min history sampled every 2s server-side (`cpu.history`, `[epoch_ms, percent]`), load average, memory, and free/used space on the drive holding `UPLOAD_FOLDER` (`disk`, no path), plus watchdog state (`watchdog`, same shape as `/api/status`). Needs `psutil` for CPU/memory; disk works without it |
| `GET` | `/api/instances/:ref/receivers` | Who is pulling this source: peer IPs + reverse-DNS hostnames of established NDI connections to the worker's sockets, with the SDK's own count (`sdk_receivers`) for cross-checking |
| `GET` | `/preview/:id` | Popup live-preview page (HTML) |

---

## Performance Notes

| Scenario | CPU per instance | Notes |
|----------|-----------------|-------|
| 1080p @ 30fps capture | Moderate | Good for most use cases |
| 720p @ 60fps capture | Light | Complex pages still fast |
| 1080p static content, 5fps capture | Very light | NDI still outputs 60fps |
| Text overlay, 15fps capture | Minimal | Simple HTML rendering |
| Webcam 1080p @ 30fps | Light | No browser — MJPEG decode + copy only |
| Webcam 1080p/720p @ 60fps | Light–Moderate | Requires a camera that offers 60fps modes |
| Video file 1080p @ 30fps | Light | No browser — FFmpeg decode via OpenCV; while stopped/holding it's a single frame resend |

- Each instance is an isolated process — scales across CPU cores
- For mostly-static content (images, text), set capture FPS low (5–15) to save CPU
- Auto-refresh causes a brief content reload; the last frame continues sending during reload
- The NDI SDK's `clock_video` option paces output to real-time
- Video, webcam, and signage sources bypass Chromium entirely. Crossfades,
  scaling, and letterboxing are CPU work (numpy/OpenCV); a GPU is not used
- Hardware video decode is requested (VAAPI/QSV/NVDEC) but depends on the
  OpenCV build: the stock `opencv-python-headless` wheel's bundled FFmpeg is
  typically built without hardware decoders, in which case decoding silently
  falls back to software. Plan CPU headroom for 4K sources accordingly
- Webcam sources run at the camera's native rate — the
  limit is the camera hardware and USB bandwidth, not the app. The camera is opened in
  MJPEG mode; uncompressed YUYV would cap out at ~5–10fps at 1080p on USB 2.0
- Multiple simultaneous cameras: spread them across USB controllers/ports if you hit
  bandwidth limits, and budget ~125 Mbps of network per 1080p60 NDI stream

### Scaling & worst-case capacity (measured, 0.4.0)

Stress-tested with a **540-file media library**, rapid switching, and
concurrent playback workers on a 4-core / 16 GB test machine. Memory was
verified leak-free at every level (per-worker PSS flat over a sustained
playback window, flat across 200 hot-swaps, API process flat across the
whole run).

**A 540-file library is a non-event.** Library size only touches the API
layer — the database and the media list, not the playback path:

| Operation (540 files in library) | Measured |
|---|---|
| Upload (per file, incl. metadata probe) | ~6 ms + disk write |
| `GET /api/media` full listing | ~13 ms |
| Media lookup by id/uid/filename | < 1 ms |
| Switch command (`/video/play/:media`) round-trip | ~5 ms median, ~7 ms p99 |
| 200 hot-swaps at 18 switches/s on one output | zero restarts, zero leaks, still playing |

**540 *simultaneous* NDI outputs is a multi-host deployment.** Each playing
video worker is one process; measured per worker at 1080p output decoding a
small source file: ~48 MB PSS and a fraction of a core (source decode cost
grows with source resolution — budget roughly ⅙ core for small sources to
half a core or more for 1080p30 H.264 sources). The per-host walls, in the
order you'll hit them:

1. **Network** — ~125 Mbps per 1080p60 NDI stream → 540 streams ≈ 67 Gbps.
   An 8-stream host saturates 1 GbE; ~70 streams saturate 10 GbE.
2. **CPU** — 540 × (decode + blit + NDI send) ≈ 90+ cores for small sources,
   several hundred for 1080p sources.
3. **RAM** — ~25 GB at 540 workers (~48 MB each). The frame buffer is
   pre-allocated per worker (8.3 MB at 1080p) and never grows.

Practical guidance for a 540-video show:

- **Library scale**: keep all 540 files on one host — uploading, browsing,
  cueing, and switching among them has no measurable cost.
- **Output scale**: plan outputs per host by the walls above (e.g. ~8 × 1080p60
  outputs per 1 GbE host, CPU-checked with `htop` during rehearsal), and add
  hosts for more simultaneous streams. Instances are independent processes, so
  the app itself has no shared bottleneck — the watchdog, playback API, and
  web UI stay responsive regardless of worker count.
- **One output, many videos** (the common case): use `/video/load/:media` to
  cue and `/video/play` to fire — switching is a hot-swap measured in
  milliseconds, so a single NDI output can serve an entire 540-clip playlist.

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR** — breaking API or config changes
- **MINOR** — new features, backward compatible
- **PATCH** — bug fixes

Current version is tracked in the `VERSION` file at the project root.

### Changelog

#### 1.10.0

**Overview stream: a built-in multiview of every output.**

- **One switch on the Overview tab** turns on a new NDI source,
  `MACHINE (Overview)`: every output in a real-time 1080p30 grid, grouped
  under a header per source type (Images, Video, Signage, Webpage, Text,
  Webcam) with each output's name under its tile. The tile on the Overview
  tab shows a live thumbnail and opens a full-size preview popup. The
  switch is remembered across restarts. See
  [Overview Stream](#overview-stream).
- **The layout follows your outputs live**, with no restart: new outputs
  appear, disabled ones disappear, stopped ones read STOPPED, and crashed
  ones read NO SIGNAL until the watchdog restores them.
- **Outputs stay independent.** The Overview runs in its own worker
  process and only receives. It has its own watchdog restarts and is left
  running by Stop All.
- **Local connections, no discovery needed.** Every worker now publishes
  its NDI sender port (`<preview folder>/<id>.ndi.json`). The Overview
  connects to `127.0.0.1:<port>`, checked against the owning process, and
  falls back to the NDI name.
- **Tile scaling is cheap.** Frames are halved with a 2× box filter, then
  given one linear step to size. That gives mipmap quality at about a tenth
  of the cost of a direct area resize, and 60fps sources only get scaled
  on the frames the 30fps Overview actually uses.
- New settings `OVERVIEW_WIDTH`, `OVERVIEW_HEIGHT`, `OVERVIEW_FPS` (30) and
  `OVERVIEW_BANDWIDTH` (`highest` | `lowest`); new API `/api/overview`
  (+ `/on`, `/off`, `/preview/stream`); new events `OVERVIEW_STARTED` and
  `OVERVIEW_STOPPED`.
- Output names: "Overview" is now reserved. The API also now rejects
  unknown `source_type` values.

#### 1.9.1

**Watchdog hardening.**

- **One bad restart can no longer disable crash recovery for every
  output.** Before, an exception while restarting a worker (e.g. a fork
  failing under memory pressure) killed the single watchdog thread. Running
  outputs kept streaming, but nothing was auto-restarted again until someone
  pressed Start. Now each instance is checked and restarted on its own, a
  failed respawn is retried on the next pass under the normal backoff, and
  the watchdog loop supervises itself and resumes after unexpected errors.
- **Self-re-arming.** `/api/system`, `/api/status` and `/api/health` restart
  the watchdog thread if it has died while outputs are running.
- **Start/idle race fixed.** When the last output stopped, the watchdog could
  be exiting at the same moment a new Start saw it as still alive, which
  left the new output unwatched. The exit decision is now made under the
  lifecycle lock.
- New `watchdog` object in `/api/status` and `/api/system`; new syslog events
  `INSTANCE_RESTART_FAILED`, `INSTANCE_KILL_FAILED`, `WATCHDOG_ERROR`,
  `WATCHDOG_REARMED`.

#### 1.9.0

**CPU graph, disk readout, security hardening.**

- **CPU graph and disk readout.** A CPU tile heads the Overview side
  column: the average across all cores on a fixed 0–100% graph of the
  last 5 minutes (hover for the value at any point), a bar per core, load
  average and RAM, with a "High load" flag at 85%+. CPU is sampled every
  2s by one background thread on the server (`GET /api/system`), so the
  graph is already full when a page opens and every viewer sees the same
  curve. Free space on the media drive shows on the Media Library tile
  and tab, flagged "Low space" under 10% free.
- **Security hardening** (from a full audit of the app):
  - **Cross-site request guard.** `POST`/`PUT`/`DELETE` requests that a
    browser marks as coming from another site (`Sec-Fetch-Site`, or a
    foreign `Origin`) get 403, so a web page elsewhere can't use a LAN
    browser to stop outputs or plant uploads. Show controllers send no such
    headers and are unaffected; the `GET` cue URLs stay open by design.
    New optional `ALLOWED_HOSTS` closes DNS rebinding.
  - **Uploads can't run as pages.** The served content type now comes from
    the file extension, never the uploading client (a spoofed `text/html`
    was served back as HTML), and media files carry
    `Content-Security-Policy: sandbox` + `nosniff`, which also defuses
    scripted SVGs.
  - **Webpage sources must be `http(s)://`.** `file://` URLs could render
    local files (`.env`, the database) onto the preview and NDI output;
    the API rejects them and the worker refuses any already saved.
  - **Resource limits.** Width/height (16–7680 × 16–4320), capture and
    output fps (1–120) and refresh interval are range-checked; images above
    `MAX_IMAGE_MEGAPIXELS` (100) are rejected, including decompression
    bombs; decks are cut at `MAX_DECK_PAGES` (300).
  - **Debugger can't be exposed.** `FLASK_ENV` is now actually read (it
    previously had no effect), defaults to `production`, and `development`
    is ignored unless `FLASK_HOST` is loopback. `.env.example` no longer
    ships `development`.
  - `/api/system` no longer returns filesystem paths; baseline
    `X-Frame-Options`, `Referrer-Policy` and `nosniff` headers on every
    response; `setup.sh` writes its build log to an unpredictable temp file.
  - Dependencies: Flask 3.1.3, Werkzeug 3.1.8, Pillow 11.3.0 (patch
    releases).
- **The edit dialog shows validation errors** instead of closing as if the
  save worked.
- **Docs**: configuration reference for every environment variable, API
  tables corrected (missing `video/load` route, `signage/status` is GET
  only, host-health rows moved to System), systemd limits, syslog events and
  project layout brought up to date.

#### 1.8.0

**Broadcast-console UI, Instances merged into Overview.**

- **Instances tab folded into Overview.** The home tab is now the one
  place to watch and run outputs: a KPI strip (outputs live, receivers,
  server egress, TCP media rate — the last three with ~5 min sparklines
  kept in the browser), the output cards, and a side column with On Air
  signage (current/next thumbnails + countdown bar) and the media library.
  Output FPS moved into the Outputs panel header. Old `#/instances`
  bookmarks land on the Overview.
- **Output cards instead of rows.** A compact grid of boxes: a 16:9 live
  screen with LIVE / PGM / PVW / RX overlays (click it to pop out the
  preview), resolution / fps / refresh chips, and a control strip. Cards
  on program or preview are framed red or green.
- **Stop and disable ask first.** Stopping an output (card, Signage tab,
  or Stop All) confirms, and names any output that is on program right
  now; disabling confirms and explains that a running output keeps
  running until stopped.
- **Broadcast-console styling.** Raised surfaces with lit top edges and
  deeper shadows, a visible aurora + grid backdrop, per-tile color tints,
  glowing tallies and pulsing live dots, machined buttons and toggles.
  Phone widths: the header wraps, tabs scroll, KPIs sit two-up.

#### 1.7.0

**Signage RAM preloading & caching, real-time status, receiver counts,
multi-user hardening, bigger UI.**

- **Signage items are preloaded into RAM before they go on air.** The
  worker builds the upcoming item's layer on a background thread ~6s before
  its transition: stills are fully decoded into a memory canvas, videos are
  opened (first frame decoded) with the file read ahead into the OS page
  cache (`posix_fadvise WILLNEED`). Transitions no longer touch the disk
  inside the send loop, so going on air can't drop frames on slow storage
  or large images. If the schedule changes between preload and transition,
  the worker falls back to the previous inline load. Slots shorter than
  the lead are safe: the preload guard builds each upcoming item exactly
  once, however often the loop checks.
- **Stills live in RAM across rotations.** Decoded, letterboxed canvases
  are kept in an LRU cache (`SIGNAGE_STILL_CACHE_MB`, default 256) keyed by
  file mtime — an image that has played once is never read from disk again
  until the file changes or the budget evicts it.
- **Constant small writes moved off the SSD.** Preview JPEGs (rewritten up
  to every 2s per running instance) and the signage now-playing status
  (1 write/s) now default to tmpfs (`/dev/shm`) on Linux, so they land in
  RAM. Playlist JSON and impression logs stay on disk — they must survive
  a reboot. Overridable via `PREVIEW_FOLDER` / `SIGNAGE_RUNTIME_FOLDER`;
  non-Linux platforms fall back to the previous app-folder paths.
- **Real-time now-playing updates (Server-Sent Events).** New
  `/api/instances/:ref/signage/events` endpoint streams the status payload
  the moment it changes — the worker writes its status file the same frame
  a transition starts, so the green on-air highlight and Now Playing text
  move within ~200ms instead of a 2s poll. The UI falls back to polling
  automatically if the stream drops, closes the stream on hidden tabs, and
  `EventSource` reconnects on its own. SSE was chosen over WebSockets
  because the flow is one-directional (commands stay on plain HTTP) and it
  needs no extra dependencies or proxy configuration.
- **Multi-user hardening.** SQLite now runs in WAL mode with a 5s busy
  timeout — several people can use the UI at once (each browser polls and
  edits) without "database is locked" errors; readers no longer block the
  writer. No-op when running on PostgreSQL.
- **NDI receiver count + tally.** Each worker polls the NDI SDK once a
  second for how many receivers are connected to its sender
  (`send_get_no_connections`) and the downstream tally state
  (`send_get_tally`). Instance cards show `◉ n RX` with red `PGM` / green
  `PVW` tally badges, the header shows the total across all sources, and
  the Signage live bar shows the count for its output. The API exposes it
  as an `ndi` object (`receivers`, `on_program`, `on_preview`) on
  `/api/instances`; `receivers` is `null` when the SDK can't report it
  (dummy mode). The count covers every transport — each NDI receiver keeps
  a reliable control connection open even when video travels over UDP or
  multicast — but the SDK does not expose per-receiver identity or which
  transport each one negotiated.
- **Receiver identification (hostnames & IPs).** The SDK says how many;
  the OS knows who. Clicking any `RX` badge (or the Signage live bar's
  Receivers count) opens a popup listing each receiver's IP address,
  reverse-DNS hostname, and connection count, read from the worker
  process's established TCP sockets via `psutil` (new dependency) — this
  sees every receiver, since the NDI control connection is TCP whatever
  the video transport. Hostname lookups are cached and run in the
  background so the API never blocks on slow DNS. Also available as
  `GET /api/instances/:ref/receivers` for automation.
- **Process hygiene: nothing outlives the app.** Three cleanup guarantees
  added: (1) workers self-terminate (clean teardown: browser closed, NDI
  destroyed) within ~5s of their manager process vanishing — even after a
  SIGKILL, where no parent cleanup can run; (2) the app registers
  atexit + SIGTERM handlers that stop every worker and its Chromium tree
  on any exit path (systemd's cgroup kill already covered services; this
  covers manual runs, NSSM and other supervisors); (3) LibreOffice
  conversions run in their own process group that is killed whole on
  timeout — previously a timeout killed only the `soffice` wrapper,
  orphaning `soffice.bin`, which holds the profile lock and breaks every
  later conversion. All other helper tools (`ffmpeg`, `ffprobe`,
  `pdftoppm`, `ss`) are single processes already reaped by their timeouts.
- **Per-receiver transport + bitrate.** Presence alone can't tell TCP
  media from UDP/multicast media (the control connection is TCP either
  way), but throughput can: the server samples each connection's
  `bytes_acked` (via `ss`, Linux) between polls and shows a live TCP rate
  per receiver — megabits flowing over the socket means TCP transport, a
  near-idle control connection means the video travels as UDP or
  multicast. The receivers popup shows both; the API adds `transport`
  (`tcp` / `udp-multicast` / `measuring` / `unknown`) and `mbps` per
  receiver. Rates need two samples, so the first poll reports
  `measuring`; platforms without `ss` report `unknown`.
- **lit-html adopted for refresh-heavy views** (vendored in
  `app/static/lit/` — ~10KB, BSD-3, no CDN, works air-gapped, no build
  step). The receivers popup converted first: its body renders through lit
  with rows keyed by IP, so the 3s auto-refresh updates cells **in place**
  — the modal element is never rebuilt, scroll position and row identity
  survive, and churn moves rows instead of recreating them. Falls back to
  the old full-rebuild rendering if the module fails to load.
- **Signage playlist converted to lit keyed rendering.** Rows (items and
  group headers) are keyed by id and rendered into a stable container:
  polls, enable toggles, drag-reorders, select-all and group collapse all
  update the list **in place** — row DOM identity and your scroll survive,
  and a reorder physically moves the existing row elements. The playlist
  also gained its own change signature: when only signage data changed
  (impressions ticking up each slot, edits from another session), the list
  refreshes without rebuilding the page at all — previously every aired
  slot forced a full re-render for everyone on the Signage tab.
- **New Overview home tab (bento grid).** The default landing tab is now
  an at-a-glance wall of modular tiles: Outputs (live count + per-instance
  health rows), On Air (now playing / countdown / up next per running
  signage output), Receivers, Server Egress, TCP Media Rate, and Media
  Library totals. Tiles refresh in place every 3s via lit and click
  through to their sections (bandwidth tiles open the receivers popup).
- **2026 UI freshening pass** (CSS only, honors
  `prefers-reduced-motion`): micro-interactions — buttons lift on hover
  and settle on press, cards elevate, inputs glow on focus, visible
  `:focus-visible` rings for keyboard users; sticky frosted tab bar and
  blurred modal backdrops (layered-glass depth cues); a subtle fixed
  aurora gradient behind the surfaces; fluid `clamp()` type on the logo,
  section titles and live stats; tabular numerals on countdowns; thin
  theme-matched scrollbars.
- **Fleet-wide bandwidth view.** The header's `RX` pill is clickable:
  every connection across all running sources in one popup, with three
  headline numbers — summed per-receiver TCP rate, unique receiver /
  connection totals, and **server egress** (NIC counters, loopback
  excluded), the ground-truth bandwidth actually leaving the box, which
  also carries UDP/multicast media the TCP sum can't see. Also at
  `GET /api/receivers` for dashboards and monitoring.
- **Full-page upload progress.** Uploads now open a full-screen overlay
  with large per-file progress bars (% sent, then a processing pulse while
  the server probes/converts, then done/error) and an m-of-n summary. A
  *Hide* button drops it to the previous bottom-right mini panel; uploads
  continue either way.
- **On-air highlight in the Signage playlist.** The now-playing preview
  image is gone from the Signage tab; instead the item currently on air is
  highlighted green in the playlist with an `ON AIR` badge (its group
  header too), updated in real time from the event stream. The slimmer
  live bar keeps Now Playing / countdown / Up Next and the transport
  buttons.
- **Larger UI text across the board** — instance FPS/refresh/resolution
  readouts, playlist rows, media cards, filter chips, tabs, section titles
  and form hints all stepped up for readability on production monitors.

#### 1.6.2

- Media-type indicators are now plain-text chips instead of emoji: `VID` /
  `IMG` badges in front of file names (library cards and playlist rows),
  `GRP` on group rows, `Deck:` labels for converted presentations,
  `Library upload` / `Signage upload` origin lines, and `n views` for
  impressions. Text can't fall through to a wrong font, so the stray
  CJK-looking glyphs some machines showed are gone for good.

#### 1.6.1

- **Fixed icons rendering as CJK-looking glyphs on some machines.** The
  media/deck/group icons used pictographs from a Unicode block without
  default emoji presentation (🖼 U+1F5BC, 🖽 U+1F5BD, 🗂 U+1F5C2, 🎞
  U+1F39E); browsers missing a text glyph for those fall back to whatever
  font covers the codepoint — often a CJK font — producing what looks like
  a Chinese character before file names. Replaced with universally
  supported emoji (🎬 for video, 📑 for decks) and forced emoji
  presentation (U+FE0F) on the rest.

#### 1.6.0

**Reliable thumbnails + media downloads.**

- **Server-generated poster thumbnails.** Library cards, media pickers, and
  signage playlist rows previously embedded a full `<video>` element per
  video (fetch metadata, seek to 0.1s) — browsers cap concurrent media
  decoders, so with several videos some thumbnails never rendered. The
  server now generates a poster JPEG per file (one decoded frame ~0.5s in
  for videos, a downscale for images), served from
  `GET /api/media/:id/thumb` with day-long caching, generated eagerly on
  upload and lazily on first request for pre-existing files. Every
  thumbnail in the UI is now a plain `<img>`; video cards get a ▶ glyph.
- **Downloads.** `GET /api/media/:id/download` returns the original upload
  as an attachment under its real filename; `?optimized=1` downloads the
  transcoded playback copy as `<name> (optimized).mp4`. In the UI: ⬇
  (original) and ⬇⚡ (optimized, when present) buttons on every media card,
  and both download buttons in the signage item settings modal.
- Thumbnails are cleaned up with their media; thumbnail generation prefers
  the optimized copy (faster to open) when one exists.
- **Snappier interactions.** Frequently-clicked controls (start/stop,
  enable toggles, video play/stop, signage skip, item/group toggles, drag
  reorder) now update the UI optimistically — local state flips and the
  page re-renders immediately, with the API call and a background refresh
  reconciling afterwards — instead of waiting out a full request round
  trip per click. The signage playlist is also fetched in the same
  parallel batch as the rest of the page data.
- **Virtual pages.** The tab and signage selection now live in the URL
  (`#/instances`, `#/signage/3`, `#/media`) — refreshing or bookmarking
  keeps your place instead of dumping you back on Instances, and
  back/forward navigate between tabs.
- **No render-blocking external requests.** Web fonts load asynchronously
  with system-font fallbacks, so on an isolated/air-gapped network the UI
  renders immediately instead of stalling on an unreachable CDN. (All
  JS/CSS is already inline — fonts were the only external fetch.)

#### 1.5.0

**Optimize everything** — every video is checked and converted unless it's
already perfect.

- Default `VIDEO_OPTIMIZE` mode is now `all`: every uploaded video is
  probed (ffprobe) and, unless it already matches the ideal playback
  format — H.264 + 4:2:0 in an MP4-family container, within the target
  box — it's converted in the background. Already-perfect files are marked
  **✓ playback-ready** without a wasted re-encode. Originals are always
  kept.
- **Boot-time library sweep**: videos never checked (uploaded before this
  feature, or while it was off) are queued automatically on service start.
- Without ffmpeg installed, files are still classified (playback-ready vs
  needs-conversion) so the library shows what would benefit.

#### 1.4.0

**4K playback** — faster native decode plus a built-in background converter.

- **Multithreaded + hardware decode in the workers.** OpenCV's FFmpeg
  capture decoded on a single thread; workers now request `threads=auto`
  (override via `OPENCV_FFMPEG_CAPTURE_OPTIONS`) and hardware-accelerated
  decode (VAAPI/QSV/NVDEC, automatic software fallback) — native 4K is
  viable on capable machines.
- **Background video optimizer.** Uploads larger than the target box
  (default 1920×1080) are transcoded once, in the background, into the
  cheapest-to-decode playback format: H.264/yuv420p MP4, `-tune
  fastdecode`, audio stripped, `faststart`, sized to the box (per-frame
  resize in the worker disappears too). One `nice`d ffmpeg at a time so
  live outputs keep priority; never realtime transcoding during playback.
- Originals stay on disk untouched (`/api/media/:id/file?original=1`);
  everything else — workers, signage playlists, video play/load commands,
  browser previews — uses the optimized copy automatically. Signage
  players hot-reload onto it when the transcode finishes; video instances
  pick it up on their next play/load/start.
- Modes via `VIDEO_OPTIMIZE`: `oversized` (default), `all` (normalize
  every upload to the standard playback format), `off`. Target box,
  CRF and preset configurable in `.env`.
- Media cards show the optimization state (⚙ optimizing / ⚡ optimized →
  1920×1080 / ⚠ failed / ⚠ ffmpeg missing) and an ⚡ button to queue
  eligible videos manually — including files uploaded before 1.4.0. New
  endpoint: `POST /api/media/:id/optimize`. Interrupted transcodes
  re-queue automatically on service restart.

#### 1.3.0

**Media Library filters** — find files by how they arrived and where they're
used.

- Files now record their **origin**: Media Library upload, Signage tab
  upload, or deck slide (with the source deck's filename). Existing files
  auto-migrate and count as Library uploads.
- **Filter bar** on the Media Library tab: Type (images/videos), Source
  (library / signage upload / deck slides), and Usage (in a signage group /
  in any playlist / unused). Chip counts reflect the other active filters,
  so they always show what clicking would display.
- Filtering to **Deck slides** switches to a per-deck view — one section
  per source presentation with its slides in order.
- Cards show an origin badge and their playlist usage
  (`Instance › Group`), alongside the existing "source for instance" line;
  the media listing API now returns `origin`, `origin_name`, and
  `signage_usage` per file.

#### 1.2.0

**Signage timing fix + bulk editing + server clock.**

- **Fixed: still images never held with a long crossfade.** Fade timing was
  anchored the same way for stills and videos (fade-out began at
  `duration − crossfade` measured from first visibility), so a 5s still
  with a 3s fade chained its fade-in straight into its fade-out and never
  held. Stills now hold for their full `duration` before the outgoing fade
  starts (5s + 3s fade = 3s fade-in, ~2s clean hold, 3s fade-out). Videos
  keep the previous behavior — the fade overlaps the file's tail so it's
  still moving as it dissolves.
- **Select All / bulk edit** — select every item (or tick rows) and apply
  duration, crossfade, start/expire, daily window, or enabled/disabled to
  all of them at once (new `POST .../signage/items/update` batch endpoint,
  one playlist reload for the whole change).
- **Multi-drag** — dragging any selected row moves the entire selection as
  a block (keeping its order): reorder together, drop into a group
  together, or drag out of groups together.
- **Schedule state at a glance** — playlist rows are tinted red-ish when an
  item can no longer play (expired or disabled, with an EXPIRED/DISABLED
  tag) and grey when it hasn't started yet (UPCOMING). Computed against
  the server's clock, not the browser's.
- **Server clock in the header** — live date/time/timezone readout of the
  clock schedules actually run on, ticked every second and synced from the
  API, so a wrong system clock is immediately visible.
- **Readable date/time pickers** — the app now declares `color-scheme:
  dark`, so the native calendar/time popups and their icons render in
  their dark variants instead of dark-on-dark.

#### 1.1.0

**Live preview popups** — pop any output into its own window for confidence
monitoring.

- **Popup viewer** at `/preview/:id` — instance name, resolution and
  live/stopped state, the live picture, auto-reconnect when the instance
  restarts, and a now-playing / up-next footer for signage (playback state
  for video sources). Opened from the UI by clicking any preview thumbnail
  or the new ⧉ button on instance cards and the signage live panel.
- **MJPEG preview stream** — `GET /api/instances/:ref/preview/stream`
  (instance by id or name) pushes a new JPEG whenever the worker saves one
  (`multipart/x-mixed-replace`, works in a plain `<img>` tag, so it can be
  embedded in dashboards too).
- **Automatic preview boost** — while at least one stream is connected the
  worker saves previews at 854px @ ~4fps instead of the 320px / 2s list
  thumbnails, then drops back automatically a few seconds after the last
  viewer disconnects. Boost is requested through a shared deadline value,
  works for every source type, and overlapping viewers extend rather than
  shorten each other.
- Static content (held video frame, single still) re-sends the last frame
  every 2s as a stream keepalive, so closed popups are detected promptly
  and reverse proxies don't time the stream out. Streams end gracefully
  ~5s after an instance stops and are capped at 4h (the popup reconnects).

#### 1.0.1

**Digital signage** — a sixth source type that turns an output into a
scheduled signage player, plus a friendlier upload experience.

- **New `signage` source type** — plays a playlist of stills and videos as a
  single uninterrupted NDI stream, rendered entirely in the worker process
  (OpenCV, no browser). When nothing is scheduled the output holds black.
- **Crossfade transitions** — per-item crossfade duration, alpha-blended per
  output frame; for videos the fade into the next item starts *before* the
  file ends (`duration − crossfade`), so motion dissolves into the next
  item. `0` = hard cut. The incoming item starts playing the moment it
  becomes visible.
- **Scheduling** — per item and per group: go-live datetime, expiry
  datetime, and a daily time-of-day window (overnight ranges wrap past
  midnight). Out-of-window content is skipped; expiry mid-item transitions
  out gracefully. All times are server-local wall-clock.
- **Groups** — bundle items into one unit for ordering, scheduling,
  transitions, enable/disable, and deletion (with an "ungroup, keep items"
  option). Group date windows intersect with item windows; group
  duration/crossfade/daily-window act as inheritable defaults.
- **PowerPoint / PDF deck upload** — .ppt/.pptx/.odp/.pdf uploaded into a
  playlist is rasterized (LibreOffice → PDF → `pdftoppm`) into one image per
  slide and appended as a ready-made group named after the file. Requires
  `libreoffice-impress` + `poppler-utils`; a clear error is returned when
  they're missing. Render DPI configurable via `PRESENTATION_RENDER_DPI`.
- **Impression counters** — each item counts how many times it went on air.
  The worker appends to a per-instance log (atomic rotation, no lost
  counts) that the API folds into the database.
- **Live control** — new **Signage** tab with drag-to-reorder playlist,
  multi-select group/delete, per-item/group settings modals, a live
  now-playing bar (name, countdown, up-next) and a **Skip Next** button.
  The item currently on air is highlighted green in the playlist (with an
  `ON AIR` badge, also on its group header); the popup preview window gives
  the visual check. Skip and status are also plain-URL show-control
  endpoints (`/api/instances/<id-or-name>/signage/skip`,
  `.../signage/status`).
- **Hot playlist reload** — every playlist mutation (add, edit, reorder,
  schedule change, media delete) is pushed to a running worker via a shared
  command channel and picked up within a second — no restart, the NDI
  stream never drops. Watchdog restarts resume with the current playlist.
- **Upload progress readouts** — uploads now go through XHR with a per-file
  progress panel: % uploaded, a "processing" state while the server
  probes/converts, done/error status with server error messages, for both
  the Media Library and Signage upload zones.
- New DB tables `signage_items` and `signage_groups` (auto-created), new
  nullable `output_instances` columns `signage_duration` /
  `signage_crossfade` (auto-migrated). Signage runtime state lives in
  `app/signage_state/` (git-ignored) and is cleaned up on instance delete.

#### 0.4.0

Playback API round two — media switching, cueing, and permanent media ids:

- **Permanent media uids:** every uploaded file gets a short `uid` assigned
  once and never reused. Numeric ids never shift for existing files, but
  SQLite can recycle a deleted file's id for the next upload — uids can't be
  recycled, so controller cues keyed on them stay correct forever. Existing
  libraries are backfilled automatically at startup, and the media cards in
  the web UI show both ids
- **Switch-and-play:** `GET`/`POST` `/api/instances/:ref/video/play/:media`
  (or `?media=`) loads a different video into a running output and plays it —
  media addressable by numeric id, permanent uid, or original filename
- **Hot-swap loading:** switching videos happens inside the running worker
  process — no restart, no NDI sender teardown, receivers never see a drop.
  The new file is verified before it replaces the old one; a bad file leaves
  current playback untouched
- **Cue support:** `/api/instances/:ref/video/load/:media` pre-loads a video
  and holds its first frame on air, so the following `play` fires with zero
  load latency — built for tight show cues
- **Hold control from the API:** `?hold=first|last` on `play`, `load`, and
  `stop` chooses which frame stays on air, applied live to the running worker
  and persisted on the instance
- `/video/status` now reports the loaded media (id, uid, name, duration)
- A media switch or hold change survives worker restarts and watchdog
  recoveries — the stored worker config and the DB are kept in sync
- Stress-tested and verified leak-free at worst-case scale: 540-file library,
  200 hot-swaps at 18/s on a live output (no restarts, flat memory), and
  sustained concurrent 1080p playback workers with flat per-worker memory —
  measured capacity numbers documented under *Performance Notes → Scaling &
  worst-case capacity*

#### 0.3.2

Memory/process-leak audit of the whole app. Verified: no external ffmpeg
processes exist (video decodes in-process via OpenCV/libavcodec and is released
on worker exit), NDI senders are destroyed on every exit path, frame buffers
stay pre-allocated, and repeated `/video/play` calls cannot double-spawn
workers. Fixed what the audit found:

- **Orphaned Chromium fix:** workers are now process-group leaders and force-kill
  escalation kills the whole group — a hung worker can no longer leave its
  Playwright driver + Chromium tree running forever (previously leaked
  ~200–500 MB per hard-killed worker)
- **Restart backoff:** the watchdog now rate-limits per-instance restarts with
  exponential backoff (5s doubling to a 5-minute cap, reset after a 2-minute
  stable run) instead of respawning a permanently-broken instance every 5s;
  chronic flappers log `INSTANCE_RESTART_BACKOFF`
- **Lifecycle race fix:** start/stop/restart are now serialized with a lock —
  concurrent start requests (e.g. a double-clicked button) could previously
  spawn two workers and orphan one untracked
- Deleting a media file now stops any running instance playing it, so no worker
  keeps an open handle to a deleted file (which kept the disk space claimed)
- Video worker stops playback cleanly if the file becomes unreadable mid-loop
  instead of spinning on reopen attempts
- A browser that fails partway through launch (context/page creation) is closed
  instead of lingering until process exit
- Preview thumbnails are only written when the frame actually changed — a video
  holding a still frame no longer rewrites an identical JPEG every 2 seconds
- Web UI polling pauses in hidden tabs (resumes instantly on focus) and preview
  fetches can no longer pile up on slow networks

#### 0.3.1

- Web UI: instance cards now show the instance ID as a badge next to the name,
  so the id used in API URLs (`/api/instances/:id/...`) is visible at a glance
- The remote-control hint in the instance editor now shows the id-based URL
  (ids avoid URL-encoding names with spaces; names still work if preferred)

#### 0.3.0

- Video file source type: upload a video to the media library and stream it as an
  NDI output, decoded natively with OpenCV/FFmpeg (no browser)
- Playback modes: play once or loop; configurable hold frame (last or first) stays
  on air while stopped or after a play-once video ends, so the stream never goes blank
- Optional autoplay when the instance starts
- HTTP playback control on the same port as the web UI:
  `GET`/`POST` `/api/instances/:ref/video/play`, `/video/stop`, and
  `GET` `/video/status` — instances addressable by id or name, `play` auto-starts
  the NDI output, built for show controllers (Companion, Crestron, QLab)
- Media library accepts video uploads (mp4, mov, m4v, mkv, webm, avi, mpg, mpeg),
  probes duration/resolution, and shows video thumbnails; default upload cap raised
  to 500 MB (`MAX_UPLOAD_SIZE_MB`)
- Web UI: video settings in the instance editor (library picker, playback mode,
  hold frame, autoplay, ready-to-copy control URLs), play/stop buttons and live
  PLAYING/HOLDING state on instance cards
- Web UI flicker fix: preview thumbnails are now fetched and fully decoded in the
  background, then swapped in place — the page also only re-renders when data
  actually changes, so images no longer flash on the 5-second poll
- Automatic lightweight DB migration on startup adds the new columns to existing
  SQLite/PostgreSQL databases — no manual migration needed

#### 0.2.0

- Webcam source type: auto-detects connected V4L2 cameras (`GET /api/webcams`) and
  streams them as NDI outputs at camera-native frame rates, bypassing the browser
- Webcam device picker in the instance editor
- Automatic camera reconnect if a webcam stalls or is unplugged (last frame keeps streaming)
- Stable camera identity: instances store the udev stable ID (`/dev/v4l/by-id`, serial-based;
  `/dev/v4l/by-path`, port-based fallback) and resolve it on every reconnect, so the right
  camera reattaches to the right output even if it re-enumerates as a different `/dev/videoN`
- Memory/cleanup audit: preview thumbnails are now deleted with their instance;
  webcam resize path reuses a pre-allocated buffer instead of allocating per frame
- Code deduplication: browser recycle/refresh, Playwright teardown, NDI cleanup, and
  worker process spawning are now shared functions between the main, dummy, and
  watchdog-restart paths (dummy-mode auto-refresh now also uses the cheaper
  `page.reload()` instead of a full navigation, matching the main loop)

#### 0.1.0 (Initial Release)

- Core NDI streaming with Playwright capture
- Multi-instance management with per-instance settings
- Custom NDI hostname and source naming
- Decoupled capture/output FPS with frame duplication
- Auto-refresh per instance
- Media library with upload, manage, assign
- Syslog integration with structured event logging
- Crash recovery watchdog with automatic restart
- Hang detection via shared heartbeat with 30s timeout
- Browser recycling every 4h (configurable) to prevent Chromium memory leaks
- Pre-allocated frame buffers for stable long-term memory usage
- Health endpoint (`/api/health`) for external monitoring
- Systemd service with security hardening
- Windows support (NSSM service, Task Scheduler)
- SQLite with PostgreSQL migration path
- Web management UI

---

## Development

### Local setup (Linux)

```bash
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python run.py
```

### Local setup (Windows)

```powershell
git clone https://github.com/showsysdan/webretriever2.git
cd webretriever2
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
python run.py
```

### Database migrations

```bash
# After model changes (same on both platforms):
flask db migrate -m "Description of change"
flask db upgrade
```

### Project structure

```
ndi-streamer/
├── VERSION                 # Semantic version
├── .env.example            # Config template (tracked)
├── .gitignore              # Ignores .env, venv, uploads, db
├── requirements.txt        # Python dependencies
├── setup.sh                # Install script (Linux)
├── run.py                  # Entry point
├── ndi-streamer.service    # Systemd unit file (Linux)
├── app/
│   ├── __init__.py         # Flask app factory
│   ├── config.py           # Configuration from .env
│   ├── logging_config.py   # Console + syslog logging
│   ├── models/
│   │   └── __init__.py     # SQLAlchemy models
│   ├── routes/
│   │   └── __init__.py     # REST API endpoints
│   ├── sysstats.py         # CPU / memory / disk sampler for /api/system
│   ├── transcode.py        # Background video optimizer (ffmpeg)
│   ├── workers/
│   │   ├── __init__.py     # Worker manager + watchdog
│   │   ├── ndi_worker.py   # Playwright / video / signage + NDI worker process
│   │   ├── multiview.py    # Overview stream: layout, tile receivers, compositor
│   │   └── webcam_utils.py # V4L2 camera discovery + stable IDs
│   ├── static/
│   │   ├── index.html      # Web management UI
│   │   ├── preview.html    # Popup live-preview window
│   │   └── lit/            # Vendored lit-html (no CDN)
│   ├── thumbs/             # Generated media poster thumbnails
│   ├── signage_state/      # Generated playlist files + impression logs
│   └── uploads/            # Media library storage
│       └── .gitkeep
└── migrations/             # Alembic migrations (after first migrate)
```

---

## Troubleshooting

### Linux

**NDI streams not visible on network**
- Verify NDI SDK is installed: `ls /usr/share/ndi/lib/libndi.so*`
- Check `LD_LIBRARY_PATH` includes the NDI lib directory
- Ensure no firewall is blocking mDNS (port 5353) or NDI traffic (TCP 5960+)

**Playwright fails to launch**
- Run `playwright install-deps chromium` to install system dependencies (Linux only — this installs required apt packages; Windows/macOS bundles them automatically)
- If running as a service, ensure the service user has access to the browser binaries

**High CPU usage**
- Lower `capture_fps` on instances with static or slow-changing content
- Use 720p instead of 1080p where possible
- Monitor with: `htop -p $(pgrep -d, -f ndi-worker)`

**Database locked (SQLite)**
- SQLite doesn't handle heavy concurrent writes well
- If running many instances, switch to PostgreSQL

**Uploads not working**
- Check `UPLOAD_FOLDER` path exists and is writable
- Check `MAX_UPLOAD_SIZE_MB` isn't too low
- Ensure the service user owns the uploads directory

### Windows

**`ModuleNotFoundError: No module named 'NDIlib'`**
- Ensure the NDI SDK is installed and the runtime DLLs are on the system PATH
- Restart your terminal/PowerShell after installing the NDI SDK
- Verify: `where.exe ndi-*` should return a path, or check `C:\Program Files\NDI\`
- If using a venv, make sure you activated it: `venv\Scripts\activate`

**`venv\Scripts\activate` — "running scripts is disabled on this system"**
- PowerShell's default execution policy blocks activation scripts. Fix with either:
  - Allow scripts for the current session only: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process` then retry `venv\Scripts\activate`
  - Or use Command Prompt instead: `venv\Scripts\activate.bat`

**`playwright._impl._errors.Error: Executable doesn't exist`**
- Run `playwright install chromium` inside the activated venv
- If behind a corporate proxy, set `HTTPS_PROXY` before running the install

**Workers fail to start / `OSError: [WinError 87]`**
- Python on Windows uses `spawn` for multiprocessing. Ensure `run.py` has the `if __name__ == "__main__"` guard (it does).
- Antivirus software can block Chromium from launching. Add an exclusion for the `venv\Lib\site-packages\playwright\` directory.

**Firewall blocking NDI**
- Windows Defender Firewall may block NDI traffic. Add inbound/outbound rules for:
  - TCP ports 5960+ (NDI uses a range starting here)
  - UDP port 5353 (mDNS discovery)
- Or allow `python.exe` from your venv through the firewall entirely.

**High memory on Windows**
- Task Manager → Details tab → sort by Memory to see per-worker usage
- Same mitigation as Linux: lower `capture_fps`, enable browser recycling, use 720p
- Monitor: `Get-Process python | Select-Object Id, WorkingSet64 | Format-Table`

**NSSM service won't start**
- Check the log file path exists (create `logs\` directory in the project folder)
- Verify paths in `nssm edit NDIStreamer` use full absolute paths
- Test manually first: `venv\Scripts\python.exe run.py` — fix any errors before wrapping in NSSM
