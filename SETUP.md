# PrintBot v3 — Setup & Deployment Guide

This guide covers installing PrintBot v3 on a Raspberry Pi 4 running Raspberry Pi OS (Bookworm 64-bit), including Cloudflare Tunnel configuration and systemd service setup.

## 1. Prerequisites

- **Hardware**: Raspberry Pi 4 (4 GB RAM recommended).
- **OS**: Raspberry Pi OS Bookworm 64-bit (desktop edition — required for Chromium kiosk).
- **Network**: Stable internet connection (WiFi or Ethernet).
- **Domain**: A domain managed on Cloudflare (required for the Tunnel and Razorpay webhooks).

---

## 2. System Dependencies

```bash
# Update system packages
sudo apt-get update && sudo apt-get upgrade -y

# Core dependencies
sudo apt-get install -y \
  python3-venv \
  libcups2-dev \
  cups \
  libreoffice-writer \
  libreoffice-java-common \
  default-jre \
  libopenjp2-7 \
  libxml2-dev \
  libxslt-dev \
  chromium-browser

# Install Cloudflared (ARM64 for Pi 4/5)
# Download the latest release from https://github.com/cloudflare/cloudflared/releases
# and install:
sudo dpkg -i cloudflared-linux-arm64.deb
```

---

## 3. Python Environment

```bash
cd /home/ijas/printbot

# Create and activate virtualenv
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

---

## 4. Environment Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
nano .env
```

Required values to set:

| Variable | Description |
| :--- | :--- |
| `TUNNEL_URL` | Your Cloudflare public URL, e.g. `https://print.yourdomain.com` |
| `RAZORPAY_KEY_ID` | Razorpay API key ID |
| `RAZORPAY_KEY_SECRET` | Razorpay API key secret |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay webhook signing secret |
| `ADMIN_PIN` | Numeric PIN for admin login (change from default `1234`) |
| `ADMIN_SESSION_SECRET` | Random string for itsdangerous cookie signing |
| `JOB_SESSION_SECRET` | Separate random string for user job session cookies |
| `DEFAULT_PRINTER` | CUPS printer name (run `lpstat -p` to list printers) |
| `ENV` | `production` on the Pi, `development` locally |

---

## 5. Cloudflare Tunnel Setup

The Cloudflare Tunnel exposes the local server (`localhost:8000`) to the internet so users can upload files from their phones. Razorpay webhooks also need this public URL.

### Option A: Dashboard Method (Recommended)

