import { useEffect, useMemo, useState } from "react";
import { CalendarCheck, Download, KeyRound, Plus, RefreshCw, Search, Users, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { AccountDrawer } from "@/pages/account-pool/AccountDrawer";
import { api, errorMessage, type AccountPoolItem, type Sub2apiProfile } from "@/lib/api";
import {
  Badge,
  Button,
  EmptyState,
  Input,
  Label,
  PageHeader,
  Select,
  StatCard,
  Switch,
  Toast,
} from "@/components/ui";

type AddForm = { profileId: string; email: string; password: string };

function formatTime(value: string) {
  if (!value) return "尚无记录";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T") + "Z");
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function accountStatus(status: string) {
  if (status === "active") return { label: "正常", variant: "success" as const };
  if (status === "authentication_failure") return { label: "认证失败", variant: "destructive" as const };
  return { label: status || "未知", variant: "warning" as const };
}

export function AccountPoolPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<AccountPoolItem[]>([]);
  const [profiles, setProfiles] = useState<Sub2apiProfile[]>([]);
  const [query, setQuery] = useState("");
  const [profileId, setProfileId] = useState(() => searchParams.get("profile") || "");
  const [status, setStatus] = useState("");
  const [relay, setRelay] = useState("");
  const [addForm, setAddForm] = useState<AddForm | null>(null);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const selectedAccountId = Number(searchParams.get("account") || 0);

  const load = async () => {
    try {
      const [accounts, profileResult] = await Promise.all([api.accountPool(), api.sub2apiProfiles()]);
      setItems(accounts.accounts);
      setProfiles(profileResult.profiles);
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const stats = useMemo(() => ({
    total: items.length,
    active: items.filter((item) => item.status === "active").length,
    error: items.filter((item) => item.status !== "active").length,
    relayReady: items.filter((item) => item.status === "active" && item.relay_enabled && item.relay_key_status === "active").length,
  }), [items]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((item) => {
      if (needle && !`${item.email} ${item.profile_name}`.toLowerCase().includes(needle)) return false;
      if (profileId && item.profile_id !== Number(profileId)) return false;
      if (status && item.status !== status) return false;
      const relayReady = !!item.relay_enabled && item.relay_key_status === "active";
      if (relay === "enabled" && !relayReady) return false;
      if (relay === "disabled" && relayReady) return false;
      return true;
    });
  }, [items, profileId, query, relay, status]);

  const selectedIds = useMemo(
    () => Object.entries(selected).filter(([, value]) => value).map(([id]) => Number(id)),
    [selected],
  );
  const checkinIds = useMemo(() => {
    const selectedSet = new Set(selectedIds);
    return items.filter((item) => selectedSet.has(item.id) && item.site_key === "bmapi" && item.status === "active").map((item) => item.id);
  }, [items, selectedIds]);
  const allVisibleSelected = visible.length > 0 && visible.every((item) => selected[item.id]);

  const openAccount = (accountId: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("account", String(accountId));
    setSearchParams(next);
  };

  const closeAccount = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("account");
    setSearchParams(next, { replace: true });
  };

  const addAccount = async () => {
    if (!addForm || busy) return;
    setBusy("add");
    try {
      const result = await api.addAccount(Number(addForm.profileId), addForm.email.trim(), addForm.password);
      setAddForm(null);
      await load();
      openAccount(result.account.id);
      setToast(`账户已验证并添加，同步 ${result.synced}/${result.discovered} 个密钥${result.unavailable ? `，${result.unavailable} 项暂不可用` : ""}`);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const toggleRelay = async (item: AccountPoolItem, enabled: boolean) => {
    setBusy(`relay-${item.id}`);
    try {
      await api.setAccountRelayEnabled(item.id, enabled);
      await load();
      setToast(enabled ? "账户已加入 API 聚合" : "账户已退出 API 聚合");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const batchCheckin = async () => {
    if (!checkinIds.length || busy) return;
    setBusy("batch-checkin");
    try {
      const result = await api.checkinPoolAccounts(checkinIds);
      await load();
      setSelected({});
      setToast(`批量签到完成：成功 ${result.success}，失败 ${result.failure}`);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const exportCredentials = async () => {
    if (!selectedIds.length || busy) return;
    setBusy("export-credentials");
    try {
      const result = await api.downloadAccountPoolCredentialsTxt(selectedIds);
      setToast(`已导出 ${result.exported} 个账户${result.skipped ? `，跳过 ${result.skipped} 个` : ""}`);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="账户池"
        description="统一管理站点账户、登录状态、签到能力、密钥和 API 聚合参与状态。"
        actions={(
          <>
            <Button variant="outline" onClick={() => void load()}><RefreshCw className="h-4 w-4" />刷新</Button>
            <Button onClick={() => setAddForm({ profileId: "", email: "", password: "" })}><Plus className="h-4 w-4" />添加账户</Button>
          </>
        )}
      />

      <div className="grid grid-cols-4 gap-3">
        <StatCard title="全部账户" value={stats.total} icon={<Users className="h-4 w-4" />} />
        <StatCard title="正常" value={stats.active} accent="success" />
        <StatCard title="异常" value={stats.error} accent={stats.error ? "destructive" : "secondary"} />
        <StatCard title="API 聚合可用" value={stats.relayReady} accent="primary" icon={<KeyRound className="h-4 w-4" />} />
      </div>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <div className="flex items-center gap-3 border-b border-slate-200 p-4">
          <div className="relative min-w-[260px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索邮箱或站点" aria-label="搜索账户" />
          </div>
          <Select className="w-48" value={profileId} onChange={(event) => setProfileId(event.target.value)} aria-label="按站点筛选">
            <option value="">全部站点</option>
            {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
          </Select>
          <Select className="w-40" value={status} onChange={(event) => setStatus(event.target.value)} aria-label="按状态筛选">
            <option value="">全部状态</option>
            <option value="active">正常</option>
            <option value="authentication_failure">认证失败</option>
          </Select>
          <Select className="w-40" value={relay} onChange={(event) => setRelay(event.target.value)} aria-label="按 API 聚合状态筛选">
            <option value="">全部 API 聚合</option>
            <option value="enabled">已启用</option>
            <option value="disabled">未启用</option>
          </Select>
        </div>

        {selectedIds.length ? (
          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-2.5 text-sm">
            <div className="text-slate-600">已选择 {selectedIds.length} 个账户，其中 {checkinIds.length} 个支持签到</div>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setSelected({})}>取消选择</Button>
              <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void exportCredentials()}>
                <Download className="h-4 w-4" />导出凭据
              </Button>
              <Button size="sm" disabled={!checkinIds.length || !!busy} onClick={() => void batchCheckin()}>
                <CalendarCheck className="h-4 w-4" />签到 BMAPI（{checkinIds.length}）
              </Button>
            </div>
          </div>
        ) : null}

        {!visible.length ? (
          <div className="p-5"><EmptyState title="没有匹配的账户" description="注册成功或手工验证的账户会进入账户池。" /></div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1050px] table-fixed text-left text-sm">
              <thead className="bg-slate-50 text-xs text-slate-500">
                <tr>
                  <th className="w-[4%] px-4 py-3 text-center font-medium">
                    <input
                      type="checkbox"
                      checked={allVisibleSelected}
                      aria-label="选择当前筛选结果"
                      onChange={(event) => setSelected((current) => {
                        const next = { ...current };
                        visible.forEach((item) => { next[item.id] = event.target.checked; });
                        return next;
                      })}
                    />
                  </th>
                  <th className="w-[22%] px-4 py-3 font-medium">账户</th>
                  <th className="w-[15%] px-4 py-3 font-medium">站点</th>
                  <th className="w-[11%] px-4 py-3 font-medium">状态</th>
                  <th className="w-[10%] px-4 py-3 text-right font-medium">密钥</th>
                  <th className="w-[15%] px-4 py-3 font-medium">聚合密钥</th>
                  <th className="w-[12%] px-4 py-3 font-medium">签到</th>
                  <th className="w-[7%] px-4 py-3 text-center font-medium">API 聚合</th>
                  <th className="w-[5%] px-4 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200">
                {visible.map((item) => {
                  const state = accountStatus(item.status);
                  const relayReady = item.relay_key_status === "active";
                  return (
                    <tr key={item.id} className="hover:bg-slate-50/80">
                      <td className="px-4 py-3 text-center"><input type="checkbox" checked={!!selected[item.id]} aria-label={`选择 ${item.email}`} onChange={(event) => setSelected((current) => ({ ...current, [item.id]: event.target.checked }))} /></td>
                      <td className="px-4 py-3">
                        <button className="max-w-full truncate text-left font-medium text-sky-700 hover:underline" onClick={() => openAccount(item.id)}>{item.email}</button>
                        <div className="mt-1 text-xs text-slate-500">最近登录：{formatTime(item.last_login_at)}</div>
                      </td>
                      <td className="truncate px-4 py-3">{item.profile_name}</td>
                      <td className="px-4 py-3"><Badge variant={state.variant}>{state.label}</Badge></td>
                      <td className="px-4 py-3 text-right tabular-nums"><span className="font-medium">{item.active_key_count}</span><span className="text-slate-400"> / {item.key_count}</span></td>
                      <td className="truncate px-4 py-3">{item.relay_key_name || <span className="text-slate-400">未选择</span>}</td>
                      <td className="px-4 py-3">{item.site_key === "bmapi" ? (item.last_checkin_at ? `已签 · ${formatTime(item.last_checkin_at)}` : "待签到") : <span className="text-slate-400">不支持</span>}</td>
                      <td className="px-4 py-3 text-center"><Switch checked={!!item.relay_enabled && relayReady} disabled={!relayReady || !!busy} label={`${item.email} API 聚合`} onCheckedChange={(enabled) => void toggleRelay(item, enabled)} /></td>
                      <td className="px-4 py-3 text-right"><Button size="sm" variant="ghost" onClick={() => openAccount(item.id)}>管理</Button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-500">显示 {visible.length} / {items.length} 个账户</div>
      </section>

      {addForm ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/30 p-6" onMouseDown={(event) => event.target === event.currentTarget && setAddForm(null)}>
          <section role="dialog" aria-modal="true" aria-labelledby="add-account-title" className="w-full max-w-lg rounded-lg border border-slate-200 bg-white shadow-2xl">
            <header className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
              <div><h2 id="add-account-title" className="text-base font-semibold">添加账户</h2><p className="mt-1 text-xs text-slate-500">登录验证成功后创建账户并同步密钥。</p></div>
              <Button size="icon" variant="ghost" aria-label="关闭" onClick={() => setAddForm(null)}><X className="h-4 w-4" /></Button>
            </header>
            <div className="space-y-4 p-5">
              <div><Label htmlFor="account-profile">站点</Label><Select id="account-profile" className="mt-2" value={addForm.profileId} onChange={(event) => setAddForm({ ...addForm, profileId: event.target.value })}><option value="">选择站点</option>{profiles.filter((profile) => profile.enabled).map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</Select></div>
              <div><Label htmlFor="account-email">邮箱</Label><Input id="account-email" className="mt-2" autoComplete="username" value={addForm.email} onChange={(event) => setAddForm({ ...addForm, email: event.target.value })} /></div>
              <div><Label htmlFor="account-password">密码</Label><Input id="account-password" className="mt-2" type="password" autoComplete="current-password" value={addForm.password} onChange={(event) => setAddForm({ ...addForm, password: event.target.value })} /></div>
            </div>
            <footer className="flex justify-end gap-2 border-t border-slate-200 px-5 py-4"><Button variant="outline" onClick={() => setAddForm(null)}>取消</Button><Button disabled={!!busy || !addForm.profileId || !addForm.email.trim() || addForm.password.length < 8} onClick={() => void addAccount()}>验证并添加</Button></footer>
          </section>
        </div>
      ) : null}

      {selectedAccountId > 0 ? <AccountDrawer accountId={selectedAccountId} onClose={closeAccount} onChanged={load} onToast={setToast} /> : null}
      {toast ? <Toast message={toast} /> : null}
    </div>
  );
}
