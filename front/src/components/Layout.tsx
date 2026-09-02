import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { Activity, LogOut, Menu, MoreHorizontal, PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { type RegistrationJobState, useRegistrationJobState } from "@/app/RegistrationJobContext";
import { mobilePrimaryItems, navigationGroups, navigationItems } from "@/app/navigation";
import { cn } from "@/lib/utils";

function navigationActive(pathname: string, to: string) {
  if (to === "/accounts") return pathname === to;
  return pathname === to || pathname.startsWith(`${to}/`);
}

function StatusPill({ state, compact = false }: { state: RegistrationJobState; compact?: boolean }) {
  const running = state === "running";
  const loading = state === "loading";
  const label = loading ? "同步中" : running ? (compact ? "注册中" : "注册运行中") : compact ? "空闲" : "系统空闲";
  return (
    <div
      className={cn(
        "inline-flex min-h-8 items-center gap-2 rounded-lg border px-2.5 text-xs font-medium",
        loading
          ? "border-slate-200 bg-slate-50 text-slate-600"
          : running
            ? "border-amber-200 bg-amber-50 text-amber-800"
            : "border-emerald-200 bg-emerald-50 text-emerald-800"
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", loading ? "animate-pulse bg-slate-400" : running ? "animate-pulse bg-amber-500" : "bg-emerald-500")} />
      {label}
    </div>
  );
}

function Brand() {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-900 text-xs font-bold tracking-wide text-white">
        S2
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold tracking-tight text-slate-950">Sub2API Native</div>
        <div className="truncate text-[11px] text-slate-500">站点聚合与 API 管理控制台</div>
      </div>
    </div>
  );
}

function NavigationContent({ onNavigate, collapsed = false }: { onNavigate?: () => void; collapsed?: boolean }) {
  const location = useLocation();
  return (
    <nav className="flex flex-col gap-5" aria-label="主导航">
      {navigationGroups.map((group) => (
        <section key={group.label}>
          {collapsed ? <div className="mx-2 mb-2 border-t border-slate-100" /> : <div className="mb-1.5 px-3 text-[11px] font-medium uppercase tracking-[0.08em] text-slate-400">{group.label}</div>}
          <div className="space-y-0.5">
            {group.items.map((item) => {
              const Icon = item.icon;
              const active = navigationActive(location.pathname, item.to);
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/accounts"}
                  onClick={onNavigate}
                  title={collapsed ? item.label : undefined}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "relative flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm transition-colors",
                    collapsed && "justify-center px-2",
                    active
                      ? "bg-sky-50 font-medium text-sky-700"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-950"
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" strokeWidth={1.8} aria-hidden="true" />
                  <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
                </NavLink>
              );
            })}
          </div>
        </section>
      ))}
    </nav>
  );
}

export function Layout({ onLogout }: { onLogout?: () => void }) {
  const jobState = useRegistrationJobState();
  const jobRunning = jobState === "running";
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("sub2api-sidebar-collapsed") === "1";
    } catch {
      return false;
    }
  });
  const current = useMemo(
    () =>
      [...navigationItems]
        .sort((a, b) => b.to.length - a.to.length)
        .find((item) => location.pathname === item.to || location.pathname.startsWith(`${item.to}/`)),
    [location.pathname]
  );
  const primaryActive = mobilePrimaryItems.some(
    (item) => navigationActive(location.pathname, item.to)
  );

  useEffect(() => setMobileMenuOpen(false), [location.pathname]);
  useEffect(() => {
    try {
      window.localStorage.setItem("sub2api-sidebar-collapsed", sidebarCollapsed ? "1" : "0");
    } catch {
      // 浏览器禁用本地存储时只保留当前会话状态。
    }
  }, [sidebarCollapsed]);
  useEffect(() => {
    document.body.style.overflow = mobileMenuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileMenuOpen]);

  return (
    <div className="min-h-[100dvh] bg-[#f7f8f7] text-foreground">
      <a
        href="#main-content"
        className="fixed left-3 top-3 z-[100] -translate-y-24 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-transform focus:translate-y-0"
      >
        跳到主要内容
      </a>

      <header className="fixed inset-x-0 top-0 z-50 flex h-12 items-center justify-between border-b border-slate-200 bg-white px-4 sm:px-5">
        <div className="flex items-center gap-2">
          <Brand />
          <button
            type="button"
            className="ml-2 hidden h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-slate-900 lg:flex"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
            title={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {sidebarCollapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </button>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className="hidden sm:inline">本地控制台</span>
          {jobRunning ? (
            <Link to="/profiles#registration-runtime" className="inline-flex items-center gap-2 text-sky-700 hover:text-sky-800">
              <StatusPill state={jobState} compact />
              <span className="font-medium">查看</span>
            </Link>
          ) : <StatusPill state={jobState} compact />}
        </div>
      </header>

      <aside className={cn("fixed inset-y-0 left-0 top-12 z-40 hidden flex-col border-r border-slate-200 bg-white transition-[width] duration-200 lg:flex", sidebarCollapsed ? "w-[68px]" : "w-[208px]")}>
        <div className={cn("flex-1 overflow-y-auto py-5", sidebarCollapsed ? "px-2" : "px-3")}>
          <NavigationContent collapsed={sidebarCollapsed} />
        </div>
        <div className={cn("border-t border-slate-100", sidebarCollapsed ? "p-2" : "p-3")}>
          {sidebarCollapsed ? (
            onLogout ? <button type="button" onClick={onLogout} className="flex h-10 w-full items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50" aria-label="退出登录" title="退出登录"><LogOut className="h-4 w-4" /></button> : null
          ) : (
            onLogout ? (
              <button
                type="button"
                onClick={onLogout}
                className="flex min-h-9 w-full items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                退出登录
              </button>
            ) : null
          )}
        </div>
      </aside>

      <div className={cn("min-w-0 pt-12 transition-[padding] duration-200", sidebarCollapsed ? "lg:pl-[68px]" : "lg:pl-[208px]")}>
        <header className="sticky top-12 z-30 flex min-h-14 items-center justify-between border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:hidden">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white lg:hidden"
              onClick={() => setMobileMenuOpen(true)}
              aria-label="打开导航"
            >
              <Menu className="h-4 w-4" aria-hidden="true" />
            </button>
            <div className="hidden lg:block">
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <span>控制台</span>
                <span>/</span>
                <span className="font-medium text-slate-700">{current?.label || "概览"}</span>
              </div>
            </div>
            <div className="min-w-0 lg:hidden">
              <div className="truncate text-sm font-semibold text-slate-950">{current?.label || "工作台"}</div>
              <div className="truncate text-[11px] text-slate-500">Sub2API Native</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <StatusPill state={jobState} compact />
            <div className="hidden items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-500 sm:flex">
              <Activity className="h-3.5 w-3.5 text-sky-600" aria-hidden="true" />
              服务正常
            </div>
            {onLogout ? (
              <button
                type="button"
                onClick={onLogout}
                className="hidden min-h-9 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-xs font-medium text-slate-700 hover:bg-slate-50 sm:flex"
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                退出
              </button>
            ) : null}
          </div>
        </header>

        <main
          id="main-content"
          className="w-full px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-5 sm:px-6 sm:pt-6 lg:px-5 lg:pb-10 lg:pt-6 xl:px-6"
        >
          <Outlet />
        </main>
      </div>

      {mobileMenuOpen ? (
        <div className="fixed inset-0 z-[80] lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-slate-950/35"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="关闭导航"
          />
          <aside className="absolute inset-y-0 left-0 flex w-[min(88vw,320px)] flex-col bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
              <Brand />
              <button
                type="button"
                className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200"
                onClick={() => setMobileMenuOpen(false)}
                aria-label="关闭导航"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-3 py-5">
              <NavigationContent onNavigate={() => setMobileMenuOpen(false)} />
            </div>
            {onLogout ? (
              <div className="border-t border-slate-100 p-3">
                <button
                  type="button"
                  onClick={onLogout}
                  className="flex min-h-10 w-full items-center justify-center gap-2 rounded-lg border border-slate-200 text-sm font-medium"
                >
                  <LogOut className="h-4 w-4" aria-hidden="true" />
                  退出登录
                </button>
              </div>
            ) : null}
          </aside>
        </div>
      ) : null}

      <nav
        className="fixed inset-x-0 bottom-0 z-50 grid grid-cols-4 border-t border-slate-200 bg-white/96 px-1 pb-[env(safe-area-inset-bottom)] shadow-[0_-8px_24px_-20px_rgba(15,23,42,0.3)] backdrop-blur lg:hidden"
        aria-label="手机端主导航"
      >
        {mobilePrimaryItems.map((item) => {
          const Icon = item.icon;
          const active = navigationActive(location.pathname, item.to);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/accounts"}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex min-h-[62px] flex-col items-center justify-center gap-1 text-[11px] font-medium",
                active ? "text-sky-600" : "text-slate-500"
              )}
            >
              <Icon className="h-5 w-5" strokeWidth={1.8} aria-hidden="true" />
              <span>{item.shortLabel}</span>
            </NavLink>
          );
        })}
        <button
          type="button"
          onClick={() => setMobileMenuOpen(true)}
          className={cn(
            "flex min-h-[62px] flex-col items-center justify-center gap-1 text-[11px] font-medium",
            !primaryActive ? "text-sky-600" : "text-slate-500"
          )}
        >
          <MoreHorizontal className="h-5 w-5" strokeWidth={1.8} aria-hidden="true" />
          <span>更多</span>
        </button>
      </nav>
    </div>
  );
}
