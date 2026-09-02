import { FormEvent, useState } from "react";
import { Check, Loader2, LockKeyhole } from "lucide-react";
import { api } from "@/lib/api";
import { Button, Input, Label } from "@/components/ui";

export function LoginPage({ setupRequired, onLoggedIn }: { setupRequired: boolean; onLoggedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      if (setupRequired) await api.setup(username, password, confirmPassword);
      else await api.login(username, password);
      onLoggedIn();
    } catch (reason: any) {
      setError(reason.message || "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="grid min-h-[100dvh] bg-[#f4f5f3] lg:grid-cols-[minmax(360px,.85fr)_minmax(520px,1.15fr)]">
      <section className="hidden flex-col justify-between border-r border-slate-200 bg-slate-900 p-10 text-white lg:flex xl:p-14">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-white text-sm font-bold text-slate-900">S2</span>
          <div><div className="font-semibold">Sub2API Native</div><div className="text-xs text-slate-400">站点聚合与 API 管理控制台</div></div>
        </div>
        <div className="max-w-md">
          <p className="text-sm font-medium text-sky-300">SUB2API NATIVE</p>
          <h1 className="mt-4 text-4xl font-semibold leading-tight tracking-tight">统一聚合管理已部署的 Sub2API 站点</h1>
          <div className="mt-8 space-y-4 text-sm text-slate-300">
            {["按站点管理接入的账户与访问凭据", "统一查看密钥、API 聚合路由与使用状态", "本地配置、服务运行与管理员访问边界清晰可查"].map((item) => <div key={item} className="flex items-center gap-3"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-sky-600/30 text-sky-200"><Check className="h-3 w-3" /></span>{item}</div>)}
          </div>
        </div>
        <p className="text-xs text-slate-500">本地管理员控制台</p>
      </section>

      <section className="flex items-center justify-center px-4 py-8 sm:px-8">
        <form onSubmit={submit} className="w-full max-w-[440px] rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_18px_50px_-35px_rgba(15,23,42,.4)] sm:p-8">
          <div className="mb-8 lg:hidden">
            <div className="flex items-center gap-3"><span className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900 text-sm font-bold text-white">S2</span><div><div className="font-semibold text-slate-950">Sub2API Native</div><div className="text-xs text-slate-500">站点聚合与 API 管理控制台</div></div></div>
          </div>
          <div className="mb-7">
            <p className="text-xs font-medium uppercase tracking-[.12em] text-sky-600">管理员</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950">{setupRequired ? "创建管理员" : "欢迎回来"}</h2>
            <p className="mt-2 text-sm leading-6 text-slate-500">{setupRequired ? "首次访问需要创建唯一管理员账户，完成后进入控制台。" : "输入管理员账户和密码继续使用控制台。"}</p>
          </div>
          <div className="space-y-4">
            <div className="space-y-2"><Label htmlFor="login-username">管理员账户</Label><Input id="login-username" autoComplete="username" placeholder="输入账户" value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus /></div>
            <div className="space-y-2"><Label htmlFor="login-password">密码</Label><Input id="login-password" type="password" autoComplete={setupRequired ? "new-password" : "current-password"} placeholder="输入密码" value={password} onChange={(event) => setPassword(event.target.value)} required /></div>
            {setupRequired ? <div className="space-y-2"><Label htmlFor="login-confirm-password">确认密码</Label><Input id="login-confirm-password" type="password" autoComplete="new-password" placeholder="再次输入密码" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required /></div> : null}
            {error ? <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-sm text-red-700">{error}</div> : null}
            <Button className="mt-2 w-full" type="submit" disabled={loading}>{loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <LockKeyhole className="h-4 w-4" />}{setupRequired ? "创建管理员账户并进入" : "登录控制台"}</Button>
          </div>
          <p className="mt-6 text-center text-xs text-slate-400">凭据仅用于本地控制台身份验证</p>
        </form>
      </section>
    </main>
  );
}
