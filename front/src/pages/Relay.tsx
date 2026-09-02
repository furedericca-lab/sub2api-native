import { useEffect, useMemo, useState } from "react";
import { Activity, Copy, RefreshCw, RotateCw, Save, Search } from "lucide-react";
import { Link } from "react-router-dom";

import { api, errorMessage, type RelayOverview, type RelayPoolItem } from "@/lib/api";
import { Badge, Button, EmptyState, Input, Label, PageHeader, Select, StatCard, Switch, Toast } from "@/components/ui";

type RequestRecord = {
  id: number;
  created_at: number;
  model: string;
  account_id: number;
  site_key: string;
  stream: number;
  http_status: number;
  outcome: string;
  duration_ms: number;
  retries: number;
};

function runtimeStatus(value: string, cooling: boolean) {
  if (cooling) return { label: "冷却中", variant: "warning" as const };
  if (value === "success") return { label: "就绪", variant: "success" as const };
  if (value === "upstream_error") return { label: "上游错误", variant: "destructive" as const };
  if (value === "transport_error") return { label: "连接错误", variant: "destructive" as const };
  if (value === "stream_interrupted") return { label: "流中断", variant: "warning" as const };
  return { label: value || "待探测", variant: "secondary" as const };
}

function outcomeLabel(value: string) {
  return ({ success: "成功", upstream_error: "上游错误", transport_error: "连接错误", stream_interrupted: "流中断" } as Record<string, string>)[value] || value;
}

