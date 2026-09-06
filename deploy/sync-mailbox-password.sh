#!/usr/bin/env bash
# 输入一个新密码，两边同步：OutlookEmail 数据库里的 bcrypt 哈希（登录校验的真相源）
# 与 Sub2API 读取的 data/outlookemail/runtime.env（含首次安装用的 deploy/outlookemail.env）。
#
# 只改一边就会出现"管理台能登进去，但控制台邮箱设置免密跳转失败、邮箱池取不到邮箱"：
# Sub2API 仍拿旧值调用 vendor 的 /api/extension/login，vendor 用新哈希校验必然 401。
#
# 顺序：先改库并验证新哈希接受，再写副本 —— 数据库那步失手就不会制造新的不一致。
# 口令只能交互输入（不回显、不进 shell 历史），不作为命令行参数或环境变量；输出不出现口令。
# 数据库只通过 vendor 自带官方入口改，且以容器内 app（uid 10001）身份执行，避免库属主变成 root。
set -euo pipefail

CONTAINER="${MAILBOX_CONTAINER:-sub2api-native}"
VENV_PY="${MAILBOX_VENV_PYTHON:-/opt/outlookemail-venv/bin/python}"
RESET_SCRIPT="/app/vendor/outlookEmail/scripts/reset_login_password.py"

fail() { echo "[mailbox-pw] FAIL: $*" >&2; exit 1; }
note() { echo "[mailbox-pw] $*"; }

[[ $# -eq 0 ]] || { echo "用法: deploy/sync-mailbox-password.sh（不接受任何参数，口令只能交互输入）" >&2; exit 2; }
[[ -f deploy/docker-compose.yml ]] || fail "必须在仓库根目录运行"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "容器 $CONTAINER 不存在"

pw="" again=""
printf '新邮箱登录密码（不回显）: ' >&2
IFS= read -rs pw
echo "" >&2
printf '再输入一次确认: ' >&2
IFS= read -rs again
echo "" >&2
[[ ${#pw} -ge 8 ]] || fail "密码至少 8 位，未做任何修改"
[[ "$pw" == "$again" ]] || fail "两次输入不一致，未做任何修改"

db_path="$(docker exec -i "$CONTAINER" sh -c 'echo "$OUTLOOKEMAIL_DATA_DIR/outlook_accounts.db"')"
# 官方脚本要求 TTY，因此预检不能走它本身（无 TTY 时它会先拒绝）；直接确认库文件存在。
docker exec -i -u 10001 "$CONTAINER" sh -c "test -f '$db_path'" \
  || fail "找不到 vendor 数据库（$db_path），拒绝继续"

# 口令放进仅本用户可读的临时文件，只把路径交给驱动脚本；驱动脚本读完立即删除。
pwfile="$(umask 077; mktemp)"
trap 'rm -f "$pwfile"' EXIT INT TERM
printf '%s' "$pw" >"$pwfile"
DB_TARGET="$db_path" PW_FILE="$pwfile" python3 - <<'PY'
import os, pty, select, signal, sys, time

pw = open(os.environ["PW_FILE"], encoding="utf-8").read().strip()  # 由 shell 统一清理
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

seen, first, second, done = b"", False, False, False
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
    raise SystemExit("[mailbox-pw] vendor 重置未在超时内完成，已终止；副本未修改")
if "已重置" not in transcript:
    raise SystemExit("[mailbox-pw] vendor 重置未成功，副本未修改：" + (transcript.splitlines() or [""])[-1])
PY

# 确认库已接受新密码，才动 Sub2API 侧副本；口令只走文件，不进命令行也不打印。
# 注意：这个检查在容器内执行，宿主的临时文件对它不可见，所以口令走 stdin；
# 脚本本身由 -c 提供，不占用 stdin，两者不冲突。
match="$(printf '%s' "$pw" | docker exec -i "$CONTAINER" "$VENV_PY" -c '
import bcrypt, os, sqlite3, sys
cand = sys.stdin.read().strip()
db = os.path.join(os.environ["OUTLOOKEMAIL_DATA_DIR"], "outlook_accounts.db")
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
row = conn.execute("select value from settings where key=?", ("login_password",)).fetchone()
print("1" if row and cand and bcrypt.checkpw(cand.encode(), row[0].encode()) else "0")
' | tr -d '\r')"
[[ "$match" == "1" ]] || fail "数据库未接受新密码，Sub2API 侧副本未修改"
note "vendor 数据库哈希已更新"

PW_TMP="$pwfile" python3 - <<'PY'
import os, re, stat

pw = open(os.environ["PW_TMP"], encoding="utf-8").read().strip()
if len(pw) < 8:
    raise SystemExit("[mailbox-pw] 口令临时文件异常（太短），副本未修改")
for path in ("data/outlookemail/runtime.env", "deploy/outlookemail.env"):
    st = os.stat(path)
    before = open(path, encoding="utf-8").read()
    if "LOGIN_PASSWORD=" not in before:
        raise SystemExit(f"[mailbox-pw] {path}: 没有 LOGIN_PASSWORD 行，副本未修改")
    # 替换串用函数：密码里的反斜杠不会被 re.sub 当转义序列解释。
    after = re.sub(r"(?m)^LOGIN_PASSWORD=.*$", lambda _m: "LOGIN_PASSWORD=" + pw, before)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(after)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, stat.S_IMODE(st.st_mode))
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    print(f"[mailbox-pw] synced {path}")
PY

# 本地三处对齐（不联网，因此不受 vendor 登录限速 5 次/5 分钟影响）。
PW_TMP="$pwfile" python3 - <<'PY'
import os

want = open(os.environ["PW_TMP"], encoding="utf-8").read().strip()
bad = []
for path in ("data/outlookemail/runtime.env", "deploy/outlookemail.env"):
    value = ""
    for line in open(path, encoding="utf-8"):
        if line.startswith("LOGIN_PASSWORD="):
            value = line.split("=", 1)[1].rstrip("\r\n")
            break
    if value != want:
        bad.append(path)
if bad:
    raise SystemExit("[mailbox-pw] 副本未对齐：" + ", ".join(bad))
print("[mailbox-pw] 两份副本与提交的口令逐字符一致")
PY
rm -f "$pwfile"
unset pw again

# 契约复验。刚改完密码时，之前的失败尝试可能已触发 vendor 的 5 次/5 分钟登录限速，
# 所以这里有限重试；仍不通就报“待复验”而不是“没同步上”。
i=0
while ! docker exec "$CONTAINER" python /app/scripts/check-outlookemail-contract.py >/dev/null 2>&1; do
  i=$(( i + 1 ))
  if (( i == 4 )); then
    fail "已同步且三处对齐，但契约复验未通过；若提示登录尝试过多（429，5 次失败锁 5 分钟），稍后重跑: docker exec $CONTAINER python /app/scripts/check-outlookemail-contract.py"
  fi
  note "契约复验未过（第 $i 次），可能在 vendor 登录限速窗口内，30 秒后重试"
  sleep 30
done
note "同步完成，契约复验通过（root / extension-login / external-accounts / external-emails）"

note "提示：旧登录会话已失效需重新登录；如需把新值刷进容器进程环境，可跑 deploy/update.sh 或 docker restart $CONTAINER"
