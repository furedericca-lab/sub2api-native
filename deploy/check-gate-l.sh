#!/usr/bin/env bash
# 只读部署门禁：断言最终 rendered Compose 中的 Gate L 批量注册上限等于期望值。
#
# 存在的理由：代码默认与 Compose 默认都是 1，但 deploy/.env 的一行覆盖可以把
# 它静默改成 1000。默认值正确 != 本机部署值正确，所以必须在 build/recreate 前
# 校验 rendered 结果，而不是相信任何一层的默认值。
#
# 语义（fail closed，任何歧义都拒绝继续）：
#   rendered SUB2API_GATE_L_MAX_COUNT == 期望值 == SUB2API_GATE_L_DEFAULT(1)
#
# 期望值默认 1。把期望值提高到 1 以上必须同时显式传 --acceptance-ack，代表
# Gate L R2（count=2 第二账户全新浏览器身份、cookies/sessionStorage 不继承
# 第一账户）的 Live 验收已经完成。缺少该旗标时提高期望值本身就是错误。
#
# 本脚本只执行 `docker compose ... config --format json` 与 jq 查询：不连接
# daemon、不访问容器、不读取或输出任何凭据。人工执行 up/recreate 前也必须先
# 通过本门禁，与 deploy/check-mailbox-handoff.sh 同级。
set -euo pipefail

# 与 backend/web/application.py::gate_l_max_count 的默认值保持一致。
SUB2API_GATE_L_DEFAULT=1
# 运行时实现把上限钳制在 1..1000；超出该范围的目标不可达，直接拒绝。
SUB2API_GATE_L_MIN=1
SUB2API_GATE_L_MAX=1000

EXPECTED="$SUB2API_GATE_L_DEFAULT"
ACCEPTANCE_ACK=0
FILES=()

fail() { echo "[gate-l] FAIL: $*" >&2; exit 1; }
note() { echo "[gate-l] $*"; }

usage() {
  cat <<'EOF'
usage: deploy/check-gate-l.sh [--expected N] [--acceptance-ack] [compose-file ...]

Asserts the rendered SUB2API_GATE_L_MAX_COUNT equals --expected (default 1).
Raising --expected above 1 requires --acceptance-ack, which asserts that the
Gate L R2 count=2 browser-identity live acceptance has been completed.
With no compose-file arguments, checks deploy/compose.yaml and
deploy/docker-compose.yml.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected)
      [[ $# -ge 2 ]] || { usage >&2; fail "--expected 需要一个参数"; }
      EXPECTED="$2"
      shift 2
      ;;
    --expected=*)
      EXPECTED="${1#*=}"
      shift
      ;;
    --acceptance-ack)
      ACCEPTANCE_ACK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      FILES+=("$1")
      shift
      ;;
  esac
done

[[ "$EXPECTED" =~ ^[0-9]+$ ]] || fail "期望值必须是整数，收到: $EXPECTED"
(( EXPECTED >= SUB2API_GATE_L_MIN && EXPECTED <= SUB2API_GATE_L_MAX )) \
  || fail "期望值必须在 ${SUB2API_GATE_L_MIN}..${SUB2API_GATE_L_MAX} 范围内（运行时按此钳制），收到: $EXPECTED"
if (( EXPECTED > SUB2API_GATE_L_DEFAULT && ACCEPTANCE_ACK != 1 )); then
  fail "期望值 $EXPECTED 高于 fail-closed 默认值 $SUB2API_GATE_L_DEFAULT；只有完成 Gate L R2 count=2 Live 验收后才允许，并且必须显式传 --acceptance-ack"
fi

command -v jq >/dev/null 2>&1 || fail "jq 不可用，无法在可断言的前提下继续（fail closed）"
command -v docker >/dev/null 2>&1 || fail "docker 不可用，无法渲染 Compose 配置（fail closed）"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ${#FILES[@]} -eq 0 ]]; then
  FILES=("$SCRIPT_DIR/compose.yaml" "$SCRIPT_DIR/docker-compose.yml")
else
  resolved=()
  for arg in "${FILES[@]}"; do
    if [[ "$arg" = /* ]]; then
      resolved+=("$arg")
    elif [[ -f "$arg" ]]; then
      resolved+=("$(cd "$(dirname "$arg")" && pwd)/$(basename "$arg")")
    elif [[ -f "$SCRIPT_DIR/$arg" ]]; then
      resolved+=("$SCRIPT_DIR/$arg")
    else
      resolved+=("$arg")
    fi
  done
  FILES=("${resolved[@]}")
fi

render_json() {
  local source="$1"
  # 这是唯一允许的 Docker 操作：Compose 客户端配置渲染，不创建或修改资源。
  docker compose -f "$source" config --format json 2>/dev/null
}

# 提取所有服务声明的 Gate L 值。同时兼容 environment 的 object 与 array 两种
# rendered 形态；缺失时返回空串。
rendered_gate_values() {
  local json="$1"
  jq -r '
    [ .services[]?
      | (.environment // {})
      | if type == "object" then (.SUB2API_GATE_L_MAX_COUNT // empty)
        elif type == "array" then
          (map(select(startswith("SUB2API_GATE_L_MAX_COUNT="))
            | sub("^[^=]*="; "")))[]?
        else empty end
      | tostring | gsub("^[[:space:]]+|[[:space:]]+$"; "")
    ] | .[]
  ' <<<"$json"
}

check_file() {
  local compose_file="$1" json values unique value
  [[ -f "$compose_file" ]] || fail "$compose_file 不存在，无法断言 Gate L"

  if ! json="$(render_json "$compose_file")"; then
    fail "$compose_file: 无法渲染配置，拒绝部署"
  fi
  [[ -n "$json" ]] || fail "$compose_file: rendered Compose 为空，拒绝部署"
  if ! jq -e . >/dev/null <<<"$json"; then
    fail "$compose_file: rendered Compose 不是有效 JSON，拒绝部署"
  fi

  values="$(rendered_gate_values "$json")"
  unique="$(printf '%s\n' "$values" | grep -v '^$' | sort -u)"
  local count
  count="$(printf '%s\n' "$unique" | grep -c . || true)"

  if (( count == 0 )); then
    fail "$compose_file: rendered 未出现 SUB2API_GATE_L_MAX_COUNT，无法确认批量上限（fail closed）"
  fi
  if (( count > 1 )); then
    fail "$compose_file: rendered 出现多个不一致的 Gate L 值（$(printf '%s' "$unique" | tr '\n' ' ')），无法确定生效上限"
  fi

  value="$unique"
  [[ "$value" =~ ^[0-9]+$ ]] \
    || fail "$compose_file: SUB2API_GATE_L_MAX_COUNT 必须是整数，rendered 为 '$value'"
  (( value >= SUB2API_GATE_L_MIN && value <= SUB2API_GATE_L_MAX )) \
    || fail "$compose_file: rendered Gate L=$value 超出 ${SUB2API_GATE_L_MIN}..${SUB2API_GATE_L_MAX}"
  if (( value != EXPECTED )); then
    fail "$compose_file: rendered Gate L=$value != 期望 $EXPECTED（批量注册上限必须以 rendered 实际值为准，不接受默认值推断）"
  fi

  note "PASS $compose_file: rendered Gate L=$value == 期望 $EXPECTED"
}

for file in "${FILES[@]}"; do
  check_file "$file"
done

note "门禁通过：Gate L 等于期望值 $EXPECTED（共检查 ${#FILES[@]} 份 Compose 配置）"
