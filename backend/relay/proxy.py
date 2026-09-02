from __future__ import annotations

import time
from typing import Any, AsyncIterator, Optional

import httpx

from .router import RelayRouter

_DROP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "cookie", "set-cookie", "x-api-key", "forwarded", "via", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}

def _upstream_headers(request_headers: dict[str, str], secret: str) -> dict[str, str]:
    headers = {k.lower(): v for k, v in request_headers.items() if k.lower() not in _DROP_HEADERS and k.lower() != "authorization"}
    headers["authorization"] = f"Bearer {secret}"
    headers.setdefault("content-type", "application/json")
    return headers

def _cooldown(response: httpx.Response, default: float, rate_default: float) -> float:
    if response.status_code == 429:
        try:
            retry_after = float(response.headers.get("retry-after", ""))
            if retry_after >= 0: return min(retry_after, 3600)
        except (TypeError, ValueError):
            pass
        return rate_default
    return default if response.status_code in (401, 403) else 0


async def forward_responses(router: RelayRouter, model: str, payload: bytes, strategy: str, proxy: Optional[str] = None, timeout_seconds: float = 600, cooldown_seconds: float = 120, rate_cooldown_seconds: float = 30, max_attempts: int = 2, request_headers: Optional[dict[str, str]] = None, session_key: str = "", affinity_ttl: float = 3600) -> tuple[Optional[httpx.Response], dict[str, Any] | None]:
    candidates = router.choose(model, strategy, session_key, affinity_ttl)
    if not candidates:
        reason = router.state.no_candidate_reason(model, router.assets())
        return None, {"error": {"message": f"no pool member serves model {model}", "type": "service_unavailable", "code": reason}}
    attempts = candidates[:max(1, min(int(max_attempts), len(candidates)))]
    started_request = time.monotonic(); retries = 0; final_row = attempts[0]
    for row in attempts:
        router.state.adjust_in_flight(row["account_id"], 1)
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=timeout_seconds) as client:
                response = await client.post(row["origin"] + "/v1/responses", content=payload, headers=_upstream_headers(request_headers or {}, row["secret"]))
            cooldown = _cooldown(response, cooldown_seconds, rate_cooldown_seconds)
            if session_key: router.state.bind_session(session_key, int(row["account_id"]))
            router.state.mark(row["account_id"], "success" if response.is_success else "upstream_error", response.status_code, cooldown)
            router.state.log_request(model, row["account_id"], row["site_key"], False, "success" if response.is_success else "upstream_error", response.status_code, int((time.monotonic() - started_request) * 1000), retries)
            return response, None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            router.state.mark(row["account_id"], "transport_error", 0, 30)
            final_row = row; retries += 1
        except httpx.TransportError:
            router.state.mark(row["account_id"], "transport_error", 0, 30)
            router.state.log_request(model, row["account_id"], row["site_key"], False, "transport_error", 0, int((time.monotonic() - started_request) * 1000), retries)
            return None, {"error": {"message": "relay upstream response failed", "type": "service_unavailable", "code": "upstream_transport_error"}}
        finally:
            router.state.adjust_in_flight(row["account_id"], -1)
    router.state.log_request(model, final_row["account_id"], final_row["site_key"], False, "transport_error", 0, int((time.monotonic() - started_request) * 1000), max(0, retries - 1))
    return None, {"error": {"message": "all relay pool members failed", "type": "service_unavailable", "code": "all_members_failed"}}


async def stream_responses(router: RelayRouter, model: str, payload: bytes, strategy: str, proxy: Optional[str] = None, first_byte_timeout_seconds: float = 180, cooldown_seconds: float = 120, rate_cooldown_seconds: float = 30, max_attempts: int = 2, request_headers: Optional[dict[str, str]] = None, session_key: str = "", affinity_ttl: float = 3600) -> tuple[Optional[httpx.Response], Optional[AsyncIterator[bytes]], dict[str, Any] | None]:
    candidates = router.choose(model, strategy, session_key, affinity_ttl)
    if not candidates:
        reason = router.state.no_candidate_reason(model, router.assets())
        return None, None, {"error": {"message": f"no pool member serves model {model}", "type": "service_unavailable", "code": reason}}
    attempts = candidates[:max(1, min(int(max_attempts), len(candidates)))]; started = time.monotonic(); retries = 0
    response = None; client = None; row = attempts[0]
    for row in attempts:
        router.state.adjust_in_flight(row["account_id"], 1)
        client = httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(None, connect=30, read=first_byte_timeout_seconds, write=60, pool=30))
        request = client.build_request("POST", row["origin"] + "/v1/responses", content=payload, headers=_upstream_headers(request_headers or {}, row["secret"]))
        try:
            response = await client.send(request, stream=True); break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            await client.aclose(); router.state.mark(row["account_id"], "transport_error", 0, 30); router.state.adjust_in_flight(row["account_id"], -1); retries += 1
        except httpx.TransportError:
            await client.aclose(); router.state.mark(row["account_id"], "transport_error", 0, 30); router.state.adjust_in_flight(row["account_id"], -1)
            router.state.log_request(model, row["account_id"], row["site_key"], True, "transport_error", 0, int((time.monotonic() - started) * 1000), retries)
            return None, None, {"error": {"message": "relay upstream response failed", "type": "service_unavailable", "code": "upstream_transport_error"}}
    if response is None or client is None:
        router.state.log_request(model, row["account_id"], row["site_key"], True, "transport_error", 0, int((time.monotonic() - started) * 1000), max(0, retries - 1))
        return None, None, {"error": {"message": "relay upstream connection failed", "type": "service_unavailable", "code": "all_members_failed"}}
    if session_key: router.state.bind_session(session_key, int(row["account_id"]))

    async def chunks() -> AsyncIterator[bytes]:
        outcome = "success" if response.is_success else "upstream_error"; saw_completed = False
        try:
            async for chunk in response.aiter_raw():
                if chunk:
                    saw_completed = saw_completed or b"response.completed" in chunk
                    yield chunk
            if response.is_success and not saw_completed: outcome = "stream_interrupted"
        except Exception:
            outcome = "stream_interrupted" if response.is_success else "upstream_error"
            raise
        finally:
            if response.is_success and not saw_completed: outcome = "stream_interrupted"
            await response.aclose(); await client.aclose()
            cooldown = _cooldown(response, cooldown_seconds, rate_cooldown_seconds)
            router.state.mark(row["account_id"], outcome, response.status_code, cooldown)
            router.state.adjust_in_flight(row["account_id"], -1)
            router.state.log_request(model, row["account_id"], row["site_key"], True, outcome, response.status_code, int((time.monotonic() - started) * 1000), retries)
    return response, chunks(), None
