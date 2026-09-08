#!/usr/bin/env bash
set -euo pipefail
umask 022

REPOSITORY=amanofvaly/crowbarr
CONFIG_DIR=/etc/crowbarr
CONFIG_FILE=$CONFIG_DIR/installer.env
UNIT_DIR=/etc/systemd/system
UPDATER=/usr/local/sbin/crowbarr-update
LATEST_URL="https://github.com/$REPOSITORY/releases/latest/download"
fail() { echo "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail "Native installs require Linux x86_64."
[[ "$EUID" -eq 0 ]] || fail "Run this installer with sudo."
for command in systemctl flock stat install mktemp tar sha256sum getent readlink; do
  command -v "$command" >/dev/null || fail "Missing prerequisite: $command"
done
[[ -d /run/systemd/system ]] || fail "A running systemd system is required."
systemctl show --property=Version --value >/dev/null || fail "Cannot contact systemd."
exec 9>/run/lock/crowbarr-install.lock
flock -n 9 || fail "Another Crowbarr install is running."

# Settings are data, never shell code. Only validated, unquoted values are written.
secure_path() {
  local path=$1 mode
  while [[ "$path" != / ]]; do
    [[ ! -L "$path" ]] || fail "Refusing symlink in privileged path: $path"
    if [[ -e "$path" ]]; then
      [[ "$(stat -c %u "$path")" == 0 ]] || fail "Privileged path must be root-owned: $path"
      mode=$(stat -c %a "$path")
      (( (8#$mode & 0022) == 0 )) || fail "Privileged path must not be writable by other users: $path"
    fi
    path=$(dirname "$path")
  done
}
secure_path "$CONFIG_FILE"
secure_path "$UNIT_DIR"
secure_path "$UPDATER"
if [[ -f "$CONFIG_FILE" ]]; then
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    case "$key" in
      CROWBARR_USER|CROWBARR_GROUP|CROWBARR_DATA_DIR|CROWBARR_INSTALL_DIR|CROWBARR_AUTO_UPDATE)
        [[ -n "$value" ]] || fail "Empty setting: $key"
        if ! declare -p "$key" >/dev/null 2>&1; then printf -v "$key" '%s' "$value"; fi ;;
      *) fail "Unknown installer setting: $key" ;;
    esac
  done < "$CONFIG_FILE"
elif [[ -f "$UNIT_DIR/crowbarr.service" ]]; then
  # Migrate the exact unit format written by the previous native installer.
  while IFS= read -r line; do
    case "$line" in
      User=*) CROWBARR_USER=${CROWBARR_USER-${line#User=}} ;;
      Group=*) CROWBARR_GROUP=${CROWBARR_GROUP-${line#Group=}} ;;
      Environment=CROWBARR_DATA=*) CROWBARR_DATA_DIR=${CROWBARR_DATA_DIR-${line#Environment=CROWBARR_DATA=}} ;;
      ExecStart=*/crowbarr\ --host*)
        legacy_path=${line#ExecStart=}
        CROWBARR_INSTALL_DIR=${CROWBARR_INSTALL_DIR-${legacy_path%/crowbarr --host*}} ;;
    esac
  done < "$UNIT_DIR/crowbarr.service"
  [[ -n ${CROWBARR_USER:-} && -n ${CROWBARR_GROUP:-} && -n ${CROWBARR_DATA_DIR:-} && -n ${CROWBARR_INSTALL_DIR:-} ]] ||
    fail "Cannot migrate custom service; specify CROWBARR_USER/GROUP/DATA_DIR/INSTALL_DIR explicitly."
fi

SERVICE_USER=${CROWBARR_USER-crowbarr}
INSTALL_DIR=${CROWBARR_INSTALL_DIR-/opt/crowbarr}
DATA_DIR=${CROWBARR_DATA_DIR-/var/lib/crowbarr}
AUTO_UPDATE=${CROWBARR_AUTO_UPDATE-false}
RELEASE=${CROWBARR_VERSION-latest}
[[ "$SERVICE_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]] || fail "Invalid CROWBARR_USER."
[[ "$AUTO_UPDATE" == true || "$AUTO_UPDATE" == false ]] || fail "CROWBARR_AUTO_UPDATE must be true or false."
[[ "$RELEASE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || fail "Invalid CROWBARR_VERSION."
for path in "$INSTALL_DIR" "$DATA_DIR"; do
  [[ "$path" =~ ^/[-a-zA-Z0-9_./]+$ && "$path" != / && "$path" != */ && "$path" != *//* && "$path" != */../* && "$path" != */.. && "$path" != */./* && "$path" != */. ]] ||
    fail "Install/data paths must be absolute, normalized paths without spaces or shell/systemd metacharacters."
done
VERSIONS_DIR=${INSTALL_DIR}.versions
[[ "$DATA_DIR" != "$INSTALL_DIR" && "$DATA_DIR" != "$INSTALL_DIR/"* && "$INSTALL_DIR" != "$DATA_DIR/"* && "$DATA_DIR" != "$VERSIONS_DIR" && "$DATA_DIR" != "$VERSIONS_DIR/"* ]] ||
  fail "Install and data directories must be separate."
secure_path "$(dirname "$INSTALL_DIR")"
secure_path "$VERSIONS_DIR"
if [[ -L "$INSTALL_DIR" ]]; then
  secure_path "$(readlink -f "$INSTALL_DIR")"
else
  secure_path "$INSTALL_DIR"
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  [[ "$SERVICE_USER" == crowbarr ]] || fail "User $SERVICE_USER does not exist."
  command -v useradd >/dev/null || fail "Missing prerequisite: useradd"
  command -v groupadd >/dev/null || fail "Missing prerequisite: groupadd"
  getent group crowbarr >/dev/null || groupadd --system crowbarr
  useradd --system --gid crowbarr --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin crowbarr
fi
[[ ${CROWBARR_USER+x} || "$(id -u "$SERVICE_USER")" != 0 ]] || fail "Default crowbarr account must be nonroot."
SERVICE_GROUP=${CROWBARR_GROUP-$(id -gn "$SERVICE_USER")}
[[ "$SERVICE_GROUP" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]] || fail "Invalid CROWBARR_GROUP."
getent group "$SERVICE_GROUP" >/dev/null || fail "Group $SERVICE_GROUP does not exist."

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl ffmpeg libgomp1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl ffmpeg libgomp
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --needed --noconfirm ca-certificates curl ffmpeg gcc-libs
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install ca-certificates curl ffmpeg libgomp1
  else
    fail "Install curl, ffmpeg, ffprobe and the OpenMP runtime, then retry."
  fi
}
if ! command -v curl >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then install_packages; fi
for command in curl ffmpeg ffprobe; do
  command -v "$command" >/dev/null || fail "Missing prerequisite after package installation: $command"
done

RELEASE_URL=$LATEST_URL
[[ "$RELEASE" == latest ]] || RELEASE_URL="https://github.com/$REPOSITORY/releases/download/v${RELEASE#v}"
TEMP_DIR=$(mktemp -d)
NEW_VERSION=
OLD_VERSION=
TRANSACTION=false
SWITCHED=false
WAS_ACTIVE=false
WAS_ENABLED=false
TIMER_ENABLED=false
TIMER_ACTIVE=false
systemctl is-active --quiet crowbarr.service && WAS_ACTIVE=true
systemctl is-enabled --quiet crowbarr.service && WAS_ENABLED=true
systemctl is-enabled --quiet crowbarr-update.timer && TIMER_ENABLED=true
systemctl is-active --quiet crowbarr-update.timer && TIMER_ACTIVE=true
FILES=("$UNIT_DIR/crowbarr.service" "$UNIT_DIR/crowbarr-update.service" "$UNIT_DIR/crowbarr-update.timer" "$CONFIG_FILE" "$UPDATER")
for i in "${!FILES[@]}"; do
  [[ ! -e "${FILES[$i]}" ]] || cp -p "${FILES[$i]}" "$TEMP_DIR/backup.$i"
done
finish() {
  local result=$?
  trap - EXIT INT TERM
  set +e
  if $TRANSACTION; then
    echo "Install failed; restoring the previous binary and service settings." >&2
    systemctl stop crowbarr.service
    if $SWITCHED && [[ -n "$OLD_VERSION" ]]; then
      ln -s "$OLD_VERSION" "$VERSIONS_DIR/rollback.$$" &&
        mv -Tf "$VERSIONS_DIR/rollback.$$" "$INSTALL_DIR"
    elif $SWITCHED; then
      rm -f "$INSTALL_DIR"
    fi
    for i in "${!FILES[@]}"; do
      if [[ -f "$TEMP_DIR/backup.$i" ]]; then cp -p "$TEMP_DIR/backup.$i" "${FILES[$i]}"; else rm -f "${FILES[$i]}"; fi
    done
    systemctl daemon-reload
    if $WAS_ENABLED; then systemctl enable crowbarr.service; else systemctl disable crowbarr.service; fi
    if $WAS_ACTIVE; then systemctl start crowbarr.service || echo "Previous service could not restart; inspect journalctl -u crowbarr." >&2; fi
    systemctl disable --now crowbarr-update.timer 2>/dev/null
    if $TIMER_ENABLED; then systemctl enable crowbarr-update.timer; fi
    if $TIMER_ACTIVE; then systemctl start crowbarr-update.timer; fi
  fi
  # Do not delete a binary still referenced if restoring the symlink failed.
  if [[ -n "$NEW_VERSION" && "$(readlink -f "$INSTALL_DIR")" != "$NEW_VERSION/crowbarr" ]]; then rm -rf "$NEW_VERSION"; fi
  rm -rf "$TEMP_DIR"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

curl -fL --retry 3 "$RELEASE_URL/crowbarr-linux-x86_64.tar.gz.sha256" -o "$TEMP_DIR/checksum"
read -r checksum _ < "$TEMP_DIR/checksum"
[[ "$checksum" =~ ^[a-fA-F0-9]{64}$ ]] || fail "Invalid release checksum."
TARGET_VERSION=$VERSIONS_DIR/$checksum
cat > "$TEMP_DIR/settings" <<EOF
CROWBARR_USER=$SERVICE_USER
CROWBARR_GROUP=$SERVICE_GROUP
CROWBARR_DATA_DIR=$DATA_DIR
CROWBARR_INSTALL_DIR=$INSTALL_DIR
CROWBARR_AUTO_UPDATE=$AUTO_UPDATE
EOF
if [[ -L "$INSTALL_DIR" && "$(readlink -f "$INSTALL_DIR")" == "$TARGET_VERSION/crowbarr" ]] &&
   [[ -x "$INSTALL_DIR/crowbarr" && -f "$UPDATER" && -f "$UNIT_DIR/crowbarr.service" ]] &&
   cmp -s "$TEMP_DIR/settings" "$CONFIG_FILE" && $WAS_ACTIVE; then
  echo "Crowbarr is already up to date."
  exit 0
fi
curl -fL --retry 3 "$RELEASE_URL/crowbarr-linux-x86_64.tar.gz" -o "$TEMP_DIR/package.tar.gz"
(cd "$TEMP_DIR"; printf '%s  package.tar.gz\n' "$checksum" | sha256sum -c -)
# Fetch before downtime; this also supports curl | sudo bash installation.
# A binary rollback must not restore an old updater that forgets persisted settings.
curl -fL --retry 3 "$LATEST_URL/install-crowbarr.sh" -o "$TEMP_DIR/updater"
bash -n "$TEMP_DIR/updater"
install -d -m 0755 "$VERSIONS_DIR"
secure_path "$TARGET_VERSION"
if [[ ! -d "$TARGET_VERSION" ]]; then
  NEW_VERSION=$(mktemp -d "$VERSIONS_DIR/release.XXXXXXXX")
  chmod 0755 "$NEW_VERSION"
  tar --extract --gzip --file "$TEMP_DIR/package.tar.gz" --directory "$NEW_VERSION" --no-same-owner
  [[ -x "$NEW_VERSION/crowbarr/crowbarr" ]] || fail "Release has no executable crowbarr/crowbarr."
  chown -R root:root "$NEW_VERSION"
  chmod -R go-w "$NEW_VERSION"
  mv "$NEW_VERSION" "$TARGET_VERSION"
  NEW_VERSION=$TARGET_VERSION
fi
[[ -x "$TARGET_VERSION/crowbarr/crowbarr" ]] || fail "Installed release has no executable."
install -d -m 0755 "$CONFIG_DIR" "$(dirname "$UPDATER")"
if [[ ! -d "$DATA_DIR" ]]; then install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"; fi

if [[ -L "$INSTALL_DIR" ]]; then
  OLD_VERSION=$(readlink -f "$INSTALL_DIR")
elif [[ -e "$INSTALL_DIR" ]]; then
  OLD_VERSION="$VERSIONS_DIR/legacy.$(date +%s).$$"
fi
TRANSACTION=true
if [[ -f "$UNIT_DIR/crowbarr.service" ]]; then systemctl stop crowbarr.service; fi
if [[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]]; then
  mv "$INSTALL_DIR" "$OLD_VERSION"
  SWITCHED=true
fi
ln -s "$TARGET_VERSION/crowbarr" "$VERSIONS_DIR/current.$$"
mv -Tf "$VERSIONS_DIR/current.$$" "$INSTALL_DIR"
SWITCHED=true

cat > "$UNIT_DIR/crowbarr.service" <<EOF
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
cat > "$TEMP_DIR/settings" <<EOF
CROWBARR_USER=$SERVICE_USER
CROWBARR_GROUP=$SERVICE_GROUP
CROWBARR_DATA_DIR=$DATA_DIR
CROWBARR_INSTALL_DIR=$INSTALL_DIR
CROWBARR_AUTO_UPDATE=$AUTO_UPDATE
EOF
install -o root -g root -m 0600 "$TEMP_DIR/settings" "$CONFIG_FILE"
install -o root -g root -m 0755 "$TEMP_DIR/updater" "$UPDATER"
cat > "$UNIT_DIR/crowbarr-update.service" <<EOF
[Unit]
Description=Update Crowbarr native installation
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$UPDATER
TimeoutStartSec=30min
EOF
cat > "$UNIT_DIR/crowbarr-update.timer" <<'EOF'
[Unit]
Description=Check daily for Crowbarr updates

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now crowbarr.service
healthy=false
for _ in {1..20}; do
  if systemctl is-active --quiet crowbarr.service && curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8449/health >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
$healthy || fail "New release failed its health check. Data is preserved; binary rollback cannot undo database migrations."
if [[ "$AUTO_UPDATE" == true ]]; then
  systemctl enable --now crowbarr-update.timer
else
  systemctl disable --now crowbarr-update.timer
fi
TRANSACTION=false
NEW_VERSION= # Retain installed versions, including the previous working binary.
echo "Crowbarr is running at http://localhost:8449"
echo "Update with: sudo crowbarr-update (settings: $CONFIG_FILE; auto-update: $AUTO_UPDATE)"
