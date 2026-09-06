#!/usr/bin/env bash
# 确定性更新流程：前置检查 -> pinned submodule -> 本地构建 -> 运行 -> 验收。
# 不拉取远端、不自动更新 submodule；版本由当前 superproject pointer 决定。
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "[update] $*" >&2; exit 1; }

# 更新前：部署配置必须存在且密钥不是模板值。迁移完成后，持久化的
# data/outlookemail/runtime.env 可以替代旧的 deploy/outlookemail.env。
[[ -f .env ]] || fail "缺少 deploy/.env（从 .env.example 复制后填写）"
RUNTIME_ENV="../data/outlookemail/runtime.env"

has_credential() {
  local file="$1" key="$2" template="$3" value
  [[ -f "$file" ]] || return 1
  value="$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$file")"
  [[ -n "${value//[[:space:]]/}" && "$value" != "$template" ]]
}

credential_file=""
for candidate in "$RUNTIME_ENV" outlookemail.env; do
  if has_credential "$candidate" SECRET_KEY change-this-outlookemail-secret-key \
      && has_credential "$candidate" LOGIN_PASSWORD change-this-outlookemail-password; then
    credential_file="$candidate"
    break
  fi
done
[[ -n "$credential_file" ]] || fail "缺少有效 OutlookEmail 凭据（runtime.env 或 deploy/outlookemail.env）"

cd ..
git submodule sync --recursive
git submodule update --init --recursive
[[ -f vendor/outlookEmail/requirements.txt ]] || fail "OutlookEmail submodule 未初始化"

# 构建前：两份 Compose 必须可解析。
docker compose -f deploy/compose.yaml config --quiet || fail "compose.yaml 校验失败"
docker compose -f deploy/docker-compose.yml config --quiet || fail "docker-compose.yml 校验失败"

# Gate L 断言（fail closed）：批量注册上限必须以 rendered 实际值为准。
# 代码默认与 Compose 默认都是 1，但 deploy/.env 或宿主环境的一行覆盖可以把
# 它静默改成 1000；只相信默认值等于没有门禁。期望值来自
# SUB2API_GATE_L_EXPECTED（默认 1）。提高到 1 以上需要 Gate L R2（count=2
# 第二账户全新浏览器身份，cookies/sessionStorage 不继承第一账户）的 Live
# 验收已完成，并显式设置 SUB2API_GATE_L_ACCEPTANCE_ACK=1。
GATE_L_EXPECTED="${SUB2API_GATE_L_EXPECTED:-1}"
GATE_L_ACK_ARGS=()
if [[ "${SUB2API_GATE_L_ACCEPTANCE_ACK:-0}" == "1" ]]; then
  GATE_L_ACK_ARGS=(--acceptance-ack)
  echo "[update] Gate L 期望值已由显式验收旗标提高到 ${GATE_L_EXPECTED}"
fi
deploy/check-gate-l.sh --expected "$GATE_L_EXPECTED" "${GATE_L_ACK_ARGS[@]}" \
  deploy/compose.yaml deploy/docker-compose.yml \
  || fail "Gate L 门禁未通过，拒绝 build/recreate"

# 部署门禁（fail closed）：在任何 build / recreate 之前校验邮箱免密跳转目标。
# 只校验 rendered Compose，不看 deploy/.env 原文：代码默认正确 != 本机部署已升级到新契约。
# 语义、原因与 fixture 测试见 deploy/check-mailbox-handoff.sh 和 deploy/README.md。
# 人工执行 docker compose up -d --no-build 之前也必须先跑同一个脚本。
deploy/check-mailbox-handoff.sh deploy/compose.yaml deploy/docker-compose.yml \
  || fail "邮箱跳转门禁未通过，拒绝 build/recreate"

# build 会把 :local 标签直接盖到新镜像上，上一版构建随即变得不可寻址，
# 逐字节回滚能力就丢了。因此只要本地已经存在 :local，就在 build 前给它一个
# 带时间戳与来源短 sha 的 rollback 标签（名字排序即时间排序）。
tag_current_image() {
  local image_id rollback_tag dirty=""
  image_id="$(docker image inspect sub2api-native:local --format '{{.Id}}' 2>/dev/null || true)"
  if [[ -z "$image_id" ]]; then
    echo "[update] 本地没有 sub2api-native:local 镜像，跳过回滚标签（首次部署）"
    return 0
  fi
  if ! git diff --quiet; then dirty="-dirty"; fi
  rollback_tag="sub2api-native:rollback-$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)${dirty}"
  docker tag sub2api-native:local "$rollback_tag"
  echo "[update] 回滚标签 ${rollback_tag} (${image_id})"
  prune_rollback_tags
}

