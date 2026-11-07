#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-systemd-unit.sh — generate a systemd unit that runs the dlbot Docker
# container, so it starts on boot and is supervised by systemd.
#
#   sudo ./scripts/install-systemd-unit.sh            # prompts for the token
#   ./scripts/install-systemd-unit.sh --dry-run        # print the unit, write nothing
#   sudo ./scripts/install-systemd-unit.sh --enable     # also enable --now
#
# Security notes:
#   * The token is read with echo disabled and written ONLY to a 0600, root-owned
#     env file. It never lands in the unit file, in `ps` output, in the journal,
#     in shell history, or in this script's own output.
#   * The repository's own local config file is deliberately not used: the unit
#     pins Compose's interpolation to the generated env file with --env-file.
#   * Requires the repository to stay where it is installed (the unit bakes in
#     absolute paths). Re-run this script after moving it.
# ---------------------------------------------------------------------------
set -euo pipefail
umask 077

UNIT_NAME="dlbot.service"
UNIT_DIR="/etc/systemd/system"
ENV_FILE="/etc/dlbot/dlbot.env"
COMPOSE_FILE_NAME="docker-compose.yml"
REPO_DIR=""
DO_BUILD=1
DO_ENABLE=0
FORCE=0
DRY_RUN=0
TOKEN_FROM_STDIN=0

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/$(basename -- "${BASH_SOURCE[0]}")"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

usage() {
  cat <<'USAGE'
Usage: install-systemd-unit.sh [options]

Writes a systemd unit that runs `docker compose up` for dlbot in the foreground
(systemd supervises and restarts it; `journalctl -u dlbot` shows the bot logs)
plus a 0600 env file holding the Discord token.

Options:
  -r, --repo DIR       Project directory holding docker-compose.yml
                       (default: detected from this script's location)
  -u, --unit NAME      Unit file name                   (default: dlbot.service)
  -o, --unit-dir DIR   Where to write the unit          (default: /etc/systemd/system)
  -e, --env-file PATH  Where the token is stored        (default: /etc/dlbot/dlbot.env)
      --token-stdin    Read the token from stdin, no prompt (for automation)
  -b, --skip-build     Do not build the image now (the unit still works)
      --enable         systemctl enable --now the unit when done
  -f, --force          Replace existing files without asking
  -n, --dry-run        Print the generated unit to stdout and change nothing
  -h, --help           Show this help

Examples:
  ./scripts/install-systemd-unit.sh --dry-run    # review the unit first, write nothing
  sudo ./scripts/install-systemd-unit.sh         # prompt for the token, build, install
  sudo ./scripts/install-systemd-unit.sh --enable  # ... and systemctl enable --now
  # Headless (run 'sudo -v' first so sudo does not need the terminal for a password;
  # 'read -rs' also keeps the token out of your shell history):
  #   read -rs T; printf '%s\n' "$T" | sudo ./scripts/install-systemd-unit.sh --token-stdin
USAGE
}

# --- argument parsing ------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--repo)     [[ $# -ge 2 ]] || die "$1 needs an argument"; REPO_DIR="$2"; shift 2 ;;
    -u|--unit)     [[ $# -ge 2 ]] || die "$1 needs an argument"; UNIT_NAME="$2"; shift 2 ;;
    -o|--unit-dir) [[ $# -ge 2 ]] || die "$1 needs an argument"; UNIT_DIR="$2"; shift 2 ;;
    -e|--env-file) [[ $# -ge 2 ]] || die "$1 needs an argument"; ENV_FILE="$2"; shift 2 ;;
    -b|--skip-build) DO_BUILD=0; shift ;;
    --token-stdin) TOKEN_FROM_STDIN=1; shift ;;
    --enable)      DO_ENABLE=1; shift ;;
    -f|--force)    FORCE=1; shift ;;
    -n|--dry-run)  DRY_RUN=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown option '$1' (see --help)" ;;
  esac
done

UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
UNIT_BASE="${UNIT_NAME%.service}"

case "$UNIT_NAME" in
  *.service) : ;;
  *) die "--unit must end in .service (got '$UNIT_NAME')" ;;
