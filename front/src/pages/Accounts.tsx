import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Bug,
  Camera,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Eye,
  Search,
  X,
} from "lucide-react";

import {
  api,
  errorMessage,
  type RegistrationAttempt,
  type Sub2apiProfile,
} from "@/lib/api";
import { formatDuration } from "@/lib/utils";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  Input,
  PageHeader,
  Select,
  Toast,
} from "@/components/ui";

function statusVariant(status: string) {
  if (status === "success") return "success" as const;
  if (status === "failure") return "destructive" as const;
  if (status === "cancelled") return "warning" as const;
  return "secondary" as const;
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    success: "成功",
    failure: "失败",
    skipped: "跳过",
    cancelled: "已停止",
  };
  return labels[status] || status || "未知";
}

function mailboxSourceLabel(item: RegistrationAttempt) {
  if (item.mailbox_source === "temp") return "临时邮箱";
  if (item.mailbox_source === "accounts") return "账户邮箱";
  return item.provider || "未记录";
}

type RegistrationAttemptFilters = {
  status: string;
  mailStatus: string;
  keyword: string;
  batchId: string;
  profileId: string;
};

function filtersFromSearchParams(searchParams: URLSearchParams): RegistrationAttemptFilters {
  return {
    status: searchParams.get("status") || "",
    mailStatus: "",
    keyword: searchParams.get("q") || "",
    batchId: searchParams.get("batch_id") || "",
    profileId: searchParams.get("profile_id") || "",
  };
}

