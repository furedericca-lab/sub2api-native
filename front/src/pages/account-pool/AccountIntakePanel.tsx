import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Clock3,
  ExternalLink,
  Loader2,
  RotateCcw,
  Square,
  TerminalSquare,
  TriangleAlert,
  Ban,
} from "lucide-react";

import { api, errorMessage, type AccountIntakeTask } from "@/lib/api";
import { Badge, Button } from "@/components/ui";

type Props = {
  tasks: AccountIntakeTask[];
  onOpenAccount: (accountId: number) => void;
  onChanged: (message: string) => void;
  onRefresh: () => Promise<void>;
};

const ERROR_LABELS: Record<string, string> = {
  authentication_failure: "站点拒绝了这组邮箱或密码",
  captcha_failed: "验证码未完成，可直接重试",
  upstream_unreachable: "上游站点不可达或请求超时",
  upstream_uncertain: "上游结果不确定，请先在站点确认后重试",
  two_factor_required: "该账号启用了两步验证，需要先补充 TOTP 密钥",
  channel_busy: "账户通道被占用，请等待其它账户操作结束",
  invalid_request: "请求无效",
  upstream_error: "上游返回错误",
  timeout: "任务超过时限",
  cancelled: "任务已取消",
  operation_failed: "任务失败",
};

function statusMeta(status: string) {
  if (status === "queued") return { label: "排队中", variant: "secondary" as const, spinning: false };
  if (status === "running") return { label: "执行中", variant: "default" as const, spinning: true };
  if (status === "succeeded") return { label: "已完成", variant: "success" as const, spinning: false };
  if (status === "cancelled") return { label: "已取消", variant: "secondary" as const, spinning: false };
  if (status === "timeout") return { label: "超时", variant: "warning" as const, spinning: false };
  return { label: "失败", variant: "destructive" as const, spinning: false };
}

function errorLabel(code: string) {
  return ERROR_LABELS[code] || code || "任务失败";
}

function successLine(task: AccountIntakeTask) {
  const discovered = task.summary?.discovered ?? 0;
  const synced = task.summary?.synced ?? 0;
  const unavailable = task.summary?.unavailable ?? 0;
  return `已同步 ${synced}/${discovered} 个密钥${unavailable ? `，${unavailable} 项暂不可用` : ""}`;
}

export function AccountIntakePanel({ tasks, onOpenAccount, onChanged, onRefresh }: Props) {
  const [busy, setBusy] = useState("");
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [logBuffers, setLogBuffers] = useState<Record<number, AccountIntakeTask["logs"]>>({});
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const running = tasks.filter((task) => task.active);
    if (!running.length) return;
    let cancelled = false;
    running.forEach((task) => {
      void api
        .accountIntakeTask(task.id)
        .then((detail) => {
          if (cancelled || !mounted.current) return;
          setLogBuffers((current) => ({ ...current, [task.id]: detail.task.logs || [] }));
        })
        .catch(() => undefined);
    });
    return () => {
      cancelled = true;
    };
  }, [tasks]);

  const run = async (action: string, call: () => Promise<unknown>, message: string) => {
    setBusy(action);
    try {
      await call();
      // 重试/取消后必须立即拉一次任务列表：空闲时没有轮询，新任务不会自己出现。
      await onRefresh();
      if (message) onChanged(message);
    } catch (error) {
      onChanged(errorMessage(error));
    } finally {
      if (mounted.current) setBusy("");
    }
  };

  if (!tasks.length) return null;

  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">添加账户任务</h2>
          <p className="mt-0.5 text-xs text-slate-500">登录验证与密钥同步在后台执行，可以关闭弹窗继续其它操作。</p>
        </div>
        <span className="text-xs text-slate-500">最近 {tasks.length} 条</span>
      </div>
      <ul className="divide-y divide-slate-100">
        {tasks.map((task) => {
          const meta = statusMeta(task.status);
          const logs = logBuffers[task.id] || task.logs || [];
          const showLogs = expanded[task.id] ?? (task.active || task.status !== "succeeded");
          return (
            <li key={task.id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={meta.variant}>
                  <span className="inline-flex items-center gap-1">
                    {meta.spinning ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                    {meta.label}
                  </span>
                </Badge>
                <span className="font-medium text-slate-800">{task.email}</span>
                <span className="text-xs text-slate-500">{task.profile_name || `站点 #${task.profile_id}`}</span>
                <span className="inline-flex items-center gap-1 text-xs text-slate-500">
                  <Clock3 className="h-3 w-3" />
                  {task.status === "queued"
                    ? `队列第 ${task.queue_position + 1} 位`
                    : `${task.elapsed_seconds.toFixed(1)}s`}
                </span>
                {task.attempt > 1 ? <span className="text-xs text-slate-400">第 {task.attempt} 次</span> : null}
                <div className="ml-auto flex items-center gap-1">
                  {task.account_id > 0 ? (
                    <Button size="sm" variant="ghost" onClick={() => onOpenAccount(task.account_id)}>
                      <ExternalLink className="h-3.5 w-3.5" />查看账户
                    </Button>
                  ) : null}
                  {task.active ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy === `cancel-${task.id}`}
                      onClick={() => void run(`cancel-${task.id}`, () => api.cancelAccountIntakeTask(task.id), "已请求取消")}
                    >
                      <Square className="h-3.5 w-3.5" />取消
                    </Button>
                  ) : task.status === "cancelled" || task.status === "failed" || task.status === "timeout" ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy === `retry-${task.id}`}
                      onClick={() => void run(`retry-${task.id}`, () => api.retryAccountIntakeTask(task.id), "已重新加入后台任务")}
                    >
                      <RotateCcw className="h-3.5 w-3.5" />重试
                    </Button>
                  ) : null}
                </div>
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                {task.active ? (
                  <span className="inline-flex items-center gap-1.5 text-slate-600">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    {task.stage || "准备中"}
                  </span>
                ) : null}
                {task.status === "succeeded" ? (
                  <span className="inline-flex items-center gap-1.5 text-emerald-700">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    {successLine(task)}
                  </span>
                ) : null}
                {task.status === "cancelled" ? (
                  <span className="inline-flex items-center gap-1.5 text-slate-500">
                    <Ban className="h-3.5 w-3.5" />
                    {task.message || errorLabel(task.error_code)}
                  </span>
                ) : null}
              </div>

              {task.status !== "succeeded" && task.status !== "cancelled" ? (
                <div className="mt-1.5 rounded-md border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-800">
                  <div className="inline-flex items-center gap-1.5 font-semibold">
                    <TriangleAlert className="h-3.5 w-3.5" />
                    {errorLabel(task.error_code)}
                  </div>
                  {task.message ? <div className="mt-1 break-words">{task.message}</div> : null}
                </div>
              ) : null}

              {logs.length ? (
                <div className="mt-2">
                  <button
                    className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700"
                    onClick={() => setExpanded((current) => ({ ...current, [task.id]: !showLogs }))}
                  >
                    <TerminalSquare className="h-3.5 w-3.5" />
                    {showLogs ? "收起日志" : `展开日志（${logs.length}）`}
                  </button>
                  {showLogs ? (
                    <div className="mt-1 max-h-40 overflow-auto rounded-md bg-slate-950 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-200">
                      {logs.slice(-40).map((item) => (
                        <div key={item.id} className="whitespace-pre-wrap break-words">
                          <span className="text-slate-500">{item.time}</span> {item.message}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
