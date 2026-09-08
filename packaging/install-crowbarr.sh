#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="amanofvaly/crowbarr"
INSTALL_DIR="${CROWBARR_INSTALL_DIR:-/opt/crowbarr}"
DATA_DIR="${CROWBARR_DATA_DIR:-/var/lib/crowbarr}"
SERVICE_USER="${CROWBARR_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_GROUP="${CROWBARR_GROUP:-$(id -gn "$SERVICE_USER" 2>/dev/null || echo "$SERVICE_USER")}"
RELEASE="${CROWBARR_VERSION:-latest}"
LATEST_URL="https://github.com/$REPOSITORY/releases/latest/download"

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "Crowbarr's native installer currently supports Linux x86_64." >&2
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "User $SERVICE_USER does not exist." >&2
  exit 1
fi

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libgomp1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl ffmpeg libgomp
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --needed --noconfirm ca-certificates curl ffmpeg gcc-libs
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install ca-certificates curl ffmpeg libgomp1
  else
    echo "Install curl, ffmpeg, and ffprobe, then run this installer again." >&2
    exit 1
  fi
}

if ! command -v curl >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  install_packages
fi

if [[ "$RELEASE" == "latest" ]]; then
  RELEASE_URL="$LATEST_URL"
else
  RELEASE_URL="https://github.com/$REPOSITORY/releases/download/v${RELEASE#v}"
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

curl -fL "$RELEASE_URL/crowbarr-linux-x86_64.tar.gz" -o "$TEMP_DIR/crowbarr-linux-x86_64.tar.gz"
curl -fL "$RELEASE_URL/crowbarr-linux-x86_64.tar.gz.sha256" -o "$TEMP_DIR/crowbarr-linux-x86_64.tar.gz.sha256"
(
  cd "$TEMP_DIR"
  sha256sum -c crowbarr-linux-x86_64.tar.gz.sha256
  tar -xzf crowbarr-linux-x86_64.tar.gz
)

systemctl stop crowbarr.service 2>/dev/null || true
rm -rf "${INSTALL_DIR}.new"
mv "$TEMP_DIR/crowbarr" "${INSTALL_DIR}.new"
rm -rf "$INSTALL_DIR"
mv "${INSTALL_DIR}.new" "$INSTALL_DIR"

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"

cat > /etc/systemd/system/crowbarr.service <<EOF
[Unit]
Description=Crowbarr subtitle service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
Environment=CROWBARR_DATA=$DATA_DIR
Environment=HOME=$DATA_DIR
ExecStart=$INSTALL_DIR/crowbarr --host 0.0.0.0 --port 8449
Restart=on-failure
RestartSec=5
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
EOF

curl -fL "$LATEST_URL/install-crowbarr.sh" -o /usr/local/sbin/crowbarr-update
chmod 0755 /usr/local/sbin/crowbarr-update

systemctl daemon-reload
systemctl enable --now crowbarr.service

for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8449/health >/dev/null; then
    echo "Crowbarr is running at http://localhost:8449"
    echo "Update later with: sudo crowbarr-update"
    exit 0
  fi
  sleep 1
done

systemctl status crowbarr.service --no-pager || true
echo "Crowbarr was installed, but its health check did not pass." >&2
exit 1
