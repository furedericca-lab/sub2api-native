import { useEffect, useMemo, useRef, useState } from "react";
import { KeyRound, Loader2, Pencil, Play, Plus, RefreshCw, Trash2, Users, X } from "lucide-react";
import { Link, useLocation } from "react-router-dom";

import { api, errorMessage, type Sub2apiProfile, type Sub2apiProfileInput, type VerifiedSite } from "@/lib/api";
import { Badge, Button, EmptyState, Input, Label, PageHeader, Select, StatCard, Switch, Toast, buttonVariants } from "@/components/ui";
import { normalizeInteger } from "@/lib/utils";
import { useRegistrationJob } from "@/app/RegistrationJobContext";
import { RegistrationRuntimePanel } from "@/pages/profiles/RegistrationRuntimePanel";

const empty: Sub2apiProfileInput = { name: "", site_key: "", promo_code: "", invitation_code: "", aff_code: "", enabled: true };

type ProfileForm = Sub2apiProfileInput & { id?: number; in_use?: boolean };

function editableProfileInput(form: ProfileForm): Sub2apiProfileInput {
  return {
    name: form.name,
    site_key: form.site_key,
    promo_code: form.promo_code,
    invitation_code: form.invitation_code,
    aff_code: form.aff_code,
    enabled: form.enabled,
  };
}

function profileForm(item: Sub2apiProfile): ProfileForm {
  return { ...editableProfileInput(item), id: item.id, in_use: item.in_use };
}