esac

# --- locate the project ---------------------------------------------------
if [[ -z "$REPO_DIR" ]]; then
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  for candidate in "$script_dir/.." "$script_dir" "$PWD"; do
    if [[ -f "$candidate/$COMPOSE_FILE_NAME" ]]; then
      REPO_DIR="$(cd -- "$candidate" && pwd)"
      break
    fi
  done
fi
[[ -n "$REPO_DIR" ]] || die "could not find $COMPOSE_FILE_NAME — pass --repo DIR"
[[ -d "$REPO_DIR" ]] || die "not a directory: $REPO_DIR"
REPO_DIR="$(cd -- "$REPO_DIR" && pwd)"
COMPOSE_FILE="$REPO_DIR/$COMPOSE_FILE_NAME"
[[ -f "$COMPOSE_FILE" ]] || die "$COMPOSE_FILE not found"
info "project: $REPO_DIR"

# --- tooling -------------------------------------------------------------
DOCKER_BIN="$(command -v docker 2>/dev/null || true)"
[[ -n "$DOCKER_BIN" ]] || die "docker not found in PATH"
[[ -x "$DOCKER_BIN" ]] || die "docker is not executable: $DOCKER_BIN"
"$DOCKER_BIN" compose version >/dev/null 2>&1 \
  || die "the 'docker compose' CLI plugin is required (the legacy docker-compose binary is not used by this unit)"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this host is not running systemd"

# The dependency must be a *unit name*, not the docker binary path. Rootless
# Docker runs the daemon as a user unit, which a system service cannot require.
DOCKER_UNIT="docker.service"
if systemctl cat docker.service >/dev/null 2>&1; then
  DOCKER_UNIT="docker.service"
elif systemctl cat docker.socket >/dev/null 2>&1; then
  DOCKER_UNIT="docker.socket"
  warn "docker.service not found; depending on docker.socket instead"
else
  warn "neither docker.service nor docker.socket exists on this host. The unit will still be generated with Requires=docker.service — edit it if you use rootless Docker (then the daemon is a *user* unit and this service should be installed under ~/.config/systemd/user/ instead)."
fi

if ((DRY_RUN == 0)); then
  "$DOCKER_BIN" info >/dev/null 2>&1 \
    || die "cannot reach the Docker daemon (run with sudo, or add this user to the 'docker' group)"
  [[ -w "$UNIT_DIR" ]] || die "$UNIT_DIR is not writable — run this script with sudo"
  env_dir="$(dirname -- "$ENV_FILE")"
  [[ -d "$env_dir" || -w "$(dirname -- "$env_dir")" ]] \
    || die "cannot create $env_dir — run this script with sudo"
fi
# --- collect the token ----------------------------------------------------
looks_like_token() {
  # Discord tokens are three dot-separated base64url-ish segments.
  local t="$1"
  [[ "${#t}" -ge 40 ]] && [[ "$t" != *[[:space:]]* ]] && [[ "$(tr -cd '.' <<<"$t" | wc -c)" -ge 2 ]]
}

prompt_token() {
  local first="" second=""
  if ((TOKEN_FROM_STDIN)); then
    # read returns non-zero at EOF even without a newline, but still fills the
    # variable — so accept that case and only fail on genuinely empty input.
    IFS= read -r first || true
    [[ -n "$first" ]] || die "no token received on stdin"
  else
    printf 'Discord bot token (input hidden): ' >&2
    IFS= read -r -s first || { printf '\n' >&2; die "aborted"; }
    printf '\n' >&2
    printf 'Repeat the token: ' >&2
    IFS= read -r -s second || { printf '\n' >&2; die "aborted"; }
    printf '\n' >&2
    [[ "$first" == "$second" ]] || die "the two tokens did not match"
  fi
  first="${first%$'\r'}"                       # tolerate CRLF pastes
  printf -v TOKEN '%s' "${first//[[:space:]]/}"  # strip stray whitespace
  [[ -n "$TOKEN" ]] || die "empty token"
  if ! looks_like_token "$TOKEN"; then
    warn "that does not look like a Discord token (expected 3 dot-separated parts, >=40 chars)"
    if ((TOKEN_FROM_STDIN == 0)); then
      printf 'Use it anyway? [y/N] ' >&2
      IFS= read -r reply || reply=""
      [[ "${reply,,}" == "y" ]] || die "aborted"
    else
      warn "--token-stdin given: continuing anyway"
    fi
  fi
  info "token accepted (${#TOKEN} characters, value not shown)"
}

