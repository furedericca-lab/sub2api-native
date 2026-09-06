#!/usr/bin/env bash
# 邮箱登录密码的一条命令入口。
#
# 存在的理由：这把凭据在主机上有两处必须同时移动的副本 —— vendor 数据库里的 bcrypt 哈希
# （settings.login_password，登录校验的真相源）与 Sub2API 读取的 data/outlookemail/runtime.env
# （以及首次安装用的 deploy/outlookemail.env）。只改一边就会出现"管理台能登进去，但控制台
# 邮箱设置免密跳转失败、邮箱池与注册取不到邮箱"：Sub2API 仍拿旧值调用 vendor 的
# /api/extension/login，vendor 用新哈希校验必然 401。人工分两次改，中间也没有可核对的状态。
#
# 子命令：
#   verify [--full]              只读核对两侧是否一致（--full 再跑契约 smoke），不写任何东西
#   set    [--from-file P]       改密码：先重置 vendor 数据库哈希并验证，再同步 Sub2API 侧副本
#   adopt  [--from-file P]       vendor 侧已经改过密码：先验证口令确实匹配数据库，再同步副本
#
# 安全约束：口令只能来自交互输入（不回显、不进 shell 历史）或 --from-file 指定的私有文件
# （权限必须 600/400 且属主为当前用户）。不提供 --password，也不读环境变量，避免口令进入
# 命令行参数与进程环境；任何输出都不出现口令本身。
#
# vendor 数据库只通过其自带官方入口 scripts/reset_login_password.py 修改，并以容器内 app
# （uid 10001）身份执行，避免把库属主带离 app；该脚本要求 TTY，故此处用 pty 驱动，
# 口令只在 pty 内流动。
set -euo pipefail

CONTAINER="${MAILBOX_CONTAINER:-sub2api-native}"
VENV_PY="${MAILBOX_VENV_PYTHON:-/opt/outlookemail-venv/bin/python}"
RESET_SCRIPT="/app/vendor/outlookEmail/scripts/reset_login_password.py"

fail() { echo "[mailbox-pw] FAIL: $*" >&2; exit 1; }
note() { echo "[mailbox-pw] $*"; }

usage() {
  cat <<'EOF'
usage:
  deploy/sync-mailbox-password.sh verify [--full]
  deploy/sync-mailbox-password.sh set   [--from-file PATH] [--recreate]
  deploy/sync-mailbox-password.sh adopt [--from-file PATH] [--recreate]

  verify  只读：vendor 数据库哈希与 Sub2API 侧副本是否一致
  set     改密码：数据库哈希 + Sub2API 侧副本一次改完（先改库并验证，再写副本）
  adopt   vendor 侧已改过密码：验证口令匹配数据库后，把 Sub2API 侧同步过去

口令来源：交互输入（不回显）或 --from-file PATH（权限须为 600、属主须为当前用户）。
不提供 --password，也不读环境变量。
EOF
}

require_repo() {
  [[ -f deploy/docker-compose.yml ]] || fail "必须在仓库根目录运行（找不到 deploy/docker-compose.yml）"
  [[ -d data/outlookemail ]] || fail "找不到 data/outlookemail，本机不是已部署状态"
  docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "容器 $CONTAINER 不存在"
}

