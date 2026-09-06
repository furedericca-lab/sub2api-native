# -*- coding: utf-8 -*-
"""账户后台任务执行器。

「添加账户」需要启动 Camoufox、过 Turnstile 再调用上游接口，正常也要十几秒，
冷启动可能数分钟，不能压在 HTTP 请求线程里等待。本模块用单工作线程 + FIFO
队列执行这类任务：请求线程只负责入队，控制台轮询任务状态与有界日志。

任务状态与日志只驻留内存，进程重启即清空；凭据只保存在任务的私有 ``_secrets``
字段里，任务快照与阶段名都不会带出密码。
"""
from __future__ import annotations

import collections
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timeout"
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMED_OUT})

_MAX_MESSAGE_CHARS = 300
_STAGE_LABELS = {
    SUCCEEDED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    TIMED_OUT: "超时",
}


class AccountTaskCancelled(RuntimeError):
    """协作式取消：任务实现在阶段边界调用 ``ctx.check_cancelled()``。"""


class AccountTaskSlotBusy(RuntimeError):
    """账户远程通道长时间被注册任务或其它账户操作占用。"""


class AccountTaskUnknownKind(ValueError):
    """未注册的任务类型。"""


class AccountTaskDuplicate(RuntimeError):
    """同一输入已有未结束的任务。"""


class AccountTaskNotFound(KeyError):
    """任务不存在（内存队列在服务重启后清空）。"""


class AccountTaskContext:
    """传给任务实现的句柄：阶段、日志、协作式取消与超时。"""

    __slots__ = ("_runner", "task_id")

    def __init__(self, runner: "AccountTaskRunner", task_id: int) -> None:
        self._runner = runner
        self.task_id = task_id

    def log(self, message: str) -> None:
        self._runner._append_log(self.task_id, str(message or ""))

    def stage(self, name: str) -> None:
        self._runner._set_stage(self.task_id, str(name or ""))

    def cancelled(self) -> bool:
        return self._runner._cancel_reason(self.task_id) is not None

    def check_cancelled(self) -> None:
        reason = self._runner._cancel_reason(self.task_id)
        if reason == TIMED_OUT:
            raise AccountTaskCancelled("任务超过时限，已中止")
        if reason == CANCELLED:
            raise AccountTaskCancelled("任务已被取消")


@dataclass(frozen=True)
class AccountTaskSpec:
    """一类后台任务的实现与边界。

    ``run`` 返回的 dict 作为成功摘要写入任务；``acquire`` 返回需要持有的账户
    远程通道上下文；``before``/``after`` 用于浏览器等运行时的成对清理；
    ``on_finish`` 可补充 ``account_id`` 一类收尾信息；``classify`` 把异常映射成
    ``(error_code, message)``，供 UI 区分认证失败、验证码失败与网络不可达。
    """

    kind: str
    run: Callable[[AccountTaskContext, Dict[str, Any]], Optional[Dict[str, Any]]]
    timeout_seconds: float = 300.0
    acquire: Optional[Callable[[AccountTaskContext], Any]] = None
    before: Optional[Callable[[AccountTaskContext], None]] = None
    after: Optional[Callable[[AccountTaskContext], None]] = None
    on_finish: Optional[
        Callable[[AccountTaskContext, Dict[str, Any], str], Optional[Dict[str, Any]]]
    ] = None
    classify: Optional[Callable[[BaseException], Any]] = None
    cancel_exceptions: Tuple[type, ...] = ()


