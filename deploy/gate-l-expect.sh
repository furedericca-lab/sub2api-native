#!/usr/bin/env bash
# 解析本机 Gate L 期望值策略（fail closed），把“抬升到哪、凭什么抬升”变成一份
# 显式记录，而不是每次命令前的临时旗标。
#
# 存在的理由：deploy/.env 的 SUB2API_GATE_L_MAX_COUNT 覆盖把本机批量上限抬到
# 验收后的值，而 deploy/check-gate-l.sh 的期望值默认仍是 1。裸跑 update.sh 就
# 必然在一次与本次变更无关的失败上耗尽注意力，久而久之操作者会习惯性带
# --acceptance-ack，正好抵消门禁的意义。这里把期望值固化成一份带出处、被 git
# 忽略、可复核的本机配置：抬升的是本机，共享默认值仍然是 1。
#
# 优先级：显式环境变量 > deploy/gate-l.local > 代码默认（期望 1，不 ack）。
# 抬升规则与 deploy/check-gate-l.sh 保持一致，且此处更严：由文件提供的 ack 必须
# 同时给出非空的 SUB2API_GATE_L_ACCEPTANCE_REF（一句可核对的验收出处）。文件是
# 持久授权，被复制到其他主机时必须连同它自己的声明一起走；一次性环境变量 ack
# 本身就是当场的 deliberate act，不额外要求出处。
#
# 本脚本不接触 docker、不读取凭据，只解析配置并打印结果：
#   EXPECTED=<n>
#   ACK=<0|1>
#   REF=<一行出处>
#   ACK_SOURCE=env|file|default
set -euo pipefail

# 与 deploy/check-gate-l.sh、backend/web/application.py::gate_l_max_count 保持一致。
SUB2API_GATE_L_DEFAULT=1
SUB2API_GATE_L_MIN=1
SUB2API_GATE_L_MAX=1000

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_FILE="${GATE_L_LOCAL_FILE:-$SCRIPT_DIR/gate-l.local}"

fail() { echo "[gate-l-expect] FAIL: $*" >&2; exit 1; }

RUN_CHECK=0
CHECK_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      RUN_CHECK=1
      shift
      # 剩下的参数全部透传给 check-gate-l.sh（Compose 文件列表）。
      CHECK_ARGS+=("$@")
      break
      ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) fail "未知参数 $1（本脚本只接受 --check 与 -h）" ;;
  esac
done

env_expected="${SUB2API_GATE_L_EXPECTED:-}"
env_ack="${SUB2API_GATE_L_ACCEPTANCE_ACK:-}"
env_ref="${SUB2API_GATE_L_ACCEPTANCE_REF:-}"

file_expected=""
file_ack=""
file_ref=""

if [[ -f "$LOCAL_FILE" ]]; then
  lineno=0
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    lineno=$(( lineno + 1 ))
    line="${raw%$'\r'}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*([A-Z0-9_]+)[[:space:]]*=[[:space:]]*(.*)$ ]] \
      || fail "$LOCAL_FILE:${lineno} 不是 KEY=value 形式: $raw"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$key" in
      SUB2API_GATE_L_EXPECTED) file_expected="$value" ;;
      SUB2API_GATE_L_ACCEPTANCE_ACK) file_ack="$value" ;;
      SUB2API_GATE_L_ACCEPTANCE_REF) file_ref="$value" ;;
      *) fail "$LOCAL_FILE:${lineno} 含未知键 $key（拼错的键不会生效，宁可拒绝也不静默忽略）" ;;
    esac
  done < "$LOCAL_FILE"
fi

expected="${env_expected:-$file_expected}"
[[ -n "$expected" ]] || expected="$SUB2API_GATE_L_DEFAULT"
ack="${env_ack:-$file_ack}"
[[ -n "$ack" ]] || ack=0
ref="${env_ref:-$file_ref}"

[[ "$expected" =~ ^[0-9]+$ ]] \
  || fail "SUB2API_GATE_L_EXPECTED 必须是整数，收到: $expected"
(( expected >= SUB2API_GATE_L_MIN && expected <= SUB2API_GATE_L_MAX )) \
  || fail "SUB2API_GATE_L_EXPECTED 必须在 ${SUB2API_GATE_L_MIN}..${SUB2API_GATE_L_MAX} 范围内（运行时按此钳制），收到: $expected"
[[ "$ack" == "0" || "$ack" == "1" ]] \
  || fail "SUB2API_GATE_L_ACCEPTANCE_ACK 只能是 0 或 1，收到: $ack"

if (( expected > SUB2API_GATE_L_DEFAULT && ack != 1 )); then
  fail "期望值 $expected 高于 fail-closed 默认值 $SUB2API_GATE_L_DEFAULT；只有完成 Gate L R2 count=2 Live 验收后才允许，并且必须显式给出 ack"
fi

ack_source="default"
if [[ -n "$env_ack" ]]; then
  ack_source="env"
elif [[ "$ack" == "1" ]]; then
  ack_source="file"
fi

if (( expected > SUB2API_GATE_L_DEFAULT )) && [[ "$ack_source" == "file" ]]; then
  [[ -n "$ref" ]] || fail "$LOCAL_FILE 用文件授予 ack，就必须写明 SUB2API_GATE_L_ACCEPTANCE_REF（可核对的验收出处：日期 + 验收记录位置）"
  case "$ref" in
    TODO*|todo*|TBD*|change-this*|*"<"*|*">"*)
      fail "SUB2API_GATE_L_ACCEPTANCE_REF 仍是模板或占位值，不构成本机抬升期望值的依据: $ref"
      ;;
  esac
fi

if (( RUN_CHECK == 1 )); then
  # --check 把解析结果直接交给真正的只读门禁，并在输出里留下本次抬升的出处，
  # 这样人工部署与 update.sh 走的是同一条路径，不会两套语义。
  check_args=(--expected "$expected")
  if [[ "$ack" == "1" ]]; then
    check_args+=(--acceptance-ack)
    echo "[gate-l-expect] 期望值 $expected 带验收旗标（来源 $ack_source）；出处: ${ref:-未提供}"
  else
    echo "[gate-l-expect] 期望值 $expected（来源 $ack_source）"
  fi
  exec "$SCRIPT_DIR/check-gate-l.sh" "${check_args[@]}" "${CHECK_ARGS[@]}"
fi

echo "EXPECTED=$expected"
echo "ACK=$ack"
echo "REF=$ref"
echo "ACK_SOURCE=$ack_source"