export function RelayPage() {
  const [items, setItems] = useState<RelayPoolItem[]>([]);
  const [requests, setRequests] = useState<RequestRecord[]>([]);
  const [overview, setOverview] = useState<RelayOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [credential, setCredential] = useState("");
  const [toast, setToast] = useState("");
  const [config, setConfig] = useState<Record<string, any>>({});
  const [requestQuery, setRequestQuery] = useState("");
  const [requestAccount, setRequestAccount] = useState("");
  const [requestOutcome, setRequestOutcome] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const [overviewResult, runtimeResult, requestResult, configResult] = await Promise.all([
        api.relayOverview(),
        api.relayPool(),
        api.relayRequests(),
        api.getConfig(),
      ]);
      setOverview(overviewResult);
      setItems(runtimeResult.items);
      setRequests(requestResult.items as RequestRecord[]);
      setConfig(configResult.config);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const visibleRequests = useMemo(() => {
    const needle = requestQuery.trim().toLowerCase();
    return requests.filter((item) => {
      if (needle && !`${item.model} ${item.site_key}`.toLowerCase().includes(needle)) return false;
      if (requestAccount && item.account_id !== Number(requestAccount)) return false;
      if (requestOutcome && item.outcome !== requestOutcome) return false;
      return true;
    });
  }, [requestAccount, requestOutcome, requestQuery, requests]);

  const save = async () => {
    try {
      await api.saveConfig({
        relay_enabled: !!config.relay_enabled,
        relay_strategy: config.relay_strategy || "fill_first",
        relay_proxy: config.relay_proxy || "",
      });
      setToast("聚合设置已保存");
      await load();
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  const rotate = async () => {
    try {
      const result = await api.relayRotate();
      setCredential(result.relay_api_key);
      setToast("聚合密钥已轮换，仅显示这一次");
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  return (
    <div className="space-y-5">
      <PageHeader title="API 聚合" description="对外提供 Responses API，管理调度策略、出口代理、运行状态和使用记录。" actions={<Button variant="outline" onClick={() => void load()} disabled={loading}><RefreshCw className="h-4 w-4" />刷新</Button>} />

      <div className="grid grid-cols-4 gap-3">
        <StatCard title="服务状态" value={overview?.enabled ? "运行中" : "未启用"} accent={overview?.enabled ? "success" : "secondary"} />
        <StatCard title="可调度账户" value={overview?.pool_count ?? 0} />
        <StatCard title="冷却账户" value={overview?.cooling_down ?? 0} accent={(overview?.cooling_down || 0) > 0 ? "warning" : "secondary"} />
        <StatCard title="进行中" value={overview?.in_flight ?? 0} accent="primary" icon={<Activity className="h-4 w-4" />} />
      </div>

      <section className="rounded-lg border border-slate-200 bg-white">
        <header className="border-b border-slate-200 px-5 py-4"><h2 className="text-base font-semibold">调度与接入</h2><p className="mt-1 text-xs text-slate-500">策略只决定新 Session 的首次分配；已有 Session 优先保持账户粘性。</p></header>
        <div className="grid grid-cols-[180px_240px_minmax(280px,1fr)_auto] items-end gap-4 p-5">
          <div className="pb-1"><Label className="mb-3 block">Responses API 聚合</Label><div className="flex items-center gap-3 text-sm"><Switch checked={!!config.relay_enabled} onCheckedChange={(enabled) => setConfig({ ...config, relay_enabled: enabled })} label="启用 API 聚合" /><span>{config.relay_enabled ? "已启用" : "未启用"}</span></div></div>
          <div><Label htmlFor="relay-strategy">新 Session 分配策略</Label><Select id="relay-strategy" className="mt-2" value={config.relay_strategy || "fill_first"} onChange={(event) => setConfig({ ...config, relay_strategy: event.target.value })}><option value="fill_first">填充优先</option><option value="round_robin">轮询优先</option></Select></div>
          <div><Label htmlFor="relay-proxy">出口代理</Label><Input id="relay-proxy" className="mt-2" value={config.relay_proxy || ""} onChange={(event) => setConfig({ ...config, relay_proxy: event.target.value })} placeholder="留空沿用全局代理" /></div>
          <Button onClick={() => void save()}><Save className="h-4 w-4" />保存</Button>
        </div>
        <div className="grid grid-cols-[minmax(320px,1fr)_minmax(320px,1fr)_auto] items-end gap-4 border-t border-slate-200 px-5 py-4">
          <div><Label>接入地址</Label><code className="mt-2 block rounded-lg bg-slate-50 px-3 py-2 text-sm">{window.location.origin}/v1</code></div>
          <div><Label>聚合密钥</Label>{credential ? <div className="mt-2 flex gap-2"><Input readOnly className="font-mono" value={credential} /><Button size="icon" variant="outline" aria-label="复制聚合密钥" onClick={() => void navigator.clipboard.writeText(credential).then(() => setToast("聚合密钥已复制"))}><Copy className="h-4 w-4" /></Button></div> : <p className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">凭据只在生成或轮换时显示一次</p>}</div>
          <Button variant="outline" onClick={() => void rotate()}><RotateCw className="h-4 w-4" />生成/轮换</Button>
        </div>
      </section>

      <RuntimeTable items={items} reload={load} notify={setToast} />

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <header className="flex items-end justify-between gap-4 border-b border-slate-200 px-5 py-4"><div><h2 className="text-base font-semibold">最近使用记录</h2><p className="mt-1 text-xs text-slate-500">一个入站 Responses 请求对应一条最终记录。</p></div><div className="grid grid-cols-[240px_220px_160px] gap-3"><div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><Input className="pl-9" value={requestQuery} onChange={(event) => setRequestQuery(event.target.value)} placeholder="搜索模型或站点" aria-label="搜索使用记录" /></div><Select value={requestAccount} onChange={(event) => setRequestAccount(event.target.value)} aria-label="按账户筛选"><option value="">全部账户</option>{items.map((item) => <option key={item.account_id} value={item.account_id}>{item.email || `账户 #${item.account_id}`}</option>)}</Select><Select value={requestOutcome} onChange={(event) => setRequestOutcome(event.target.value)} aria-label="按结果筛选"><option value="">全部结果</option><option value="success">成功</option><option value="upstream_error">上游错误</option><option value="transport_error">连接错误</option><option value="stream_interrupted">流中断</option></Select></div></header>
        <RequestTable items={visibleRequests} />
        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-500">显示 {visibleRequests.length} / {requests.length} 条记录</div>
      </section>

      {toast ? <Toast message={toast} /> : null}
    </div>
  );
}

function RuntimeTable({ items, reload, notify }: { items: RelayPoolItem[]; reload: () => Promise<void>; notify: (message: string) => void }) {
  const refreshModels = async () => {
    try {
      await api.relayRefreshModels();
      notify("模型缓存已刷新");
      await reload();
    } catch (error) {
      notify(errorMessage(error));
    }
  };
  const probe = async (accountId: number) => {
    try {
      await api.relayProbe(accountId);
      await reload();
      notify("账户探测完成");
    } catch (error) {
      notify(errorMessage(error));
    }
  };
  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-5 py-4"><div><h2 className="text-base font-semibold">运行状态</h2><p className="mt-1 text-xs text-slate-500">只展示派生调度状态；账户和密钥在资源管理页维护。</p></div><Button size="sm" variant="outline" onClick={() => void refreshModels()}><RefreshCw className="h-4 w-4" />刷新模型</Button></header>
      {!items.length ? <div className="p-5"><EmptyState title="暂无可调度账户" description="请在账户池选择有效聚合密钥并启用 API 聚合。" /></div> : <div className="overflow-x-auto"><table className="w-full min-w-[900px] table-fixed text-left text-sm"><thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="w-[25%] px-4 py-3 font-medium">账户</th><th className="w-[18%] px-4 py-3 font-medium">站点</th><th className="w-[18%] px-4 py-3 font-medium">密钥</th><th className="w-[10%] px-4 py-3 text-right font-medium">模型</th><th className="w-[13%] px-4 py-3 font-medium">状态</th><th className="w-[8%] px-4 py-3 text-right font-medium">当前请求</th><th className="w-[8%] px-4 py-3 text-right font-medium">操作</th></tr></thead><tbody className="divide-y divide-slate-200">{items.map((item) => { const state = runtimeStatus(item.last_status, item.cooldown_until > Date.now() / 1000); return <tr key={item.account_id} className="hover:bg-slate-50/80"><td className="truncate px-4 py-3"><Link className="text-sky-700 hover:underline" to={`/account-pool?account=${item.account_id}`}>{item.email || `账户 #${item.account_id}`}</Link></td><td className="truncate px-4 py-3">{item.profile_name}</td><td className="truncate px-4 py-3"><Link className="text-sky-700 hover:underline" to={`/api-keys?account=${item.account_id}`}>{item.key_name || "未命名"}</Link></td><td className="px-4 py-3 text-right tabular-nums">{item.models.length}</td><td className="px-4 py-3"><Badge variant={state.variant}>{state.label}</Badge></td><td className="px-4 py-3 text-right tabular-nums">{item.in_flight || 0}</td><td className="px-4 py-3 text-right"><Button size="sm" variant="outline" onClick={() => void probe(item.account_id)}>探测</Button></td></tr>; })}</tbody></table></div>}
    </section>
  );
}

function RequestTable({ items }: { items: RequestRecord[] }) {
  const formatTime = (value: number) => value ? new Date(value * 1000).toLocaleString() : "-";
  if (!items.length) return <div className="p-5"><EmptyState title="暂无使用记录" description="Responses 请求完成后会在这里显示元数据。" /></div>;
  return <div className="overflow-x-auto"><table className="w-full min-w-[900px] table-fixed text-left text-sm"><thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="w-[20%] px-4 py-3 font-medium">时间</th><th className="w-[23%] px-4 py-3 font-medium">模型</th><th className="w-[17%] px-4 py-3 font-medium">账户</th><th className="w-[13%] px-4 py-3 font-medium">站点</th><th className="w-[15%] px-4 py-3 font-medium">结果</th><th className="w-[8%] px-4 py-3 text-right font-medium">总耗时</th><th className="w-[4%] px-4 py-3 text-right font-medium">重试</th></tr></thead><tbody className="divide-y divide-slate-200">{items.map((row) => <tr key={row.id}><td className="whitespace-nowrap px-4 py-3">{formatTime(row.created_at)}</td><td className="truncate px-4 py-3">{row.model}</td><td className="px-4 py-3"><Link className="text-sky-700 hover:underline" to={`/account-pool?account=${row.account_id}`}>#{row.account_id}</Link></td><td className="px-4 py-3">{row.site_key}</td><td className="px-4 py-3"><Badge variant={row.outcome === "success" ? "success" : "destructive"}>{row.http_status || "网络错误"} · {outcomeLabel(row.outcome)}{row.stream ? " · 流式" : ""}</Badge></td><td className="px-4 py-3 text-right tabular-nums">{row.duration_ms} ms</td><td className="px-4 py-3 text-right tabular-nums">{row.retries || 0}</td></tr>)}</tbody></table></div>;
}
