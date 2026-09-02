#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${SUB2API_DATA_DIR:-/app/data}"
LOG_DIR="${SUB2API_LOG_DIR:-/app/logs}"
CONFIG_FILE="${SUB2API_CONFIG_FILE:-${DATA_DIR}/config.json}"
OUTLOOK_DATA_DIR="${OUTLOOKEMAIL_DATA_DIR:-${DATA_DIR}/outlookemail}"
OUTLOOK_RUNTIME_ENV="${OUTLOOKEMAIL_RUNTIME_ENV:-${OUTLOOK_DATA_DIR}/runtime.env}"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$DATA_DIR/accounts" "$OUTLOOK_DATA_DIR"

# 创建缺失的默认配置；已有 config.json 一律不修改，磁盘配置是唯一状态源。
python /app/docker/config_bootstrap.py "$CONFIG_FILE"

# Load the private OutlookEmail runtime file as data, not shell code.  This
# keeps migrated secrets in the unified data root while rejecting malformed
# lines and command substitutions.
load_outlook_runtime_env() {
  local line key value
  if [[ -f "$OUTLOOK_RUNTIME_ENV" ]]; then
    chmod 600 "$OUTLOOK_RUNTIME_ENV"
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line#${line%%[![:space:]]*}}"
      [[ -z "$line" || "${line:0:1}" == "#" || "$line" != *=* ]] && continue
      key="${line%%=*}"
      value="${line#*=}"
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      case "$key" in
        LOGIN_PASSWORD|SECRET_KEY) ;;
        *) continue ;;
      esac
      if [[ ${#value} -ge 2 ]] && { [[ "${value:0:1}" == "\"" && "${value: -1}" == "\"" ]] || [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; }; then
        value="${value:1:${#value}-2}"
      fi
      export "$key=$value"
    done < "$OUTLOOK_RUNTIME_ENV"
  elif [[ -n "${LOGIN_PASSWORD:-}" && -n "${SECRET_KEY:-}" ]]; then
    umask 077
    {
      printf 'LOGIN_PASSWORD=%s\n' "$LOGIN_PASSWORD"
      printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
    } > "$OUTLOOK_RUNTIME_ENV"
    chmod 600 "$OUTLOOK_RUNTIME_ENV"
  fi
}

load_outlook_runtime_env

# The embedded app always owns this path.  A legacy relative DATABASE_PATH is
# intentionally ignored so the old two-container data cannot be reopened.
export OUTLOOK_EMAIL_HOME="$OUTLOOK_DATA_DIR"
export DATABASE_PATH="$OUTLOOK_DATA_DIR/outlook_accounts.db"
export OUTLOOKEMAIL_DATA_DIR="$OUTLOOK_DATA_DIR"
export OUTLOOKEMAIL_RUNTIME_ENV="$OUTLOOK_RUNTIME_ENV"
export HOST="${OUTLOOKEMAIL_HOST:-0.0.0.0}"
export PORT="5000"
export FLASK_ENV="${FLASK_ENV:-production}"
export DOCKER_UPDATE_ENABLED="false"

if [[ -z "${SECRET_KEY:-}" || -z "${LOGIN_PASSWORD:-}" ]]; then
  echo "[docker] OutlookEmail requires SECRET_KEY and LOGIN_PASSWORD from $OUTLOOK_RUNTIME_ENV or the Compose env file" >&2
  exit 1
fi

# Bind mount 可能由宿主机 root 创建，启动时修正容器内权限。
chown -R app:app "$DATA_DIR" "$LOG_DIR"

LOG_FILE="$LOG_DIR/container-$(date -u +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[docker] DISPLAY=${DISPLAY:-:99}"
echo "[docker] 浏览器后端: Camoufox"
echo "[docker] 浏览器模式: 有头（Xvfb 虚拟显示器）"
echo "[docker] 配置: $CONFIG_FILE"
echo "[docker] 数据: $DATA_DIR"
echo "[docker] OutlookEmail 数据: $OUTLOOK_DATA_DIR"
echo "[docker] 日志: $LOG_FILE"

export HOME=/home/app
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/opt/camoufox-cache}
export DISPLAY=${DISPLAY:-:99}
export SUB2API_FORCE_HEADED=${SUB2API_FORCE_HEADED:-1}

mail_pid=0
app_pid=0

stop_children() {
  local status=0
  trap - TERM INT
  if [[ "$mail_pid" -gt 0 ]] && kill -0 "$mail_pid" 2>/dev/null; then
    kill -TERM "$mail_pid" 2>/dev/null || true
  fi
  if [[ "$app_pid" -gt 0 ]] && kill -0 "$app_pid" 2>/dev/null; then
    kill -TERM "$app_pid" 2>/dev/null || true
  fi
  [[ "$mail_pid" -gt 0 ]] && wait "$mail_pid" 2>/dev/null || status=$?
  [[ "$app_pid" -gt 0 ]] && wait "$app_pid" 2>/dev/null || status=$?
  return "$status"
}

trap 'stop_children; exit 143' TERM INT

(
  cd /app/vendor/outlookEmail
  exec gosu app /opt/outlookemail-venv/bin/gunicorn \
    -k gthread \
    -w 1 \
    --threads "${GUNICORN_THREADS:-4}" \
    -b 0.0.0.0:5000 \
    --timeout "${GUNICORN_TIMEOUT:-300}" \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    web_outlook_app:app
) &
mail_pid=$!

(
  exec gosu app xvfb-run \
    --auto-servernum \
    --server-args="-screen 0 ${XVFB_SCREEN:-1920x1080x24} -nolisten tcp" \
    "$@"
) &
app_pid=$!

set +e
wait -n "$mail_pid" "$app_pid"
status=$?
set -e
stop_children || true
# A clean child exit is still an unexpected appliance failure.  Docker's
# restart policy must observe a failure rather than a successful shutdown.
[[ "$status" -eq 0 ]] && status=1
exit "$status"
