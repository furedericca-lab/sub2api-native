import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ExternalLink,
  Eye,
  EyeOff,
  Mail,
  RefreshCw,
  Save,
  Server,
  Settings2,
} from "lucide-react";
import { api } from "@/lib/api";
import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
  PageHeader,
  Select,
  Switch,
  Toast,
  Badge,
} from "@/components/ui";
import { normalizeInteger } from "@/lib/utils";

// 设置分区仅保留注册 / 邮箱设置；旧 CPA 路由已重定向
export type SettingsSection = "registration" | "outlook";

const SECTION_META: Record<SettingsSection, { title: string; description: string }> = {
  registration: { title: "注册设置", description: "决定站点池每次注册任务如何执行：数量、节奏与网络出口。" },
  outlook: { title: "邮箱设置", description: "管理内置邮箱服务，并配置注册时的邮箱来源与读取规则。" },
};
const OUTLOOK_SOURCES = [
  { value: "accounts", label: "外部账户来源 accounts" },
  { value: "temp", label: "站内临时邮箱 temp" },
];
const OUTLOOK_PICK_MODES = [
  { value: "random", label: "随机选取" },
  { value: "sequential", label: "顺序选取" },
];

function ToggleRow({
  title,
  description,
  checked,
  onCheckedChange,
  disabled,
}: {
  title: string;
  description?: string;
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className={`flex min-h-16 items-center justify-between gap-4 rounded-xl border bg-muted/35 px-3 py-3 sm:px-4 ${disabled ? "opacity-60" : ""}`}>
      <div className="min-w-0">
        <div className="text-sm font-medium text-foreground">{title}</div>
        {description ? <div className="mt-0.5 text-xs leading-5 text-muted-foreground">{description}</div> : null}
      </div>
      <Switch checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} label={title} />
    </div>
  );
}

function SectionIcon({ children }: { children: React.ReactNode }) {
  return (
    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-600">
      {children}
    </span>
  );
}

