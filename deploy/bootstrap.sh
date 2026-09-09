#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 instance to run the ephemeral rooms server.
#
# Covers steps 3-8 of the README's deployment section: system packages, service
# user, directories, virtualenv, configuration, systemd, and Nginx. It stops
# short of certbot, because that must not run until DNS actually resolves to
# this instance; the script prints the exact command to run next.
#
# Idempotent: safe to re-run after a failure, and safe to re-run to deploy a
# new build. It never overwrites an existing /etc/ephemeral-rooms.env, because
# that file is where your real configuration lives.
#
# Usage, from the repository root on the instance:
#
#     sudo ./deploy/bootstrap.sh example.com
#     sudo ./deploy/bootstrap.sh example.com --data-root /mnt/uploads
#
# The frontend must already be BUILT before you copy the repo across. Node is
# not installed on the instance and is not needed there:
#
#     # on your machine
#     cd frontend && npm ci && npm run build
#     rsync -av --exclude node_modules ./ ubuntu@<ip>:/tmp/ephemeral-rooms/

set -euo pipefail

SERVICE_USER=ephemeral
INSTALL_DIR=/opt/ephemeral-rooms
DATA_ROOT=/var/lib/ephemeral-rooms
ENV_FILE=/etc/ephemeral-rooms.env
UNIT_FILE=/etc/systemd/system/ephemeral-rooms.service
NGINX_SITE=/etc/nginx/sites-available/ephemeral-rooms
PYTHON=python3.12

# ----------------------------------------------------------------- output

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '    %s✔%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n%serror:%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# ----------------------------------------------------------------- arguments

