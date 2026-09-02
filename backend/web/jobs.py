# -*- coding: utf-8 -*-
"""注册任务协调器。

以单任务模型管理后台线程、停止信号、进度统计和有界日志队列。
最近一次任务的 batch_id 与进度摘要会写入 SQLite，服务重启后可恢复。
Profile 是唯一注册作用域：任务输入 = profile_id + count。
"""
from __future__ import annotations

import collections
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Deque, Dict, List, Optional


class RegistrationJobCoordinator:
    """Single-flight registration runner with ring-buffer logs."""

    def __init__(self, max_logs: int = 2000):
        self._lock = threading.RLock()
        self._logs: Deque[Dict[str, Any]] = collections.deque(maxlen=max(100, int(max_logs)))
        self._log_seq = 0
        self._running = False
        self._stop_controller = None
        self._stop_requested_before_controller = False
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._last_error = ""
        self._target_count = 0
        self._workers = 1
        self._source = "web"
        self._completed_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._current_stage = "等待启动"
        self._current_email = ""
        self._batch_id = ""
        # 任务级 Profile 输入：profile_id + 启动时冻结的 Profile 快照（不落 config.json）
        self._profile_id = 0
        self._profile_snapshot: Dict[str, Any] = {}
        self._restored = False
        self._last_persist_at = 0.0
        self._last_persisted_completed = -1
        # 任务状态迁移独占锁：job start 置 running 与 release 临界区共用同一
        # guard，闭合 “release 检查 idle → job start → release 删账本” TOCTOU。
        self._transition_guard = threading.Lock()

    def _repository(self):
        try:
            from backend.registration import engine

            return engine.get_registration_repository()
        except Exception:
            return None

    def _snapshot_payload(self) -> Dict[str, Any]:
        return {
            "batch_id": self._batch_id,
            "running": self._running,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "target_count": self._target_count,
            "workers": self._workers,
            "source": self._source,
            "profile_id": self._profile_id,
            "last_error": self._last_error,
            "completed_count": self._completed_count,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "current_stage": self._current_stage,
            "current_email": self._current_email,
        }

    def _persist_snapshot(self, *, force: bool = False) -> None:
        """把当前任务摘要写入 SQLite；默认对进度变更做轻量节流。"""
        now = time.time()
        with self._lock:
            completed = self._completed_count
            if (
                not force
                and completed == self._last_persisted_completed
                and (now - self._last_persist_at) < 1.0
            ):
                return
            payload = self._snapshot_payload()
            self._last_persist_at = now
            self._last_persisted_completed = completed
        repo = self._repository()
        if repo is None:
            return
        try:
            repo.save_job_snapshot(payload)
        except Exception:
            # 持久化失败不影响注册主流程
            pass

    def restore_from_database(self) -> None:
        """进程启动后从 SQLite 恢复最近批次；不会恢复为 running。"""
        with self._lock:
            if self._restored:
                return
            self._restored = True
        repo = self._repository()
        if repo is None:
            return
        try:
            snap = repo.get_job_snapshot() or {}
        except Exception:
            snap = {}

        batch_id = str(snap.get("batch_id") or "").strip()
        if not batch_id:
            try:
                batch_id = str(repo.latest_web_batch_id() or "").strip()
            except Exception:
                batch_id = ""

        with self._lock:
            if self._running:
                return
            if batch_id:
                self._batch_id = batch_id
            if snap:
                self._started_at = snap.get("started_at")
                self._finished_at = snap.get("finished_at")
                self._target_count = int(snap.get("target_count") or 0)
                self._workers = max(1, int(snap.get("workers") or 1))
                self._source = str(snap.get("source") or "web")
                self._last_error = str(snap.get("last_error") or "")
                self._completed_count = int(snap.get("completed_count") or 0)
                self._success_count = int(snap.get("success_count") or 0)
                self._failure_count = int(snap.get("failure_count") or 0)
                self._current_email = str(snap.get("current_email") or "")
                # 恢复任务 Profile 显示名（profile_id 有效时从 Profile 表查名）。
                try:
                    restored_profile_id = int(snap.get("profile_id") or 0)
                except (TypeError, ValueError):
                    restored_profile_id = 0
                self._profile_id = restored_profile_id
                self._profile_snapshot = {}
                if restored_profile_id > 0:
                    try:
                        profile_row = repo.get_profile(restored_profile_id)
                        if profile_row is not None:
                            self._profile_snapshot = {
                                "id": restored_profile_id,
                                "name": str(profile_row.get("name") or ""),
                            }
                    except Exception:
                        pass
                was_running = bool(snap.get("running"))
                stage = str(snap.get("current_stage") or "").strip()
                if was_running:
                    self._running = False
                    if not self._finished_at:
                        self._finished_at = time.time()
                    self._current_stage = stage or "任务已中断（服务重启）"
                    if not self._last_error:
                        self._last_error = "服务重启，上次任务未正常收尾"
                else:
                    self._current_stage = stage or ("等待启动" if not batch_id else "最近任务已结束")
            elif batch_id:
                self._current_stage = "最近任务已结束"

        # 若快照标记仍在 running，写回为已中断，避免下次启动重复提示逻辑混乱
        if snap and snap.get("running"):
            self._persist_snapshot(force=True)

    def _update_progress_from_log(self, message: str) -> bool:
        """根据日志更新进度；返回 completed_count 是否变化。"""
        text = str(message or "").strip()
        if not text:
            return False

        changed = False
        with self._lock:
            before = self._completed_count
            if "开始第" in text and "个账号" in text:
                self._current_stage = "注册中"

            email_match = re.search(r"(?:邮箱|注册成功):\s*([^\s]+@[^\s]+)", text)
            if email_match:
                self._current_email = email_match.group(1).strip()

            boot_failure = re.search(r"(\d+)\s*个任务均记为失败", text)
            success = bool(re.search(r"\[\+\]\s*注册成功", text))
            failure = bool(re.search(r"\[-\] (?:注册)?失败 \[", text))

            remaining = max(self._target_count - self._completed_count, 0)
            if boot_failure:
                amount = min(int(boot_failure.group(1)), remaining)
                self._failure_count += amount
                self._completed_count += amount
            elif success and remaining:
                self._success_count += 1
                self._completed_count += 1
            elif failure and remaining:
                self._failure_count += 1
                self._completed_count += 1

            if self._completed_count:
                self._current_stage = (
                    "任务收尾中"
                    if self._completed_count >= self._target_count
                    else f"准备第 {self._completed_count + 1} 个账号"
                )
            changed = self._completed_count != before
        return changed

    def _append_log(self, message: str) -> None:
        text = str(message or "")
        progress_changed = self._update_progress_from_log(text)
        with self._lock:
            self._log_seq += 1
            self._logs.append(
                {
                    "id": self._log_seq,
                    "time": time.strftime("%H:%M:%S"),
                    "message": text,
                }
            )
        if progress_changed:
            self._persist_snapshot(force=True)

    def status(self) -> Dict[str, Any]:
        self.restore_from_database()
        # running 读取不取 _transition_guard：避免与长临界区的 release 操作
        # 相互阻塞（status 是高频轮询路径）。
        with self._lock:
            return {
                "running": self._running,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "target_count": self._target_count,
                "workers": self._workers,
                "source": self._source,
                "last_error": self._last_error,
                "log_count": len(self._logs),
                "latest_log_id": self._log_seq,
                "completed_count": self._completed_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "progress_percent": round(
                    (self._completed_count / self._target_count * 100)
                    if self._target_count
                    else 0,
                    1,
                ),
                "current_stage": self._current_stage,
                "current_email": self._current_email,
                "batch_id": self._batch_id,
                "profile_id": self._profile_id,
                "profile_name": (self._profile_snapshot or {}).get("name", ""),
            }

    def get_logs(self, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 500), 2000))
        threshold = max(0, int(after_id or 0))
        with self._lock:
            items = [item for item in self._logs if int(item["id"]) > threshold]
        if len(items) > safe_limit:
            items = items[-safe_limit:]
        return items

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    @contextmanager
    def idle_guard(self):
        """release 临界区独占 guard：check→delete→release 全程持有。

        job start 置 running 也通过同一 guard（锁序 guard → _lock），
        因此 “release 检查 idle → job start → release 删账本” 的 TOCTOU
        窗口被闭合：临界区内不可能有新任务开始。
        """
        with self._transition_guard:
            with self._lock:
                if self._running:
                    raise RuntimeError("已有注册任务在运行，禁止释放消费标记")
            yield

    def start(
        self,
        count: int = 1,
        profile_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        from backend.registration import engine as gr
        from backend.registration.store import normalize_profile_id

        self.restore_from_database()

        # 持久化账本失败守卫：存在时拒绝启动（fail-closed，跨进程）；
        # runner 内部还有一道同语义检查（防御直接调用与竞态）。
        guard = gr.check_ledger_guard()
        if guard:
            raise RuntimeError(
                "检测到账本写入失败守卫（ledger_write_failure.json），拒绝启动注册任务："
                f"邮箱={guard.get('email') or '未知'}，Profile={guard.get('profile_id') or '未知'}；"
                "请人工确认该邮箱已补写消费账本后删除守卫文件再启动"
            )
        result_guard = gr.check_result_guard()
        if isinstance(result_guard, dict) and result_guard:
            raise RuntimeError(
                "检测到 Sub2API 凭据恢复守卫（sub2api_result_write_failure.json），"
                f"拒绝启动注册任务：邮箱={result_guard.get('email') or '未知'}，"
                f"Profile={result_guard.get('profile_id') or '未知'}；"
                "请人工将该凭据补录进 registration_results 后删除守卫文件再启动"
            )

        # Profile 解析（任务输入，绝不写 config.json）：
        #   profile_id 必填，Profile 必须启用
        try:
            pid = normalize_profile_id(profile_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Sub2API 任务必须指定有效 Profile: {exc}") from exc
        store = gr.get_registration_repository()
        profile = store.get_profile(pid)
        if profile is None:
            raise ValueError(f"Sub2API Profile 不存在: {pid}")
        if not profile.get("enabled", True):
            raise ValueError(
                f"Sub2API Profile #{profile['id']}（{profile.get('name')}）已禁用，请先启用"
            )
        # 启动时冻结 Profile 快照：运行中改 Profile 不影响本任务
        profile_snapshot = {
            "id": profile["id"],
            "name": profile["name"],
            "site_key": profile.get("site_key", ""),
            "register_url": profile["register_url"],
            "register_origin": profile["register_origin"],
            "promo_code": profile["promo_code"],
            "invitation_code": profile["invitation_code"],
            "aff_code": profile["aff_code"],
            "whitelist": list(profile.get("whitelist") or []),
        }

        gr._bs.allow_browser_launches()
        count = max(1, min(int(count or 1), 1000))
        # v1：单 worker 固定（全局协调器仍单任务）；状态快照保留 workers=1 只读字段
        workers = 1

        # 锁序与 release 临界区一致：guard → _lock，避免死锁。
        with self._transition_guard, self._lock:
            if self._running:
                raise RuntimeError("已有注册任务在运行")
            self._running = True
            self._started_at = time.time()
            self._finished_at = None
            self._last_error = ""
            self._target_count = count
            self._workers = workers
            self._stop_controller = None
            self._stop_requested_before_controller = False
            self._completed_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._current_stage = "任务启动中"
            self._current_email = ""
            self._batch_id = ""
            self._profile_id = pid
            self._profile_snapshot = profile_snapshot
            self._append_log(
                f"[*] Web 任务启动：Profile={profile_snapshot.get('name')}#{pid} 数量={count}"
            )

        self._persist_snapshot(force=True)

        manager = self

        def runner() -> None:
            original_registration_log = gr.registration_log
            original_controller_cls = gr.RegistrationStopController

            class WebStopController:
                """Compatible with RegistrationStopController; instance is kept by manager."""

                def __init__(self) -> None:
                    self.stop_requested = False
                    with manager._lock:
                        if manager._stop_requested_before_controller:
                            self.stop_requested = True
                        manager._stop_controller = self

                def should_stop(self) -> bool:
                    return self.stop_requested

                def stop(self) -> None:
                    self.stop_requested = True

            def web_registration_log(message: str) -> None:
                try:
                    original_registration_log(message)
                except Exception:
                    pass
                manager._append_log(str(message or ""))

            try:
                gr.load_config()
                gr._wire_runtime_modules()
                gr.config["register_count"] = count
                if gr.config.get("debug_mode"):
                    gr.config["register_count"] = 1
                    manager._append_log("[*] 调试模式：强制单账号，结束后不关闭浏览器")
                    count_local = 1
                else:
                    count_local = count

                gr.registration_log = web_registration_log
                gr.RegistrationStopController = WebStopController

                original_new_batch_id = gr.new_registration_batch_id

                def capture_batch_id(source="web"):
                    batch_id = original_new_batch_id(source)
                    with manager._lock:
                        manager._batch_id = str(batch_id or "")
                    manager._append_log(f"[*] 任务批次: {batch_id}")
                    manager._persist_snapshot(force=True)
                    return batch_id

                gr.new_registration_batch_id = capture_batch_id
                try:
                    gr.run_sub2api_registration_job(
                        count_local, manager._profile_snapshot
                    )
                finally:
                    gr.new_registration_batch_id = original_new_batch_id
            except Exception as exc:
                with manager._lock:
                    manager._last_error = str(exc)
                manager._append_log(f"[!] Web 任务异常: {exc}")
                trace_text = gr.current_exception_traceback(gr.TRACEBACK_LOG_MAX_CHARS)
                manager._append_log(f"[异常堆栈]\n{trace_text}")
            finally:
                gr.registration_log = original_registration_log
                gr.RegistrationStopController = original_controller_cls
                with manager._lock:
                    self._running = False
                    self._finished_at = time.time()
                    self._stop_controller = None
                    self._stop_requested_before_controller = False
                    self._current_stage = (
                        "任务已停止"
                        if self._completed_count < self._target_count
                        else "任务已完成"
                    )
                self._append_log("[*] Web 任务已结束")
                self._persist_snapshot(force=True)

        self._thread = threading.Thread(target=runner, name="web-registration", daemon=True)
        self._thread.start()
        return self.status()

    def request_stop(self) -> Dict[str, Any]:
        with self._lock:
            controller = self._stop_controller
            running = self._running
            if running and controller is None:
                self._stop_requested_before_controller = True
        if not running:
            return self.status()
        if controller is None:
            self._append_log("[!] 已预登记停止，等待注册流程响应")
            return self.status()
        try:
            controller.stop()
            self._append_log("[!] 已请求停止注册任务")
        except Exception as exc:
            self._append_log(f"[!] 停止失败: {exc}")
            raise
        self._persist_snapshot(force=True)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        status = self.request_stop()
        if not status.get("running"):
            return status
        with self._lock:
            controller = self._stop_controller
        if controller is None:
            deadline = time.time() + 8.0
            while time.time() < deadline:
                with self._lock:
                    controller = self._stop_controller
                    running = self._running
                if controller is not None or not running:
                    break
                time.sleep(0.05)
        if controller is None:
            self._append_log("[!] 停止控制器仍未就绪")
            return self.status()
        return self.status()


job_coordinator = RegistrationJobCoordinator()