# 只清理本脚本按 rollback-YYYYmmdd-HHMMSS-<sha> 命名产出的标签：历史或手工
# 命名的回滚标签永远不进入删除集合；仍被任何容器（含已停止容器）引用的镜像
# 也不删。ROLLBACK_KEEP 至少保留 1 个。
prune_rollback_tags() {
  local keep="${ROLLBACK_KEEP:-5}" tags tag image_id i
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=5
  if (( keep < 1 )); then keep=1; fi
  mapfile -t tags < <(
    docker images --format '{{.Repository}}:{{.Tag}}' sub2api-native |
      grep -E '^sub2api-native:rollback-[0-9]{8}-[0-9]{6}-' | sort -r
  )
  if (( ${#tags[@]} <= keep )); then return 0; fi
  for (( i = keep; i < ${#tags[@]}; i++ )); do
    tag="${tags[i]}"
    image_id="$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null || true)"
    if [[ -n "$image_id" ]] && [[ -n "$(docker ps -aq --filter "ancestor=$image_id" 2>/dev/null)" ]]; then
      echo "[update] 保留仍被容器引用的回滚标签 ${tag}"
      continue
    fi
    if docker rmi "$tag" >/dev/null 2>&1; then
      echo "[update] 已移除过旧回滚标签 ${tag}"
    fi
  done
}

tag_current_image

cd deploy
docker compose -f compose.yaml build --pull=false

# build 与 recreate 之间再过一次门禁：构建本身不应改变配置，但 recreate 是唯一
# 不可逆的本地生产动作，必须在 apply 前重新断言。
./check-mailbox-handoff.sh compose.yaml docker-compose.yml \
  || fail "build 后邮箱跳转门禁未通过，拒绝 recreate"

# Gate L 同样在 recreate 前重新断言，与邮箱门禁同级。
./check-gate-l.sh --expected "$GATE_L_EXPECTED" "${GATE_L_ACK_ARGS[@]}" \
  compose.yaml docker-compose.yml \
  || fail "build 后 Gate L 门禁未通过，拒绝 recreate"

docker compose -f docker-compose.yml up -d --no-build

# 部署后：等待健康并验证业务链路。
wait_healthy() {
  local name="$1" state
  for _ in $(seq 1 30); do
    state="$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    if [[ "$state" == "healthy" ]]; then
      return 0
    fi
    sleep 5
  done
  fail "$name 未在 150 秒内转为 healthy"
}
wait_healthy sub2api-native

# 宿主端口可配置（SUB2API_WEB_PORT，读 deploy/.env）；容器内契约端口固定 8787。
HOST_WEB_PORT="$(awk -F= '/^SUB2API_WEB_PORT=/{value=$2} END{gsub(/[ \"\r]/, "", value); print value}' .env 2>/dev/null || true)"
HOST_WEB_PORT="${HOST_WEB_PORT:-8787}"
MAIL_PORT="$(awk -F= '/^OUTLOOKEMAIL_PORT=/{value=$2} END{gsub(/[ \"\r]/, "", value); print value}' .env 2>/dev/null || true)"
MAIL_PORT="${MAIL_PORT:-15000}"
MAIL_BIND_HOST="$(awk -F= '/^OUTLOOKEMAIL_BIND_HOST=/{value=$2} END{gsub(/[ \"\r]/, "", value); print value}' .env 2>/dev/null || true)"
MAIL_BIND_HOST="${MAIL_BIND_HOST:-127.0.0.1}"
case "$MAIL_BIND_HOST" in
  0.0.0.0|::) MAIL_CHECK_HOST="127.0.0.1" ;;
  \[*\]) MAIL_CHECK_HOST="$MAIL_BIND_HOST" ;;
  *:*) MAIL_CHECK_HOST="[$MAIL_BIND_HOST]" ;;
  *) MAIL_CHECK_HOST="$MAIL_BIND_HOST" ;;
esac
curl -fsS "http://127.0.0.1:${HOST_WEB_PORT}/api/health" >/dev/null \
  || fail "/api/health 不通过"
docker exec sub2api-native python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=5).read()" \
  || fail "容器内 /api/health 不通过"
docker exec sub2api-native python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=5).read()" \
  || fail "容器内 OutlookEmail 不可用"
curl --noproxy '*' -fsS "http://${MAIL_CHECK_HOST}:${MAIL_PORT}/" >/dev/null \
  || fail "宿主机 OutlookEmail 管理端口不通过"

docker exec sub2api-native python /app/scripts/check-outlookemail-contract.py \
  || fail "OutlookEmail HTTP compatibility contract 不通过"

docker compose -f docker-compose.yml ps

echo "[update] 更新完成并通过验收"
