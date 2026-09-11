import { createContext, useContext } from "react";

import type { AuthMeResponse } from "@/lib/api";

export interface DashboardSessionState {
  gated: boolean;
  loading: boolean;
  error: string | null;
  session: AuthMeResponse | null;
}

export const DashboardSessionContext =
  createContext<DashboardSessionState | null>(null);

export function useDashboardSession(): DashboardSessionState {
  const value = useContext(DashboardSessionContext);
  if (value === null) {
    throw new Error("useDashboardSession requires DashboardSessionProvider");
  }
  return value;
}
