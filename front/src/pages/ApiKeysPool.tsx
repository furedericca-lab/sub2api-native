import { useEffect, useMemo, useState } from "react";
import { Copy, Eye, KeyRound, RefreshCw, Search, Star, Trash2 } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { AccountDrawer } from "@/pages/account-pool/AccountDrawer";
import {
  api,
  errorMessage,
  type AccountPoolItem,
  type ApiKeyGroup,
  type ApiKeyPoolItem,
  type Sub2apiProfile,
} from "@/lib/api";
import { Badge, Button, EmptyState, Input, PageHeader, Select, StatCard, Toast } from "@/components/ui";

function keyStatus(value: string) {
  return ({ active: "有效", missing: "远端缺失", deleted: "已删除", revoked: "已撤销", invalid: "已失效" } as Record<string, string>)[value] || value || "未知";
}

export function ApiKeysPoolPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<ApiKeyPoolItem[]>([]);
  const [accounts, setAccounts] = useState<AccountPoolItem[]>([]);
  const [profiles, setProfiles] = useState<Sub2apiProfile[]>([]);
  const [query, setQuery] = useState("");
  const [profileId, setProfileId] = useState(() => searchParams.get("profile") || "");
  const [accountId, setAccountId] = useState(() => searchParams.get("account") || "");
  const [groupId, setGroupId] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");
  const [revealed, setRevealed] = useState<Record<number, string>>({});
  const [groupEditor, setGroupEditor] = useState<{ key: ApiKeyPoolItem; groups: ApiKeyGroup[] } | null>(null);
  const drawerAccountId = Number(searchParams.get("manage-account") || 0);

  const load = async () => {
    try {
      const [keyResult, accountResult, profileResult] = await Promise.all([api.apiKeys(), api.accountPool(), api.sub2apiProfiles()]);
      setItems(keyResult.keys);
      setAccounts(accountResult.accounts);
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
    invalid: items.filter((item) => item.status !== "active").length,
    relay: items.filter((item) => !!item.is_relay && item.status === "active").length,
  }), [items]);

  const groups = useMemo(() => Array.from(new Set(items.map((item) => item.group_id).filter(Boolean))).sort((a, b) => a - b), [items]);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const accountProfile = new Map(accounts.map((account) => [account.id, account.profile_id]));
    return items.filter((item) => {
      if (needle && !`${item.name} ${item.email} ${item.profile_name} ${item.remote_key_id}`.toLowerCase().includes(needle)) return false;
      if (profileId && accountProfile.get(item.account_id) !== Number(profileId)) return false;
      if (accountId && item.account_id !== Number(accountId)) return false;
      if (groupId && item.group_id !== Number(groupId)) return false;
      if (status && item.status !== status) return false;
      return true;
    });
  }, [accountId, accounts, groupId, items, profileId, query, status]);

  const openAccount = (id: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("manage-account", String(id));
    setSearchParams(next);
  };

  const closeAccount = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("manage-account");
    setSearchParams(next, { replace: true });
  };

  const reveal = async (item: ApiKeyPoolItem) => {
    try {
      const result = await api.revealSavedApiKey(item.id);
      setRevealed((current) => ({ ...current, [item.id]: result.secret }));
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  const copy = async (value: string) => {
    await navigator.clipboard.writeText(value);
    setToast("密钥已复制");
  };

  const selectRelay = async (item: ApiKeyPoolItem) => {
    setBusy(`relay-${item.id}`);
    try {
      await api.selectRelayKey(item.account_id, item.id);
      await load();
      setToast("聚合密钥已切换");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const editGroup = async (item: ApiKeyPoolItem) => {
    setBusy(`groups-${item.id}`);
    try {
      const result = await api.accountGroups(item.account_id);
      setGroupEditor({ key: item, groups: result.groups });
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const updateGroup = async (group: number) => {
    if (!groupEditor) return;
    setBusy(`group-${groupEditor.key.id}`);
    try {
      await api.updatePoolApiKeyGroup(groupEditor.key.account_id, groupEditor.key.id, group);
      setGroupEditor(null);
      await load();
      setToast("密钥分组已更新");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const remove = async (item: ApiKeyPoolItem) => {
    if (!window.confirm(`确认删除远端密钥「${item.name || item.remote_key_id}」？`)) return;
    setBusy(`delete-${item.id}`);
    try {
      await api.deletePoolApiKey(item.account_id, item.id);
      await load();
      setToast("密钥已删除");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-5">
      <PageHeader title="密钥池" description="全局查看账户密钥、远端分组和当前聚合密钥。" actions={<Button variant="outline" onClick={() => void load()}><RefreshCw className="h-4 w-4" />刷新</Button>} />

      <div className="grid grid-cols-4 gap-3">
        <StatCard title="全部密钥" value={stats.total} icon={<KeyRound className="h-4 w-4" />} />
        <StatCard title="有效" value={stats.active} accent="success" />
        <StatCard title="不可用" value={stats.invalid} accent={stats.invalid ? "destructive" : "secondary"} />
        <StatCard title="聚合使用" value={stats.relay} accent="primary" icon={<Star className="h-4 w-4" />} />
      </div>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <div className="grid grid-cols-[minmax(260px,1fr)_180px_220px_140px_150px] gap-3 border-b border-slate-200 p-4">
          <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索密钥、账户或远端 ID" aria-label="搜索密钥" /></div>
          <Select value={profileId} onChange={(event) => setProfileId(event.target.value)} aria-label="按站点筛选"><option value="">全部站点</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</Select>
          <Select value={accountId} onChange={(event) => setAccountId(event.target.value)} aria-label="按账户筛选"><option value="">全部账户</option>{accounts.filter((account) => !profileId || account.profile_id === Number(profileId)).map((account) => <option key={account.id} value={account.id}>{account.email}</option>)}</Select>
          <Select value={groupId} onChange={(event) => setGroupId(event.target.value)} aria-label="按分组筛选"><option value="">全部分组</option>{groups.map((group) => <option key={group} value={group}>分组 #{group}</option>)}</Select>
          <Select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="按状态筛选"><option value="">全部状态</option><option value="active">有效</option><option value="missing">远端缺失</option><option value="revoked">已撤销</option><option value="invalid">已失效</option><option value="deleted">已删除</option></Select>
        </div>

        {!visible.length ? <div className="p-5"><EmptyState title="没有匹配的密钥" description="从账户详情同步远端密钥或创建新密钥。" /></div> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] table-fixed text-left text-sm">
              <thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="w-[20%] px-4 py-3 font-medium">密钥</th><th className="w-[23%] px-4 py-3 font-medium">账户</th><th className="w-[15%] px-4 py-3 font-medium">站点</th><th className="w-[12%] px-4 py-3 font-medium">分组</th><th className="w-[11%] px-4 py-3 font-medium">状态</th><th className="w-[9%] px-4 py-3 font-medium">API 聚合</th><th className="w-[10%] px-4 py-3 text-right font-medium">操作</th></tr></thead>
              <tbody className="divide-y divide-slate-200">
                {visible.map((item) => <tr key={item.id} className="hover:bg-slate-50/80">
                  <td className="px-4 py-3"><div className="flex items-center gap-2"><span className="truncate font-medium">{item.name || "未命名"}</span>{item.is_relay ? <Star className="h-4 w-4 shrink-0 fill-amber-400 text-amber-500" aria-label="聚合密钥" /> : null}</div><div className="mt-1 truncate font-mono text-xs text-slate-500">{revealed[item.id] || `远端 #${item.remote_key_id}`}</div></td>
                  <td className="px-4 py-3"><button className="max-w-full truncate text-left text-sky-700 hover:underline" onClick={() => openAccount(item.account_id)}>{item.email}</button></td>
                  <td className="truncate px-4 py-3">{item.profile_name}</td>
                  <td className="px-4 py-3"><button className="text-sky-700 hover:underline disabled:text-slate-400" disabled={item.status !== "active" || !!busy} onClick={() => void editGroup(item)}>分组 #{item.group_id || "-"}</button></td>
                  <td className="px-4 py-3"><Badge variant={item.status === "active" ? "success" : "destructive"}>{keyStatus(item.status)}</Badge></td>
                  <td className="px-4 py-3">{item.is_relay ? <Badge variant="success">当前聚合密钥</Badge> : <Button size="sm" variant="outline" disabled={item.status !== "active" || !!busy} onClick={() => void selectRelay(item)}>设为聚合密钥</Button>}</td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-1"><Button size="icon" variant="ghost" aria-label="读取密钥" onClick={() => void reveal(item)}><Eye className="h-4 w-4" /></Button>{revealed[item.id] ? <Button size="icon" variant="ghost" aria-label="复制密钥" onClick={() => void copy(revealed[item.id])}><Copy className="h-4 w-4" /></Button> : null}<Button size="icon" variant="ghost" aria-label="删除密钥" disabled={item.status === "deleted" || !!busy} onClick={() => void remove(item)}><Trash2 className="h-4 w-4" /></Button></div></td>
                </tr>)}
              </tbody>
            </table>
          </div>
        )}
        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-500">显示 {visible.length} / {items.length} 个密钥</div>
      </section>

      {groupEditor ? <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/30 p-6" onMouseDown={(event) => event.target === event.currentTarget && setGroupEditor(null)}><section role="dialog" aria-modal="true" className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-2xl"><h2 className="text-base font-semibold">修改密钥分组</h2><p className="mt-1 text-sm text-slate-500">{groupEditor.key.name || `密钥 #${groupEditor.key.remote_key_id}`}</p><Select className="mt-5" defaultValue={String(groupEditor.key.group_id || "")} disabled={!!busy} onChange={(event) => void updateGroup(Number(event.target.value))}><option value="">选择分组</option>{groupEditor.groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</Select><div className="mt-5 flex justify-end"><Button variant="outline" onClick={() => setGroupEditor(null)}>取消</Button></div></section></div> : null}
      {drawerAccountId > 0 ? <AccountDrawer accountId={drawerAccountId} onClose={closeAccount} onChanged={load} onToast={setToast} /> : null}
      {toast ? <Toast message={toast} /> : null}
    </div>
  );
}
