#!/usr/bin/env bash
# leadmap installer — one command on a fresh Ubuntu/Debian server.
#
#   sudo ./install.sh                       # interactive
#   sudo ./install.sh --city "شیراز" --yes  # non-interactive (self-signed dashboard cert)
#   sudo ./install.sh --city "شیراز" --domain example.com --cert /path/fullchain.pem --key /path/privkey.pem
#   sudo ./install.sh --hub https://panel:8880 --hub-token TOKEN --contact me@mail.com   # worker of a central panel
#
# Safe to re-run: keeps an existing config.yaml, database and browser profile.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CITY="" DOMAIN="" CERT="" KEY="" CONTACT="" PORT=8880
DASHBOARD=1 SYSTEMD=1 START=1 TESTS=1 YES=0 RECONFIGURE=0
HUB_URL="" HUB_TOKEN="" HUB_INSECURE=false HUB_SERVER=false

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF
Options:
  --city NAME        city to scrape (Persian name as on OpenStreetMap, e.g. "اصفهان")
  --contact EMAIL    contact for the OpenStreetMap Nominatim User-Agent (their policy asks for one)
  --domain HOST      dashboard hostname (default: this server's public IP)
  --cert FILE        TLS certificate (fullchain) for the dashboard; default: self-signed
  --key FILE         TLS private key
  --port N           dashboard port (default 8880)
  --no-dashboard     don't install the status dashboard
  --hub URL          worker mode: take cities from the central panel at URL (no --city needed)
  --hub-token T      this server's token (central panel → "ساخت توکن")
  --hub-insecure     the central panel uses a self-signed certificate
  --hub-server       make THIS server the central panel (queue of cities for all servers)
  --no-systemd       don't install/start systemd services (just set up the app)
  --no-start         install services but don't start the scraper yet
  --skip-tests       don't run the unit tests
  --reconfigure      rewrite config.yaml even if it exists (DB and profile are kept)
  -y, --yes          don't ask questions; use defaults
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --city) CITY="$2"; shift 2 ;;
    --contact) CONTACT="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --cert) CERT="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-dashboard) DASHBOARD=0; shift ;;
    --hub) HUB_URL="${2%/}"; shift 2 ;;
    --hub-token) HUB_TOKEN="$2"; shift 2 ;;
    --hub-insecure) HUB_INSECURE=true; shift ;;
    --hub-server) HUB_SERVER=true; shift ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --no-start) START=0; shift ;;
    --skip-tests) TESTS=0; shift ;;
    --reconfigure) RECONFIGURE=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask "question" default → echoes answer
  local a
  if [[ $YES == 1 ]]; then echo "$2"; return; fi
  read -r -p "    $1 [$2]: " a </dev/tty || true
  echo "${a:-$2}"
}

cd "$APP_DIR"
[[ -f main.py && -d leadmap ]] || die "run this script from inside the leadmap folder"
[[ -n $HUB_URL && -z $HUB_TOKEN ]] && die "--hub needs --hub-token (create one in the central panel)"
[[ -z $HUB_URL && -n $HUB_TOKEN ]] && die "--hub-token needs --hub URL"
[[ $HUB_SERVER == true && $DASHBOARD == 0 ]] && die "--hub-server needs the dashboard (drop --no-dashboard)"

