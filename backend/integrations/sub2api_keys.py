"""Account-scoped Sub2API group discovery and API Key creation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from .sub2api_transport import Sub2ApiClient, Sub2ApiNetworkError


class ApiKeyProtocolError(RuntimeError):
    pass


class ApiKeyValidationError(RuntimeError):
    pass


class ApiKeyCreateUncertainError(RuntimeError):
    pass


class ApiKeyMutationUncertainError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiKeyCreateResult:
    id: int
    name: str
    group_id: int
    status: str
    secret: str
    reconciled: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "group_id": self.group_id,
            "status": self.status,
            "secret": self.secret,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class ApiKeyRevealResult:
    id: int
    name: str
    group_id: int
    status: str
    secret: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "group_id": self.group_id,
            "status": self.status,
            "secret": self.secret,
        }


def _collection(body: Dict[str, Any], names: Iterable[str]) -> List[Dict[str, Any]]:
    current: Any = body.get("data", body)
    if isinstance(current, dict):
        for name in names:
            value = current.get(name)
            if isinstance(value, list):
                current = value
                break
    if not isinstance(current, list):
        return []
    return [item for item in current if isinstance(item, dict)]


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _normalize_group(item: Dict[str, Any]) -> Dict[str, Any] | None:
    group_id = _positive_int(item.get("id"))
    name = str(item.get("name") or "").strip()
    if not group_id or not name:
        return None
    rate = item.get("display_rate_multiplier", item.get("rate_multiplier"))
    return {
        "id": group_id,
        "name": name,
        "platform": str(item.get("platform") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "rate_multiplier": rate if isinstance(rate, (int, float)) else None,
    }


def _raw_key_snapshot(item: Dict[str, Any]) -> Dict[str, Any] | None:
    key_id = _positive_int(item.get("id"))
    if not key_id:
        return None
    return {
        "id": key_id,
        "name": str(item.get("name") or "").strip(),
        "group_id": _positive_int(item.get("group_id")),
        "status": str(item.get("status") or "").strip(),
        "secret": str(item.get("key") or ""),
    }


def _safe_key(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "group_id": snapshot["group_id"],
        "status": snapshot["status"],
        "masked_key": "********" if snapshot.get("secret") else "",
    }


def _usable_secret(value: str) -> bool:
    secret = str(value or "").strip()
    return bool(secret) and not any(marker in secret for marker in ("*", "...", "…"))


class Sub2ApiKeyService:
    def __init__(self, client: Sub2ApiClient) -> None:
        self.client = client

    def list_groups(self, token: str) -> List[Dict[str, Any]]:
        body = self.client.request("GET", "/api/v1/groups/available", token=token)
        groups: List[Dict[str, Any]] = []
        seen = set()
        for item in _collection(body, ("groups", "items")):
            group = _normalize_group(item)
            if group is None or group["id"] in seen:
                continue
            seen.add(group["id"])
            groups.append(group)
        return groups

    def _list_key_snapshots(self, token: str) -> List[Dict[str, Any]]:
        page = 1
        snapshots: List[Dict[str, Any]] = []
        while True:
            body = self.client.request(
                "GET",
                f"/api/v1/keys?page={page}&page_size=100",
                token=token,
            )
            items = _collection(body, ("items", "keys"))
            for item in items:
                snapshot = _raw_key_snapshot(item)
                if snapshot is not None:
                    snapshots.append(snapshot)
            pages = _positive_int(body.get("pages")) if isinstance(body, dict) else 0
            if not items or page >= max(pages, 1):
                break
            page += 1
            if page > 100:
                raise ApiKeyProtocolError("上游 API Key 分页数量异常")
        return snapshots

    def list_keys(self, token: str) -> List[Dict[str, Any]]:
        return [_safe_key(item) for item in self._list_key_snapshots(token)]

    def reveal_key(
        self,
        token: str,
        key_id: int,
        *,
        owned_key_ids: set[int] | None = None,
    ) -> ApiKeyRevealResult:
        normalized_id = _positive_int(key_id)
        if not normalized_id:
            raise ApiKeyValidationError("必须选择有效 API Key")
        owned = owned_key_ids
        if owned is None:
            owned = {item["id"] for item in self._list_key_snapshots(token)}
        if normalized_id not in owned:
            raise ApiKeyValidationError("该 API Key 不属于当前账号")
        body = self.client.request("GET", f"/api/v1/keys/{normalized_id}", token=token)
        snapshot = _raw_key_snapshot(body)
        if (
            snapshot is None
            or snapshot["id"] != normalized_id
            or not _usable_secret(snapshot["secret"])
        ):
            raise ApiKeyProtocolError("远端未返回该 API Key 的完整值")
        return ApiKeyRevealResult(
            snapshot["id"],
            snapshot["name"],
            snapshot["group_id"],
            snapshot["status"] or "active",
            snapshot["secret"],
        )

    def update_group(self, token: str, key_id: int, group_id: int) -> Dict[str, Any]:
        normalized_id = _positive_int(key_id)
        normalized_group = _positive_int(group_id)
        if not normalized_id:
            raise ApiKeyValidationError("必须选择有效 API Key")
        if not normalized_group:
            raise ApiKeyValidationError("必须选择有效分组")
        if normalized_group not in {item["id"] for item in self.list_groups(token)}:
            raise ApiKeyValidationError("所选分组已不可用，请刷新后重试")
        before = self._list_key_snapshots(token)
        owned = next((item for item in before if item["id"] == normalized_id), None)
        if owned is None:
            raise ApiKeyValidationError("该 API Key 不属于当前账号")
        try:
            self.client.request(
                "PUT",
                f"/api/v1/keys/{normalized_id}",
                payload={"group_id": normalized_group},
                token=token,
            )
        except Sub2ApiNetworkError:
            return self._reconcile_group_update(token, normalized_id, normalized_group)
        return self._reconcile_group_update(token, normalized_id, normalized_group)

    def _reconcile_group_update(
        self, token: str, key_id: int, group_id: int
    ) -> Dict[str, Any]:
        try:
            current = next(
                (item for item in self._list_key_snapshots(token) if item["id"] == key_id),
                None,
            )
        except Exception as exc:
            raise ApiKeyMutationUncertainError(
                "分组更新请求已发出，但无法读取远端状态；请勿立即重试"
            ) from exc
        if current is None or int(current.get("group_id") or 0) != group_id:
            raise ApiKeyMutationUncertainError(
                "分组更新请求已发出，但远端状态尚未确认；请先同步 Keys"
            )
        return _safe_key(current)

    def delete_key(self, token: str, key_id: int) -> bool:
        normalized_id = _positive_int(key_id)
        if not normalized_id:
            raise ApiKeyValidationError("必须选择有效 API Key")
        owned = {item["id"] for item in self._list_key_snapshots(token)}
        if normalized_id not in owned:
            raise ApiKeyValidationError("该 API Key 不属于当前账号")
        try:
            self.client.request(
                "DELETE", f"/api/v1/keys/{normalized_id}", token=token
            )
        except Sub2ApiNetworkError:
            try:
                remaining = {item["id"] for item in self._list_key_snapshots(token)}
            except Exception as exc:
                raise ApiKeyMutationUncertainError(
                    "删除请求已发出，但无法读取远端状态；请勿立即重试"
                ) from exc
            if normalized_id in remaining:
                raise ApiKeyMutationUncertainError(
                    "删除请求已发出，但远端仍返回该 Key；请先同步确认"
                )
        return True

    def create_key(self, token: str, name: str, group_id: int) -> ApiKeyCreateResult:
        normalized_name = str(name or "").strip()
        normalized_group = _positive_int(group_id)
        if not normalized_name or len(normalized_name) > 100:
            raise ApiKeyValidationError("API Key 名称长度必须为 1-100 个字符")
        if not normalized_group:
            raise ApiKeyValidationError("必须选择有效分组")

        groups = self.list_groups(token)
        if normalized_group not in {item["id"] for item in groups}:
            raise ApiKeyValidationError("所选分组已不可用，请刷新后重试")
        before = self._list_key_snapshots(token)
        before_ids = {item["id"] for item in before}
        try:
            response = self.client.request(
                "POST",
                "/api/v1/keys",
                payload={"name": normalized_name, "group_id": normalized_group},
                token=token,
            )
        except Sub2ApiNetworkError:
            return self._reconcile_create(
                token,
                normalized_name,
                normalized_group,
                before_ids,
            )
        snapshot = _raw_key_snapshot(response)
        if snapshot is not None and _usable_secret(snapshot["secret"]):
            return ApiKeyCreateResult(
                snapshot["id"],
                snapshot["name"] or normalized_name,
                snapshot["group_id"] or normalized_group,
                snapshot["status"] or "active",
                snapshot["secret"],
            )
        return self._reconcile_create(token, normalized_name, normalized_group, before_ids)

    def _reconcile_create(
        self,
        token: str,
        name: str,
        group_id: int,
        before_ids: set[int],
    ) -> ApiKeyCreateResult:
        try:
            after = self._list_key_snapshots(token)
        except Exception as exc:
            raise ApiKeyCreateUncertainError(
                "创建请求结果未知，且无法读取远端状态；请勿立即重试"
            ) from exc
        candidates = [
            item
            for item in after
            if item["id"] not in before_ids
            and item["name"] == name
            and item["group_id"] == group_id
        ]
        if len(candidates) != 1 or not _usable_secret(candidates[0]["secret"]):
            raise ApiKeyCreateUncertainError(
                "创建请求结果未知，无法唯一确认新密钥；请先到远端核对"
            )
        item = candidates[0]
        return ApiKeyCreateResult(
            item["id"],
            item["name"],
            item["group_id"],
            item["status"] or "active",
            item["secret"],
            reconciled=True,
        )


def site_capabilities(site_key: str) -> Dict[str, bool]:
    return {"fallback_group": str(site_key or "").strip().lower() == "ctai"}