# 让 vendor 自己那套 bcrypt 来判定：一次容器内进程同时读库与 runtime.env，
# 只输出长度与布尔结果，口令不越过容器边界。
verify_pair() {
  docker exec -i "$CONTAINER" "$VENV_PY" -c '
import bcrypt, os, sqlite3
runtime = os.environ.get("OUTLOOKEMAIL_RUNTIME_ENV") or os.path.join(
    os.environ["OUTLOOKEMAIL_DATA_DIR"], "runtime.env"
)
value = ""
with open(runtime, encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("LOGIN_PASSWORD="):
            value = line.split("=", 1)[1].strip()
            break
db = os.path.join(os.environ["OUTLOOKEMAIL_DATA_DIR"], "outlook_accounts.db")
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
row = conn.execute("select value from settings where key=?", ("login_password",)).fetchone()
stored = row[0] if row else ""
print("RUNTIME_LEN=%d" % len(value))
print("DB_MATCHES=%d" % (1 if stored and value and bcrypt.checkpw(value.encode(), stored.encode()) else 0))
'
}

# 校验来自 stdin 的候选口令是否匹配数据库哈希；只回布尔。
candidate_matches_db() {
  docker exec -i "$CONTAINER" "$VENV_PY" -c '
import bcrypt, os, sqlite3, sys
cand = sys.stdin.read().strip()
db = os.path.join(os.environ["OUTLOOKEMAIL_DATA_DIR"], "outlook_accounts.db")
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
row = conn.execute("select value from settings where key=?", ("login_password",)).fetchone()
stored = row[0] if row else ""
print("DB_MATCHES=%d" % (1 if stored and cand and bcrypt.checkpw(cand.encode(), stored.encode()) else 0))
'
}

read_password() {
  local from_file="${1:-}"
  if [[ -n "$from_file" ]]; then
    [[ -f "$from_file" ]] || fail "--from-file 文件不存在"
    local mode owner
    mode="$(stat -c %a "$from_file")"
    owner="$(stat -c %u "$from_file")"
    [[ "$mode" == "600" || "$mode" == "400" ]] || fail "口令文件权限必须是 600（当前 $mode），拒绝读取"
    [[ "$owner" == "$(id -u)" ]] || fail "口令文件必须属于当前用户，拒绝读取"
    tr -d '\r\n' <"$from_file"
    return 0
  fi
  local pw
  IFS= read -rs "pw?新邮箱登录密码（不回显）: "
  echo "" >&2
  [[ ${#pw} -ge 8 ]] || fail "密码至少 8 位，未做任何修改"
  printf '%s' "$pw"
}

confirm_password() {
  local pw="$1" again
  IFS= read -rs "again?再输入一次确认: " >&2
  echo "" >&2
  [[ "$pw" == "$again" ]] || fail "两次输入不一致，未做任何修改"
}

sync_sub2api_side() {
  local pw="$1"
  printf '%s' "$pw" | python3 - <<'PY'
import os, re, stat, sys

pw = sys.stdin.read().strip()
if len(pw) < 8:
    raise SystemExit("[mailbox-pw] 密码太短，未做任何修改")
for path in ("data/outlookemail/runtime.env", "deploy/outlookemail.env"):
    st = os.stat(path)
    before = open(path, encoding="utf-8").read()
    if "LOGIN_PASSWORD=" not in before:
        raise SystemExit(f"[mailbox-pw] {path}: 没有 LOGIN_PASSWORD 行，拒绝猜测格式")
    after = re.sub(r"(?m)^LOGIN_PASSWORD=.*$", "LOGIN_PASSWORD=" + pw, before)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(after)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, stat.S_IMODE(st.st_mode))
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    print(f"[mailbox-pw] synced {path} (mode {oct(stat.S_IMODE(os.stat(path).st_mode))}, uid {os.stat(path).st_uid})")
PY
}

vendor_db_reset() {
  local pw="$1" db_path pwtmp
  db_path="$(docker exec -i "$CONTAINER" sh -c 'echo "$OUTLOOKEMAIL_DATA_DIR/outlook_accounts.db"')"
  # 先确认官方脚本解析到的正是线上库路径，再真正写入。
  docker exec -i -u 10001 -e "DATABASE_PATH=$db_path" "$CONTAINER" "$VENV_PY" "$RESET_SCRIPT" \
    --dry-run-check-db >/dev/null 2>&1 \
    || fail "vendor 重置脚本无法定位数据库（$db_path），拒绝继续"
  # 口令交给一个仅本用户可读的临时文件，只传路径；驱动脚本读完立即 unlink。
  pwtmp="$(umask 077; mktemp)"
  printf '%s' "$pw" >"$pwtmp"
  DB_TARGET="$db_path" PW_FILE="$pwtmp" python3 - <<'PY'
import os, pty, select, signal, sys, time

pw = open(os.environ["PW_FILE"], encoding="utf-8").read().strip()
os.unlink(os.environ["PW_FILE"])
if len(pw) < 8:
    raise SystemExit("[mailbox-pw] 密码太短，未做任何修改")
cmd = [
    "docker", "exec", "-i", "-t", "-u", "10001",
    "-e", "DATABASE_PATH=" + os.environ["DB_TARGET"],
    os.environ.get("MAILBOX_CONTAINER", "sub2api-native"),
    os.environ.get("MAILBOX_VENV_PYTHON", "/opt/outlookemail-venv/bin/python"),
    "/app/vendor/outlookEmail/scripts/reset_login_password.py",
]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)

seen = b""
first = second = False
done = False
deadline = time.time() + 60
try:
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 1.0)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        text = seen.decode("utf-8", "replace")
        if not first and "新登录密码: " in text:
            os.write(fd, (pw + "\n").encode())
            first = True
        elif first and not second and "确认新登录密码: " in text:
            os.write(fd, (pw + "\n").encode())
            second = True
        if second and ("已重置" in text or "错误" in text):
            time.sleep(0.4)
            while True:
                r2, _, _ = select.select([fd], [], [], 1.0)
                if not r2:
                    break
                try:
                    more = os.read(fd, 4096)
                except OSError:
                    break
                if not more:
                    break
                seen += more
            done = True
            break
finally:
    if not done:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    os.close(fd)

transcript = seen.decode("utf-8", "replace").replace(pw, "<redacted>")
if not done:
    raise SystemExit("[mailbox-pw] vendor 重置未在超时内完成，已终止；数据库状态未确认")
if "已重置" not in transcript:
    tail = transcript.splitlines()[-1] if transcript.strip() else ""
    raise SystemExit("[mailbox-pw] vendor 重置未成功，拒绝同步 Sub2API 侧：" + tail)
print("[mailbox-pw] vendor 数据库哈希已重置")
PY
}

run_gates_and_recreate() {
  [[ -x deploy/gate-l-expect.sh && -x deploy/check-mailbox-handoff.sh ]] || fail "门禁脚本不可用，拒绝 recreate"
  deploy/gate-l-expect.sh --check >/dev/null || fail "Gate L 门禁未通过，拒绝 recreate"
  deploy/check-mailbox-handoff.sh deploy/compose.yaml deploy/docker-compose.yml >/dev/null || fail "邮箱跳转门禁未通过，拒绝 recreate"
  note "两道门禁通过，recreate 以刷新容器进程环境"
  (cd deploy && docker compose -f docker-compose.yml up -d --no-build --force-recreate >/dev/null)
  local i state
  for i in $(seq 1 30); do
    state="$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    [[ "$state" == "healthy" ]] && break
    sleep 5
  done
  [[ "$state" == "healthy" ]] || fail "容器未在 150 秒内转为 healthy"
  note "容器 healthy"
}

full_verify() {
  docker exec "$CONTAINER" python /app/scripts/check-outlookemail-contract.py >/dev/null 2>&1 \
    || fail "契约 smoke 未通过（extension-login 仍可能不一致）"
  note "契约 smoke 通过（root / extension-login / external-accounts / external-emails）"
}

cmd_verify() {
  require_repo
  local out runtime_len match
  out="$(verify_pair)"
  runtime_len="$(sed -n 's/^RUNTIME_LEN=//p' <<<"$out")"
  match="$(sed -n 's/^DB_MATCHES=//p' <<<"$out")"
  note "Sub2API 侧 runtime.env 生效值长度: ${runtime_len:-unknown}"
  if [[ "$match" == "1" ]]; then
    note "PASS: vendor 数据库哈希接受 Sub2API 侧凭据（两侧一致）"
  else
    note "FAIL: vendor 数据库哈希不接受 Sub2API 侧凭据 —— 有一侧被单独改过"
    note "      这就是控制台邮箱设置跳转失败、邮箱池取不到邮箱的直接原因"
    echo "修复：deploy/sync-mailbox-password.sh adopt   （vendor 侧已改过，把 Sub2API 侧同步过去）"
    echo "  或：deploy/sync-mailbox-password.sh set    （重新定一个密码，两侧一起改）"
    exit 1
  fi
  [[ "${1:-}" == "--full" ]] && full_verify
  return 0
}

cmd_adopt() {
  require_repo
  local pw="$1"
  local match
  match="$(printf '%s' "$pw" | candidate_matches_db | tr -d '\r')"
  [[ "$match" == "DB_MATCHES=1" ]] || fail "该口令与 vendor 数据库哈希不匹配，拒绝写入任何文件（fail closed）"
  note "口令与 vendor 数据库匹配，开始同步 Sub2API 侧"
  sync_sub2api_side "$pw"
  full_verify
  note "完成：两侧一致；若还在别处（deploy/.env、笔记、密码库）留有旧副本，请一并核对"
}

cmd_set() {
  require_repo
  local pw="$1"
  vendor_db_reset "$pw"
  local match
  match="$(printf '%s' "$pw" | candidate_matches_db | tr -d '\r')"
  [[ "$match" == "DB_MATCHES=1" ]] || fail "数据库哈希未接受新密码，拒绝同步 Sub2API 侧（避免制造不一致）"
  sync_sub2api_side "$pw"
  full_verify
  note "完成：vendor 数据库哈希与 Sub2API 侧副本已同时更新"
}

main() {
  local sub="${1:-}"
  shift || true
  local from_file="" recreate=0 extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --from-file) from_file="${2:-}"; shift 2 ;;
      --password | -p) fail "出于凭据卫生不支持 --password；请交互输入或使用 --from-file PATH" ;;
      --recreate) recreate=1; shift ;;
      *) extra+=("$1"); shift ;;
    esac
  done
  case "$sub" in
    verify) cmd_verify "${extra[@]:-}" ;;
    set | adopt)
      local pw
      pw="$(read_password "$from_file")"
      if [[ -z "$from_file" && "$sub" == "set" ]]; then confirm_password "$pw"; fi
      if [[ "$sub" == "set" ]]; then cmd_set "$pw"; else cmd_adopt "$pw"; fi
      if [[ "$recreate" == "1" ]]; then run_gates_and_recreate; fi
      ;;
    -h | --help) usage; exit 0 ;;
    "") usage >&2; fail "需要子命令：verify | set | adopt" ;;
    *) usage >&2; fail "未知子命令: $sub" ;;
  esac
}

main "$@"