# --- 1. checks -------------------------------------------------------------------
step "Checking the server"
[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"
command -v apt-get >/dev/null || die "only Debian/Ubuntu (apt) is supported"
. /etc/os-release; ok "OS: ${PRETTY_NAME:-unknown}"

avail_mb=$(df --output=avail -BM "$APP_DIR" | tail -1 | tr -dc 0-9)
(( avail_mb >= 1500 )) || die "only ${avail_mb} MB free disk; need at least 1.5 GB (3+ GB recommended)"
(( avail_mb >= 3000 )) && ok "disk: ${avail_mb} MB free" || warn "disk: only ${avail_mb} MB free (3+ GB recommended)"

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
(( mem_mb >= 1500 )) && ok "RAM: ${mem_mb} MB" || warn "RAM: ${mem_mb} MB — Chromium may struggle below 2 GB"

# --- 2. system packages ----------------------------------------------------------
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_SUSPEND=1   # don't let Ubuntu's needrestart restart running services (e.g. leadmap)
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl openssl ca-certificates xvfb tmux >/dev/null
ok "python3, venv, curl, openssl, xvfb, tmux"

# --- 3. Python ≥ 3.11 + venv ------------------------------------------------------
step "Setting up Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done

if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  ok "existing .venv ($(.venv/bin/python --version))"
elif [[ -n $PY ]]; then
  apt-get install -y -qq "$(basename "$PY")-venv" >/dev/null 2>&1 || true
  rm -rf .venv && "$PY" -m venv .venv
  ok "venv with $($PY --version)"
else
  # e.g. Ubuntu 22.04 / Debian 11 ship 3.10: fetch a managed Python via uv
  warn "system Python is older than 3.11 — installing Python 3.12 via uv"
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  rm -rf .venv && uv venv --seed --python 3.12 .venv >/dev/null
  ok "venv with $(.venv/bin/python --version)"
fi

.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q --no-cache-dir -r requirements.txt
ok "Python packages installed"

mkdir -p data
step "Installing Chromium (Playwright)"
.venv/bin/playwright install --with-deps --no-shell chromium > data/playwright-install.log 2>&1 \
  || { tail -20 data/playwright-install.log; die "Chromium install failed (log: data/playwright-install.log)"; }
ok "Chromium ready"

if [[ $TESTS == 1 ]]; then
  step "Running unit tests"
  out=$(.venv/bin/python -m pytest -q tests 2>&1) || { echo "$out" | tail -30; die "unit tests failed"; }
  echo "$out" | tail -1
  ok "tests pass"
fi

# --- 4. config.yaml --------------------------------------------------------------
step "Configuration"
PUBLIC_IP=$(curl -s -m 8 https://api.ipify.org || true)
PUBLIC_IP=${PUBLIC_IP:-$(hostname -I | awk '{print $1}')}
DASH_PASSWORD=""

if [[ -f config.yaml && $RECONFIGURE == 0 ]]; then
  ok "keeping existing config.yaml (use --reconfigure to rewrite it)"
  CITY=${CITY:-$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.yaml"))["city"])')}
  if [[ -n $HUB_URL || $HUB_SERVER == true ]]; then
    # add/replace only the hub: section; everything else stays as it is
    HUB_URL="$HUB_URL" HUB_TOKEN="$HUB_TOKEN" HUB_INSECURE="$HUB_INSECURE" HUB_SERVER="$HUB_SERVER" \
    .venv/bin/python - <<'PY'
import os, re, yaml
cur = (yaml.safe_load(open("config.yaml", encoding="utf-8")).get("hub") or {})
ex = open("config.example.yaml", encoding="utf-8").read()
block = ex[ex.index("\nhub:") + 1:]
url = os.environ["HUB_URL"] or cur.get("url") or ""
token = os.environ["HUB_TOKEN"] or cur.get("token") or ""
server = "true" if os.environ["HUB_SERVER"] == "true" or cur.get("server") else "false"
insecure = "true" if os.environ["HUB_INSECURE"] == "true" or (cur.get("insecure_tls") and not os.environ["HUB_URL"]) else "false"
for k, v in {"__HUB_URL__": url, "__HUB_TOKEN__": token, "__HUB_SERVER__": server,
             "__HUB_INSECURE__": insecure}.items():
    block = block.replace(k, v)
s = open("config.yaml", encoding="utf-8").read()
s = re.sub(r"(?ms)^hub:.*?(?=^\S|\Z)", "", s).rstrip("\n") + "\n\n" + block
open("config.yaml", "w", encoding="utf-8").write(s)
PY
    ok "hub settings written to config.yaml"
  fi
else
  if [[ -n $HUB_URL ]]; then
    CITY=${CITY:-"-"}   # cities come from the central panel
  fi
  [[ -n $CITY ]] || CITY=$(ask "City to scrape (Persian name, e.g. اصفهان)" "اصفهان")
  # OpenStreetMap's Nominatim rejects (HTTP 403) requests without a real contact address.
  while [[ ! $CONTACT =~ ^[^@[:space:]]+@[^@[:space:]]+\.[a-z]{2,}$ || $CONTACT =~ @example\. ]]; do
    [[ $YES == 1 ]] && die "--contact YOUR_REAL_EMAIL is required (OpenStreetMap blocks fake/example addresses)"
    CONTACT=$(ask "Your real e-mail (OpenStreetMap requires a contact; only sent to them)" "")
  done
  if [[ $DASHBOARD == 1 ]]; then
    [[ -n $DOMAIN ]] || DOMAIN=$(ask "Dashboard hostname (domain or IP)" "$PUBLIC_IP")
    if [[ -z $CERT ]]; then
      CERT="$APP_DIR/data/tls/cert.pem"; KEY="$APP_DIR/data/tls/key.pem"
      if [[ ! -f $CERT ]]; then
        mkdir -p data/tls
        if [[ $DOMAIN =~ ^[0-9.]+$ ]]; then SAN="IP:$DOMAIN"; else SAN="DNS:$DOMAIN"; fi
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$DOMAIN" \
          -addext "subjectAltName=$SAN" -keyout "$KEY" -out "$CERT" >/dev/null 2>&1
        chmod 600 "$KEY"
        warn "no --cert given: generated a self-signed certificate (browser will warn once; that's expected)"
      fi
    fi
    [[ -f $CERT && -f $KEY ]] || die "certificate or key not found: $CERT / $KEY"
    DASH_PASSWORD=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')
  fi
  CITY="$CITY" CONTACT="$CONTACT" DOMAIN="${DOMAIN:-$PUBLIC_IP}" CERT="${CERT:-none}" KEY="${KEY:-none}" \
  PASSWORD="$DASH_PASSWORD" PORT="$PORT" DASH="$([[ $DASHBOARD == 1 ]] && echo true || echo false)" \
  HUB_URL="$HUB_URL" HUB_TOKEN="$HUB_TOKEN" HUB_INSECURE="$HUB_INSECURE" HUB_SERVER="$HUB_SERVER" \
  .venv/bin/python - <<'PY'
import os, re
s = open("config.example.yaml", encoding="utf-8").read()
for k, v in {"__CITY__": os.environ["CITY"], "__CONTACT__": os.environ["CONTACT"],
             "__PUBLIC_HOST__": os.environ["DOMAIN"], "__PASSWORD__": os.environ["PASSWORD"],
             "__CERT__": os.environ["CERT"], "__KEY__": os.environ["KEY"],
             "__DASH_ENABLED__": os.environ["DASH"], "__HUB_URL__": os.environ["HUB_URL"],
             "__HUB_TOKEN__": os.environ["HUB_TOKEN"], "__HUB_INSECURE__": os.environ["HUB_INSECURE"],
             "__HUB_SERVER__": os.environ["HUB_SERVER"]}.items():
    s = s.replace(k, v)
s = re.sub(r"^(  port:) \d+", rf"\1 {os.environ['PORT']}", s, flags=re.M)
open("config.yaml", "w", encoding="utf-8").write(s)
PY
  chmod 600 config.yaml
  ok "config.yaml written (city: $CITY)"
fi

HUB_MODE=$(.venv/bin/python -c 'import yaml; h = yaml.safe_load(open("config.yaml")).get("hub") or {}; print(int(bool(h.get("url") and h.get("token"))))')

# --- 5. grid for the city / hub connection ---------------------------------------------
if [[ $HUB_MODE == 1 ]]; then
  step "Checking the connection to the central panel"
  # the dashboard must be up first when this server is its own hub
  [[ $SYSTEMD == 1 && $DASHBOARD == 1 ]] && systemctl is-active -q leadmap-dashboard && systemctl restart leadmap-dashboard && sleep 2
  out=$(.venv/bin/python main.py hub ping 2>&1) || { echo "$out" | tail -3; die "cannot reach the central panel — check --hub URL / --hub-token (and --hub-insecure for a self-signed cert)"; }
  echo "$out" | grep -vE ' INFO ' | sed 's/^/    /'
  ok "cities will be taken from the central queue (grids are built when a city is claimed)"
else
  step "Building the tile grid for $CITY (OpenStreetMap)"
  out=$(.venv/bin/python main.py init --city "$CITY" 2>&1) \
    || { echo "$out" | tail -3
           grep -q "403" <<<"$out" && die "OpenStreetMap refused the request (403): set a real contact e-mail in config.yaml → nominatim.user_agent, then re-run"
           die "grid build failed — check the city name (Persian, as on openstreetmap.org)"; }
  echo "$out" | grep -vE ' INFO ' | sed 's/^/    /'
fi

# --- 6. systemd ------------------------------------------------------------------
if [[ $SYSTEMD == 1 ]]; then
  step "Installing systemd services"
  sed "s|@APP_DIR@|$APP_DIR|g" deploy/leadmap.service > /etc/systemd/system/leadmap.service
  if [[ $DASHBOARD == 1 ]]; then
    sed "s|@APP_DIR@|$APP_DIR|g" deploy/leadmap-dashboard.service > /etc/systemd/system/leadmap-dashboard.service
  fi
  systemctl daemon-reload
  if [[ $DASHBOARD == 1 ]]; then
    ss -ltn | grep -q ":$PORT " && ! systemctl is-active -q leadmap-dashboard \
      && warn "port $PORT is already used by another program — change dashboard.port in config.yaml"
    systemctl enable -q leadmap-dashboard && systemctl restart leadmap-dashboard
    ok "dashboard service running"
  fi
  systemctl enable -q leadmap
  if [[ $START == 1 ]]; then
    systemctl restart leadmap && ok "scraper service running"
  else
    ok "scraper service installed (start later: systemctl start leadmap)"
  fi
fi

# --- 7. summary --------------------------------------------------------------------
step "Done"
echo "    App folder : $APP_DIR"
if [[ $HUB_MODE == 1 ]]; then
  echo "    Cities     : from the central panel ($(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.yaml"))["hub"]["url"])')/hub)"
else
  echo "    City       : $CITY"
fi
echo "    Status     : cd $APP_DIR && .venv/bin/python main.py status"
echo "    Logs       : tail -f $APP_DIR/logs/run.log"
echo "    Test 1 tile: systemctl stop leadmap; .venv/bin/python main.py test-tile; systemctl start leadmap"
if [[ $DASHBOARD == 1 && $SYSTEMD == 1 ]]; then
  HOST=$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.yaml"))["dashboard"]["public_host"])')
  echo "    Dashboard  : https://$HOST:$PORT"
  if [[ -n $DASH_PASSWORD ]]; then
    echo "    Login      : admin / $DASH_PASSWORD   (stored in config.yaml → dashboard)"
  else
    echo "    Login      : see config.yaml → dashboard"
  fi
  if .venv/bin/python -c 'import sys, yaml; sys.exit(not (yaml.safe_load(open("config.yaml")).get("hub") or {}).get("server"))'; then
    echo "    Central    : https://$HOST:$PORT/hub   (queue cities, create worker tokens)"
  fi
fi
echo
