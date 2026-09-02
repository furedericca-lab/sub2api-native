#!/usr/bin/env bash
# 只读预部署门禁：断言最终 rendered Compose 中 OutlookEmail 免密跳转目标可达。
#
# 本脚本只执行 `docker compose ... config --format json` 和 jq 查询，不连接
# daemon、不访问容器，也不读取或输出凭据。任何渲染、解析或契约歧义都会拒绝
# 继续（fail closed）。人工执行 up/recreate 前必须先通过本门禁。
#
# BIND 与 PUBLIC 是两个独立概念：
#   OUTLOOKEMAIL_BIND_HOST   Docker 在宿主机发布 5000 的监听地址
#   OUTLOOKEMAIL_PUBLIC_HOST 浏览器实际访问的地址，即免密跳转 URL 的目标
# PUBLIC 不得从 BIND 推导，也不要求两者字符串相等；例如 LAN 绑定配合一个
# 可达 hostname 是合法配置。真正禁止的是 LAN 发布却把浏览器目标设成回环地址。
set -euo pipefail

fail() { echo "[mailbox-gate] FAIL: $*" >&2; exit 1; }
note() { echo "[mailbox-gate] $*"; }

command -v jq >/dev/null 2>&1 || fail "jq 不可用，无法在可断言的前提下继续（fail closed）"
command -v docker >/dev/null 2>&1 || fail "docker 不可用，无法渲染 Compose 配置（fail closed）"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES=()
if [[ $# -eq 0 ]]; then
  FILES=("$SCRIPT_DIR/compose.yaml" "$SCRIPT_DIR/docker-compose.yml")
else
  for arg in "$@"; do
    if [[ "$arg" = /* ]]; then
      FILES+=("$arg")
    elif [[ -f "$arg" ]]; then
      FILES+=("$(cd "$(dirname "$arg")" && pwd)/$(basename "$arg")")
    elif [[ -f "$SCRIPT_DIR/$arg" ]]; then
      FILES+=("$SCRIPT_DIR/$arg")
    else
      FILES+=("$arg")
    fi
  done
fi

# PUBLIC 基本非法目标：空值、未指定地址，或明显不是 host 值。
is_invalid_public_target() {
  local original="$1" value ip_candidate
  value="${original,,}"
  while [[ "$value" == *. ]]; do value="${value%.}"; done
  case "$value" in
    ""|0.0.0.0|"::"|"[::]"|\
      0:0:0:0:0:0:0:0|"[0:0:0:0:0:0:0:0]")
      return 0
      ;;
  esac
  # Keep the gate aligned with the backend host contract: a host, not a URL,
  # credential-bearing authority, or value containing control/space characters.
  case "$original" in
    *[[:space:]]*|*/*|*\\*|*@*|*\?*|*\#*|*%*|*\[*|*\]*) return 0 ;;
  esac
  # Reject every textual spelling of the IPv6 unspecified address, not only
  # the common `::` form. IPv6 values are otherwise limited to host characters.
  if [[ "$value" == *:* ]]; then
    ip_candidate="$value"
    [[ "$ip_candidate" =~ ^[0-9a-f:.]+$ ]] || return 0
    [[ "$ip_candidate" =~ [1-9a-f] ]] || return 0
    return 1
  fi
  # Match the same DNS-label shape used by backend/mailbox/service.py.
  [[ "$value" =~ ^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$ ]] || return 0
  [[ "$value" != *..* ]] || return 0
  return 1
}

is_loopback_public() {
  local value="${1,,}"
  while [[ "$value" == *. ]]; do value="${value%.}"; done
  case "$value" in
    localhost|"[localhost]"|127.*|"::1"|"[::1]") return 0 ;;
  esac
  return 1
}

# 仅本机可见的发布地址。
is_loopback_published() {
  local value="${1,,}"
  while [[ "$value" == *. ]]; do value="${value%.}"; done
  case "$value" in
    localhost|"[localhost]"|127.*|"::1"|"[::1]") return 0 ;;
  esac
  return 1
}

render_json() {
  local source="$1"
  # 这是唯一允许的 Docker 操作：Compose 客户端配置渲染，不创建或修改资源。
  docker compose -f "$source" config --format json 2>/dev/null
}

check_file() {
  local compose_file="$1" json target_count public public_port published
  [[ -f "$compose_file" ]] || fail "$compose_file 不存在"

  if ! json="$(render_json "$compose_file")"; then
    fail "$compose_file: 无法渲染配置，拒绝部署"
  fi
  [[ -n "$json" ]] || fail "$compose_file: rendered Compose 为空，拒绝部署"
  if ! jq -e . >/dev/null <<<"$json"; then
    fail "$compose_file: rendered Compose 不是有效 JSON，拒绝部署"
  fi

  # 只允许一个服务拥有目标 5000 映射，避免把一个服务的 host_ip 与另一个
  # 服务的 PUBLIC_HOST 错配。没有映射时仍校验 PUBLIC 的基本合法性。
  target_count="$(jq -r '
    [.services[]? | (.ports // [])[]? | select((.target | tostring) == "5000")] | length
  ' <<<"$json")"
  [[ "$target_count" =~ ^[0-9]+$ ]] || fail "$compose_file: 无法解析 5000 端口映射"
  (( target_count <= 1 )) || fail "$compose_file: 发现多个 5000 端口映射，无法确定邮箱发布边界"

  if (( target_count == 1 )); then
    published="$(jq -r '
      [ .services[]? as $service
        | ($service.ports // [])[]?
        | select((.target | tostring) == "5000")
        | (.host_ip // "") | tostring
        | gsub("^[[:space:]]+|[[:space:]]+$"; "")
        | if . == "" then "0.0.0.0" else . end
      ] | first // "<none>"
    ' <<<"$json")"
    public="$(jq -r '
      [ .services[]? as $service
        | select(any(($service.ports // [])[]?; (.target | tostring) == "5000"))
        | ($service.environment // {})
        | if type == "object" then (.OUTLOOKEMAIL_PUBLIC_HOST // "")
          elif type == "array" then
            (map(select(startswith("OUTLOOKEMAIL_PUBLIC_HOST="))
              | sub("^[^=]*="; "")) | first // "")
          else "" end
        | tostring | gsub("^[[:space:]]+|[[:space:]]+$"; "")
      ] | first // ""
    ' <<<"$json")"
    public_port="$(jq -r '
      [ .services[]? as $service
        | select(any(($service.ports // [])[]?; (.target | tostring) == "5000"))
        | ($service.environment // {})
        | if type == "object" then (.OUTLOOKEMAIL_PUBLIC_PORT // "")
          elif type == "array" then
            (map(select(startswith("OUTLOOKEMAIL_PUBLIC_PORT="))
              | sub("^[^=]*="; "")) | first // "")
          else "" end
        | tostring | gsub("^[[:space:]]+|[[:space:]]+$"; "")
      ] | first // ""
    ' <<<"$json")"
  else
    published="<none>"
    # 没有 5000 映射时，仍从 rendered service environment 读取并校验 PUBLIC。
    public="$(jq -r '
      [ .services[]? | (.environment // {})
        | if type == "object" then (.OUTLOOKEMAIL_PUBLIC_HOST // "")
          elif type == "array" then
            (map(select(startswith("OUTLOOKEMAIL_PUBLIC_HOST="))
              | sub("^[^=]*="; "")) | first // "")
          else "" end
        | tostring | gsub("^[[:space:]]+|[[:space:]]+$"; "")
      ] | map(select(. != "")) | first // ""
    ' <<<"$json")"
    public_port="$(jq -r '
      [ .services[]? | (.environment // {})
        | if type == "object" then (.OUTLOOKEMAIL_PUBLIC_PORT // "")
          elif type == "array" then
            (map(select(startswith("OUTLOOKEMAIL_PUBLIC_PORT="))
              | sub("^[^=]*="; "")) | first // "")
          else "" end
        | tostring | gsub("^[[:space:]]+|[[:space:]]+$"; "")
      ] | map(select(. != "")) | first // ""
    ' <<<"$json")"
  fi

  [[ -n "$public" ]] || fail "$compose_file: OUTLOOKEMAIL_PUBLIC_HOST 渲染为空，浏览器跳转目标未知"
  public_port="${public_port:-15000}"
  if [[ ! "$public_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$public_port < 1 || 10#$public_port > 65535 )); then
    fail "$compose_file: OUTLOOKEMAIL_PUBLIC_PORT 必须是 1-65535 的端口"
  fi
  if is_invalid_public_target "$public"; then
    fail "$compose_file: OUTLOOKEMAIL_PUBLIC_HOST 不是可用的明确浏览器目标"
  fi

  if [[ "$published" == "<none>" ]]; then
    note "PASS $compose_file: 未发布 5000 原生端口（无 LAN 跳转面），PUBLIC=明确值"
  elif is_loopback_published "$published"; then
    note "PASS $compose_file: 本机发布（published=回环），PUBLIC=已配置"
  elif is_loopback_public "$public"; then
    fail "$compose_file: mailbox 端口发布到非回环地址，但 OUTLOOKEMAIL_PUBLIC_HOST 是回环地址"
  else
    # 端口发布到 LAN 或全部接口：上面的 PUBLIC 检查已确保其为非回环目标。
    note "PASS $compose_file: mailbox 端口发布到非回环地址，PUBLIC=已配置"
  fi
}

for file in "${FILES[@]}"; do
  check_file "$file"
done

note "门禁通过（共检查 ${#FILES[@]} 份 Compose 配置）"
