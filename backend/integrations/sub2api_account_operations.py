from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.registration.verified_sites import get_verified_site

from .sub2api_account_session import Sub2ApiAccountSession
from .sub2api_checkin import (
    STATUS_ALREADY,
    STATUS_AUTH_FAILURE,
    STATUS_SUCCESS,
    CamoufoxCaptchaSolver,
    Sub2ApiCheckinService,
    Sub2ApiClient,
)
from .sub2api_transport import Sub2ApiApiError


class AccountOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeySyncSummary:
    discovered: int
    synced: int
    unavailable: int
    missing: int

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "synced": self.synced,
            "unavailable": self.unavailable,
            "missing": self.missing,
        }


class AccountOperationsService:
    """Account-centered login, check-in, and API Key operations."""

    def __init__(
        self,
        repository: Any,
        crypto: Any,
        *,
        proxies: Optional[dict[str, str]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        session_factory: Callable[..., Sub2ApiAccountSession] = Sub2ApiAccountSession,
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.proxies = dict(proxies or {})
        self.log_callback = log_callback
        self.session_factory = session_factory

    def _account(self, account_id: int) -> dict[str, Any]:
        account = self.repository.get_account_context(account_id)
        if not account:
            raise AccountOperationError("Account 不存在")
        site = get_verified_site(str(account.get("site_key") or ""))
        if not site:
            raise AccountOperationError("Account 对应站点尚未验证")
        origin = str(account.get("register_origin") or "").strip()
        if not origin:
            raise AccountOperationError("Account 对应 Profile origin 无效")
        account["site"] = site
        account["origin"] = origin
        return account

    @staticmethod
    def _authentication_failure(exc: BaseException) -> bool:
        return isinstance(exc, Sub2ApiApiError) and exc.status_code in {401, 403}

    def _session(self, account: dict[str, Any]) -> Sub2ApiAccountSession:
        return self.session_factory(
            account["origin"],
            str(account.get("email") or ""),
            str(account.get("password") or ""),
            proxies=self.proxies,
            log_callback=self.log_callback,
        )

    @contextmanager
    def _authenticated_session(self, account_id: int, account: dict[str, Any]):
        try:
            with self._session(account) as session:
                yield session
        except Exception as exc:
            self.repository.record_account_login(
                account_id,
                success=False,
                error=str(exc),
                authentication_failure=self._authentication_failure(exc),
            )
            raise
        else:
            self.repository.record_account_login(account_id, success=True)

    def verify(self, account_id: int) -> dict[str, Any]:
        account = self._account(account_id)
        with self._authenticated_session(account_id, account):
            pass
        return self.repository.get_account_context(account_id)

    def add_account(
        self, profile_id: int, email: str, password: str
    ) -> tuple[dict[str, Any], KeySyncSummary]:
        profile = self.repository.get_profile(profile_id)
        site = get_verified_site(str((profile or {}).get("site_key") or ""))
        if not profile:
            raise AccountOperationError("Profile 不存在")
        if not site:
            raise AccountOperationError("Profile 站点尚未验证")
        if not bool(profile.get("enabled")):
            raise AccountOperationError("Profile 已停用，不能添加 Account")
        account_stub = {
            "origin": str(profile.get("register_origin") or ""),
            "email": str(email or "").strip(),
            "password": str(password or ""),
        }
        with self._session(account_stub) as session:
            account = self.repository.create_account(
                profile_id, account_stub["email"], account_stub["password"], "manual"
            )
            account_id = int(account["id"])
            self.repository.record_account_login(account_id, success=True)
            try:
                remote = session.keys.list_keys(session.token)
            except Exception as exc:
                self.repository.record_account_login(
                    account_id, success=False, error=f"API Key 同步失败: {exc}"
                )
                summary = KeySyncSummary(0, 0, 1, 0)
            else:
                summary = self._sync_with_session(account_id, session, remote)
        return self.repository.get_account_context(account["id"]), summary

    def _sync_with_session(
        self,
        account_id: int,
        session: Sub2ApiAccountSession,
        remote: Optional[list[dict[str, Any]]] = None,
    ) -> KeySyncSummary:
        listed = remote if remote is not None else session.keys.list_keys(session.token)
        remote_ids = {int(item.get("id") or 0) for item in listed if int(item.get("id") or 0) > 0}
        synced_ids: list[int] = []
        unavailable = 0
        for item in listed:
            remote_id = int(item.get("id") or 0)
            if remote_id <= 0:
                continue
            try:
                revealed = session.keys.reveal_key(
                    session.token, remote_id, owned_key_ids=remote_ids
                )
            except Exception:
                unavailable += 1
                self.repository.update_account_key_metadata(
                    account_id,
                    remote_id,
                    name=str(item.get("name") or ""),
                    group_id=int(item.get("group_id") or 0),
                    status=str(item.get("status") or "active"),
                )
                continue
            synced_ids.append(
                self.repository.upsert_account_key(
                    account_id,
                    revealed.id,
                    revealed.name,
                    self.crypto.encrypt(revealed.secret),
                    revealed.group_id,
                    revealed.status,
                )
            )
        missing = self.repository.reconcile_account_keys(account_id, remote_ids)
        account = self.repository.get_account(account_id)
        if synced_ids and account and not account.get("relay_key_id"):
            self.repository.set_relay_key(account_id, synced_ids[0])
        return KeySyncSummary(len(listed), len(synced_ids), unavailable, missing)

    def sync_keys(self, account_id: int) -> KeySyncSummary:
        account = self._account(account_id)
        with self._authenticated_session(account_id, account) as session:
            return self._sync_with_session(account_id, session)

    def groups(self, account_id: int) -> list[dict[str, Any]]:
        account = self._account(account_id)
        with self._authenticated_session(account_id, account) as session:
            groups = session.keys.list_groups(session.token)
        return groups

    def create_key(
        self, account_id: int, name: str, group_id: int
    ) -> tuple[int, dict[str, Any]]:
        account = self._account(account_id)
        with self._authenticated_session(account_id, account) as session:
            created = session.keys.create_key(session.token, name, group_id)
        row_id = self.repository.upsert_account_key(
            account_id,
            created.id,
            created.name,
            self.crypto.encrypt(created.secret),
            created.group_id,
            created.status,
        )
        if not account.get("relay_key_id"):
            self.repository.set_relay_key(account_id, row_id)
        return row_id, created.as_dict()

    def update_key_group(
        self, account_id: int, key_row_id: int, group_id: int
    ) -> dict[str, Any]:
        account = self._account(account_id)
        key = self.repository.get_account_key(key_row_id)
        if not key or int(key.get("account_id") or 0) != account_id:
            raise AccountOperationError("API Key 不属于该账号")
        with self._authenticated_session(account_id, account) as session:
            updated = session.keys.update_group(
                session.token, int(key["remote_key_id"]), group_id
            )
        self.repository.update_account_key_metadata(
            account_id,
            int(key["remote_key_id"]),
            name=str(updated.get("name") or key.get("name") or ""),
            group_id=int(updated.get("group_id") or group_id),
            status=str(updated.get("status") or key.get("status") or "active"),
        )
        return self.repository.get_account_key(key_row_id)

    def delete_key(self, account_id: int, key_row_id: int) -> None:
        account = self._account(account_id)
        key = self.repository.get_account_key(key_row_id)
        if not key or int(key.get("account_id") or 0) != account_id:
            raise AccountOperationError("API Key 不属于该账号")
        with self._authenticated_session(account_id, account) as session:
            session.keys.delete_key(session.token, int(key["remote_key_id"]))
        self.repository.mark_account_key_deleted(account_id, key_row_id)

    def checkin(self, account_id: int) -> dict[str, Any]:
        account = self._account(account_id)
        if not bool(account["site"].checkin_supported):
            raise AccountOperationError("该站点尚未验证签到功能")
        solver = CamoufoxCaptchaSolver(log_callback=self.log_callback)
        client = Sub2ApiClient(account["origin"], timeout=30, proxies=self.proxies)
        try:
            result = Sub2ApiCheckinService(client, solver).run(
                str(account.get("email") or ""),
                str(account.get("password") or ""),
            )
        finally:
            try:
                solver.close()
            finally:
                client.close()
        success = result.status in {STATUS_SUCCESS, STATUS_ALREADY}
        self.repository.record_account_checkin(
            account_id, success=success, error="" if success else result.message
        )
        if result.status == STATUS_AUTH_FAILURE:
            self.repository.record_account_login(
                account_id,
                success=False,
                error=result.message,
                authentication_failure=True,
            )
        return result.as_dict()