1. Log in to [Cloudflare Zero Trust](https://one.dash.cloudflare.com/).
2. Navigate to **Networks > Tunnels** → **Create a Tunnel**.
3. Choose **Cloudflared** connector and name it `printbot-pi`.
4. Under **Install Connector**, select **Debian / arm64**.
5. Copy the install command (starts with `sudo cloudflared service install ey...`) and run it on the Pi.
6. Under **Public Hostname**:
   - Subdomain: `print` (or your choice)
   - Domain: your Cloudflare-managed domain
   - Service: `HTTP` → `localhost:8000`
7. Save. Visit `https://print.yourdomain.com` on your phone to verify.

### Option B: CLI Method

```bash
cloudflared tunnel login
cloudflared tunnel create printbot

# Create ~/.cloudflared/config.yml:
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <Tunnel-UUID>
credentials-file: /home/ijas/.cloudflared/<Tunnel-UUID>.json

ingress:
  - hostname: print.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
EOF

cloudflared tunnel route dns printbot print.yourdomain.com
cloudflared tunnel run printbot
```

---

## 6. Auto-Start with systemd

The project ships two service files in `systemd/`:

| Service File | Description |
| :--- | :--- |
| `printbot-backend.service` | FastAPI/Uvicorn backend on port 8000 |
| `printbot-kiosk.service` | Chromium kiosk pointing to `http://localhost:8000/kiosk` |

> **Note**: Both service files are configured for `User=ijas`. If your username differs, edit the `User=` field and all paths (`WorkingDirectory`, `EnvironmentFile`, `ExecStart`) before copying.

### Step 1 — Verify your username in the service files

```bash
whoami   # confirm your username

# Edit if your username is not 'ijas'
nano systemd/printbot-backend.service
nano systemd/printbot-kiosk.service
```

### Step 2 — Copy service files to systemd

```bash
sudo cp systemd/printbot-backend.service /etc/systemd/system/
sudo cp systemd/printbot-kiosk.service /etc/systemd/system/
```

### Step 3 — Reload systemd and enable services

```bash
sudo systemctl daemon-reload

sudo systemctl enable printbot-backend
sudo systemctl enable printbot-kiosk
```

### Step 4 — Start services now (without rebooting)

```bash
sudo systemctl start printbot-backend
sudo systemctl start printbot-kiosk
```

### Step 5 — Check status

```bash
sudo systemctl status printbot-backend
sudo systemctl status printbot-kiosk
```

Both should show `Active: active (running)`.

### Step 6 — View live logs

```bash
sudo journalctl -u printbot-backend -f
sudo journalctl -u printbot-kiosk -f
```

### Stopping / Disabling Services

```bash
sudo systemctl stop printbot-backend printbot-kiosk
sudo systemctl disable printbot-backend printbot-kiosk
```

---

## 7. Kiosk Display Setup

The kiosk uses Chromium in `--kiosk` mode (not Kivy). It requires the Pi desktop to be running.

### Enable auto-login to desktop

```bash
sudo raspi-config
# System Options → Boot / Auto Login → Desktop Autologin
```

### Verify Chromium is installed

```bash
chromium-browser --version
```

The `printbot-kiosk.service` will launch Chromium automatically after boot, pointing to `http://localhost:8000/kiosk`. It uses `ExecStartPre=/bin/sleep 5` to wait for the backend to be ready.

---

## 8. CUPS Printer Setup

```bash
# Add your user to the lpadmin group for CUPS management
sudo usermod -aG lpadmin ijas

# Open the CUPS web UI (from the Pi's browser)
http://localhost:631

# Or add a printer from the command line:
lpadmin -p "HP_LaserJet" -E -v "usb://HP/LaserJet" -m "everywhere"

# List configured printers:
lpstat -p

# Set DEFAULT_PRINTER in .env to the printer name shown by lpstat -p
```

---

## 9. Accessing PrintBot

| Method | URL |
| :--- | :--- |
| Public (phone users) | `https://print.yourdomain.com` |
| Local network | `http://<Pi-IP>:8000` |
| Kiosk (local only) | `http://localhost:8000/kiosk` |
| Admin panel | `http://<Pi-IP>:8000/admin/login` |

---

## 10. Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| Backend fails — `.env` not loaded | Ensure `/home/ijas/printbot/.env` exists and `EnvironmentFile=` path in service is correct |
| Backend fails — port in use | Check `sudo lsof -i :8000`; stop any conflicting process |
| Kiosk shows blank screen | Confirm `DISPLAY=:0` is set and desktop auto-login is enabled |
| Kiosk fails — `XDG_RUNTIME_DIR` error | Set `Environment=XDG_RUNTIME_DIR=/run/user/1000`; replace `1000` with your UID (`id -u`) |
| Chromium not found | Install with `sudo apt-get install chromium-browser` |
| Printer not found | Run `lpstat -p` and set `DEFAULT_PRINTER` in `.env` |
| QR code not generated | Check `TUNNEL_URL` is set in `.env` (not the example `https://print.example.com`) |
| Razorpay webhooks failing | Confirm `TUNNEL_URL` is set to the public Cloudflare URL (not localhost) |
| Service won't restart after crash | Verify `Restart=always` and `RestartSec=5` are present in `[Service]` block |

---

## 11. Development vs Production

- **Development**: Set `ENV=development` in `.env`; run with `./launch.sh` (hot reload enabled).
- **Production**: Set `ENV=production`; use the systemd services. Swagger UI (`/docs`) is hidden in production.