class AccountTaskRunner:
    """单工作线程的账户后台任务队列。"""

    def __init__(
        self,
        *,
        history_limit: int = 20,
        log_limit: int = 200,
        slot_poll_interval: float = 0.5,
    ) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._specs: Dict[str, AccountTaskSpec] = {}
        self._tasks: List[Dict[str, Any]] = []
        self._pending: Deque[int] = collections.deque()
        self._history_limit = max(1, int(history_limit))
        self._log_limit = max(20, int(log_limit))
        self._slot_poll_interval = max(0.05, float(slot_poll_interval))
        self._seq = 0
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    # ---------------------------------------------------------------- 注册

    def register(self, spec: AccountTaskSpec) -> None:
        with self._lock:
            self._specs[spec.kind] = spec

    def _spec(self, kind: str) -> AccountTaskSpec:
        with self._lock:
            spec = self._specs.get(str(kind or ""))
        if spec is None:
            raise AccountTaskUnknownKind(f"未注册的账户任务类型: {kind}")
        return spec

    # ---------------------------------------------------------------- 提交

    def submit(
        self,
        kind: str,
        *,
        payload: Dict[str, Any],
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        """登记一个任务并立即返回快照；执行在工作线程内进行。"""
        spec = self._spec(kind)
        with self._wake:
            if self._stopping:
                raise RuntimeError("账户任务执行器已停止")
            key = str(dedupe_key or "")
            if key:
                for task in self._tasks:
                    if task["status"] not in TERMINAL_STATUSES and task["_dedupe"] == key:
                        raise AccountTaskDuplicate(
                            f"该邮箱的添加任务正在进行中（任务 #{task['id']}）"
                        )
            self._seq += 1
            task: Dict[str, Any] = {
                "id": self._seq,
                "kind": spec.kind,
                "status": QUEUED,
                "stage": "排队中",
                "message": "",
                "error_code": "",
                "account_id": 0,
                "summary": {},
                "attempt": 0,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "logs": collections.deque(maxlen=self._log_limit),
                "log_seq": 0,
                "cancel_reason": "",
                "_deadline": None,
                "_dedupe": key,
                "_secrets": dict((payload or {}).get("_secrets") or {}),
            }
            for field, value in (payload or {}).items():
                if field in ("_secrets", "logs"):
                    continue
                task[str(field)] = value
            self._tasks.insert(0, task)
            while len(self._tasks) > self._history_limit:
                self._tasks.pop()
            self._pending.append(task["id"])
            self._ensure_worker_locked()
            self._wake.notify_all()
            return self._snapshot(task)

    def retry(self, task_id: int) -> Dict[str, Any]:
        """用同一输入重新提交一个任务；原任务保持终态作为历史。"""
        with self._lock:
            source = self._by_id_locked(task_id)
            if source is None:
                raise AccountTaskNotFound(str(task_id))
            if source["status"] not in TERMINAL_STATUSES:
                raise AccountTaskDuplicate("任务尚未结束，无法重试")
            payload: Dict[str, Any] = {
                key: value
                for key, value in source.items()
                if not key.startswith("_") and key not in {
                    "id", "kind", "status", "stage", "message", "error_code",
                    "account_id", "summary", "attempt", "created_at", "started_at",
                    "finished_at", "logs", "log_seq", "cancel_reason",
                }
            }
            payload["_secrets"] = dict(source.get("_secrets") or {})
            payload["retried_from"] = source["id"]
            dedupe = str(source.get("_dedupe") or "")
        if not payload.get("profile_id") or not payload.get("email"):
            raise AccountTaskNotFound("任务缺少可重试的输入")
        if not payload.get("_secrets"):
            raise RuntimeError("任务凭据已失效，请重新填写添加账户表单")
        return self.submit(source["kind"], payload=payload, dedupe_key=dedupe)

    # ---------------------------------------------------------------- 查询

    def list_tasks(self, *, limit: int = 10) -> List[Dict[str, Any]]:
        safe = max(1, min(int(limit or 10), 50))
        with self._lock:
            return [self._snapshot(task) for task in self._tasks[:safe]]

    def get_task(
        self,
        task_id: int,
        *,
        after_log_id: int = 0,
        log_limit: int = 120,
    ) -> Dict[str, Any]:
        with self._lock:
            task = self._by_id_locked(task_id)
            if task is None:
                raise AccountTaskNotFound(str(task_id))
            return self._snapshot(
                task,
                include_logs=True,
                after_log_id=after_log_id,
                log_limit=log_limit,
            )

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = next(
                (t for t in self._tasks if t["status"] == RUNNING), None
            )
            return {
                "running": running is not None,
                "current_task_id": int(running["id"]) if running else 0,
                "queued": len(self._pending),
                "worker_alive": bool(self._thread and self._thread.is_alive()),
            }

    # ---------------------------------------------------------------- 取消

    def cancel(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            task = self._by_id_locked(task_id)
            if task is None:
                raise AccountTaskNotFound(str(task_id))
            if task["status"] in TERMINAL_STATUSES:
                return self._snapshot(task)
            task["cancel_reason"] = CANCELLED
            self._append_log(task["id"], "已请求取消，正在等待当前步骤结束")
            return self._snapshot(task)

    def stop(self) -> None:
        """停止工作线程；未开始的任务标记为已取消。"""
        with self._wake:
            self._stopping = True
            for task in self._tasks:
                if task["status"] == QUEUED:
                    task["status"] = CANCELLED
                    task["stage"] = _STAGE_LABELS[CANCELLED]
                    task["message"] = "服务已停止，任务未执行"
                    task["finished_at"] = time.time()
            self._pending.clear()
            self._wake.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    # ---------------------------------------------------------------- 工作线程

    def _ensure_worker_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="account-intake-worker", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._wake:
                while not self._pending and not self._stopping:
                    self._wake.wait(0.5)
                if self._stopping:
                    return
                task_id = self._pending.popleft()
                task = self._by_id_locked(task_id)
            if task is None or task["status"] not in {QUEUED}:
                continue
            self._execute(task)

    def _execute(self, task: Dict[str, Any]) -> None:
        spec = self._spec(task["kind"])
        ctx = AccountTaskContext(self, task["id"])
        with self._lock:
            task["status"] = RUNNING
            task["attempt"] = int(task["attempt"]) + 1
            task["started_at"] = time.time()
            task["finished_at"] = None
            task["message"] = ""
            task["error_code"] = ""
            task["cancel_reason"] = ""
            task["stage"] = "等待账户通道空闲"
            task["_deadline"] = time.monotonic() + max(1.0, spec.timeout_seconds)
        try:
            if spec.before is not None:
                self._call_boundary(ctx, spec.before)
            acquire = spec.acquire(ctx) if spec.acquire else nullcontext(None)
            with acquire:
                summary = spec.run(ctx, task)
            if not isinstance(summary, dict):
                summary = {}
            self._finish(ctx, task, SUCCEEDED, summary=summary)
        except (AccountTaskSlotBusy, AccountTaskCancelled) as exc:
            if isinstance(exc, AccountTaskSlotBusy):
                self._finish(
                    ctx, task, FAILED, code="channel_busy", message=str(exc) or "账户通道被占用"
                )
                return
            self._finish_cancelled(ctx, task, exc)
        except BaseException as exc:  # noqa: BLE001 - 后台任务必须吞掉异常并留痕
            if isinstance(exc, tuple(spec.cancel_exceptions or ())):
                self._finish_cancelled(ctx, task, exc)
                return
            code, message = self._classify(spec, exc)
            self._finish(ctx, task, FAILED, code=code, message=message)
        finally:
            if spec.after is not None:
                self._call_boundary(ctx, spec.after)
            with self._lock:
                task["_deadline"] = None

    def _call_boundary(
        self, ctx: AccountTaskContext, hook: Callable[[AccountTaskContext], None]
    ) -> None:
        try:
            hook(ctx)
        except Exception as exc:  # noqa: BLE001 - 边界清理失败只记录，不改变任务结论
            ctx.log(f"[!] 运行时清理失败: {str(exc)[:200]}")

    def _classify(
        self, spec: AccountTaskSpec, exc: BaseException
    ) -> tuple[str, str]:
        message = str(exc).strip() or exc.__class__.__name__
        if spec.classify is not None:
            try:
                outcome = spec.classify(exc)
                if isinstance(outcome, tuple) and len(outcome) == 2:
                    return str(outcome[0]), str(outcome[1])[:_MAX_MESSAGE_CHARS]
            except Exception:  # noqa: BLE001 - 分类失败退回默认结论
                pass
        return "operation_failed", message[:_MAX_MESSAGE_CHARS]

    def _finish_cancelled(
        self, ctx: AccountTaskContext, task: Dict[str, Any], exc: BaseException
    ) -> None:
        """取消/超时统一结论：面向人的文案走 message，具体原因只进日志。"""
        timed_out = self._cancel_reason(task["id"]) == TIMED_OUT
        detail = str(exc).strip() or exc.__class__.__name__
        if detail != "用户停止注册":
            ctx.log(f"[*] 中止原因: {detail}")
        self._finish(
            ctx,
            task,
            TIMED_OUT if timed_out else CANCELLED,
            code="timeout" if timed_out else "cancelled",
            message="任务超过时限，已中止" if timed_out else "任务已被取消",
        )

    def _finish(
        self,
        ctx: AccountTaskContext,
        task: Dict[str, Any],
        status: str,
        *,
        code: str = "",
        message: str = "",
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        spec = self._spec(task["kind"])
        extras: Optional[Dict[str, Any]] = None
        if spec.on_finish is not None:
            try:
                extras = spec.on_finish(ctx, task, status)
            except Exception as exc:  # noqa: BLE001 - 收尾信息缺失不影响结论
                ctx.log(f"[!] 任务收尾信息读取失败: {str(exc)[:200]}")
        with self._lock:
            task["status"] = status
            task["error_code"] = str(code or "")
            task["message"] = str(message or "")[:_MAX_MESSAGE_CHARS]
            task["stage"] = _STAGE_LABELS.get(status, status)
            task["finished_at"] = time.time()
            task["cancel_reason"] = ""
            if summary:
                task["summary"] = dict(summary)
                if int(summary.get("account_id") or 0) > 0:
                    task["account_id"] = int(summary["account_id"])
            if isinstance(extras, dict):
                for key, value in extras.items():
                    if key == "account_id":
                        task["account_id"] = int(value or 0)
                    elif key == "summary" and isinstance(value, dict):
                        merged = dict(task.get("summary") or {})
                        merged.update(value)
                        task["summary"] = merged
            if status == SUCCEEDED:
                task["message"] = ""
                task["error_code"] = ""

    # ---------------------------------------------------------------- 内部

    def _by_id_locked(self, task_id: int) -> Optional[Dict[str, Any]]:
        try:
            wanted = int(task_id)
        except (TypeError, ValueError):
            return None
        for task in self._tasks:
            if int(task["id"]) == wanted:
                return task
        return None

    def _append_log(self, task_id: int, message: str) -> None:
        if not message:
            return
        with self._lock:
            task = self._by_id_locked(task_id)
            if task is None:
                return
            task["log_seq"] = int(task["log_seq"]) + 1
            task["logs"].append(
                {
                    "id": int(task["log_seq"]),
                    "time": time.strftime("%H:%M:%S"),
                    "message": message[:400],
                }
            )

    def _set_stage(self, task_id: int, name: str) -> None:
        if not name:
            return
        with self._lock:
            task = self._by_id_locked(task_id)
            if task is None:
                return
            task["stage"] = name[:120]
        self._append_log(task_id, f"[stage] {name}")

    def _cancel_reason(self, task_id: int) -> Optional[str]:
        with self._lock:
            task = self._by_id_locked(task_id)
            if task is None:
                return None
            reason = str(task.get("cancel_reason") or "")
            if reason:
                return reason if reason in {CANCELLED, TIMED_OUT} else None
            deadline = task.get("_deadline")
            if deadline is not None and time.monotonic() >= float(deadline):
                task["cancel_reason"] = TIMED_OUT
                return TIMED_OUT
            return None

    def _snapshot(
        self,
        task: Dict[str, Any],
        *,
        include_logs: bool = False,
        after_log_id: int = 0,
        log_limit: int = 120,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": int(task["id"]),
            "kind": str(task.get("kind") or ""),
            "status": str(task.get("status") or ""),
            "stage": str(task.get("stage") or ""),
            "message": str(task.get("message") or ""),
            "error_code": str(task.get("error_code") or ""),
            "profile_id": int(task.get("profile_id") or 0),
            "profile_name": str(task.get("profile_name") or ""),
            "email": str(task.get("email") or ""),
            "account_id": int(task.get("account_id") or 0),
            "summary": dict(task.get("summary") or {}),
            "attempt": int(task.get("attempt") or 0),
            "retried_from": int(task.get("retried_from") or 0),
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "elapsed_seconds": round(
                float(
                    (
                        (task.get("finished_at") or time.time())
                        - float(task.get("started_at") or task.get("created_at") or 0.0)
                    )
                    if task.get("started_at")
                    else 0.0
                ),
                1,
            ),
            "queue_position": 0,
            "active": str(task.get("status")) not in TERMINAL_STATUSES,
        }
        status = str(task.get("status") or "")
        if status == QUEUED:
            try:
                data["queue_position"] = max(
                    0, list(self._pending).index(int(task["id"]))
                )
            except ValueError:
                data["queue_position"] = 0
        if include_logs:
            threshold = max(0, int(after_log_id or 0))
            safe_limit = max(1, min(int(log_limit or 120), 500))
            logs = [
                dict(item)
                for item in task["logs"]
                if int(item["id"]) > threshold
            ]
            data["logs"] = logs[-safe_limit:]
            data["log_cursor"] = int(task["log_seq"])
        return data


def wait_for_slot(
    runner: AccountTaskRunner,
    ctx: AccountTaskContext,
    acquire_once: Callable[[], bool],
    *,
    busy_hint: str = "已有账户远程操作正在执行，正在排队等待",
    poll_interval: Optional[float] = None,
    on_wait: Optional[Callable[[AccountTaskContext], None]] = None,
) -> None:
    """阻塞等待 ``acquire_once()`` 成功，超时/取消时抛出协作式取消。"""
    interval = poll_interval if poll_interval is not None else runner._slot_poll_interval
    waited = False
    while True:
        ctx.check_cancelled()
        if acquire_once():
            if waited and on_wait is not None:
                on_wait(ctx)
            return
        if not waited:
            ctx.log(f"[*] {busy_hint}")
            if on_wait is not None:
                on_wait(ctx)
            waited = True
        time.sleep(interval)
