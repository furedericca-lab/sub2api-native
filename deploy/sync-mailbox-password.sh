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
IFS= read -rs "pw?新邮箱登录密码（不回显）: "
IFS= read -rs "again?再输入一次确认: " >&2
echo "" >&2
[[ ${#pw} -ge 8 ]] || fail "密码至少 8 位，未做任何修改"
[[ "$pw" == "$again" ]] || fail "两次输入不一致，未做任何修改"

db_path="$(docker exec -i "$CONTAINER" sh -c 'echo "$OUTLOOKEMAIL_DATA_DIR/outlook_accounts.db"')"
docker exec -i -u 10001 -e "DATABASE_PATH=$db_path" "$CONTAINER" "$VENV_PY" "$RESET_SCRIPT" \
  --dry-run-check-db >/dev/null 2>&1 || fail "vendor 重置脚本无法定位数据库（$db_path），拒绝继续"

# 口令放进仅本用户可读的临时文件，只把路径交给驱动脚本；驱动脚本读完立即删除。
pwfile="$(umask 077; mktemp)"
printf '%s' "$pw" >"$pwfile"
DB_TARGET="$db_path" PW_FILE="$pwfile" python3 - <<'PY'
import os, pty, select, signal, sys, time

pw = open(os.environ["PW_FILE"], encoding="utf-8").read().strip()
os.unlink(os.environ["PW_FILE"])
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
rm -f "$pwfile"

# 确认库已接受新密码，才动 Sub2API 侧副本；口令只进不出。
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

printf '%s' "$pw" | python3 - <<'PY'
import os, re, stat, sys

pw = sys.stdin.read().strip()
for path in ("data/outlookemail/runtime.env", "deploy/outlookemail.env"):
    st = os.stat(path)
    before = open(path, encoding="utf-8").read()
    if "LOGIN_PASSWORD=" not in before:
        raise SystemExit(f"[mailbox-pw] {path}: 没有 LOGIN_PASSWORD 行，副本未修改")
    after = re.sub(r"(?m)^LOGIN_PASSWORD=.*$", "LOGIN_PASSWORD=" + pw, before)
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

docker exec "$CONTAINER" python /app/scripts/check-outlookemail-contract.py >/dev/null 2>&1 \
  && note "同步完成，契约复验通过（root / extension-login / external-accounts / external-emails）" \
  || fail "已同步，但契约复验未通过：运行 docker exec $CONTAINER python /app/scripts/check-outlookemail-contract.py 查看"

note "提示：旧登录会话已失效需重新登录；如需把新值刷进容器进程环境，可跑 deploy/update.sh 或 docker restart $CONTAINER"
