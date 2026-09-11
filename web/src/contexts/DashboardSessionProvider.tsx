import { useEffect, useMemo, useState, type ReactNode } from "react";

import { DashboardSessionContext } from "./dashboard-session-context";
import { api, type AuthMeResponse } from "@/lib/api";

export function DashboardSessionProvider({
  children,
}: {
  children: ReactNode;
}) {
  const gated =
    typeof window !== "undefined" && !!window.__HERMES_AUTH_REQUIRED__;
  const [session, setSession] = useState<AuthMeResponse | null>(null);
  const [loading, setLoading] = useState(gated);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!gated) return;
    let cancelled = false;
    api
      .getAuthMe()
      .then(identity => {
        if (!cancelled) setSession(identity);
      })
      .catch(() => {
        if (!cancelled) setError("Access denied");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [gated]);

  const value = useMemo(
    () => ({ gated, loading, error, session }),
    [error, gated, loading, session],
  );
  return (
    <DashboardSessionContext.Provider value={value}>
      {children}
    </DashboardSessionContext.Provider>
  );
}
