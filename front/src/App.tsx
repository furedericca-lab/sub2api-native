import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { RegistrationJobProvider } from "@/app/RegistrationJobContext";
import { Layout } from "@/components/Layout";
import { AccountsPage } from "@/pages/Accounts";
import { SettingsPage } from "@/pages/Settings";
import { api } from "@/lib/api";
import { LoginPage } from "@/pages/Login";
import { RelayPage } from "@/pages/Relay";
import { ProfilesPage } from "@/pages/Profiles";
import { AccountPoolPage } from "@/pages/AccountPool";
import { ApiKeysPoolPage } from "@/pages/ApiKeysPool";

export default function App() {
  const [authLoading, setAuthLoading] = useState(true);
  const [auth, setAuth] = useState({ enabled: false, setup_required: true, authenticated: false });

  useEffect(() => {
    const onAuthRequired = (event: Event) => {
      const setupRequired = !!(event as CustomEvent<{ setupRequired?: boolean }>).detail?.setupRequired;
      setAuth({ enabled: !setupRequired, setup_required: setupRequired, authenticated: false });
    };
    window.addEventListener("sub2api-auth-required", onAuthRequired);
    api.authMe()
      .then((data) => setAuth({
        enabled: !!data.enabled,
        setup_required: !!data.setup_required,
        authenticated: !!data.authenticated,
      }))
      .catch(() => setAuth({ enabled: true, setup_required: false, authenticated: false }))
      .finally(() => setAuthLoading(false));
    return () => {
      window.removeEventListener("sub2api-auth-required", onAuthRequired);
    };
  }, []);

  if (authLoading) {
    return <div className="flex min-h-[100dvh] items-center justify-center text-muted-foreground">加载中…</div>;
  }
  if (auth.setup_required || (auth.enabled && !auth.authenticated)) {
    return <LoginPage setupRequired={!!auth.setup_required} onLoggedIn={() => setAuth({ enabled: true, setup_required: false, authenticated: true })} />;
  }

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      setAuth({ enabled: true, setup_required: false, authenticated: false });
    }
  };

  return (
    <RegistrationJobProvider>
      <Routes>
      <Route element={<Layout onLogout={auth.enabled ? logout : undefined} />}>
        <Route index element={<Navigate to="/profiles" replace />} />
        <Route path="register" element={<Navigate to="/profiles" replace />} />
        {/* 旧路由重定向，避免书签/外链 404 */}
        <Route path="overview" element={<Navigate to="/profiles" replace />} />
        <Route path="registration/new" element={<Navigate to="/profiles" replace />} />
        <Route path="registration/runtime" element={<Navigate to="/profiles" replace />} />
        <Route path="accounts" element={<Navigate to="/account-pool" replace />} />
        <Route path="registration-attempts" element={<AccountsPage />} />
        <Route path="profiles" element={<ProfilesPage />} />
        <Route path="account-pool" element={<AccountPoolPage />} />
        <Route path="api-keys" element={<ApiKeysPoolPage />} />
        <Route path="relay" element={<RelayPage />} />
        <Route path="settings/registration" element={<SettingsPage section="registration" />} />
        <Route path="settings/relay" element={<Navigate to="/relay" replace />} />
        <Route path="settings/outlook" element={<SettingsPage section="outlook" />} />
        {/* 旧 CPA 路由重定向，避免书签 404 */}
        <Route path="settings/cpa" element={<Navigate to="/settings/registration" replace />} />
        <Route path="settings/tokenauth" element={<Navigate to="/settings/registration" replace />} />
        <Route path="settings/mail" element={<Navigate to="/settings/outlook" replace />} />
        <Route path="settings/config" element={<Navigate to="/settings/registration" replace />} />
        <Route path="settings" element={<Navigate to="/settings/registration" replace />} />
        <Route path="*" element={<Navigate to="/profiles" replace />} />
      </Route>
      </Routes>
    </RegistrationJobProvider>
  );
}
