export type JobStatus = {
  running: boolean;
  started_at?: number | null;
  finished_at?: number | null;
  target_count: number;
  workers: number;
  source: string;
  last_error?: string;
  log_count: number;
  latest_log_id: number;
  completed_count: number;
  success_count: number;
  failure_count: number;
  progress_percent: number;
  current_stage: string;
  current_email: string;
  batch_id?: string;
  profile_id?: number;
  profile_name?: string;
};

export type Sub2apiProfile = {
  id: number;
  name: string;
  site_key: string;
  checkin_supported?: boolean;
  account_count?: number;
  key_count?: number;
  active_key_count?: number;
  register_url: string;
  register_origin: string;
  promo_code: string;
  invitation_code: string;
  aff_code: string;
  whitelist: string[];
  enabled: boolean;
  in_use?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type Sub2apiProfileInput = {
  name: string;
  site_key: string;
  promo_code?: string;
  invitation_code?: string;
  aff_code?: string;
  enabled?: boolean;
};

export type VerifiedSite = {
  key: string;
  name: string;
  register_url: string;
  default_aff_code: string;
  email_suffix_whitelist: string[];
  checkin_supported: boolean;
};

export type RegistrationAttempt = {
  id: number;
  email: string;
  status: string;
  registration_status: string;
  success: boolean;
  provider: string;
  profile_id: number;
  mailbox_source?: string;
  mail_consumed: boolean;
  mailbox_consumed_at: string;
  failure_type: string;
  failure_reason: string;
  screenshot_path: string;
  screenshot_url: string;
  exception_traceback: string;
  exception_type: string;
  has_exception_traceback: boolean;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  batch_id: string;
  source: string;
  worker_id: number;
  extra?: Record<string, unknown>;
};

export type CheckinResult = {
  status:
    | "success"
    | "already_checked_in"
    | "authentication_failure"
    | "captcha_manual_required"
    | "unsupported"
    | "uncertain"
    | "upstream_failure";
  message: string;
  checkin_date: string;
  next_reset_at: string;
};

export type ApiKeyGroup = {
  id: number;
  name: string;
  platform: string;
  description: string;
  rate_multiplier: number | null;
};

export type CreatedApiKey = {
  id: number;
  name: string;
  group_id: number;
  status: string;
  secret: string;
  reconciled: boolean;
};

export type ProfileStat = {
  profile_id: number;
  profile_name: string;
  total: number;
  success: number;
  failure: number;
  consumed: number;
};

export type Stats = {
  total: number;
  success: number;
  failure: number;
  skipped: number;
  cancelled: number;
  mailbox_consumed: number;
  orphan_consumptions?: number;
  today_total: number;
  today_success: number;
  unique_success_accounts?: number;
  avg_success_seconds: number;
  profiles?: ProfileStat[];
};

export type LogItem = {
  id: number;
  time: string;
  message: string;
};

export type AuthState = {
  enabled: boolean;
  setup_required?: boolean;
  authenticated: boolean;
  username: string;
};

export type ArchiveDownload = {
  blob: Blob;
  filename: string;
  exported: number;
  skipped: number;
};

export type ConfigFileSnapshot = {
  path: string;
  exists: boolean;
  size: number;
  modified_at: string;
  content: string;
  parse_error: string;
  sensitive_keys: string[];
};

export type MailboxStatus = {
  ok: boolean;
  service: "outlookemail" | string;
  healthy: boolean;
  version: string;
  account_count: number | null;
  management_port: number;
  integration_key_configured: boolean;
};

export type AccountPoolItem = {
  id: number;
  profile_id: number;
  profile_name: string;
  site_key: string;
  email: string;
  status: string;
  relay_enabled: number;
  relay_key_id: number | null;
  relay_key_name: string | null;
  key_count: number;
  active_key_count: number;
  result_id: number | null;
  relay_key_status: string | null;
  source: string;
  last_login_at: string;
  last_checkin_at: string;
  last_error: string;
  created_at: string;
  updated_at: string;
};

export type ApiKeyPoolItem = {
  id: number;
  account_id: number;
  remote_key_id: number;
  name: string;
  group_id: number;
  status: string;
  created_at: string;
  last_seen_at: string;
  email: string;
  profile_name: string;
  is_relay: number;
};

export type RelayPoolItem = {
  account_id: number;
  email: string;
  profile_name: string;
  site_key: string;
  enabled: boolean;
  key_name: string;
  models: string[];
  in_flight: number;
  cooldown_until: number;
  last_status: string;
};

export type RelayOverview = {
  ok: boolean;
  enabled: boolean;
  strategy: string;
  pool_count: number;
  models: number;
  requests: number;
  in_flight?: number;
  cooling_down?: number;
};

export function errorMessage(error: unknown, fallback = "请求失败") {
  return error instanceof Error && error.message ? error.message : fallback;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    ...init,
  });
  let data: any = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok || data?.ok === false) {
    if (response.status === 401 && data?.auth_required) {
      window.dispatchEvent(
        new CustomEvent("sub2api-auth-required", { detail: { setupRequired: !!data?.setup_required } })
      );
    }
    const detail = data?.detail;
    const detailText = Array.isArray(detail)
      ? detail.map((item: any) => item?.msg || JSON.stringify(item)).join("; ")
      : detail;
    const error = new Error(data?.error || detailText || `请求失败 (${response.status})`) as Error & {
      status?: number;
    };
    error.status = response.status;
    throw error;
  }
  return data as T;
}