confirm_overwrite() {  # $1 = path, $2 = description
  [[ -e "$1" ]] || return 0
  ((FORCE)) && { info "replacing $2 ($1) as requested"; return 0; }
  ((DRY_RUN)) && return 0
  # stdin is the token pipe in --token-stdin mode: never try to prompt on it.
  ((TOKEN_FROM_STDIN)) && die "$2 already exists at $1 — pass --force to replace it"
  printf '%s already exists at %s — overwrite? [y/N] ' "$2" "$1" >&2
  IFS= read -r reply || reply=""
  [[ "${reply,,}" == "y" ]] || die "refusing to overwrite $1 (use --force to override)"
}

# --- host user ids: the image's non-root uid must match the data owners ----
owner_uid=1000
owner_gid=1000
for dir in "$REPO_DIR/data" "$REPO_DIR/downloads" "$REPO_DIR/logs" "$REPO_DIR"; do
  if [[ -e "$dir" ]] && stat -c '%u %g' "$dir" >/dev/null 2>&1; then
    read -r owner_uid owner_gid < <(stat -c '%u %g' "$dir")
    break
  fi
done
info "bind-mounted state is owned by uid=$owner_uid gid=$owner_gid (passed to the image build)"

if ((DRY_RUN == 0)); then
  prompt_token
fi

# --- the unit -------------------------------------------------------------
# The template below is a QUOTED heredoc ('TEMPLATE'): nothing in it is expanded
# by the shell, so prose like 'docker compose' can never be executed at generate
# time. (An unquoted heredoc does exactly that: backticked words run and their
# output gets spliced into the unit, which is what makes systemd emit pages of
# "Missing '='".) Values are substituted afterwards with sed.
generated_on="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

sed_repl() {  # escape a value for the right-hand side of s|a|b|
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//&/\\&}"
  s="${s//|/\\|}"
  printf '%s' "$s"
}

unit_template="$(cat <<'TEMPLATE'
# Generated by @SCRIPT@ on @STAMP@
# Regenerate instead of hand-editing:  sudo @SCRIPT@ --force
#
# Runs 'docker compose up' in the foreground so systemd supervises the container
# and the bot's console output lands in the journal.
# The Discord token is NOT in this file: Compose reads it from @ENV@ (0600).

[Unit]
Description=dlbot — Discord video download bot (Docker: bot + yt-dlp worker)
Documentation=file://@REPO@/README.md
Requires=@DOCKER_UNIT@
After=@DOCKER_UNIT@
Wants=network-online.target
After=network-online.target
# Caps restarts of the Compose client itself (misconfigured daemon, etc.).
# App-level crashes are restarted by the container's own restart policy in
# docker-compose.yml, so a bad token shows up in the journal rather than here.
StartLimitIntervalSec=600
StartLimitBurst=30

[Service]
Type=simple
WorkingDirectory=@REPO@
# Best-effort image refresh after a git pull. The leading '-' means a failure here
# never blocks boot; 'docker compose up' would build a missing image anyway.
ExecStartPre=-@DOCKER@ compose --env-file @ENV@ -f @COMPOSE@ build
# --env-file pins interpolation to the generated file, so Compose never depends on
# files in the checkout. 'up' without -d keeps this unit attached to the container
# and forwards SIGTERM/SIGINT to it for a graceful stop.
ExecStart=@DOCKER@ compose --env-file @ENV@ -f @COMPOSE@ up --remove-orphans
# 'always' is systemd's closest match to Docker's 'unless-stopped': restart on any
# exit, while an explicit 'systemctl stop' is honoured and stops everything.
Restart=always
RestartSec=10
TimeoutStartSec=15min
# docker-compose.yml sets stop_grace_period: 15m so an in-flight download can
# finish; systemd's 90s default would cut that short, so it must be longer.
TimeoutStopSec=16min
KillMode=control-group
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
TEMPLATE
)"

