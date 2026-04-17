# NDI Streamer

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)]()
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)]()
[![License](https://img.shields.io/badge/license-MIT-gray.svg)]()

A self-hosted Flask application that captures webpages, images, or text via headless Chromium and outputs them as NDI video streams on your network.

---

## Features

- **Multiple NDI output instances** — each with its own stream name, resolution, and capture rate
- **Three source types** — webpage URL, uploaded image, or custom styled text
- **Custom NDI naming** — fully configurable hostname + per-instance stream name (e.g. `PRODUCTION (Lower Third)`)
- **Decoupled FPS** — capture at any rate (e.g. 15fps for a weather radar), NDI always outputs at the global rate (60fps) by duplicating frames
- **Auto-refresh** — per-instance configurable interval to reload content (e.g. refresh a weather page every 30 minutes)
- **Media library** — upload, manage, and assign images to instances via a built-in file manager
- **Crash recovery** — watchdog automatically restarts crashed worker processes
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
14. [API Reference](#api-reference)
15. [Performance Notes](#performance-notes)
16. [Versioning](#versioning)
17. [Development](#development)
18. [Troubleshooting](#troubleshooting)

---

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| **OS** | Ubuntu 22.04+ / Windows 10+ | Linux recommended for production; Windows fully supported |
| **Python** | 3.10+ | 3.11 or 3.12 recommended. Windows: [python.org](https://www.python.org/downloads/) (check "Add to PATH") |
| **NDI SDK** | 5.x or 6.x | Free runtime from [ndi.video](https://ndi.video/tools/ndi-sdk/) |
| **Chromium** | (auto-installed) | Managed by Playwright |
| **Git** | Any | Windows: [git-scm.com](https://git-scm.com/download/win) |

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
- Set `FLASK_ENV=production` (disables the debugger / auto-reloader).
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
# This is the hostname prefix for all NDI sources.
# Streams appear as: NDI_HOSTNAME (Instance Name)
NDI_HOSTNAME=NDI-STREAMER

# Global NDI output frame rate (all senders output at this rate)
NDI_OUTPUT_FPS=60
```

### Flask

```env
SECRET_KEY=change-me-to-a-random-string
FLASK_ENV=production
FLASK_HOST=0.0.0.0            # bind address. Set to 127.0.0.1 when behind nginx.
FLASK_PORT=5000
```

> **Security:** the app has no built-in authentication. Anyone who can reach
> `FLASK_HOST:FLASK_PORT` can create, start, stop, and delete instances.
> Restrict access with a firewall (see [Firewall](#firewall)), put it behind
> nginx with basic auth (see [Reverse proxy with TLS](#reverse-proxy-with-tls)),
> or set `FLASK_HOST=127.0.0.1` so it's only reachable from the local machine.

> **SECRET_KEY:** defaults to `dev-secret-key` if unset. The app logs a warning
> on startup when it sees the default. Always generate a fresh one for
> production: `python3 -c 'import secrets; print(secrets.token_hex(32))'`.

### Uploads

```env
UPLOAD_FOLDER=app/uploads
MAX_UPLOAD_SIZE_MB=50
```

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
| `MemoryMax=4G` | Cgroup-enforced cap; raise for big multi-instance setups |
| `TasksMax=4096` | Caps total threads/processes |

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
│  └────┬────────┬────────┬───────────────┘ │
└───────┼────────┼────────┼─────────────────┘
        │        │        │
   ┌────▼──┐ ┌──▼────┐ ┌─▼─────┐
   │Worker1│ │Worker2│ │WorkerN│   ← Separate processes
   │Playw. │ │Playw. │ │Playw. │   ← Headless Chromium
   │ NDI ──│►│ NDI ──│►│ NDI ──│►  ← NDI senders
   └───────┘ └───────┘ └───────┘
```

- Each instance runs in an isolated **process** (not thread) — a crash in one does not affect others
- The **watchdog thread** monitors processes every 5 seconds and auto-restarts any that crash
- Playwright captures at the **instance capture FPS**; NDI sends at the **global output FPS** by duplicating frames
- **Auto-refresh** reloads the page/content at a configurable interval per instance

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

1. The process is terminated (SIGTERM → SIGKILL if needed)
2. A new worker is launched with the same configuration
3. `INSTANCE_UNHEALTHY` and `INSTANCE_RESTARTED` events are logged to syslog

```
Apr 10 03:42:15 prod ndi-streamer: [WARNING] [INSTANCE_UNHEALTHY] id=3 reason=hung (heartbeat stale 34s)
Apr 10 03:42:17 prod ndi-streamer: [INFO] [INSTANCE_RESTARTED] id=3 reason=hung new_pid=51203
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

## API Reference

### Global Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/settings` | Get global settings |
| `PUT` | `/api/settings` | Update hostname / output FPS |

### Global Controls

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/start-all` | Start all enabled instances |
| `POST` | `/api/stop-all` | Stop all running instances |

### Instances

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/instances` | List all instances |
| `POST` | `/api/instances` | Create instance |
| `GET` | `/api/instances/:id` | Get instance |
| `PUT` | `/api/instances/:id` | Update instance |
| `DELETE` | `/api/instances/:id` | Delete instance |
| `POST` | `/api/instances/:id/start` | Start instance |
| `POST` | `/api/instances/:id/stop` | Stop instance |
| `POST` | `/api/instances/:id/refresh` | Reload content |

### Media Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/media` | List all uploaded files |
| `POST` | `/api/media` | Upload a file (multipart) |
| `GET` | `/api/media/:id` | Get file metadata |
| `GET` | `/api/media/:id/file` | Serve the actual file |
| `DELETE` | `/api/media/:id` | Delete file (unlinks from instances) |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Running count, totals |
| `GET` | `/api/health` | Per-instance health with heartbeat age |

---

## Performance Notes

| Scenario | CPU per instance | Notes |
|----------|-----------------|-------|
| 1080p @ 30fps capture | Moderate | Good for most use cases |
| 720p @ 60fps capture | Light | Complex pages still fast |
| 1080p static content, 5fps capture | Very light | NDI still outputs 60fps |
| Text overlay, 15fps capture | Minimal | Simple HTML rendering |

- Each instance is an isolated process — scales across CPU cores
- For mostly-static content (images, text), set capture FPS low (5–15) to save CPU
- Auto-refresh causes a brief content reload; the last frame continues sending during reload
- The NDI SDK's `clock_video` option paces output to real-time

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR** — breaking API or config changes
- **MINOR** — new features, backward compatible
- **PATCH** — bug fixes

Current version is tracked in the `VERSION` file at the project root.

### Changelog

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
│   ├── workers/
│   │   ├── __init__.py     # Worker manager + watchdog
│   │   └── ndi_worker.py   # Playwright + NDI worker process
│   ├── static/
│   │   └── index.html      # Web management UI
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
