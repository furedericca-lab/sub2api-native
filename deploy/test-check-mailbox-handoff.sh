#!/usr/bin/env bash
# deploy/check-mailbox-handoff.sh 的确定性测试。
#
# 每个 fixture 都是合成 Compose YAML，并由真实的 `docker compose config`
# 渲染；测试绝不调用任何资源生命周期命令，不接触生产容器。
#
# 用法：deploy/test-check-mailbox-handoff.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$SCRIPT_DIR/check-mailbox-handoff.sh"
[[ -x "$GATE" ]] || { echo "找不到可执行门禁脚本: $GATE" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker 不可用" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq 不可用" >&2; exit 1; }

WORKDIR="$(mktemp -d)"
trap 'find "$WORKDIR" -mindepth 1 -depth -delete; rmdir "$WORKDIR"' EXIT

# fixture(<name>, <host_ip | __OMIT__>, <public host | __NONE__ => 缺失>, <mail?>)
fixture() {
  local name="$1" host_ip="$2" public="$3" mail="${4:-yes}" file="$WORKDIR/$1.yaml"
  {
    printf '%s\n' 'services:' '  fixture:' '    image: fixture:local'
    printf '%s\n' '    ports:'
    if [[ "$mail" == yes ]]; then
      printf '%s\n' '      - target: 5000' '        published: "15000"'
      if [[ "$host_ip" != __OMIT__ ]]; then
        printf '        host_ip: "%s"\n' "$host_ip"
      fi
    else
      printf '%s\n' '      - target: 8787' '        published: "8787"'
    fi
    printf '%s\n' '    environment:'
    if [[ "$public" == __NONE__ ]]; then
      printf '%s\n' '      TZ: UTC'
    else
      printf '      OUTLOOKEMAIL_PUBLIC_HOST: "%s"\n' "$public"
    fi
  } > "$file"
}

fixture loopback_loopback    127.0.0.1     127.0.0.1 yes
fixture lan_same_ip          192.0.2.153   192.0.2.153 yes
fixture lan_hostname         192.0.2.153   sub2api.local yes
fixture wildcard_bind_ip     __OMIT__      192.0.2.153 yes
fixture no_mailbox_mapping   __OMIT__      sub2api.local no

fixture lan_loopback_public  192.0.2.153   127.0.0.1 yes
fixture lan_missing_public   192.0.2.153   __NONE__ yes
fixture lan_empty_public     192.0.2.153   "" yes
fixture wildcard_public      192.0.2.153   0.0.0.0 yes
fixture wildcard_public_v6   192.0.2.153   "[::]" yes
fixture lan_ipv6_loopback    192.0.2.153   "[::1]" yes
fixture lan_localhost        192.0.2.153   localhost yes
fixture no_mail_wildcard     __OMIT__      0.0.0.0 no
fixture no_mail_empty        __OMIT__      "" no

case_run() {
  local name="$1" expect="$2" reason="$3" output rc got verdict
  if output="$($GATE "$WORKDIR/$name.yaml" 2>&1)"; then rc=0; else rc=$?; fi
  got=fail
  [[ $rc -eq 0 ]] && got=pass
  if [[ "$got" == "$expect" ]]; then verdict=PASS; else verdict=FAIL; fi
  printf '%-6s %-22s expect=%-4s got=%-4s  %s\n' "$verdict" "$name" "$expect" "$got" "$reason"
  if [[ "$verdict" != PASS ]]; then
    printf '%s\n' "$output" | sed 's/^/         /'
    LAST_STATUS=1
  fi
}

LAST_STATUS=0
echo '== 应通过 =='
case_run loopback_loopback  pass '本机发布，回环跳转合法'
case_run lan_same_ip        pass 'LAN 发布与明确 IP 目标一致'
case_run lan_hostname       pass 'LAN 发布允许可达 hostname，刻意不要求等于 BIND'
case_run wildcard_bind_ip   pass '省略 host_ip 等价于全接口，明确 PUBLIC 合法'
case_run no_mailbox_mapping pass '未发布 5000 时仍接受合法 PUBLIC'

echo '== 应拒绝（fail closed）=='
case_run lan_loopback_public fail 'LAN 发布却指向 127.0.0.1'
case_run lan_missing_public  fail 'LAN 发布但 PUBLIC 缺失'
case_run lan_empty_public    fail 'LAN 发布但 PUBLIC 为空串'
case_run wildcard_public     fail 'PUBLIC 为 IPv4 未指定地址'
case_run wildcard_public_v6  fail 'PUBLIC 为 IPv6 未指定地址'
case_run lan_ipv6_loopback   fail 'LAN 发布却指向 IPv6 回环'
case_run lan_localhost       fail 'LAN 发布却指向 localhost'
case_run no_mail_wildcard    fail '无端口映射也不得接受 wildcard PUBLIC'
case_run no_mail_empty       fail '无端口映射也不得接受空 PUBLIC'

echo '== 真实 Compose 配置（只读渲染）=='
if production_output="$($GATE 2>&1)"; then
  printf 'PASS   %-22s expect=pass got=pass\n' production
  printf '%s\n' "$production_output" | sed 's/^/         /'
else
  printf 'FAIL   %-22s expect=pass got=fail\n' production
  printf '%s\n' "$production_output" | sed 's/^/         /'
  LAST_STATUS=1
fi

echo '== 只读性静态检查 =='
# 门禁源码中唯一的 Docker 命令应是 compose config；拒绝出现资源生命周期命令。
if grep -nE 'docker[[:space:]]+[^#]*\b(create|up|down|start|stop|restart|build|pull|push|rm|rmi|run|exec|prune|tag)\b' "$GATE"; then
  echo 'FAIL   门禁脚本出现非只读 Docker 子命令'
  LAST_STATUS=1
else
  echo 'PASS   门禁源码只执行 docker compose config --format json'
fi
if grep -nE '(^|[^[:alnum:]_])(LOGIN_PASSWORD|SECRET_KEY|runtime\.env|outlookemail\.env)([^[:alnum:]_]|$)' "$GATE"; then
  echo 'FAIL   门禁脚本引用了凭据名称或凭据文件'
  LAST_STATUS=1
else
  echo 'PASS   门禁源码不读取或输出凭据'
fi
if grep -nE '\.json|docker[[:space:]]+compose[[:space:]]+create' "$GATE"; then
  echo 'FAIL   门禁脚本包含 fixture bypass 或 compose create 路径'
  LAST_STATUS=1
else
  echo 'PASS   门禁只接受 Compose 文件并真实渲染'
fi

echo
if [[ $LAST_STATUS -ne 0 ]]; then
  echo '[gate-tests] 存在失败用例' >&2
  exit 1
fi
echo '[gate-tests] 全部通过'