function RegistrationAttemptDetails({
  detail,
  profileName,
}: {
  detail: RegistrationAttempt;
  profileName?: string;
}) {
  const siteUrl = String(detail.extra?.register_url || "");
  const hasFailure = detail.status === "failure" || !!detail.failure_reason || !!detail.exception_traceback;

  return (
    <div className="space-y-4 text-sm">
      <div className="rounded-lg border border-sky-100 bg-sky-50/70 p-3">
        <div className="min-w-0 break-all font-medium text-foreground">{detail.email || "未记录邮箱"}</div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge variant={statusVariant(detail.status)}>{statusLabel(detail.status)}</Badge>
          {detail.profile_id ? (
            <Badge variant="outline">站点 {profileName ? `#${detail.profile_id} · ${profileName}` : `#${detail.profile_id}`}</Badge>
          ) : null}
          {detail.mail_consumed ? <Badge variant="secondary">邮箱已消费</Badge> : null}
        </div>
        {siteUrl ? (
          <div className="mt-3 truncate rounded-lg border border-slate-200 bg-white/60 px-3 py-2 text-xs text-slate-500" title={siteUrl}>
            注册站点：{siteUrl}
          </div>
        ) : null}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-lg border bg-muted/20 p-3 text-xs leading-5">
        <div>
          <dt className="text-muted-foreground">邮箱来源</dt>
          <dd className="font-medium text-foreground">{mailboxSourceLabel(detail)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">耗时</dt>
          <dd className="font-medium text-foreground">{formatDuration(detail.duration_seconds)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">完成时间</dt>
          <dd className="font-medium text-foreground">{detail.finished_at || detail.started_at || "未记录"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">邮箱状态</dt>
          <dd className="font-medium text-foreground">{detail.mail_consumed ? "已消费（保持 active）" : "未到提交边界"}</dd>
        </div>
        <div className="col-span-2">
          <dt className="text-muted-foreground">批次</dt>
          <dd className="break-all font-mono font-medium text-foreground">{detail.batch_id || "未记录"}</dd>
        </div>
      </dl>

      {hasFailure ? (
        <section className="overflow-hidden rounded-lg border border-red-200 bg-red-50/60">
          <div className="flex items-center justify-between gap-3 border-b border-red-200 px-3 py-2.5">
            <div className="flex min-w-0 items-center gap-2 text-sm font-semibold text-red-800">
              <Bug className="h-4 w-4 shrink-0" aria-hidden="true" />
              <span>失败详情</span>
            </div>
            <span className="shrink-0 text-xs text-red-600">{detail.finished_at || detail.started_at || "时间未记录"}</span>
          </div>
          <div className="space-y-3 p-3">
            <dl className="grid grid-cols-1 gap-2 sm:grid-cols-[7rem_minmax(0,1fr)]">
              <dt className="text-xs font-medium text-red-700">失败类型</dt>
              <dd className="break-words text-sm text-slate-800">{detail.failure_type || detail.exception_type || "未分类"}</dd>
              <dt className="text-xs font-medium text-red-700">失败原因</dt>
              <dd className="whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">{detail.failure_reason || "未记录原因"}</dd>
            </dl>

            {detail.screenshot_url ? (
              <div className="overflow-hidden rounded-lg border border-rose-200 bg-rose-50/50">
                <div className="flex items-center gap-2 border-b border-rose-200 px-3 py-2 text-sm font-medium text-rose-800">
                  <Camera className="h-4 w-4" aria-hidden="true" />
                  浏览器失败现场
                </div>
                <a href={detail.screenshot_url} target="_blank" rel="noreferrer" title="在新窗口查看原图">
                  <img
                    src={detail.screenshot_url}
                    alt={`注册失败截图 ${detail.email || detail.id}`}
                    className="max-h-[28rem] w-full bg-slate-100 object-contain"
                    loading="lazy"
                  />
                </a>
              </div>
            ) : null}

            {detail.exception_traceback ? (
              <details className="group overflow-hidden rounded-lg border border-red-200 bg-slate-50/90">
                <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm font-medium text-red-800 [&::-webkit-details-marker]:hidden">
                  <span className="min-w-0 truncate">异常日志</span>
                  <span className="shrink-0 text-xs font-normal text-red-600 group-open:hidden">展开查看</span>
                  <span className="hidden shrink-0 text-xs font-normal text-red-600 group-open:inline">收起</span>
                </summary>
                <pre className="max-h-[48dvh] overflow-auto whitespace-pre-wrap break-words border-t border-red-200 p-3 font-mono text-[11px] leading-5 text-slate-700 sm:text-xs">
                  {detail.exception_traceback}
                </pre>
              </details>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

export function AccountsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const routeFilterKey = searchParams.toString();
  const [items, setItems] = useState<RegistrationAttempt[]>([]);
  const [filters, setFilters] = useState<RegistrationAttemptFilters>(() => filtersFromSearchParams(searchParams));
  const [profiles, setProfiles] = useState<Sub2apiProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<RegistrationAttempt | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({ message: "" });
  const requestIdRef = useRef(0);
  const totalRef = useRef(0);
  const pageSizeRef = useRef(pageSize);
  const activeFilterKeyRef = useRef("");

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const pageNumbers = useMemo(() => {
    const count = Math.min(totalPages, 5);
    const start = Math.max(1, Math.min(page - 2, totalPages - count + 1));
    return Array.from({ length: count }, (_, index) => start + index);
  }, [page, totalPages]);

  const profileName = (id: number) => profiles.find((item) => item.id === id)?.name || `#${id}`;

  const showToast = useCallback((message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  }, []);

  const load = useCallback(async (
    targetPage: number,
    targetPageSize: number,
    activeFilters: RegistrationAttemptFilters,
  ) => {
    const filterKey = JSON.stringify(activeFilters);
    if (filterKey !== activeFilterKeyRef.current) {
      activeFilterKeyRef.current = filterKey;
      totalRef.current = 0;
      setTotal(0);
    }
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      const data = await api.registrationAttempts({
        status: activeFilters.status,
        mailStatus: activeFilters.mailStatus || undefined,
        q: activeFilters.keyword,
        batchId: activeFilters.batchId || undefined,
        profileId: activeFilters.profileId ? Number(activeFilters.profileId) : undefined,
        limit: targetPageSize,
        offset: (targetPage - 1) * targetPageSize,
      });
      if (requestId !== requestIdRef.current) return;
      const responseTotal = data.total;
      const hasExactTotal = responseTotal !== null && responseTotal !== undefined && Number.isFinite(Number(responseTotal));
      const responseCount = Number(data.count ?? data.items?.length ?? 0);
      const offset = (targetPage - 1) * targetPageSize;
      const nextHasMore = typeof data.has_more === "boolean" ? data.has_more : responseCount >= targetPageSize;
      const nextTotal = hasExactTotal ? Number(responseTotal) : Math.max(totalRef.current, offset + responseCount + (nextHasMore ? 1 : 0));
      const maxPage = Math.max(1, Math.ceil(nextTotal / targetPageSize));
      if (targetPage > maxPage) {
        void load(maxPage, targetPageSize, activeFilters);
        return;
      }
      setItems(data.items || []);
      totalRef.current = nextTotal;
      setTotal(nextTotal);
      setHasMore(nextHasMore);
      setPage(targetPage);
      setPageSize(targetPageSize);
      setDetail((current) => current ? (data.items || []).find((item) => item.id === current.id) || null : null);
    } catch (error) {
      if (requestId === requestIdRef.current) showToast(errorMessage(error, "加载失败"), "error");
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    let alive = true;
    api.sub2apiProfiles()
      .then((data) => {
        if (alive) setProfiles(data.profiles || []);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    pageSizeRef.current = pageSize;
  }, [pageSize]);

  useEffect(() => {
    const routeFilters = filtersFromSearchParams(new URLSearchParams(routeFilterKey));
    setFilters(routeFilters);
    void load(1, pageSizeRef.current, routeFilters);
  }, [load, routeFilterKey]);

  useEffect(() => {
    if (!detail) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetail(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [detail]);

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader title="注册历史" description="按批次审计注册结果、失败原因和浏览器现场。" />

      {filters.batchId ? (
        <div className="rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
          当前按批次筛选：<span className="font-mono text-xs sm:text-sm">{filters.batchId}</span>
          <button
            type="button"
            className="ml-3 text-sky-700 underline-offset-2 hover:underline"
            onClick={() => {
              const next = new URLSearchParams(searchParams);
              next.delete("batch_id");
              setSearchParams(next, { replace: true });
            }}
          >
            清除
          </button>
        </div>
      ) : null}

      <Card className="overflow-hidden">
        <CardContent className="p-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
            <div className="w-full lg:w-36">
              <label htmlFor="attempt-status-filter" className="mb-1.5 block text-xs font-medium text-slate-500">注册状态</label>
              <Select id="attempt-status-filter" value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value }))} aria-label="按注册状态筛选">
                <option value="">全部状态</option>
                <option value="success">成功</option>
                <option value="failure">失败</option>
                <option value="cancelled">已停止</option>
              </Select>
            </div>
            <div className="w-full lg:w-44">
              <label htmlFor="attempt-profile-filter" className="mb-1.5 block text-xs font-medium text-slate-500">站点</label>
              <Select id="attempt-profile-filter" value={filters.profileId} onChange={(event) => setFilters((current) => ({ ...current, profileId: event.target.value }))} aria-label="按站点筛选">
                <option value="">全部站点</option>
                {profiles.map((item) => <option key={item.id} value={String(item.id)}>{item.name}</option>)}
              </Select>
            </div>
            <div className="w-full lg:w-44">
              <label htmlFor="attempt-mail-status-filter" className="mb-1.5 block text-xs font-medium text-slate-500">邮箱消费状态</label>
              <Select id="attempt-mail-status-filter" value={filters.mailStatus} onChange={(event) => setFilters((current) => ({ ...current, mailStatus: event.target.value }))} aria-label="按邮箱消费状态筛选">
                <option value="">全部</option>
                <option value="consumed">已消费</option>
                <option value="not_attempted">未到提交边界</option>
              </Select>
            </div>
            <div className="min-w-0 flex-1">
              <label htmlFor="attempt-search" className="mb-1.5 block text-xs font-medium text-slate-500">搜索记录</label>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input
                  id="attempt-search"
                  className="pl-9"
                  type="search"
                  placeholder="搜索邮箱、来源、失败原因或批次"
                  value={filters.keyword}
                  onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void load(1, pageSize, filters);
                  }}
                  aria-label="搜索注册记录"
                />
              </div>
            </div>
            <Button onClick={() => void load(1, pageSize, filters)} disabled={loading}>
              <Search className="h-4 w-4" aria-hidden="true" />
              查询
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="min-w-0 overflow-hidden">
        <CardHeader>
          <div>
            <CardTitle>注册记录</CardTitle>
            <CardDescription>共 {total} 条，第 {page} / {totalPages} 页。</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {items.length === 0 ? (
            <div className="p-4 sm:p-6">
              <EmptyState title="暂无注册记录" description="从站点池启动注册后，成功或失败结果会显示在这里。" />
            </div>
          ) : (
            <div className="divide-y">
              {items.map((item) => (
                <article key={item.id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="break-all font-medium leading-6 text-foreground">{item.email || "未记录邮箱"}</span>
                      <Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge>
                      {item.profile_id ? <Badge variant="outline">{profileName(item.profile_id)}</Badge> : null}
                      {item.mail_consumed ? <Badge variant="secondary">邮箱已消费</Badge> : null}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs leading-5 text-muted-foreground">
                      <span className="flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" aria-hidden="true" />{item.finished_at || item.started_at || "未记录时间"}</span>
                      <span>{formatDuration(item.duration_seconds)}</span>
                      <span>{mailboxSourceLabel(item)}</span>
                    </div>
                    {item.failure_reason ? <p className="mt-1 line-clamp-2 text-xs leading-5 text-red-700" title={item.failure_reason}>失败：{item.failure_reason}</p> : null}
                  </div>
                  <Button variant="secondary" className="shrink-0" onClick={() => setDetail(item)}>
                    <Eye className="h-4 w-4" aria-hidden="true" />
                    查看详情
                  </Button>
                </article>
              ))}
            </div>
          )}
        </CardContent>
        {items.length > 0 ? (
          <div className="flex flex-col gap-3 border-t px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>每页</span>
              <Select className="h-9 min-h-9 w-20 py-1" value={String(pageSize)} onChange={(event) => void load(1, Number(event.target.value), filters)} aria-label="每页记录数">
                <option value="50">50</option>
                <option value="100">100</option>
                <option value="200">200</option>
                <option value="500">500</option>
              </Select>
              <span>条，共 {total} 条</span>
            </div>
            <div className="flex items-center justify-between gap-2 sm:justify-end">
              <Button size="sm" variant="outline" disabled={loading || page <= 1} onClick={() => void load(page - 1, pageSize, filters)}>
                <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                上一页
              </Button>
              <span className="min-w-16 text-center text-xs font-medium text-muted-foreground sm:hidden">{page} / {totalPages}</span>
              <div className="hidden items-center gap-1 sm:flex" aria-label="页码">
                {pageNumbers.map((pageNumber) => (
                  <Button key={pageNumber} size="sm" variant={pageNumber === page ? "default" : "outline"} className="h-9 min-h-9 w-9 px-0" disabled={loading} onClick={() => void load(pageNumber, pageSize, filters)} aria-current={pageNumber === page ? "page" : undefined}>
                    {pageNumber}
                  </Button>
                ))}
              </div>
              <Button size="sm" variant="outline" disabled={loading || !hasMore} onClick={() => void load(page + 1, pageSize, filters)}>
                下一页
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>
          </div>
        ) : null}
      </Card>

      {detail ? (
        <div className="fixed inset-0 z-[70] flex items-end bg-slate-950/50 sm:items-center sm:justify-center sm:p-6" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setDetail(null);
        }}>
          <section role="dialog" aria-modal="true" aria-labelledby="registration-attempt-detail-title" className="max-h-[92dvh] w-full overflow-hidden rounded-t-lg bg-card shadow-2xl sm:max-w-2xl sm:rounded-lg">
            <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b bg-card px-4 py-3">
              <div className="min-w-0">
                <h2 id="registration-attempt-detail-title" className="font-semibold text-foreground">注册记录详情</h2>
                <p className="truncate text-xs text-muted-foreground">{detail.email || "未记录邮箱"}</p>
              </div>
              <Button size="icon" variant="ghost" onClick={() => setDetail(null)} aria-label="关闭注册记录详情"><X className="h-5 w-5" aria-hidden="true" /></Button>
            </header>
            <div className="max-h-[calc(92dvh-64px)] overflow-y-auto px-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))] pt-4">
              <RegistrationAttemptDetails detail={detail} profileName={detail.profile_id ? profileName(detail.profile_id) : undefined} />
            </div>
          </section>
        </div>
      ) : null}

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
