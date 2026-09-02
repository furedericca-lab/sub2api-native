import { useCallback, useEffect, useState } from "react";
import { CalendarCheck, CheckCircle2, Copy, Eye, KeyRound, Loader2, RefreshCw, Trash2, X } from "lucide-react";

import { api, errorMessage, type AccountPoolItem, type ApiKeyGroup, type ApiKeyPoolItem } from "@/lib/api";
import { Badge, Button, Input, Label, Select, Switch } from "@/components/ui";

type Tab = "overview" | "keys" | "activity";

type Props = {
  accountId: number;
  onClose: () => void;
  onChanged: () => Promise<void>;
  onToast: (message: string) => void;
};

function statusLabel(status: string) {
  if (status === "active") return "正常";
  if (status === "authentication_failure") return "认证失败";
  return status || "未知";
}

function keyStatusLabel(status: string) {
  return ({ active: "有效", missing: "远端缺失", deleted: "已删除", revoked: "已撤销", invalid: "已失效" } as Record<string, string>)[status] || status || "未知";
}

export function AccountDrawer({ accountId, onClose, onChanged, onToast }: Props) {
  const [account, setAccount] = useState<AccountPoolItem | null>(null);
  const [keys, setKeys] = useState<ApiKeyPoolItem[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState("");
  const [credentials, setCredentials] = useState<{ email: string; password: string } | null>(null);
  const [groups, setGroups] = useState<ApiKeyGroup[]>([]);
  const [groupsLoaded, setGroupsLoaded] = useState(false);
  const [revealed, setRevealed] = useState<Record<number, string>>({});
  const [newKey, setNewKey] = useState({ name: "codex-relay", groupId: "" });

  const load = useCallback(async () => {
    const detail = await api.accountPoolDetail(accountId);
    setAccount(detail.account);
    setKeys(detail.keys);
  }, [accountId]);

  useEffect(() => {
    setAccount(null);
    setCredentials(null);
    setGroups([]);
    setGroupsLoaded(false);
    setRevealed({});
    void load().catch((error) => onToast(errorMessage(error)));
  }, [accountId, load, onToast]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  const run = async (name: string, action: () => Promise<unknown>, message: string) => {
    if (busy) return;
    setBusy(name);
    try {
      await action();
      await load();
      await onChanged();
      onToast(message);
    } catch (error) {
      onToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const loadGroups = async () => {
    if (busy) return;
    setBusy("groups");
    try {
      const result = await api.accountGroups(accountId);
      setGroups(result.groups);
      setGroupsLoaded(true);
      if (result.groups.length === 1) {
        setNewKey((current) => ({ ...current, groupId: String(result.groups[0].id) }));
      }
    } catch (error) {
      onToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const revealCredentials = async () => {
    if (credentials) return;
    try {
      setCredentials(await api.accountCredentials(accountId));
    } catch (error) {
      onToast(errorMessage(error));
    }
  };

  const revealKey = async (keyId: number) => {
    try {
      const result = await api.revealSavedApiKey(keyId);
      setRevealed((current) => ({ ...current, [keyId]: result.secret }));
    } catch (error) {
      onToast(errorMessage(error));
    }
  };

  const copy = async (value: string, label: string) => {
    await navigator.clipboard.writeText(value);
    onToast(`${label}已复制`);
  };

  const createKey = async () => {
    const groupId = Number(newKey.groupId);
    if (!newKey.name.trim() || !Number.isInteger(groupId) || groupId <= 0) return;
    await run("create-key", async () => {
      const result = await api.createPoolApiKey(accountId, newKey.name.trim(), groupId);
      setRevealed((current) => ({ ...current, [result.key_row_id]: result.key.secret }));
    }, "密钥已创建");
  };

  const updateGroup = async (key: ApiKeyPoolItem, groupId: number) => {
    await run(`group-${key.id}`, () => api.updatePoolApiKeyGroup(accountId, key.id, groupId), "密钥分组已更新");
  };

  const deleteKey = async (key: ApiKeyPoolItem) => {
    if (!window.confirm(`确认删除远端密钥「${key.name || key.remote_key_id}」？`)) return;
    await run(`delete-${key.id}`, () => api.deletePoolApiKey(accountId, key.id), "密钥已删除");
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/20" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside role="dialog" aria-modal="true" aria-labelledby="account-drawer-title" className="ml-auto flex h-full w-[min(620px,calc(100vw-80px))] flex-col border-l border-slate-200 bg-white shadow-2xl">
        <header className="flex h-16 items-center justify-between border-b border-slate-200 px-6">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 id="account-drawer-title" className="truncate text-base font-semibold">{account?.email || "账户"}</h2>
              {account ? <Badge variant={account.status === "active" ? "success" : "destructive"}>{statusLabel(account.status)}</Badge> : null}
            </div>
            <p className="truncate text-xs text-slate-500">{account?.profile_name || "正在读取"}</p>
          </div>
          <Button size="icon" variant="ghost" aria-label="关闭" onClick={onClose}><X className="h-5 w-5" /></Button>
        </header>

        <nav className="flex h-12 items-end gap-6 border-b border-slate-200 px-6">
          {(["overview", "keys", "activity"] as Tab[]).map((value) => (
            <button key={value} className={`h-12 border-b-2 text-sm font-medium ${tab === value ? "border-sky-500 text-sky-700" : "border-transparent text-slate-500"}`} onClick={() => setTab(value)}>
              {{ overview: "概览", keys: `密钥 (${keys.length})`, activity: "操作记录" }[value]}
            </button>
          ))}
        </nav>

        <div className="flex-1 overflow-y-auto p-6">
          {!account ? <div className="flex h-40 items-center justify-center"><Loader2 className="h-5 w-5 animate-spin" /></div> : null}

          {account && tab === "overview" ? (
            <div className="space-y-6">
              <dl className="grid grid-cols-[140px_1fr] gap-x-5 gap-y-4 text-sm">
                <dt className="text-slate-500">邮箱</dt><dd>{account.email}</dd>
                <dt className="text-slate-500">密码</dt>
                <dd className="flex items-center gap-2">
                  <span className="font-mono">{credentials?.password || "••••••••••••"}</span>
                  <Button size="icon" variant="ghost" aria-label="显示密码" onClick={revealCredentials}><Eye className="h-4 w-4" /></Button>
                  {credentials ? <Button size="icon" variant="ghost" aria-label="复制密码" onClick={() => copy(credentials.password, "密码")}><Copy className="h-4 w-4" /></Button> : null}
                </dd>
                <dt className="text-slate-500">站点</dt><dd>{account.profile_name}</dd>
                <dt className="text-slate-500">来源</dt><dd>{account.source === "manual" ? "手工添加" : "自动注册"}</dd>
                <dt className="text-slate-500">最近登录</dt><dd>{account.last_login_at || "尚未验证"}</dd>
                <dt className="text-slate-500">最近签到</dt><dd>{account.site_key === "bmapi" ? account.last_checkin_at || "尚未签到" : "不支持"}</dd>
                <dt className="text-slate-500">聚合密钥</dt><dd>{account.relay_key_name || "未选择"}</dd>
                <dt className="text-slate-500">API 聚合</dt>
                <dd><Switch checked={!!account.relay_enabled && account.relay_key_status === "active"} disabled={account.relay_key_status !== "active" || !!busy} label="API 聚合" onCheckedChange={(enabled) => run("relay", () => api.setAccountRelayEnabled(account.id, enabled), enabled ? "已加入 API 聚合" : "已退出 API 聚合")} /></dd>
              </dl>
              <div className="flex flex-wrap gap-2 border-t border-slate-200 pt-5">
                <Button variant="outline" disabled={!!busy} onClick={() => run("verify", () => api.verifyAccount(account.id), "登录验证成功")}><CheckCircle2 className="h-4 w-4" />验证登录</Button>
                {account.site_key === "bmapi" ? <Button variant="outline" disabled={!!busy} onClick={() => run("checkin", () => api.checkinPoolAccount(account.id), "签到操作完成")}><CalendarCheck className="h-4 w-4" />签到</Button> : null}
                <Button variant="outline" disabled={!!busy} onClick={() => run("sync", () => api.syncAccountKeys(account.id), "密钥已同步")}><RefreshCw className="h-4 w-4" />同步密钥</Button>
              </div>
            </div>
          ) : null}

          {account && tab === "keys" ? (
            <div className="space-y-5">
              <div className="flex items-center justify-between"><h3 className="text-sm font-semibold">账户密钥</h3><Button variant="outline" disabled={!!busy} onClick={loadGroups}><KeyRound className="h-4 w-4" />{groupsLoaded ? "刷新分组" : "加载分组"}</Button></div>
              {groupsLoaded ? <div className="grid grid-cols-[1fr_220px_auto] items-end gap-3 border-b border-slate-200 pb-5"><div><Label htmlFor="new-key-name">密钥名称</Label><Input id="new-key-name" value={newKey.name} onChange={(event) => setNewKey({ ...newKey, name: event.target.value })} /></div><div><Label htmlFor="new-key-group">分组</Label><Select id="new-key-group" value={newKey.groupId} onChange={(event) => setNewKey({ ...newKey, groupId: event.target.value })}><option value="">选择分组</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</Select></div><Button disabled={!!busy || !newKey.groupId || !newKey.name.trim()} onClick={createKey}>创建</Button></div> : null}
              <div className="divide-y divide-slate-200 border-y border-slate-200">
                {keys.map((key) => <div key={key.id} className="space-y-3 py-4"><div className="flex items-start justify-between gap-4"><div><div className="flex items-center gap-2"><span className="font-medium">{key.name || "未命名"}</span>{key.is_relay ? <Badge variant="success">聚合密钥</Badge> : null}<Badge variant={key.status === "active" ? "secondary" : "destructive"}>{keyStatusLabel(key.status)}</Badge></div><p className="mt-1 text-xs text-slate-500">远端 #{key.remote_key_id} · 分组 #{key.group_id || "-"}</p></div><div className="flex gap-1">{!key.is_relay && key.status === "active" ? <Button size="sm" variant="outline" onClick={() => run(`relay-key-${key.id}`, () => api.selectRelayKey(account.id, key.id), "聚合密钥已切换")}>设为聚合密钥</Button> : null}<Button size="icon" variant="ghost" aria-label="读取密钥" onClick={() => revealKey(key.id)}><Eye className="h-4 w-4" /></Button><Button size="icon" variant="ghost" aria-label="删除密钥" disabled={!!busy || key.status === "deleted"} onClick={() => deleteKey(key)}><Trash2 className="h-4 w-4" /></Button></div></div>{revealed[key.id] ? <div className="flex gap-2"><Input readOnly className="font-mono" value={revealed[key.id]} /><Button size="icon" variant="outline" aria-label="复制密钥" onClick={() => copy(revealed[key.id], "密钥")}><Copy className="h-4 w-4" /></Button></div> : null}{groupsLoaded && key.status === "active" ? <Select className="w-56" value={String(key.group_id || "")} disabled={!!busy} onChange={(event) => updateGroup(key, Number(event.target.value))}><option value="">选择分组</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</Select> : null}</div>)}
                {!keys.length ? <p className="py-10 text-center text-sm text-slate-500">暂无密钥</p> : null}
              </div>
            </div>
          ) : null}

          {account && tab === "activity" ? <div className="divide-y divide-slate-200 border-y border-slate-200 text-sm"><div className="grid grid-cols-[120px_1fr] gap-4 py-4"><span className="text-slate-500">创建</span><span>{account.created_at}</span></div><div className="grid grid-cols-[120px_1fr] gap-4 py-4"><span className="text-slate-500">最近登录</span><span>{account.last_login_at || "无记录"}</span></div>{account.site_key === "bmapi" ? <div className="grid grid-cols-[120px_1fr] gap-4 py-4"><span className="text-slate-500">最近签到</span><span>{account.last_checkin_at || "无记录"}</span></div> : null}<div className="grid grid-cols-[120px_1fr] gap-4 py-4"><span className="text-slate-500">最近更新</span><span>{account.updated_at}</span></div>{account.last_error ? <div className="grid grid-cols-[120px_1fr] gap-4 py-4"><span className="text-red-600">最近错误</span><span className="text-red-700">{account.last_error}</span></div> : null}</div> : null}
        </div>
      </aside>
    </div>
  );
}