DOMAIN=${1:-}
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root) DATA_ROOT=${2:-}; shift 2 ;;
        --data-root=*) DATA_ROOT=${1#*=}; shift ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n $DOMAIN ]] || die "usage: sudo $0 <domain> [--data-root PATH]"
[[ $DOMAIN =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] \
    || die "'$DOMAIN' does not look like a domain name"
[[ $DATA_ROOT = /* ]] || die "--data-root must be an absolute path"

# A `www.` alias belongs to an apex domain, not to a subdomain: nobody points
# www.rooms.example.com anywhere. It matters more than it looks, because
# certbot validates every -d name and fails the whole request if one of them
# does not resolve - and Let's Encrypt rate-limits failures. Two labels is
# treated as apex. A multi-label public suffix such as example.co.uk is
# misread as a subdomain, which only skips the alias and breaks nothing.
if [[ $(tr -cd '.' <<<"$DOMAIN" | wc -c) -eq 1 ]]; then
    SERVER_NAMES="$DOMAIN www.$DOMAIN"
    CERTBOT_NAMES="-d $DOMAIN -d www.$DOMAIN"
    IS_APEX=yes
else
    SERVER_NAMES="$DOMAIN"
    CERTBOT_NAMES="-d $DOMAIN"
    IS_APEX=no
fi

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# ----------------------------------------------------------------- preflight
#
# Everything that could fail is checked before anything is changed, so a bad
# instance fails in five seconds rather than halfway through an install.

step "Preflight"

[[ $EUID -eq 0 ]] || die "run with sudo"

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    [[ ${ID:-} == ubuntu ]] || warn "this targets Ubuntu; found '${ID:-unknown}'"
    ok "OS: ${PRETTY_NAME:-unknown}"
fi

# httptools ships manylinux_2_28 wheels, so anything older than glibc 2.28
# (Ubuntu 18.04, CentOS 7) cannot install the dependency set without a
# compiler. Catch that here rather than three minutes into pip.
GLIBC=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | tail -1 || true)
if [[ -z ${GLIBC:-} ]]; then
    warn "could not determine the glibc version; continuing"
elif [[ ${GLIBC%%.*} -eq 2 && ${GLIBC#*.} -lt 28 ]]; then
    die "glibc $GLIBC is too old. httptools publishes manylinux_2_28 wheels, so
  the dependency set needs glibc >= 2.28. Ubuntu 24.04 has 2.39."
else
    ok "glibc $GLIBC"
fi

[[ -d $REPO/backend/app ]] || die "no backend/app in $REPO; run this from the repository"

# The frontend is built on a developer machine or in CI, because Node is a
# build-time dependency only and is deliberately absent here.
if [[ ! -f $REPO/frontend/dist/index.html ]]; then
    die "frontend/dist/index.html is missing.

  The frontend must be built BEFORE copying the repo to this instance:

      cd frontend && npm ci && npm run build

  Node is not installed here and is not needed at runtime."
fi
ok "frontend build present"

ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
ok "architecture: $ARCH"
ok "domain: $DOMAIN"
if [[ $IS_APEX == yes ]]; then
    ok "apex domain; a www. alias is included"
else
    ok "subdomain; no www. alias, which is what certbot needs here"
fi
ok "data root: $DATA_ROOT"

# ----------------------------------------------------------------- packages

step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    "$PYTHON" "$PYTHON-venv" \
    nginx certbot python3-certbot-nginx rsync >/dev/null
ok "python3.12, nginx, certbot"

command -v $PYTHON >/dev/null || die "$PYTHON not available after install"
ok "$($PYTHON --version)"

# ----------------------------------------------------------- user and layout

step "Service user and directories"

if id "$SERVICE_USER" &>/dev/null; then
    ok "user '$SERVICE_USER' already exists"
else
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "created user '$SERVICE_USER'"
fi

mkdir -p "$INSTALL_DIR/backend" "$INSTALL_DIR/frontend" "$DATA_ROOT"

# 0700: room files are readable only by the service user. Nothing here is meant
# to survive, but while it exists it is nobody else's business.
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_ROOT"
chmod 700 "$DATA_ROOT"
ok "$INSTALL_DIR and $DATA_ROOT"

# ----------------------------------------------------------------- application

step "Copying application"

rsync -a --delete \
    --exclude '__pycache__' --exclude '.venv' --exclude '.pytest_cache' \
    --exclude '.mypy_cache' --exclude '.data' --exclude 'tests' \
    "$REPO/backend/" "$INSTALL_DIR/backend/"
ok "backend"

rsync -a --delete "$REPO/frontend/dist/" "$INSTALL_DIR/frontend/dist/"
ok "frontend/dist"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ----------------------------------------------------------------- virtualenv

step "Python environment"

if [[ ! -x $INSTALL_DIR/.venv/bin/python ]]; then
    sudo -u "$SERVICE_USER" $PYTHON -m venv "$INSTALL_DIR/.venv"
    ok "created virtualenv"
else
    ok "virtualenv already present"
fi

sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip

# --only-binary=:all: is a deliberate assertion, not an optimisation. Every
# dependency publishes a wheel for cp312 Linux on both x86_64 and aarch64, so
# if pip ever wants to build from source something has changed and we want a
# loud failure here rather than a silent demand for a Rust toolchain.
if ! sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -q \
        --only-binary=:all: -r "$INSTALL_DIR/backend/requirements.txt"; then
    die "dependency install failed.

  Every pinned dependency should have a wheel for this architecture ($ARCH).
  If pip reported that it could not find one, a version was probably bumped
  without checking. Diagnose with:

      $INSTALL_DIR/.venv/bin/pip install --only-binary=:all: -r \\
          $INSTALL_DIR/backend/requirements.txt"
fi
ok "dependencies installed from wheels (no compiler used)"

sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/python" -c \
    'import pycrdt, fastapi, uvicorn; print("    pycrdt", pycrdt.__version__)'

# ----------------------------------------------------------------- configuration

step "Configuration"

if [[ -f $ENV_FILE ]]; then
    ok "$ENV_FILE exists; leaving it untouched"
    warn "check PUBLIC_ORIGIN and DATA_ROOT there if this is a new domain"
else
    install -m 640 -o root -g "$SERVICE_USER" \
        "$REPO/backend/.env.example" "$ENV_FILE"
    sed -i \
        -e "s|^PUBLIC_ORIGIN=.*|PUBLIC_ORIGIN=https://$DOMAIN|" \
        -e "s|^DATA_ROOT=.*|DATA_ROOT=$DATA_ROOT|" \
        "$ENV_FILE"
    ok "wrote $ENV_FILE (PUBLIC_ORIGIN=https://$DOMAIN)"
fi

# ----------------------------------------------------------------- systemd

step "systemd"

# ReadWritePaths must name the real data root, or ProtectSystem=strict makes
# the directory read-only and every upload fails.
sed "s|^ReadWritePaths=.*|ReadWritePaths=$DATA_ROOT|" \
    "$REPO/deploy/ephemeral-rooms.service" > "$UNIT_FILE"

# The single-worker rule is the one thing in this file that must never drift.
grep -q -- '--workers 1' "$UNIT_FILE" \
    || die "the unit file lost its '--workers 1'; refusing to install it (see spec 20.1)"

systemctl daemon-reload
systemctl enable --quiet ephemeral-rooms
systemctl restart ephemeral-rooms
ok "installed and started ephemeral-rooms.service"

sleep 2
if ! systemctl is-active --quiet ephemeral-rooms; then
    printf '\n'
    journalctl -u ephemeral-rooms -n 30 --no-pager || true
    die "the service failed to start; the last 30 log lines are above"
fi
ok "service is active"

# ----------------------------------------------------------------- nginx

step "Nginx"

# The site file includes this snippet by absolute path, so it has to be
# in place before `nginx -t` runs.
mkdir -p /etc/nginx/snippets
install -m 644 "$REPO/deploy/security-headers.conf" /etc/nginx/snippets/ephemeral-rooms-security.conf
ok "security headers snippet installed"

# The template carries `server_name example.com www.example.com;`. Rewrite
# that line wholesale before the blanket substitution, or a subdomain
# deployment inherits a www. alias that does not resolve.
sed -e "s/^\([[:space:]]*\)server_name .*/\1server_name $SERVER_NAMES;/" \
    -e "s/example\.com/$DOMAIN/g" \
    "$REPO/deploy/nginx.conf" > "$NGINX_SITE"
sed -i "s|root /opt/ephemeral-rooms/frontend/dist;|root $INSTALL_DIR/frontend/dist;|" "$NGINX_SITE"

ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/ephemeral-rooms
rm -f /etc/nginx/sites-enabled/default

# On a first run the TLS certificate does not exist yet, so the 443 block
# cannot load. Serve plain HTTP until certbot has run; it rewrites this file
# and adds the TLS block itself.
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
    warn "no certificate yet; installing an HTTP-only site for the ACME challenge"
    cat > "$NGINX_SITE" <<NGINX
# Temporary HTTP-only site. Run certbot (see below) and it will replace this
# with the full TLS configuration from deploy/nginx.conf.
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAMES;

    client_max_body_size 0;
    proxy_request_buffering off;

    root $INSTALL_DIR/frontend/dist;
    index index.html;

    location /.well-known/acme-challenge/ { root /var/www/html; }

    include /etc/nginx/snippets/ephemeral-rooms-security.conf;

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
    }

    location / { try_files \$uri \$uri/ /index.html; }
}
NGINX
fi

nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx configuration is invalid"; }
systemctl reload nginx
ok "site enabled and nginx reloaded"

# ----------------------------------------------------------------- smoke test

step "Smoke test"

sleep 1
CODE=$(curl -fsS -X POST http://127.0.0.1:8000/api/rooms 2>/dev/null || echo "")
[[ $CODE == *roomCode* ]] || die "the API did not answer on 127.0.0.1:8000; check: journalctl -u ephemeral-rooms -n 50"
ok "API created a room: $CODE"

curl -fsS -o /dev/null http://127.0.0.1/ || die "nginx did not serve the frontend"
ok "nginx served the landing page"

# ----------------------------------------------------------------- next steps

printf '\n%s==> Done.%s\n\n' "$BOLD$GREEN" "$OFF"

if [[ -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
    cat <<DONE
  A certificate for $DOMAIN is already installed.

  Verify renewal still works:
      sudo certbot renew --dry-run

DONE
else
    cat <<NEXT
  ${BOLD}The site is live over plain HTTP. Two things remain.${OFF}

  1. Point DNS at this instance and WAIT for it to resolve. certbot fails if
     it cannot reach you, and you can be rate-limited for retrying:

         dig +short $DOMAIN

     That must return this instance's Elastic IP.

  2. Then get the certificate. certbot rewrites the Nginx site itself:

         sudo certbot --nginx $CERTBOT_NAMES
         sudo certbot renew --dry-run

     Afterwards, confirm the WebSocket block still carries
     'proxy_read_timeout 3600s' — certbot edits this file, and Nginx's 60s
     default would cut idle sockets:

         grep -A2 'location /ws' $NGINX_SITE

NEXT
fi

cat <<INFO
  Useful commands:
      systemctl status ephemeral-rooms
      journalctl -u ephemeral-rooms -f
      systemctl restart ephemeral-rooms     # loses every active room, by design

  Remember: rooms live in this one process's memory. Never add workers.
INFO