unit_body="$(printf '%s\n' "$unit_template" | sed \
  -e "s|@SCRIPT@|$(sed_repl "$SCRIPT_PATH")|g" \
  -e "s|@STAMP@|$(sed_repl "$generated_on")|g" \
  -e "s|@REPO@|$(sed_repl "$REPO_DIR")|g" \
  -e "s|@COMPOSE@|$(sed_repl "$COMPOSE_FILE")|g" \
  -e "s|@ENV@|$(sed_repl "$ENV_FILE")|g" \
  -e "s|@DOCKER_UNIT@|$(sed_repl "$DOCKER_UNIT")|g" \
  -e "s|@DOCKER@|$(sed_repl "$DOCKER_BIN")|g" \
)"

if [[ "$unit_body" == *"@"* && "$unit_body" =~ @(SCRIPT|STAMP|REPO|COMPOSE|ENV|DOCKER|DOCKER_UNIT)@ ]]; then
  die "internal error: an unsubstituted placeholder survived into the unit"
fi

if ((DRY_RUN)); then
  info "dry run — nothing was written"
  printf '%s\n' "$unit_body"
  exit 0
fi
# --- state directories ----------------------------------------------------
# Docker bind-mounts ./data, ./downloads and ./logs. These are gitignored, so on
# a fresh checkout they are missing and Docker would create them as root:root —
# the container's non-root user could then not write the queue DB. Create them
# up front with the right ownership.
for sub in data downloads logs; do
  target="$REPO_DIR/$sub"
  if [[ ! -d "$target" ]]; then
    if mkdir -p -- "$target" 2>/dev/null; then
      # umask is 077 in this script: set the mode explicitly so the directories
      # match the repo's normal 0775 and stay browsable by the owning user's group.
      chmod 0775 -- "$target" 2>/dev/null || true
      chown "$owner_uid:$owner_gid" "$target" 2>/dev/null || true
      info "created $sub/ (0775) owned by $owner_uid:$owner_gid"
    else
      warn "could not create $target — the container will fail to start until it exists and is writable by uid $owner_uid"
    fi
  fi
done

# --- credential file ------------------------------------------------------
confirm_overwrite "$ENV_FILE" "the credential file"
mkdir -p -- "$(dirname -- "$ENV_FILE")"
tmp_env="$(mktemp -- "$(dirname -- "$ENV_FILE")/${UNIT_BASE}.env.XXXXXX")"
cleanup_tmp() { [[ -n "${tmp_env:-}" && -e "$tmp_env" ]] && rm -f -- "$tmp_env"; return 0; }
trap cleanup_tmp EXIT

{
  printf '# dlbot credentials for systemd — generated by %s on %s\n' "$SCRIPT_PATH" "$generated_on"
  printf '# Keep this file root-owned and mode 0600. The unit file never contains\n'
  printf '# the token; it only hands this path to `docker compose --env-file`.\n\n'
  printf 'DISCORD_TOKEN=%s\n' "$TOKEN"
  printf '\n# uid/gid baked into the image so it can write ./data ./downloads ./logs\n'
  printf 'DLBOT_UID=%s\nDLBOT_GID=%s\n' "$owner_uid" "$owner_gid"
  printf '\n# Optional extras (uncomment; full list in .env.example). After editing:\n'
  printf '#   sudo systemctl restart %s\n' "$UNIT_NAME"
  printf '# DISCORD_GUILD_ID=             # guild-scoped slash commands: instant sync\n'
  printf '# DISCORD_CHANNEL_ID=           # restrict /download to one channel\n'
  printf '# DLBOT_DEFAULT_FORMAT=video    # audio | video\n'
  printf '# DLBOT_MAX_ATTEMPTS=2\n'
  printf '# DLBOT_WORKER_POLL_INTERVAL=3\n'
  printf '# DLBOT_LOG_LEVEL=INFO\n'
  printf '# DLBOT_COOKIES_FILE=/app/cookies.txt   # also add the mount in docker-compose.yml\n'
} > "$tmp_env"
chmod 600 -- "$tmp_env"
mv -f -- "$tmp_env" "$ENV_FILE"
tmp_env=""
TOKEN=""                      # drop it from the script environment once written
info "wrote $ENV_FILE (0600, token stored — not shown here)"