function ConfigField({
  config,
  onFieldChange,
  label,
  field,
  type = "text",
  placeholder = "",
  helper = "",
}: {
  config: Record<string, any>;
  onFieldChange: (key: string, value: any) => void;
  label: string;
  field: string;
  type?: string;
  placeholder?: string;
  helper?: string;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const isPassword = type === "password";
  return (
    <div className="min-w-0 space-y-2">
      <Label htmlFor={field}>{label}</Label>
      <div className="relative">
        <Input
          id={field}
          type={isPassword && showSecret ? "text" : type}
          inputMode={type === "number" ? "numeric" : undefined}
          autoComplete={isPassword ? "new-password" : "off"}
          className={isPassword ? "pr-10" : undefined}
          placeholder={placeholder}
          value={config[field] ?? ""}
          onChange={(event) =>
            onFieldChange(
              field,
              type === "number" && event.target.value !== ""
                ? Number(event.target.value)
                : event.target.value
            )
          }
        />
        {isPassword ? (
          <button
            type="button"
            className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground transition hover:text-foreground"
            aria-label={showSecret ? `隐藏${label}` : `显示${label}`}
            aria-pressed={showSecret}
            onClick={() => setShowSecret((value) => !value)}
          >
            {showSecret ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
          </button>
        ) : null}
      </div>
      {helper ? <p className="text-xs leading-5 text-muted-foreground">{helper}</p> : null}
    </div>
  );
}

export function SettingsPage({ section = "registration" }: { section?: SettingsSection }) {
  const [searchParams] = useSearchParams();
  const [config, setConfig] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  // Gate L 单一真相源：GET /api/config 动态下发（后端 clamp 1–1000）
  const [gateLimit, setGateLimit] = useState(1);
  const [mailboxStatus, setMailboxStatus] = useState<import("@/lib/api").MailboxStatus | null>(null);
  const [mailboxLoading, setMailboxLoading] = useState(false);
  const [mailboxLaunching, setMailboxLaunching] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({
    message: "",
  });

  const showToast = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  };

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getConfig();
      setConfig(data.config || {});
      setGateLimit(normalizeInteger(data.gate_l_max_count ?? 1, 1, 1000));
    } catch (err: any) {
      showToast(err.message || "加载配置失败", "error");
    } finally {
      setLoading(false);
    }
  };

  const loadMailboxStatus = async () => {
    setMailboxLoading(true);
    try {
      setMailboxStatus(await api.mailboxStatus());
    } catch (err: any) {
      setMailboxStatus(null);
      showToast(err.message || "邮箱服务暂不可用", "error");
    } finally {
      setMailboxLoading(false);
    }
  };

  useEffect(() => {
    load();
    if (section === "outlook") {
      loadMailboxStatus();
    }
  }, [section]);

  const setField = (key: string, value: any) => {
    setConfig((previous) => ({ ...previous, [key]: value }));
  };
  const fieldState = { config, onFieldChange: setField };
  const onSave = async () => {
    setSaving(true);
    try {
      const data = await api.saveConfig(config);
      setConfig(data.config || config);
      showToast(`已保存 ${data.changed?.length || 0} 项配置`, "success");
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const openMailboxManagement = async () => {
    const managementWindow = window.open("", "_blank");
    if (!managementWindow) {
      showToast("浏览器阻止了新标签页，请允许弹出窗口后重试", "error");
      return;
    }
    managementWindow.opener = null;
    setMailboxLaunching(true);
    try {
      const result = await api.launchMailbox("/");
      managementWindow.location.replace(result.url);
    } catch (err: any) {
      managementWindow.close();
      showToast(err.message || "邮箱账户管理入口不可用", "error");
    } finally {
      setMailboxLaunching(false);
    }
  };

  const meta = SECTION_META[section];

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader
        title={meta.title}
        description={meta.description}
        actions={
          <>
            <Button variant="outline" onClick={load} disabled={loading || saving}>
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              重新加载
            </Button>
            <Button onClick={onSave} disabled={saving || loading}>
              <Save className="h-4 w-4" aria-hidden="true" />
              {saving ? "保存中…" : "保存配置"}
            </Button>
          </>
        }
      />

      <div className="space-y-4">
        {section === "registration" ? (
        <Card>
          <CardHeader className="flex-row items-start gap-3">
            <SectionIcon><Settings2 className="h-5 w-5" aria-hidden="true" /></SectionIcon>
            <div>
              <CardTitle>基础与注册</CardTitle>
              <CardDescription>注册数量、账户间隔、网络代理与浏览器运行选项。</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <ConfigField
              {...fieldState}
              label="网络代理"
              field="proxy"
              type="password"
              placeholder="http://user:password@host:port"
              helper="支持无认证或用户名/密码认证的 HTTP(S) 代理；凭据含 @、:、/、#、% 等特殊字符时请使用 URL 百分号编码，例如 @ 写成 %40。注册浏览器与目标站点请求会共用此代理。"
            />
            <ConfigField {...fieldState}
              label="账户间隔（秒）"
              field="account_interval"
              placeholder="60-120"
              helper="支持固定秒数或区间；等待过程可随时停止。"
            />
            <ConfigField
              {...fieldState}
              label="注册数量"
              field="register_count"
              type="number"
              helper={
                gateLimit > 1
                  ? `任务启动上限 1–${gateLimit}（Gate L）。`
                  : "Gate L 未通过：任务启动仅接受 1（批量待 R2 Live 验收）。"
              }
            />
            <ConfigField {...fieldState} label="日志级别" field="log_level" placeholder="info（普通）/ debug（详细）" />
            <div className="min-w-0 space-y-2">
              <Label htmlFor="browser_locale">浏览器界面语言</Label>
              <Select
                id="browser_locale"
                value={config.browser_locale || "en-US"}
                onChange={(event) => setField("browser_locale", event.target.value)}
              >
                <option value="en-US">English (en-US，推荐)</option>
                <option value="zh-CN">简体中文 (zh-CN)</option>
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">
                固定注册页面语言，不跟随代理出口自动切换。
              </p>
            </div>
            <div className="space-y-3 sm:col-span-2">
              <ToggleRow
                title="调试模式"
                description="任务结束后保留浏览器，便于检查页面状态"
                checked={!!config.debug_mode}
                onCheckedChange={(value) => setField("debug_mode", value)}
              />
              <ToggleRow
                title="停止即关"
                description="收到停止请求后清理当前浏览器实例"
                checked={!!config.close_browser_on_stop}
                onCheckedChange={(value) => setField("close_browser_on_stop", value)}
              />
            </div>
          </CardContent>
        </Card>
        ) : null}

        {section === "outlook" ? (
        <Card>
          <CardHeader className="flex-row items-start gap-3">
            <SectionIcon><Mail className="h-5 w-5" aria-hidden="true" /></SectionIcon>
            <div>
              <CardTitle>邮箱设置</CardTitle>
              <CardDescription>内置 OutlookEmail 服务状态、账户管理入口与注册邮箱规则。</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between sm:p-4">
              <div className="flex min-w-0 items-start gap-3">
                <SectionIcon><Server className="h-5 w-5" aria-hidden="true" /></SectionIcon>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-900">
                    <span>邮箱服务</span>
                    <Badge variant={mailboxStatus?.healthy ? "success" : "warning"}>
                      {mailboxLoading ? "检查中" : mailboxStatus?.healthy ? "运行正常" : "暂不可用"}
                    </Badge>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-slate-500">
                    版本 {mailboxStatus?.version || "未知"} · 账户 {mailboxStatus?.account_count ?? "未知"} · 集成密钥 {mailboxStatus?.integration_key_configured ? "已配置" : "未配置"}
                  </p>
                </div>
              </div>
              <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
                <Button variant="outline" onClick={loadMailboxStatus} disabled={mailboxLoading || mailboxLaunching}>
                  <RefreshCw className={`h-4 w-4 ${mailboxLoading ? "animate-spin" : ""}`} aria-hidden="true" />
                  检查服务
                </Button>
                <Button onClick={openMailboxManagement} disabled={mailboxLaunching || mailboxLoading}>
                  <ExternalLink className="h-4 w-4" aria-hidden="true" />
                  {mailboxLaunching ? "打开中…" : "邮箱账户管理"}
                </Button>
              </div>
            </div>
            <div className="min-w-0 space-y-2 sm:col-span-2">
              <Label htmlFor="outlookemail_source">邮箱来源</Label>
              <Select
                id="outlookemail_source"
                value={config.outlookemail_source || "accounts"}
                onChange={(event) => setField("outlookemail_source", event.target.value)}
              >
                {OUTLOOK_SOURCES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">
                accounts 为正式账户来源，注册后邮箱保持 active、可长期收验证码；temp 为一次性临时邮箱。两种来源提交目标站点后都会在本地永久标记为已消费，不再参与注册。
              </p>
            </div>
            {(config.outlookemail_source || "accounts") === "accounts" ? (
              <>
                <ConfigField {...fieldState}
                  label="集成密钥"
                  field="outlookemail_api_key"
                  type="password"
                  helper={config.outlookemail_api_key_configured ? "已配置；留空可保持不变，需要更换时输入新密钥。" : "accounts 来源读取账户列表和邮件时使用。"}
                />
                <ConfigField {...fieldState} label="分组 ID" field="outlookemail_group_id" />
                <div className="min-w-0 space-y-2">
                  <Label htmlFor="outlookemail_pick_mode">邮箱选取方式</Label>
                  <Select
                    id="outlookemail_pick_mode"
                    value={config.outlookemail_pick_mode || "random"}
                    onChange={(event) => setField("outlookemail_pick_mode", event.target.value)}
                  >
                    {OUTLOOK_PICK_MODES.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </Select>
                </div>
                <ConfigField {...fieldState} label="邮件文件夹" field="outlookemail_folder" helper="accounts 来源拉取邮件的文件夹，默认 all" />
                <ConfigField {...fieldState} label="单次拉取邮件数" field="outlookemail_top" type="number" />
              </>
            ) : (
              <>
                <ConfigField {...fieldState} label="临时邮箱标签 ID" field="outlookemail_temp_tag_ids" helper="多个 ID 用逗号分隔；留空不过滤" />
                <div className="min-w-0 space-y-2">
                  <Label htmlFor="outlookemail_pick_mode">邮箱选取方式</Label>
                  <Select
                    id="outlookemail_pick_mode"
                    value={config.outlookemail_pick_mode || "random"}
                    onChange={(event) => setField("outlookemail_pick_mode", event.target.value)}
                  >
                    {OUTLOOK_PICK_MODES.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </Select>
                </div>
              </>
            )}
          </CardContent>
        </Card>
        ) : null}
      </div>

      <div className="sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-20 rounded-2xl border bg-card/95 p-2 shadow-lg backdrop-blur lg:hidden">
        <Button className="w-full" onClick={onSave} disabled={saving || loading}>
          <Save className="h-4 w-4" aria-hidden="true" />
          {saving ? "保存中…" : "保存全部配置"}
        </Button>
      </div>

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