async function postDownload(
  endpoint: string,
  ids: number[]
): Promise<ArchiveDownload> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!response.ok) {
    let data: any = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (response.status === 401 && data?.auth_required) {
      window.dispatchEvent(
        new CustomEvent("sub2api-auth-required", { detail: { setupRequired: !!data?.setup_required } })
      );
    }
    throw new Error(data?.detail || data?.error || `下载失败 (${response.status})`);
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return {
    blob: await response.blob(),
    filename: match?.[1] || "download",
    exported: Number(response.headers.get("X-Exported-Count") || 0),
    skipped: Number(response.headers.get("X-Skipped-Count") || 0),
  };
}

export const api = {
  health: () => request<{ ok: boolean; service?: string }>("/api/health"),
  authMe: () => request<{ ok: boolean } & AuthState>("/api/auth/me"),
  setup: (username: string, password: string, confirmPassword: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, confirm_password: confirmPassword }),
    }),
  login: (username: string, password: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  stats: () => request<{ ok: boolean; stats: Stats; job: JobStatus }>("/api/stats"),
  registrationAttempts: (
    params: {
      status?: string;
      mailStatus?: string;
      q?: string;
      batchId?: string;
      profileId?: number;
      limit?: number;
      offset?: number;
    } = {}
  ) => {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.mailStatus) sp.set("email_disable_status", params.mailStatus);
    if (params.q) sp.set("q", params.q);
    if (params.batchId) sp.set("batch_id", params.batchId);
    if (params.profileId) sp.set("profile_id", String(params.profileId));
    if (params.limit) sp.set("limit", String(params.limit));
    if (params.offset) sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return request<{
      ok: boolean;
      items: RegistrationAttempt[];
      total: number | null;
      count: number;
      has_more?: boolean;
      offset: number;
      limit: number;
    }>(
      `/api/accounts${qs ? `?${qs}` : ""}`
    );
  },
  getConfig: () =>
    request<{ ok: boolean; config: Record<string, any>; gate_l_max_count: number }>("/api/config"),
  getConfigFile: () => request<{ ok: boolean; file: ConfigFileSnapshot }>("/api/config/file"),
  mailboxStatus: () => request<MailboxStatus>("/api/mailbox/status"),
  launchMailbox: (next = "/") =>
    request<{ ok: boolean; url: string }>("/api/mailbox/launch", {
      method: "POST",
      body: JSON.stringify({ next }),
    }),
  saveConfig: (config: Record<string, any>) =>
    request<{ ok: boolean; config: Record<string, any>; changed: string[] }>("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  job: () => request<{ ok: boolean; job: JobStatus }>("/api/job"),
  logs: (afterId = 0, limit = 500) =>
    request<{ ok: boolean; logs: LogItem[]; job: JobStatus }>(
      `/api/job/logs?after_id=${afterId}&limit=${limit}`
    ),
  startJob: (
    payload: {
      count?: number;
      config?: Record<string, any>;
      profile_id?: number | null;
    }
  ) =>
    request<{ ok: boolean; job: JobStatus }>("/api/job/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  // ---- Profile 管理 ----
  sub2apiProfiles: () =>
    request<{ ok: boolean; profiles: Sub2apiProfile[] }>("/api/sub2api/profiles"),
  sub2apiSites: () =>
    request<{ ok: boolean; sites: VerifiedSite[] }>("/api/sub2api/sites"),
  sub2apiProfileCreate: (input: Sub2apiProfileInput) =>
    request<{ ok: boolean; profile: Sub2apiProfile }>("/api/sub2api/profiles", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  sub2apiProfileUpdate: (id: number, input: Partial<Sub2apiProfileInput>) =>
    request<{ ok: boolean; profile: Sub2apiProfile }>(`/api/sub2api/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(input),
    }),
  sub2apiProfileDelete: (id: number) =>
    request<{ ok: boolean; deleted: number }>(`/api/sub2api/profiles/${id}`, {
      method: "DELETE",
    }),
  stopJob: () => request<{ ok: boolean; job: JobStatus }>("/api/job/stop", { method: "POST" }),
  killAllBrowsers: () =>
    request<{ ok: boolean; killed: number; profiles_cleaned: number; job: JobStatus }>(
      "/api/browser/kill-all",
      { method: "POST" }
    ),
  connectivity: () =>
    request<{ ok: boolean; items: Array<{ name: string; ok: boolean; detail: string }>; blocked: boolean }>(
      "/api/connectivity",
      { method: "POST" }
    ),
  relayOverview: () => request<RelayOverview>("/api/relay/overview"),
  relayPool: () => request<{ ok: boolean; items: RelayPoolItem[] }>("/api/relay/pool"),
  relayRequests: () => request<{ ok: boolean; items: Array<Record<string, any>> }>("/api/relay/requests"),
  relayRotate: () => request<{ ok: boolean; relay_api_key: string }>("/api/relay/keys/rotate", { method: "POST" }),
  setAccountRelayEnabled: (id: number, enabled: boolean) => request<{ ok: boolean }>(`/api/account-pool/${id}/relay`, { method: "PUT", body: JSON.stringify({ enabled }) }),
  accountPool: () => request<{ ok: boolean; accounts: AccountPoolItem[] }>("/api/account-pool"),
  accountPoolDetail: (accountId: number) => request<{ok:boolean;account:AccountPoolItem;keys:ApiKeyPoolItem[]}>(`/api/account-pool/${accountId}`),
  accountCredentials: (accountId: number) => request<{ok:boolean;email:string;password:string}>(`/api/account-pool/${accountId}/credentials`),
  downloadAccountPoolCredentialsTxt: (ids: number[]) =>
    postDownload("/api/account-pool/credentials-txt/download", ids),
  addAccount: (profileId: number, email: string, password: string) => request<{ok:boolean;account:AccountPoolItem;discovered:number;synced:number;unavailable:number;missing:number}>("/api/account-pool", {method:"POST",body:JSON.stringify({profile_id:profileId,email,password})}),
  verifyAccount: (accountId: number) => request<{ok:boolean;account:AccountPoolItem}>(`/api/account-pool/${accountId}/verify`, {method:"POST"}),
  checkinPoolAccount: (accountId: number) => request<{ok:boolean;result:CheckinResult}>(`/api/account-pool/${accountId}/checkin`, {method:"POST"}),
  checkinPoolAccounts: (ids: number[]) => request<{ok:boolean;success:number;failure:number;items:Array<Record<string,any>>}>("/api/account-pool/checkin", {method:"POST",body:JSON.stringify({ids})}),
  syncAccountKeys: (accountId: number) => request<{ok:boolean;synced:number;discovered:number;unavailable:number;missing:number}>(`/api/account-pool/${accountId}/sync-keys`, {method:"POST"}),
  accountGroups: (accountId: number) => request<{ok:boolean;groups:ApiKeyGroup[]}>(`/api/account-pool/${accountId}/groups`),
  createPoolApiKey: (accountId: number, name: string, groupId: number) => request<{ok:boolean;key_row_id:number;key:CreatedApiKey}>(`/api/account-pool/${accountId}/api-keys`, {method:"POST",body:JSON.stringify({name,group_id:groupId})}),
  updatePoolApiKeyGroup: (accountId: number, keyRowId: number, groupId: number) => request<{ok:boolean;key:ApiKeyPoolItem}>(`/api/account-pool/${accountId}/api-keys/${keyRowId}`, {method:"PUT",body:JSON.stringify({group_id:groupId})}),
  deletePoolApiKey: (accountId: number, keyRowId: number) => request<{ok:boolean;deleted:number}>(`/api/account-pool/${accountId}/api-keys/${keyRowId}`, {method:"DELETE"}),
  revealSavedApiKey: (keyRowId: number) => request<{ok:boolean;secret:string}>(`/api/api-keys/${keyRowId}/reveal`),
  selectRelayKey: (accountId: number, keyId: number) => request<{ok:boolean}>(`/api/account-pool/${accountId}/relay-key`, {method:"POST",body:JSON.stringify({key_id:keyId})}),
  apiKeys: () => request<{ ok: boolean; keys: ApiKeyPoolItem[] }>("/api/api-keys"),
  relayProbe: (id: number) => request<{ ok: boolean; models: number; status: string }>(`/api/relay/pool/${id}/probe`, { method: "POST" }),
  relayRefreshModels: () => request<{ ok: boolean; refreshed: number }>("/api/relay/refresh-models", { method: "POST" }),
};
