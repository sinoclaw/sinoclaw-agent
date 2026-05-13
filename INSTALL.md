# Sinoclaw Agent — Installation Guide

This document covers every supported way to install Sinoclaw Agent. For the **fastest path**, see the one-liners in [README.md](README.md). This guide is for users who want more control: choosing between pip / Docker / source, running on specific operating systems, or deploying as a long-running service.

## Table of Contents

- [Choosing an Install Method](#choosing-an-install-method)
- [Method 1 — One-liner Installer (Recommended)](#method-1--one-liner-installer-recommended)
- [Method 2 — pip / pipx (PyPI)](#method-2--pip--pipx-pypi)
- [Method 3 — Docker / docker-compose](#method-3--docker--docker-compose)
- [Method 4 — From Source (Developers)](#method-4--from-source-developers)
- [Operating System Notes](#operating-system-notes)
  - [Linux (Ubuntu/Debian, RHEL/CentOS, Arch)](#linux)
  - [macOS (Intel & Apple Silicon)](#macos)
  - [Windows (native + WSL2)](#windows)
  - [Android / Termux](#android--termux)
- [Running as a Service (systemd)](#running-as-a-service-systemd)
- [Upgrading](#upgrading)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

---

## Choosing an Install Method

| Method | Best For | Pros | Cons |
|--------|----------|------|------|
| **One-liner** (`install.sh` / `install.ps1`) | Most users | Handles uv, Python 3.11, Node.js, ripgrep, ffmpeg automatically | Modifies shell rc files |
| **pip / pipx** | Users with existing Python 3.11+ envs | Familiar, isolated per-user | You install Node.js / ripgrep / ffmpeg yourself |
| **Docker** | Servers, isolated deployments, CI | Fully reproducible, no host pollution | ~2 GB image, needs Docker daemon |
| **From source** | Contributors, plugin developers | Editable install, all dev tools | More setup steps |

Quick rule of thumb:
- **Personal laptop** → one-liner
- **VPS / home server** → Docker (or one-liner + systemd)
- **Existing Python project** → `pip install sinoclaw-agent`
- **Hacking on Sinoclaw itself** → from source

---

## Method 1 — One-liner Installer (Recommended)

### Linux / macOS / WSL2 / Termux

```bash
curl -fsSL https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.sh | bash
```

What it does:
1. Installs [`uv`](https://docs.astral.sh/uv/) (fast Python package manager)
2. Installs Python 3.11 (isolated, doesn't touch system Python)
3. Installs Node.js, ripgrep, ffmpeg via your OS package manager
4. Creates `~/.sinoclaw/` data directory
5. Symlinks `sinoclaw` into `~/.local/bin/`
6. Adds `~/.local/bin` to your `PATH` if needed

After install:
```bash
source ~/.bashrc        # or ~/.zshrc
sinoclaw                # start chatting
```

### Windows (PowerShell, native)

```powershell
irm https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.ps1 | iex
```

The Windows installer additionally bundles **MinGit** (~45 MB portable Git Bash, no admin rights needed) under `%LOCALAPPDATA%\sinoclaw\git`. Sinoclaw uses this isolated bash to run shell commands without depending on or interfering with any system Git you may have.

> Native Windows is **early beta**. The most battle-tested Windows path is to run the Linux one-liner inside **WSL2** instead.

---

## Method 2 — pip / pipx (PyPI)

The package is published on PyPI as **`sinoclaw-agent`**.

### With pipx (recommended for CLI tools)

```bash
# Install pipx if you don't have it
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Install Sinoclaw with all optional features
pipx install "sinoclaw-agent[all]" --python python3.11
```

### With pip (in a virtualenv)

```bash
python3.11 -m venv ~/sinoclaw-venv
source ~/sinoclaw-venv/bin/activate
pip install "sinoclaw-agent[all]"
sinoclaw --help
```

### Available extras

| Extra | What it adds |
|-------|-------------|
| `[all]` | Recommended — full feature set (voice, browser tools, image gen, etc.) |
| `[dev]` | Development tools (pytest, ruff, ty) — for contributors |
| `[termux]` | Curated subset for Android/Termux (skips voice deps that don't compile on Android) |
| `[rl]` | RL/Atropos training environment (heavy: torch + tinker + wandb) |

> **You still need Node.js, ripgrep, and ffmpeg** for the messaging gateway, code review, and voice tools to work fully. Install them via your OS package manager (see the OS-specific sections below).

### Verify the install

```bash
sinoclaw --version
sinoclaw doctor          # diagnoses missing system deps
```

---

## Method 3 — Docker / docker-compose

Docker is the easiest way to run Sinoclaw on a **VPS or home server** without touching the host system.

### Quick start with docker-compose

```bash
git clone https://github.com/sinoclaw/sinoclaw-agent.git
cd sinoclaw-agent
SINOCLAW_UID=$(id -u) SINOCLAW_GID=$(id -g) docker compose up -d --build
```

This brings up two services:
- **gateway** — messaging gateway (Telegram, Discord, Slack, etc.) on host network
- **dashboard** — browser dashboard on `127.0.0.1:9119` (localhost only by default for security)

Configuration lives in `~/.sinoclaw/` on the host (mounted into the container at `/opt/data`).

### First-time configuration

```bash
# Open a shell inside the running container
docker exec -it -u sinoclaw $(docker ps -qf name=gateway) bash

# Then run the setup wizard
sinoclaw setup
```

Or set credentials via environment variables in `docker-compose.yml` (see the comments in that file for Telegram, Discord, Teams, etc.).

### Build the image manually

```bash
docker build -t sinoclaw-agent:latest .
docker run -d \
  --name sinoclaw \
  --network host \
  -v ~/.sinoclaw:/opt/data \
  -e SINOCLAW_UID=$(id -u) \
  -e SINOCLAW_GID=$(id -g) \
  sinoclaw-agent:latest gateway run
```

### Useful commands

```bash
docker compose logs -f gateway        # tail gateway logs
docker compose restart gateway        # restart after config change
docker compose down                   # stop everything
docker compose pull && docker compose up -d --build   # update
```

### Security notes

- The dashboard binds to `127.0.0.1` only — it stores API keys, exposing it on LAN without auth is unsafe. For remote access, use SSH tunnel: `ssh -L 9119:localhost:9119 your-server`.
- The OpenAI-compatible API server is **off** by default. To enable it, uncomment `API_SERVER_HOST` and `API_SERVER_KEY` in `docker-compose.yml` — the key is mandatory.
- `SINOCLAW_UID` / `SINOCLAW_GID` should match the host user that owns `~/.sinoclaw` so files stay readable on the host.

---

## Method 4 — From Source (Developers)

For contributors and plugin developers who want an editable install.

```bash
git clone https://github.com/sinoclaw/sinoclaw-agent.git
cd sinoclaw-agent

# Easy path — uses the bundled bootstrap script
./setup-sinoclaw.sh

# Then run directly from the checkout
./sinoclaw
```

`setup-sinoclaw.sh` handles: installing uv, creating `.venv` with Python 3.11, installing `.[all]` editable, and symlinking `~/.local/bin/sinoclaw` to the checkout.

Manual equivalent:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh        # run the test suite
```

> **国内用户镜像加速：** GitHub clone 慢可以走 GitCode 镜像：
> ```bash
> git clone --recurse-submodules https://gitcode.com/GitHub_Trending/si/sinoclaw-agent.git
> ```
> 或者 Gitee：`git clone https://gitee.com/sinoclaw/sinoclaw-agent.git`

---

## Operating System Notes

### Linux

System dependencies you may need to install yourself if you go the pip route:

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip nodejs npm ripgrep ffmpeg git
```

**RHEL / CentOS / Fedora:**
```bash
sudo dnf install -y python3.11 nodejs npm ripgrep ffmpeg git
```

**Arch / Manjaro:**
```bash
sudo pacman -S python nodejs npm ripgrep ffmpeg git
```

**Alpine (musl):** The one-liner installer doesn't currently support Alpine. Use the Docker image instead.

### macOS

The one-liner installer uses **Homebrew** for system dependencies. Install Homebrew first if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then run the install script. Apple Silicon (M1/M2/M3) is fully supported — uv installs the native arm64 Python build automatically.

For pip-based install:
```bash
brew install python@3.11 node ripgrep ffmpeg git
pipx install "sinoclaw-agent[all]" --python python3.11
```

### Windows

**Recommended:** WSL2 + Linux one-liner. WSL2 install:
```powershell
wsl --install -d Ubuntu-22.04
```
Then inside WSL: `curl -fsSL https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.sh | bash`

**Native Windows (early beta):** Use the PowerShell one-liner from the README. Sinoclaw lives under `%LOCALAPPDATA%\sinoclaw`.

Known native-Windows limitations (use WSL2 if you need these):
- Browser dashboard chat pane (requires POSIX PTY)
- Some terminal backends (Singularity, certain cloud sandbox modes)

### Android / Termux

```bash
pkg update && pkg upgrade
pkg install python rust nodejs ripgrep ffmpeg git
curl -fsSL https://raw.githubusercontent.com/sinoclaw/sinoclaw-agent/main/scripts/install.sh | bash
```

The installer auto-detects Termux and installs the `[termux]` extra (skips voice dependencies that don't compile on Android). Full guide: [Termux quickstart](https://sinoclaw-agent.nousresearch.com/docs/getting-started/termux).

---

## Running as a Service (systemd)

To run the messaging gateway as a long-lived background service on Linux:

```bash
sudo tee /etc/systemd/system/sinoclaw-gateway.service > /dev/null <<'EOF'
[Unit]
Description=Sinoclaw Agent — Messaging Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
Environment=HOME=/home/YOUR_USER
ExecStart=/home/YOUR_USER/.local/bin/sinoclaw gateway run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sinoclaw-gateway
sudo systemctl status sinoclaw-gateway
journalctl -u sinoclaw-gateway -f       # tail logs
```

Replace `YOUR_USER` with your actual username.

> The `install.sh` script offers to set this up automatically when it detects a messaging platform token has been configured.

---

## Upgrading

| Install method | How to upgrade |
|----------------|----------------|
| One-liner | `sinoclaw update` |
| pipx | `pipx upgrade sinoclaw-agent` |
| pip | `pip install -U "sinoclaw-agent[all]"` |
| Docker | `docker compose pull && docker compose up -d --build` |
| From source | `git pull && uv pip install -e ".[all,dev]"` |

---

## Uninstalling

```bash
# Remove the binary symlink and venv
rm -rf ~/.local/bin/sinoclaw ~/.local/share/sinoclaw

# Remove user data (config, sessions, skills) — IRREVERSIBLE
rm -rf ~/.sinoclaw

# pip / pipx
pipx uninstall sinoclaw-agent
# or: pip uninstall sinoclaw-agent

# Docker
docker compose down -v
docker rmi sinoclaw-agent

# systemd
sudo systemctl disable --now sinoclaw-gateway
sudo rm /etc/systemd/system/sinoclaw-gateway.service
```

---

## Troubleshooting

### `sinoclaw: command not found` after install
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### `sinoclaw doctor` reports missing dependencies
Install them via your OS package manager (see [OS notes](#operating-system-notes)).

### Python version errors (`requires Python >=3.11`)
```bash
# With uv
uv python install 3.11

# Or use your OS package manager — see OS notes above
```

### Docker: permission denied on `~/.sinoclaw`
Make sure `SINOCLAW_UID` and `SINOCLAW_GID` match your host user:
```bash
SINOCLAW_UID=$(id -u) SINOCLAW_GID=$(id -g) docker compose up -d
```

### China network: GitHub clone / pip install too slow

```bash
# Git: use GitCode mirror
git clone https://gitcode.com/GitHub_Trending/si/sinoclaw-agent.git

# pip: use Tsinghua mirror
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "sinoclaw-agent[all]"

# npm: use npmmirror
npm config set registry https://registry.npmmirror.com
```

### Still stuck?

- Run `sinoclaw doctor` — diagnoses 30+ common issues
- Browse logs: `sinoclaw logs --follow`
- Open an issue: https://github.com/sinoclaw/sinoclaw-agent/issues
- Discord: https://github.com/sinoclaw/sinoclaw-agent

---

📖 **Full documentation:** https://sinoclaw-agent.nousresearch.com/docs/