# --- unit file ------------------------------------------------------------
confirm_overwrite "$UNIT_PATH" "unit file"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -f "$UNIT_PATH" ]]; then
  backup="$UNIT_PATH.bak.$stamp"
  cp -p -- "$UNIT_PATH" "$backup"
  info "backed up the previous unit to $backup"
fi
tmp_unit="$(mktemp -- "$(dirname -- "$UNIT_PATH")/${UNIT_NAME}.XXXXXX")"
printf '%s\n' "$unit_body" > "$tmp_unit"
chmod 644 -- "$tmp_unit"
mv -f -- "$tmp_unit" "$UNIT_PATH"
info "wrote $UNIT_PATH"

# --- build ----------------------------------------------------------------
if ((DO_BUILD)); then
  info "building the image (first build pulls ffmpeg + deps and takes a few minutes)"
  "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build \
    || die "image build failed — fix it and re-run this script"
else
  info "skipping the build (--skip-build); 'docker compose up' will build a missing image"
fi

# --- sanity checks --------------------------------------------------------
info "verifying Compose resolves the configuration from $ENV_FILE"
if "$DOCKER_BIN" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet >/dev/null 2>&1; then
  info "configuration OK"
else
  # Never print the resolved config here: it would include the token.
  warn "the configuration did not validate — inspect with: sudo docker compose -f $COMPOSE_FILE config --quiet"
fi

if systemctl daemon-reload 2>/dev/null; then
  info "systemd daemon reloaded"
else
  warn "could not run 'systemctl daemon-reload' (needs root) — run it yourself before enabling"
fi
if command -v systemd-analyze >/dev/null 2>&1; then
  # 'verify' exits 0 even when it ignores malformed lines, so judge its output,
  # not its status code.
  verify_out="$(systemd-analyze verify "$UNIT_PATH" 2>&1 || true)"
  if [[ -n "${verify_out//[$'\n' ]/}" ]]; then
    printf 'WARNING: systemd-analyze verify reported problems with %s:\n%s\n' "$UNIT_PATH" "$verify_out" | sed 's/^/    /'
    warn "do not enable the unit until these lines are resolved (re-run this script after fixing)"
  else
    info "systemd-analyze verify: unit parses cleanly"
  fi
fi

if ((DO_ENABLE)); then
  info "enabling and starting $UNIT_NAME"
  systemctl enable --now "$UNIT_NAME"
else
  info "not enabled yet (pass --enable to do it in one step)"
fi

# --- next steps -----------------------------------------------------------
cat <<EOF

Done. To start dlbot on every boot:

    sudo systemctl enable --now $UNIT_NAME
    systemctl status $UNIT_NAME
    journalctl -u $UNIT_NAME -f

Handy operations:

    sudo systemctl restart $UNIT_NAME     # after editing $ENV_FILE
    sudo systemctl stop $UNIT_NAME        # drains an in-flight download (up to 15 min)
    sudo docker compose -f $COMPOSE_FILE logs -f dlbot

Troubleshooting:

    systemctl status $UNIT_NAME                      # exit code + last log lines
    sudo sqlite3 $REPO_DIR/data/dlbot.db "select id,status,error from download_queue order by id desc limit 5;"
    ls -ld $REPO_DIR/{data,downloads,logs}           # must be writable by uid $owner_uid

Note: the unit has absolute paths baked in. If the checkout moves or the token
changes, re-run:  sudo $SCRIPT_PATH --force

To remove it later:

    sudo systemctl disable --now $UNIT_NAME
    sudo rm -f $UNIT_PATH $ENV_FILE
    sudo systemctl daemon-reload
EOF