export function ProfilesPage() {
  const location = useLocation();
  const [items, setItems] = useState<Sub2apiProfile[]>([]);
  const [sites, setSites] = useState<VerifiedSite[]>([]);
  const [form, setForm] = useState<ProfileForm | null>(null);
  const { job, replaceJob } = useRegistrationJob();
  const [registrationProfile, setRegistrationProfile] = useState<Sub2apiProfile | null>(null);
  const [registrationCount, setRegistrationCount] = useState("1");
  const [registrationLimit, setRegistrationLimit] = useState(1);
  const [registrationBusy, setRegistrationBusy] = useState(false);
  const [registrationError, setRegistrationError] = useState("");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState("");
  const previousJobRunning = useRef(false);
  const registrationStateLoading = job === null;
  const registrationBlocked = registrationStateLoading || !!job?.running;
  const registrationBlockedTitle = registrationStateLoading
    ? "正在同步注册任务状态"
    : job?.running
      ? "已有注册任务正在运行"
      : undefined;

  const load = async () => {
    try {
      const [profiles, verifiedSites] = await Promise.all([api.sub2apiProfiles(), api.sub2apiSites()]);
      setItems(profiles.profiles);
      setSites(verifiedSites.sites);
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (location.hash !== "#registration-runtime") return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById("registration-runtime")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.hash, job?.running]);

  useEffect(() => {
    if (!job?.running && previousJobRunning.current) void load();
    previousJobRunning.current = !!job?.running;
  }, [job?.running]);

  const stats = useMemo(() => ({
    enabled: items.filter((item) => item.enabled).length,
    accounts: items.reduce((total, item) => total + (item.account_count || 0), 0),
    keys: items.reduce((total, item) => total + (item.active_key_count || 0), 0),
  }), [items]);

  const save = async () => {
    if (!form || busy) return;
    setBusy(true);
    try {
      const input = editableProfileInput(form);
      if (form.id) await api.sub2apiProfileUpdate(form.id, input);
      else await api.sub2apiProfileCreate(input);
      setForm(null);
      await load();
      setToast("站点已保存");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (item: Sub2apiProfile) => {
    if (!window.confirm(`确认删除站点「${item.name}」？`)) return;
    try {
      await api.sub2apiProfileDelete(item.id);
      await load();
      setToast("站点已删除");
    } catch (error) {
      setToast(errorMessage(error));
    }
  };

  const openRegistration = async (item: Sub2apiProfile) => {
    if (!item.enabled || registrationBlocked) return;
    setRegistrationProfile(item);
    setRegistrationCount("1");
    setRegistrationLimit(1);
    setRegistrationError("");
    try {
      const data = await api.getConfig();
      const limit = normalizeInteger(data.gate_l_max_count ?? 1, 1, 1000);
      setRegistrationLimit(limit);
      setRegistrationCount(String(normalizeInteger(data.config.register_count || 1, 1, limit)));
    } catch (error) {
      setRegistrationError(errorMessage(error, "无法读取注册数量限制"));
    }
  };

  const startRegistration = async () => {
    if (!registrationProfile || registrationBusy) return;
    if (registrationBlocked) {
      setRegistrationError(registrationStateLoading ? "正在同步注册任务状态，请稍后重试" : "已有注册任务正在运行");
      return;
    }
    setRegistrationBusy(true);
    setRegistrationError("");
    try {
      const count = normalizeInteger(registrationCount, 1, registrationLimit);
      setRegistrationCount(String(count));
      const data = await api.startJob({ count, profile_id: registrationProfile.id });
      replaceJob(data.job);
      setRegistrationProfile(null);
      setToast("注册任务已启动");
      window.setTimeout(() => document.getElementById("registration-runtime")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
    } catch (error) {
      setRegistrationError(errorMessage(error, "启动失败"));
    } finally {
      setRegistrationBusy(false);
    }
  };

  const viewRegistration = () => {
    document.getElementById("registration-runtime")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="站点池"
        description="管理已验证 Sub2API 站点。站点能力和邮箱域名白名单由内置目录维护。"
        actions={(
          <>
            <Button variant="outline" onClick={() => void load()}><RefreshCw className="h-4 w-4" />刷新</Button>
            <Button onClick={() => setForm({ ...empty })}><Plus className="h-4 w-4" />添加站点</Button>
          </>
        )}
      />

      <div className="grid grid-cols-3 gap-3">
        <StatCard title="已启用站点" value={stats.enabled} />
        <StatCard title="账户总数" value={stats.accounts} accent="secondary" icon={<Users className="h-4 w-4" />} />
        <StatCard title="可用密钥" value={stats.keys} accent="success" icon={<KeyRound className="h-4 w-4" />} />
      </div>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        {!items.length ? <div className="p-5"><EmptyState title="暂无站点" description="添加一个已验证站点后即可接入账户。" /></div> : (
          <>
            <div className="space-y-3 p-3 md:hidden">
              {items.map((item) => {
                const runningThisSite = !!job?.running && job.profile_id === item.id;
                return (
                  <article key={item.id} className="rounded-lg border border-slate-200 bg-white p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate font-medium text-slate-950">{item.name}</div>
                        <div className="mt-1 text-xs text-slate-500">站点 #{item.id} · {item.site_key}</div>
                      </div>
                      {runningThisSite ? <Badge variant="warning">注册中 {job?.completed_count || 0}/{job?.target_count || 0}</Badge> : <Badge variant={item.enabled ? "success" : "secondary"}>{item.enabled ? "启用" : "停用"}</Badge>}
                    </div>
                    <div className="mt-3 border-t border-slate-100 pt-3">
                      <div className="text-xs text-slate-500">接入地址</div>
                      <div className="mt-1 break-all text-sm text-slate-700">{item.register_origin}</div>
                    </div>
                    <div className="mt-3 grid grid-cols-2 divide-x divide-slate-200 rounded-lg border border-slate-200 bg-slate-50/70">
                      <Link to={`/account-pool?profile=${item.id}`} className="px-3 py-2.5 text-sm text-slate-700 hover:bg-slate-100">
                        <span className="block text-xs text-slate-500">账户</span>
                        <span className="mt-1 block font-medium text-sky-700">{item.account_count || 0}</span>
                      </Link>
                      <Link to={`/api-keys?profile=${item.id}`} className="px-3 py-2.5 text-sm text-slate-700 hover:bg-slate-100">
                        <span className="block text-xs text-slate-500">密钥</span>
                        <span className="mt-1 block font-medium text-sky-700">{item.active_key_count || 0}<span className="font-normal text-slate-400"> / {item.key_count || 0}</span></span>
                      </Link>
                    </div>
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                      <span className="text-xs text-slate-500">注册 · 登录 · 密钥管理{item.checkin_supported ? " · 签到" : ""}</span>
                      <div className="flex flex-wrap justify-end gap-1">
                        {runningThisSite ? <Button size="sm" variant="outline" onClick={viewRegistration}>查看</Button> : item.enabled ? <Button size="sm" variant="outline" disabled={registrationBlocked} title={registrationBlockedTitle} onClick={() => void openRegistration(item)}><Play className="h-4 w-4" />注册</Button> : <Button size="sm" variant="outline" disabled title="站点已停用"><Play className="h-4 w-4" />注册</Button>}
                        <Button size="icon" variant="ghost" aria-label={`编辑 ${item.name}`} onClick={() => setForm(profileForm(item))}><Pencil className="h-4 w-4" /></Button>
                        <Button size="icon" variant="ghost" aria-label={`删除 ${item.name}`} disabled={item.in_use} title={item.in_use ? "已有账户或注册记录，不能删除" : "删除站点"} onClick={() => void remove(item)}><Trash2 className="h-4 w-4" /></Button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full min-w-[1100px] table-fixed text-left text-sm">
                <thead className="bg-slate-50 text-xs text-slate-500"><tr><th className="w-[17%] px-4 py-3 font-medium">站点名称</th><th className="w-[22%] px-4 py-3 font-medium">接入地址</th><th className="w-[12%] px-4 py-3 font-medium">状态</th><th className="w-[8%] px-4 py-3 text-right font-medium">账户</th><th className="w-[10%] px-4 py-3 text-right font-medium">密钥</th><th className="w-[17%] px-4 py-3 font-medium">能力</th><th className="w-[14%] px-4 py-3 text-right font-medium">操作</th></tr></thead>
                <tbody className="divide-y divide-slate-200">
                  {items.map((item) => {
                    const runningThisSite = !!job?.running && job.profile_id === item.id;
                    return <tr key={item.id} className="hover:bg-slate-50/80">
                      <td className="px-4 py-3"><div className="truncate font-medium">{item.name}</div><div className="mt-1 text-xs text-slate-500">站点 #{item.id}</div></td>
                      <td className="px-4 py-3"><div className="truncate">{item.register_origin}</div><div className="mt-1 truncate text-xs text-slate-500">{item.site_key}</div></td>
                      <td className="px-4 py-3">{runningThisSite ? <Badge variant="warning">注册中 {job?.completed_count || 0}/{job?.target_count || 0}</Badge> : <Badge variant={item.enabled ? "success" : "secondary"}>{item.enabled ? "启用" : "停用"}</Badge>}</td>
                      <td className="px-4 py-3 text-right"><Link className="font-medium text-sky-700 hover:underline" to={`/account-pool?profile=${item.id}`}>{item.account_count || 0}</Link></td>
                      <td className="px-4 py-3 text-right"><Link className="font-medium text-sky-700 hover:underline" to={`/api-keys?profile=${item.id}`}>{item.active_key_count || 0}<span className="font-normal text-slate-400"> / {item.key_count || 0}</span></Link></td>
                      <td className="px-4 py-3 text-xs text-slate-600">注册 · 登录 · 密钥管理{item.checkin_supported ? " · 签到" : ""}</td>
                      <td className="px-4 py-3"><div className="flex justify-end gap-1">{runningThisSite ? <Button size="sm" variant="outline" onClick={viewRegistration}>查看</Button> : item.enabled ? <Button size="sm" variant="outline" disabled={registrationBlocked} title={registrationBlockedTitle} onClick={() => void openRegistration(item)}><Play className="h-4 w-4" />注册</Button> : <Button size="sm" variant="outline" disabled title="站点已停用"><Play className="h-4 w-4" />注册</Button>}<Button size="icon" variant="ghost" aria-label={`编辑 ${item.name}`} onClick={() => setForm(profileForm(item))}><Pencil className="h-4 w-4" /></Button><Button size="icon" variant="ghost" aria-label={`删除 ${item.name}`} disabled={item.in_use} title={item.in_use ? "已有账户或注册记录，不能删除" : "删除站点"} onClick={() => void remove(item)}><Trash2 className="h-4 w-4" /></Button></div></td>
                    </tr>;
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <RegistrationRuntimePanel />

      {registrationProfile ? (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/30 p-3 sm:items-center sm:p-6"
          onMouseDown={(event) => event.target === event.currentTarget && !registrationBusy && setRegistrationProfile(null)}
        >
          <section role="dialog" aria-modal="true" aria-labelledby="registration-start-title" className="w-full max-w-md rounded-lg border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
              <div>
                <h2 id="registration-start-title" className="text-base font-semibold text-slate-950">注册账户</h2>
                <p className="mt-1 text-sm text-slate-500">{registrationProfile.name}</p>
              </div>
              <Button size="icon" variant="ghost" aria-label="关闭" disabled={registrationBusy} onClick={() => setRegistrationProfile(null)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="space-y-4 p-5">
              <div className="space-y-2">
                <Label htmlFor="registration-count">注册数量</Label>
                <Input
                  id="registration-count"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={registrationLimit}
                  value={registrationCount}
                  disabled={registrationBusy}
                  autoFocus
                  onChange={(event) => setRegistrationCount(event.target.value)}
                  onBlur={() => setRegistrationCount(String(normalizeInteger(registrationCount, 1, registrationLimit)))}
                />
                <p className="text-xs leading-5 text-slate-500">
                  {registrationLimit > 1
                    ? `支持 1-${registrationLimit} 个账户，按单 worker 顺序执行。`
                    : "当前仅支持 1 个账户，批量接入仍受 Gate L 限制。"}
                </p>
              </div>
              {registrationError ? <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{registrationError}</p> : null}
            </div>
            <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-4">
              <Button variant="outline" disabled={registrationBusy} onClick={() => setRegistrationProfile(null)}>取消</Button>
              <Button disabled={registrationBusy || registrationBlocked} title={registrationBlockedTitle} onClick={() => void startRegistration()}>
                {registrationBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                开始注册
              </Button>
            </div>
          </section>
        </div>
      ) : null}

      {form ? (
        <div className="fixed inset-0 z-50 bg-slate-950/20" onMouseDown={(event) => event.target === event.currentTarget && setForm(null)}>
          <aside role="dialog" aria-modal="true" aria-labelledby="profile-editor-title" className="ml-auto flex h-full w-[560px] flex-col border-l border-slate-200 bg-white shadow-2xl">
            <header className="flex h-16 items-center justify-between border-b border-slate-200 px-6"><div><h2 id="profile-editor-title" className="text-base font-semibold">{form.id ? "编辑站点" : "添加站点"}</h2><p className="mt-1 text-xs text-slate-500">站点只能从已验证目录中选择。</p></div><Button size="icon" variant="ghost" aria-label="关闭" onClick={() => setForm(null)}><X className="h-4 w-4" /></Button></header>
            <div className="flex-1 space-y-5 overflow-y-auto p-6">
              <div><Label htmlFor="profile-name">站点名称</Label><Input id="profile-name" className="mt-2" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></div>
              <div><Label htmlFor="profile-site">已验证站点</Label><Select id="profile-site" className="mt-2" value={form.site_key} disabled={!!form.in_use} onChange={(event) => { const site = sites.find((candidate) => candidate.key === event.target.value); setForm({ ...form, site_key: event.target.value, aff_code: form.aff_code || site?.default_aff_code || "" }); }}><option value="">选择站点</option>{sites.map((site) => <option key={site.key} value={site.key}>{site.name}</option>)}</Select>{form.in_use ? <p className="mt-2 text-xs text-slate-500">站点已有关联资产，站点身份不可更改。</p> : null}</div>
              <div><Label htmlFor="profile-promo">推广码</Label><Input id="profile-promo" className="mt-2" value={form.promo_code || ""} onChange={(event) => setForm({ ...form, promo_code: event.target.value })} /></div>
              <div><Label htmlFor="profile-invitation">邀请码</Label><Input id="profile-invitation" className="mt-2" value={form.invitation_code || ""} onChange={(event) => setForm({ ...form, invitation_code: event.target.value })} /></div>
              <div><Label htmlFor="profile-aff">Aff</Label><Input id="profile-aff" className="mt-2" value={form.aff_code || ""} onChange={(event) => setForm({ ...form, aff_code: event.target.value })} /></div>
              <div className="flex items-center justify-between border-y border-slate-200 py-4"><div><div className="text-sm font-medium">启用站点</div><p className="mt-1 text-xs text-slate-500">停用后不再注册，关联账户也不参与 API 聚合。</p></div><Switch checked={form.enabled !== false} label="启用站点" onCheckedChange={(enabled) => setForm({ ...form, enabled })} /></div>
              {form.site_key ? <div className="rounded-lg bg-slate-50 p-4 text-sm"><div className="font-medium">站点能力</div><p className="mt-2 text-slate-600">注册 · 登录 · 密钥管理{sites.find((site) => site.key === form.site_key)?.checkin_supported ? " · 签到" : ""}</p><p className="mt-2 text-xs leading-5 text-slate-500">邮箱域名白名单：{sites.find((site) => site.key === form.site_key)?.email_suffix_whitelist.join("、") || "-"}</p></div> : null}
            </div>
            <footer className="flex justify-end gap-2 border-t border-slate-200 px-6 py-4"><Button variant="outline" onClick={() => setForm(null)}>取消</Button><Button disabled={busy || !form.name.trim() || !form.site_key} onClick={() => void save()}>保存</Button></footer>
          </aside>
        </div>
      ) : null}

      {toast ? <Toast message={toast} /> : null}
    </div>
  );
}
