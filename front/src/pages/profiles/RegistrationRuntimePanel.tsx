import { memo, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowDownToLine,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleOff,
  Clock3,
  Copy,
  ExternalLink,
  Image as ImageIcon,
  Loader2,
  MoreHorizontal,
  RotateCcw,
  Search,
  Square,
  TerminalSquare,
  Wifi,
  X,
  XCircle,
} from "lucide-react";
import {
  api,
  errorMessage,
  type JobStatus,
  type LogItem,
  type RegistrationAttempt,
} from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  PageHeader,
  Switch,
  Toast,
  buttonVariants,
} from "@/components/ui";
import { useRegistrationJob } from "@/app/RegistrationJobContext";
import { cn } from "@/lib/utils";

type BusyAction = "" | "stop" | "check" | "kill";
type LogTone = "default" | "success" | "error" | "warn" | "info";
type DisplayLogItem = LogItem & { tone: LogTone; searchText: string };

const MAX_LOG_BUFFER = 2000;
const DEFAULT_RENDERED_LOGS = 300;
const LOG_RENDER_STEP = 300;

function formatDuration(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatClock(ts?: number | null) {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

function detectLogTone(message: string): LogTone {
  if (/(error|failed|failure|exception|traceback|拒绝|失败|异常|拦截|timeout|timed out)/i.test(message)) {
    if (/(success|成功|完成)/i.test(message) && !/(fail|失败|error)/i.test(message)) return "success";
    return "error";
  }
  if (/(warn|warning|风险|注意|重试|retry)/i.test(message)) return "warn";
  if (/(success|成功|完成|imported|saved|已保存)/i.test(message)) return "success";
  if (/(stage|step|开始|启动|waiting|proxy|browser)/i.test(message)) return "info";
  return "default";
}

const logToneClass: Record<LogTone, string> = {
  default: "text-slate-700",
  success: "text-emerald-700",
  error: "text-rose-700",
  warn: "text-amber-700",
  info: "text-slate-800",
};

const ElapsedTime = memo(function ElapsedTime({
  startedAt,
  finishedAt,
  running,
}: {
  startedAt?: number | null;
  finishedAt?: number | null;
  running: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);

  if (!startedAt) return <>—</>;
  const endMs = running ? now : finishedAt ? finishedAt * 1000 : now;
  return <>{formatDuration(Math.max(0, (endMs - startedAt * 1000) / 1000))}</>;
});

const LogLine = memo(function LogLine({ item }: { item: DisplayLogItem }) {
  return (
    <div className="border-b border-slate-200/60 py-0.5 last:border-0 [contain-intrinsic-size:auto_24px] [content-visibility:auto]">
      <span className="text-sky-600">[{item.time}]</span>{" "}
      <span className={cn("whitespace-pre-wrap break-all", logToneClass[item.tone])}>{item.message}</span>
    </div>
  );
});

export function RegistrationRuntimePanel() {
  const { acceptJobObservation, beginJobObservation, job, replaceJob } = useRegistrationJob();
  const [logs, setLogs] = useState<DisplayLogItem[]>([]);
  const [renderedLogLimit, setRenderedLogLimit] = useState(DEFAULT_RENDERED_LOGS);
  const [logViewCleared, setLogViewCleared] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [showJumpBottom, setShowJumpBottom] = useState(false);
  const [logQuery, setLogQuery] = useState("");
  const [logLevel, setLogLevel] = useState<"all" | LogTone>("all");
  const [busyAction, setBusyAction] = useState<BusyAction>("");
  const [checks, setChecks] = useState<Array<{ name: string; ok: boolean; detail: string }>>([]);
  const [opsOpen, setOpsOpen] = useState(false);
  const [checksOpen, setChecksOpen] = useState(false);
  const [runResults, setRunResults] = useState<RegistrationAttempt[]>([]);
  const [runResultsTotal, setRunResultsTotal] = useState(0);
  const [resultDetail, setResultDetail] = useState<RegistrationAttempt | null>(null);
  const [resultTab, setResultTab] = useState<"all" | "success" | "failure">("all");
  const [resultsExpanded, setResultsExpanded] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({
    message: "",
  });
  const logRef = useRef<HTMLDivElement | null>(null);
  const afterIdRef = useRef(0);
  const logViewVersionRef = useRef(0);
  const pollingRef = useRef(false);
  const userPinnedRef = useRef(false);
  const wasRunningRef = useRef(false);

  const progressTarget = Math.max(Number(job?.target_count || 1), 1);
  const progressCompleted = Math.min(Number(job?.completed_count || 0), progressTarget);
  const progressPercent = Math.min(
    100,
    Math.max(0, Number(job?.progress_percent ?? (progressCompleted / progressTarget) * 100))
  );
  const successCount = Number(job?.success_count || 0);
  const failureCount = Number(job?.failure_count || 0);
  const pendingCount = Math.max(progressTarget - progressCompleted, 0);
  const successRate =
    progressCompleted > 0 ? Math.round((successCount / Math.max(progressCompleted, 1)) * 100) : null;

  const deferredLogQuery = useDeferredValue(logQuery);

  const filteredLogs = useMemo(() => {
    const q = deferredLogQuery.trim().toLowerCase();
    if (!q && logLevel === "all") return logs;
    return logs.filter((item) => {
      if (logLevel !== "all" && item.tone !== logLevel) return false;
      if (!q) return true;
      return item.searchText.includes(q);
    });
  }, [logs, deferredLogQuery, logLevel]);

  const renderedLogs = useMemo(
    () => filteredLogs.slice(-renderedLogLimit),
    [filteredLogs, renderedLogLimit]
  );
  const hiddenFilteredLogCount = Math.max(filteredLogs.length - renderedLogs.length, 0);
  const latestRenderedLogId = renderedLogs[renderedLogs.length - 1]?.id || 0;

  const showToast = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  };

  const refreshLogs = useCallback(async (): Promise<JobStatus | null> => {
    if (pollingRef.current) return null;
    pollingRef.current = true;
    const viewVersion = logViewVersionRef.current;
    const observation = beginJobObservation();
    try {
      const data = await api.logs(afterIdRef.current, 500);
      if (!acceptJobObservation(observation, data.job)) return null;
      if (viewVersion !== logViewVersionRef.current) return data.job;
      const freshLogs = (data.logs || []).filter((item) => item.id > afterIdRef.current);
      if (freshLogs.length) {
        const preparedLogs = freshLogs.map((item) => ({
          ...item,
          tone: detectLogTone(item.message),
          searchText: `${item.time || ""}\n${item.message}`.toLowerCase(),
        }));
        setLogs((prev) => [...prev, ...preparedLogs].slice(-MAX_LOG_BUFFER));
        afterIdRef.current = freshLogs[freshLogs.length - 1].id;
        setLogViewCleared(false);
      }
      return data.job;
    } catch {
      return null;
    } finally {
      pollingRef.current = false;
    }
  }, [acceptJobObservation, beginJobObservation]);

  useEffect(() => {
    if (!job || job.running || !(job.batch_id || job.started_at || job.finished_at)) return;
    void refreshLogs();
  }, [job?.batch_id, job?.finished_at, job?.running, job?.started_at, refreshLogs]);

  useEffect(() => {
    if (!job?.running) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const current = await refreshLogs();
      if (cancelled) return;
      if (current && !current.running) return;
      timer = window.setTimeout(tick, current ? 1500 : 3000);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [job?.running, refreshLogs]);

  useEffect(() => {
    if (!resultsExpanded) return;
    let cancelled = false;
    let timer: number | undefined;

    const pullResults = async () => {
      try {
        const batchId = String(job?.batch_id || "").trim();
        const data = await api.registrationAttempts(
          batchId
            ? { batchId, limit: 100, offset: 0 }
            : { limit: 20, offset: 0 }
        );
        if (cancelled) return;
        setRunResults(data.items || []);
        setRunResultsTotal(Number(data.total ?? data.items?.length ?? 0));
      } catch {
        // 忽略列表刷新失败，不影响日志轮询
      }
    };

    void pullResults();
    // 运行中更频繁刷新结果；空闲时也拉一次以便展示最近批次
    if (job?.running) {
      timer = window.setInterval(() => void pullResults(), 4000);
    }
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
  }, [resultsExpanded, job?.batch_id, job?.running]);

  useEffect(() => {
    setRunResults([]);
    setRunResultsTotal(0);
    setResultDetail(null);
  }, [job?.batch_id]);

  useEffect(() => {
    setRenderedLogLimit(DEFAULT_RENDERED_LOGS);
  }, [deferredLogQuery, logLevel]);

  useEffect(() => {
    if (autoScroll && !userPinnedRef.current && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
      setShowJumpBottom(false);
    }
  }, [latestRenderedLogId, autoScroll]);

  useEffect(() => {
    if (!opsOpen && !checksOpen && !resultDetail) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpsOpen(false);
        setChecksOpen(false);
        setResultDetail(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [opsOpen, checksOpen, resultDetail]);

  useEffect(() => {
    const running = !!job?.running;
    if (running) setDetailsOpen(true);
    else if (wasRunningRef.current) setDetailsOpen(false);
    wasRunningRef.current = running;
  }, [job?.running]);

  const onLogScroll = () => {
    const el = logRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    userPinnedRef.current = !nearBottom;
    setShowJumpBottom(!nearBottom);
  };

  const jumpToBottom = () => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    userPinnedRef.current = false;
    setShowJumpBottom(false);
  };

  const revealOlderLogs = () => {
    const el = logRef.current;
    const previousHeight = el?.scrollHeight || 0;
    const previousTop = el?.scrollTop || 0;
    setRenderedLogLimit((current) =>
      Math.min(filteredLogs.length, current + LOG_RENDER_STEP)
    );
    window.requestAnimationFrame(() => {
      if (!el) return;
      el.scrollTop = previousTop + Math.max(el.scrollHeight - previousHeight, 0);
    });
  };

  const onStop = async () => {
    setBusyAction("stop");
    try {
      const data = await api.stopJob();
      replaceJob(data.job);
      showToast("已请求停止", "success");
    } catch (error) {
      showToast(errorMessage(error, "停止失败"), "error");
    } finally {
      setBusyAction("");
    }
  };

  const onCheck = async () => {
    setBusyAction("check");
    try {
      const data = await api.connectivity();
      setChecks(data.items || []);
      setChecksOpen(true);
      setOpsOpen(false);
      showToast(data.blocked ? "目标站点被拦截，请检查代理" : "连通性检查完成", data.blocked ? "error" : "success");
    } catch (error) {
      showToast(errorMessage(error, "检查失败"), "error");
    } finally {
      setBusyAction("");
    }
  };

  const onKillBrowsers = async () => {
    if (!window.confirm("终止所有托管浏览器进程？正在运行的注册任务也会先请求停止。")) return;
    setBusyAction("kill");
    try {
      const data = await api.killAllBrowsers();
      replaceJob(data.job);
      setOpsOpen(false);
      showToast(`已终止 ${data.killed} 个进程，清理 ${data.profiles_cleaned} 个资料目录`, "success");
    } catch (error) {
      showToast(errorMessage(error, "终止浏览器失败"), "error");
    } finally {
      setBusyAction("");
    }
  };

  const clearLogView = () => {
    const latestId = Math.max(afterIdRef.current, Number(job?.latest_log_id || 0));
    logViewVersionRef.current += 1;
    setLogs([]);
    setRenderedLogLimit(DEFAULT_RENDERED_LOGS);
    afterIdRef.current = latestId;
    setLogViewCleared(true);
    showToast(job?.running ? "视图已清空，将继续接收新日志" : "日志视图已清空");
  };

  const copyVisibleLogs = async () => {
    const text = filteredLogs.map((item) => `[${item.time}] ${item.message}`).join("\n");
    if (!text) {
      showToast("没有可复制的日志", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast(`已复制 ${filteredLogs.length} 行日志`, "success");
    } catch {
      showToast("复制失败，请手动选择文本", "error");
    }
  };

  const stageText = job?.current_stage || (job?.running ? "执行中" : "空闲");
  const levelFilters: Array<{ id: "all" | LogTone; label: string }> = [
    { id: "all", label: "全部" },
    { id: "error", label: "错误" },
    { id: "warn", label: "警告" },
    { id: "success", label: "成功" },
    { id: "info", label: "流程" },
  ];

  const metricItems = [
    { label: "目标账户", value: job?.target_count ?? progressTarget, tone: "text-slate-950" },
    { label: "已完成", value: progressCompleted, tone: "text-slate-950" },
    { label: "成功", value: successCount, tone: "text-emerald-600" },
    { label: "失败", value: failureCount, tone: "text-rose-600" },
    { label: "待处理", value: pendingCount, tone: "text-slate-950" },
    { label: "成功率", value: successRate == null ? "—" : `${successRate}%`, tone: "text-slate-950" },
    { label: "日志行", value: logs.length, tone: "text-slate-950" },
  ];

  const hasTask = !!(job?.running || job?.batch_id || job?.started_at || job?.finished_at);

  if (!job) {
    return (
      <section id="registration-runtime" className="scroll-mt-28 flex min-h-14 items-center justify-between gap-3 border-t border-slate-200 pt-4 text-sm text-slate-500" aria-live="polite">
        <span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />正在同步注册任务状态</span>
        <Link to="/registration-attempts" className="shrink-0 text-sky-700 hover:underline">查看注册历史</Link>
      </section>
    );
  }

  if (!hasTask) {
    return (
      <section id="registration-runtime" className="scroll-mt-28 flex min-h-14 items-center justify-between gap-3 border-t border-slate-200 pt-4 text-sm text-slate-500">
        <span>当前没有运行中的注册任务</span>
        <Link to="/registration-attempts" className="shrink-0 text-sky-700 hover:underline">查看注册历史</Link>
      </section>
    );
  }

  if (!job?.running && !detailsOpen) {
    const profile = job?.profile_name || "最近站点";
    return (
      <section id="registration-runtime" className="scroll-mt-28 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 pt-4 text-sm">
        <div className="min-w-0 text-slate-600">
          <span className="font-medium text-slate-900">最近注册：</span>
          {profile} · {job?.target_count || 0} 个 · 成功 {successCount} / 失败 {failureCount}
          {job?.finished_at ? ` · ${formatClock(job.finished_at)}` : ""}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <Link to={`/registration-attempts${job?.profile_id ? `?profile_id=${job.profile_id}` : ""}`} className="text-sky-700 hover:underline">查看历史</Link>
          <Button size="sm" variant="outline" onClick={() => setDetailsOpen(true)}>查看详情</Button>
        </div>
      </section>
    );
  }

  return (
    <div id="registration-runtime" className="scroll-mt-28 space-y-5 sm:space-y-6">
      <PageHeader
        title="注册运行"
        description={job?.profile_name ? `${job.profile_name} 的当前任务进度、实时日志与结果摘要。` : "查看任务进度、实时日志与结果摘要。"}
        actions={
          <>
            <Badge variant={job?.running ? "warning" : job?.last_error ? "destructive" : "success"}>
              {job?.running ? "任务运行中" : job?.last_error ? "异常结束" : "空闲"}
            </Badge>
            <Link to="/registration-attempts" className={cn(buttonVariants({ variant: "secondary", size: "sm" }), "inline-flex")}>
              查看历史
            </Link>
          </>
        }
      />

      {/* 指标条：信息尽量铺全 */}
      <Card className="overflow-hidden">
        <section className="grid grid-cols-2 divide-x divide-y divide-slate-200 sm:grid-cols-4 xl:grid-cols-8 xl:divide-y-0">
          {metricItems.map((item) => (
            <div key={item.label} className="p-3 sm:p-4">
              <div className="text-[11px] text-slate-500 sm:text-xs">{item.label}</div>
              <div className={cn("mt-1.5 text-xl font-semibold tabular-nums sm:text-2xl", item.tone)}>{item.value}</div>
            </div>
          ))}
        </section>
      </Card>

      {/* 进度 + 主操作 */}
      <Card className="overflow-hidden">
        <CardHeader className="border-b border-slate-100">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <CardTitle className="flex flex-wrap items-center gap-2">
                注册进度
                <span
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
                    job?.running ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-600"
                  )}
                >
                  <span className={cn("h-1.5 w-1.5 rounded-full", job?.running ? "animate-pulse bg-amber-500" : "bg-slate-400")} />
                  {job?.running ? "执行中" : "未运行"}
                </span>
              </CardTitle>
              <CardDescription className="mt-1 break-all">
                {stageText}
                {job?.current_email ? ` · ${job.current_email}` : ""}
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="destructive" size="sm" onClick={onStop} disabled={!!busyAction || !job?.running}>
                {busyAction === "stop" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                停止任务
              </Button>
              <Button variant="outline" size="sm" onClick={() => setOpsOpen(true)}>
                <MoreHorizontal className="h-4 w-4" />
                更多操作
              </Button>
              <div className="text-right sm:min-w-24 sm:pl-2">
                <div className="text-3xl font-semibold tabular-nums text-slate-950">{Math.round(progressPercent)}%</div>
                <div className="text-xs tabular-nums text-slate-500">
                  {progressCompleted} / {progressTarget}
                </div>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 p-4 sm:p-6">
          <div
            className="h-3 overflow-hidden rounded-full bg-slate-100"
            role="progressbar"
            aria-label="账户注册进度"
            aria-valuemin={0}
            aria-valuemax={progressTarget}
            aria-valuenow={progressCompleted}
          >
            <div
              className="h-full rounded-full bg-sky-500 transition-[width] duration-500"
              style={{ width: `${progressPercent}%` }}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
              <div className="flex items-center gap-1.5 text-xs text-slate-500">
                <Clock3 className="h-3.5 w-3.5" />
                已用时
              </div>
              <div className="mt-1 text-base font-semibold tabular-nums text-slate-950">
                <ElapsedTime
                  startedAt={job?.started_at}
                  finishedAt={job?.finished_at}
                  running={!!job?.running}
                />
              </div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
              <div className="text-xs text-slate-500">开始时间</div>
              <div className="mt-1 text-sm font-medium tabular-nums text-slate-900">{formatClock(job?.started_at)}</div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
              <div className="text-xs text-slate-500">结束时间</div>
              <div className="mt-1 text-sm font-medium tabular-nums text-slate-900">
                {job?.running ? "进行中" : formatClock(job?.finished_at)}
              </div>
            </div>
            <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3">
              <div className="text-xs text-slate-500">当前阶段 / 邮箱</div>
              <div className="mt-1 truncate text-sm font-medium text-slate-900" title={`${stageText}${job?.current_email ? ` · ${job.current_email}` : ""}`}>
                {stageText}
                {job?.current_email ? ` · ${job.current_email}` : ""}
              </div>
            </div>
          </div>

          {job?.last_error ? (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-6 text-red-800">
              <span className="font-medium">最近错误：</span>
              {job.last_error}
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
              <span>{job?.running ? "任务执行中，日志会持续同步" : "当前没有运行中的注册任务"}</span>
              <span className="tabular-nums">
                视图缓冲 {logs.length} 行
                {filteredLogs.length !== logs.length ? ` · 筛选后 ${filteredLogs.length}` : ""}
                {renderedLogs.length !== filteredLogs.length ? ` · 当前渲染 ${renderedLogs.length}` : ""}
                {" · "}
                源 {job?.source || "—"}
              </span>
            </div>
          )}
        </CardContent>
      </Card>

      
      {/* 本次结果：默认折叠，标题显示成功/失败 */}
      {(() => {
        const PREVIEW_LIMIT = 20;
        const batchId = String(job?.batch_id || "").trim();
        const successItems = runResults.filter((item) => item.status === "success" || item.success);
        const failureItems = runResults.filter(
          (item) => item.status === "failure" || (!item.success && item.status !== "success")
        );
        const resultSuccessCount = Number(job?.success_count ?? successItems.length);
        const resultFailureCount = Number(job?.failure_count ?? failureItems.length);
        const totalCount = Math.max(
          Number(job?.completed_count || 0),
          runResultsTotal,
          runResults.length,
          resultSuccessCount + resultFailureCount
        );
        const visible =
          resultTab === "success" ? successItems : resultTab === "failure" ? failureItems : runResults;
        const accountsQuery = (status?: string) => {
          const sp = new URLSearchParams();
          if (status) sp.set("status", status);
          if (batchId) sp.set("batch_id", batchId);
          const qs = sp.toString();
          return qs ? `/registration-attempts?${qs}` : "/registration-attempts";
        };
        return (
          <Card className="overflow-hidden">
            <button
              type="button"
              onClick={() => setResultsExpanded((value) => !value)}
              className="flex w-full items-center gap-3 border-b border-slate-100 px-4 py-3 text-left transition hover:bg-slate-50/80 sm:px-5"
              aria-expanded={resultsExpanded}
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-600">
                {resultsExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-base font-semibold text-slate-950">本次结果</span>
                  <Badge variant="success">成功 {resultSuccessCount}</Badge>
                  <Badge variant="destructive">失败 {resultFailureCount}</Badge>
                  <span className="text-xs tabular-nums text-slate-500">共 {totalCount} 条</span>
                </span>
                <span className="mt-1 block truncate text-xs text-slate-500">
                  {resultsExpanded
                    ? batchId
                      ? `当前批次 ${batchId} · 展开后最多预览 ${PREVIEW_LIMIT} 条`
                      : job?.running
                        ? "批次号生成后将按本轮任务精确筛选"
                        : "展开查看本页预览；完整列表请到注册历史"
                    : batchId
                      ? `已折叠 · 批次 ${batchId}`
                      : "已折叠 · 点击展开查看明细"}
                </span>
              </span>
              <span className="shrink-0 text-xs font-medium text-slate-500">
                {resultsExpanded ? "收起" : "展开"}
              </span>
            </button>

            {resultsExpanded ? (
              <>
                <div className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
                  <div className="flex flex-wrap gap-1.5">
                    {(
                      [
                        ["all", `全部 ${totalCount}`],
                        ["success", `成功 ${resultSuccessCount}`],
                        ["failure", `失败 ${resultFailureCount}`],
                      ] as const
                    ).map(([id, label]) => (
                      <button
                        key={id}
                        type="button"
                        onClick={() => setResultTab(id)}
                        className={cn(
                          "rounded-full px-2.5 py-1 text-xs font-medium transition",
                          resultTab === id ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                        )}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Link
                      to={accountsQuery()}
                      className={cn(buttonVariants({ variant: "outline", size: "sm" }), "inline-flex items-center gap-1.5")}
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                      全部注册记录
                    </Link>
                    <Link
                      to={accountsQuery("success")}
                      className={cn(buttonVariants({ variant: "outline", size: "sm" }), "inline-flex")}
                    >
                      成功筛选
                    </Link>
                    <Link
                      to={accountsQuery("failure")}
                      className={cn(buttonVariants({ variant: "outline", size: "sm" }), "inline-flex")}
                    >
                      失败筛选
                    </Link>
                  </div>
                </div>

                <CardContent className="p-0">
                  {visible.length === 0 ? (
                    <div className="px-4 py-10 text-center text-sm text-slate-500 sm:px-5">
                      {job?.running
                        ? "尚无落库结果，完成单个账户后会出现在这里。"
                        : "暂无结果。可从上方站点行发起注册，或打开注册历史查看。"}
                    </div>
                  ) : (
                    <ul className="divide-y divide-slate-100">
                      {visible.slice(0, PREVIEW_LIMIT).map((item) => {
                        const ok = item.status === "success" || item.success;
                        return (
                          <li key={item.id}>
                            <button
                              type="button"
                              onClick={() => setResultDetail(item)}
                              className="flex w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-slate-50 sm:px-5"
                            >
                              <span
                                className={cn(
                                  "mt-0.5 inline-flex min-w-14 shrink-0 items-center justify-center rounded-full px-2 py-0.5 text-[11px] font-medium",
                                  ok ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"
                                )}
                              >
                                {ok ? "成功" : item.status || "失败"}
                              </span>
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-sm font-medium text-slate-900">
                                  {item.email || `记录 #${item.id}`}
                                </span>
                                <span className="mt-0.5 block truncate text-xs text-slate-500">
                                  {[item.provider, item.failure_type || item.failure_reason, item.finished_at]
                                    .filter(Boolean)
                                    .join(" · ") || "点击查看详情"}
                                </span>
                              </span>
                              {item.screenshot_url ? (
                                <ImageIcon className="mt-1 h-4 w-4 shrink-0 text-slate-400" />
                              ) : null}
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  {visible.length > PREVIEW_LIMIT ||
                  (runResultsTotal || 0) > visible.length ||
                  runResults.length > PREVIEW_LIMIT ? (
                    <div className="border-t border-slate-100 px-4 py-3 text-center text-xs text-slate-500 sm:px-5">
                      本页仅展示最近 {PREVIEW_LIMIT} 条预览，完整批次请
                      <Link
                        to={accountsQuery(resultTab === "all" ? undefined : resultTab)}
                        className="mx-1 text-sky-600 hover:underline"
                      >
                        打开注册历史
                      </Link>
                    </div>
                  ) : null}
                </CardContent>
              </>
            ) : null}
          </Card>
        );
      })()}

      {/* 日志：全宽浅底 */}
      <Card className="min-w-0 overflow-hidden">
        <CardHeader className="space-y-3 border-b border-slate-100">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <TerminalSquare className="h-4 w-4 text-slate-600" />
                实时日志
              </CardTitle>
              <CardDescription>
                {job?.last_error ? `最近错误：${job.last_error}` : "按时间顺序显示浏览器和注册流程日志。"}
              </CardDescription>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => void copyVisibleLogs()}>
                <Copy className="h-3.5 w-3.5" />
                复制
              </Button>
              <Button size="sm" variant="outline" onClick={clearLogView}>
                <RotateCcw className="h-3.5 w-3.5" />
                清空视图
              </Button>
            </div>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <Input
                value={logQuery}
                onChange={(event) => setLogQuery(event.target.value)}
                placeholder="搜索日志内容或时间…"
                className="h-9 pl-9"
              />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {levelFilters.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setLogLevel(item.id)}
                  className={cn(
                    "rounded-full px-2.5 py-1 text-xs font-medium transition",
                    logLevel === item.id ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <label className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600">
              <Switch
                checked={autoScroll}
                onCheckedChange={(checked) => {
                  setAutoScroll(checked);
                  if (checked) {
                    userPinnedRef.current = false;
                    requestAnimationFrame(jumpToBottom);
                  }
                }}
                label="自动滚动"
              />
              <span>自动滚动</span>
            </label>
          </div>
        </CardHeader>

        <CardContent className="relative p-3 sm:p-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500">
            <span className="flex items-center gap-2">
              <span className={cn("h-2 w-2 rounded-full", job?.running ? "animate-pulse bg-amber-500" : "bg-emerald-500")} />
              {job?.running ? "日志持续同步中" : "等待新任务"}
            </span>
            <span className="tabular-nums">
              完成 {progressCompleted} · 显示 {renderedLogs.length} / {filteredLogs.length} · 缓冲 {logs.length}
            </span>
          </div>

          <div className="sr-only" aria-live="polite" aria-atomic="true">
            {renderedLogs.length ? `最新日志：${renderedLogs[renderedLogs.length - 1].message}` : ""}
          </div>

          <div
            ref={logRef}
            onScroll={onLogScroll}
            role="log"
            aria-label="实时注册日志"
            aria-live="off"
            className="font-mono-log h-[50dvh] min-h-[360px] max-h-[640px] overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs leading-6 sm:h-[540px] sm:p-4"
          >
            {filteredLogs.length === 0 ? (
              <div className="flex h-full min-h-40 flex-col items-center justify-center gap-2 text-center text-slate-500">
                <div>
                  {logs.length === 0
                    ? job?.running
                      ? logViewCleared
                        ? "视图已清空，正在等待下一条实时日志…"
                        : "任务运行中，正在等待实时日志…"
                      : "等待日志…启动任务后会在这里实时输出。"
                    : "没有符合筛选条件的日志。"}
                </div>
                {!job?.running && logs.length === 0 ? <span className="text-sm">可从上方站点行发起注册。</span> : null}
              </div>
            ) : (
              <>
                {hiddenFilteredLogCount > 0 ? (
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 font-sans text-xs text-slate-500">
                    <span>为保持流畅，前面 {hiddenFilteredLogCount} 行暂未生成页面节点。</span>
                    <button
                      type="button"
                      onClick={revealOlderLogs}
                      className="font-medium text-sky-600 hover:text-sky-700"
                    >
                      再显示 {Math.min(LOG_RENDER_STEP, hiddenFilteredLogCount)} 行
                    </button>
                  </div>
                ) : null}
                {renderedLogs.map((item) => (
                  <LogLine key={item.id} item={item} />
                ))}
              </>
            )}
          </div>

          {showJumpBottom ? (
            <button
              type="button"
              onClick={jumpToBottom}
              className="absolute bottom-8 right-8 inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-md hover:bg-slate-50"
            >
              <ArrowDownToLine className="h-3.5 w-3.5" />
              回到底部
            </button>
          ) : null}
        </CardContent>
      </Card>

      {/* 更多操作：右侧抽屉 */}
      {opsOpen ? (
        <div className="fixed inset-0 z-50 flex justify-end">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/35"
            aria-label="关闭更多操作"
            onClick={() => setOpsOpen(false)}
          />
          <aside className="relative flex h-full w-full max-w-md flex-col bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3 sm:px-5">
              <div>
                <div className="text-base font-semibold text-slate-950">任务操作</div>
                <div className="mt-0.5 text-xs text-slate-500">连通检查、终止浏览器与日志偏好</div>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setOpsOpen(false)} aria-label="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 space-y-5 overflow-auto p-4 sm:p-5">
              <div className="space-y-2">
                <div className="text-sm font-medium text-slate-900">控制</div>
                <Button className="w-full" variant="destructive" onClick={onStop} disabled={!!busyAction || !job?.running}>
                  {busyAction === "stop" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                  停止注册任务
                </Button>
                <Button className="w-full" variant="outline" onClick={onCheck} disabled={!!busyAction}>
                  {busyAction === "check" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wifi className="h-4 w-4" />}
                  连通性检查
                </Button>
                <Button className="w-full" variant="outline" onClick={onKillBrowsers} disabled={!!busyAction}>
                  {busyAction === "kill" ? <Loader2 className="h-4 w-4 animate-spin" /> : <CircleOff className="h-4 w-4" />}
                  终止全部浏览器
                </Button>
              </div>

              <div className="rounded-xl border border-slate-200 bg-slate-50/80 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-medium text-slate-900">日志自动滚动</div>
                    <div className="mt-0.5 text-xs text-slate-500">上滑阅读时会暂停贴底，可点「回到底部」</div>
                  </div>
                  <Switch checked={autoScroll} onCheckedChange={setAutoScroll} label="日志自动滚动" />
                </div>
              </div>

              <div className="space-y-2 rounded-xl border border-slate-200 p-3 text-xs text-slate-600">
                <div className="text-sm font-medium text-slate-900">当前任务摘要</div>
                <div className="flex justify-between gap-2">
                  <span>状态</span>
                  <span className="font-medium text-slate-900">{job?.running ? "运行中" : "空闲"}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>进度</span>
                  <span className="tabular-nums font-medium text-slate-900">
                    {progressCompleted}/{progressTarget}（{Math.round(progressPercent)}%）
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>成功 / 失败</span>
                  <span className="tabular-nums font-medium text-slate-900">
                    {successCount} / {failureCount}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>已用时</span>
                  <span className="tabular-nums font-medium text-slate-900">
                    <ElapsedTime
                      startedAt={job?.started_at}
                      finishedAt={job?.finished_at}
                      running={!!job?.running}
                    />
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>当前邮箱</span>
                  <span className="max-w-[58%] truncate font-medium text-slate-900">{job?.current_email || "—"}</span>
                </div>
              </div>

              <Link
                to="/registration-attempts"
                className={cn(buttonVariants({ variant: "outline" }), "inline-flex w-full justify-center")}
                onClick={() => setOpsOpen(false)}
              >
                查看注册历史
              </Link>

              {checks.length > 0 ? (
                <Button
                  variant="outline"
                  className="w-full"
                  onClick={() => {
                    setChecksOpen(true);
                    setOpsOpen(false);
                  }}
                >
                  查看最近连通性结果
                </Button>
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}

      {/* 连通性结果弹框 */}
      {checksOpen ? (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/35 p-3 sm:items-center sm:p-6"
          onClick={(event) => {
            if (event.target === event.currentTarget) setChecksOpen(false);
          }}
        >
          <div className="max-h-[85dvh] w-full max-w-lg overflow-auto rounded-2xl border border-slate-200 bg-white shadow-2xl">
            <div className="sticky top-0 flex items-center justify-between border-b border-slate-100 bg-white px-4 py-3 sm:px-5">
              <div>
                <div className="text-base font-semibold text-slate-950">连通性检查结果</div>
                <div className="mt-0.5 text-xs text-slate-500">共 {checks.length} 项</div>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setChecksOpen(false)} aria-label="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="grid gap-3 p-4 sm:p-5">
              {checks.length === 0 ? (
                <div className="py-8 text-center text-sm text-slate-500">暂无检查结果</div>
              ) : (
                checks.map((item) => (
                  <div
                    key={item.name}
                    className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50/60 p-3 text-sm leading-6"
                  >
                    {item.ok ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                    ) : (
                      <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                    )}
                    <span>
                      <strong className="font-medium text-slate-800">{item.name}</strong>
                      <span className="text-slate-600">：{item.detail}</span>
                    </span>
                  </div>
                ))
              )}
            </div>
            <div className="border-t border-slate-100 px-4 py-3 sm:px-5">
              <Button className="w-full" variant="secondary" onClick={() => setChecksOpen(false)}>
                关闭
              </Button>
            </div>
          </div>
        </div>
      ) : null}


      {resultDetail ? (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/35 p-3 sm:items-center sm:p-6"
          onClick={(event) => {
            if (event.target === event.currentTarget) setResultDetail(null);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="max-h-[85dvh] w-full max-w-lg overflow-auto rounded-2xl border border-slate-200 bg-white shadow-2xl"
          >
            <div className="sticky top-0 flex items-start justify-between gap-3 border-b border-slate-100 bg-white px-4 py-3 sm:px-5">
              <div className="min-w-0">
                <div className="text-base font-semibold text-slate-950">结果详情</div>
                <div className="mt-0.5 truncate text-xs text-slate-500">{resultDetail.email || `记录 #${resultDetail.id}`}</div>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setResultDetail(null)} aria-label="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="space-y-3 p-4 text-sm sm:p-5">
              <div className="flex flex-wrap gap-2">
                <Badge variant={resultDetail.success || resultDetail.status === "success" ? "success" : "destructive"}>
                  {resultDetail.status || (resultDetail.success ? "success" : "failure")}
                </Badge>
                {resultDetail.provider ? <Badge variant="secondary">{resultDetail.provider}</Badge> : null}
                {resultDetail.profile_id ? <Badge variant="secondary">站点 #{resultDetail.profile_id}</Badge> : null}
              </div>
              <dl className="grid gap-2 rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs text-slate-600">
                {(
                  [
                    ["邮箱", resultDetail.email || "—"],
                    ["完成时间", resultDetail.finished_at || "—"],
                    ["耗时", resultDetail.duration_seconds ? `${Math.round(resultDetail.duration_seconds)}s` : "—"],
                    ["失败类型", resultDetail.failure_type || "—"],
                    ["失败原因", resultDetail.failure_reason || "—"],
                    ["批次", resultDetail.batch_id || job?.batch_id || "—"],
                    ["邮箱消费", resultDetail.mail_consumed ? "已消费（保持 active）" : "—"],
                    ["Worker", resultDetail.worker_id ? String(resultDetail.worker_id) : "—"],
                  ] as const
                ).map(([label, value]) => (
                  <div key={label} className="grid grid-cols-[88px_minmax(0,1fr)] gap-2">
                    <dt className="text-slate-500">{label}</dt>
                    <dd className="break-all font-medium text-slate-900">{value}</dd>
                  </div>
                ))}
              </dl>
              {resultDetail.screenshot_url ? (
                <div className="space-y-2">
                  <div className="text-xs font-medium text-slate-700">失败截图</div>
                  <a href={resultDetail.screenshot_url} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-xl border border-slate-200">
                    <img src={resultDetail.screenshot_url} alt="失败截图" className="max-h-64 w-full object-contain bg-slate-100" />
                  </a>
                </div>
              ) : null}
              {resultDetail.has_exception_traceback && resultDetail.exception_traceback ? (
                <div className="space-y-2">
                  <div className="text-xs font-medium text-slate-700">异常堆栈</div>
                  <pre className="max-h-48 overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-3 text-[11px] leading-5 text-slate-700 whitespace-pre-wrap break-all">
                    {resultDetail.exception_traceback}
                  </pre>
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2 pt-1">
                <Link
                  to={(() => {
                    const sp = new URLSearchParams();
                    if (resultDetail.status) sp.set("status", resultDetail.status);
                    const batch = resultDetail.batch_id || job?.batch_id;
                    if (batch) sp.set("batch_id", String(batch));
                    if (resultDetail.email) sp.set("q", resultDetail.email);
                    const qs = sp.toString();
                    return qs ? `/registration-attempts?${qs}` : "/registration-attempts";
                  })()}
                  className={cn(buttonVariants({ variant: "secondary" }), "inline-flex flex-1 justify-center sm:flex-none")}
                  onClick={() => setResultDetail(null)}
                >
                  在注册历史中打开
                </Link>
                <Button variant="outline" className="flex-1 sm:flex-none" onClick={() => setResultDetail(null)}>
                  关闭
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
