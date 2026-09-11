// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  logout: vi.fn(),
  session: {
    gated: true,
    loading: false,
    error: null as string | null,
    session: {
      user_id: "matt",
      email: "matt@example.test",
      display_name: "Matt",
      org_id: "fcar",
      provider: "fcar-access",
      expires_at: 4_000_000_000,
      surface: "plugin:fcar-command",
    },
  },
  manifests: [] as Array<Record<string, unknown>>,
  sidebarStatusCalls: vi.fn(),
  usePlugins: vi.fn(() => ({
    loading: false,
    manifests: mocks.manifests,
    plugins: [],
  })),
}));

vi.mock("@/contexts/DashboardSessionProvider", () => ({
  DashboardSessionProvider: ({ children }: { children: React.ReactNode }) =>
    children,
}));
vi.mock("@/contexts/dashboard-session-context", () => ({
  useDashboardSession: () => mocks.session,
}));
vi.mock("@/plugins", () => ({
  PluginPage: ({ name }: { name: string }) => (
    <div data-testid="plugin-page">plugin:{name}</div>
  ),
  PluginSlot: () => null,
  usePlugins: mocks.usePlugins,
}));
vi.mock("@/lib/api", () => ({
  api: { getConfig: mocks.getConfig, logout: mocks.logout },
}));
vi.mock("@/contexts/ProfileProvider", () => ({
  ProfileProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="profile-provider">{children}</div>
  ),
}));
vi.mock("@/hooks/useSidebarStatus", () => ({
  useSidebarStatus: () => {
    mocks.sidebarStatusCalls();
    return null;
  },
}));
vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({
    profile: "",
    currentProfile: "default",
    profiles: [],
    setProfile: vi.fn(),
  }),
}));
vi.mock("@/contexts/useSystemActions", () => ({
  useSystemActions: () => ({
    activeAction: null,
    isBusy: false,
    isRunning: false,
    pendingAction: null,
    runAction: vi.fn(),
  }),
}));
vi.mock("@/themes", () => ({
  useTheme: () => ({ theme: {} }),
}));
vi.mock("@/components/ThemeSwitcher", () => ({ ThemeSwitcher: () => null }));
vi.mock("@/components/LanguageSwitcher", () => ({ LanguageSwitcher: () => null }));
vi.mock("@/components/AuthWidget", () => ({ AuthWidget: () => null }));
vi.mock("@/components/SidebarFooter", () => ({ SidebarFooter: () => null }));
vi.mock("@/components/SidebarStatusStrip", () => ({
  SidebarStatusStrip: () => null,
  gatewayLine: () => null,
}));
vi.mock("@/components/ProfileSwitcher", () => ({ ProfileSwitcher: () => null }));
vi.mock("@/components/ProfileScopeBanner", () => ({
  ProfileScopeBanner: () => null,
}));
vi.mock("@/components/MemoryPressureBanner", () => ({
  MemoryPressureBanner: () => null,
}));
vi.mock("@/pages/SessionsPage", () => ({
  default: () => <div>Hermes sessions</div>,
}));
vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: { app: { nav: {} }, common: {}, status: {}, language: {} },
  }),
}));

import App from "./App";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.stubGlobal("matchMedia", () => ({
    addEventListener() {},
    matches: false,
    removeEventListener() {},
  }));
  sessionStorage.setItem("hermes:plugin-manifests", "privileged-cache");
  mocks.session.gated = true;
  mocks.session.loading = false;
  mocks.session.error = null;
  mocks.session.session = {
    user_id: "matt",
    email: "matt@example.test",
    display_name: "Matt",
    org_id: "fcar",
    provider: "fcar-access",
    expires_at: 4_000_000_000,
    surface: "plugin:fcar-command",
  };
  mocks.manifests = [
    {
      name: "fcar-command",
      label: "FCAR Command",
      description: "Scoped dashboard",
      icon: "Shield",
      version: "1.0.0",
      tab: { path: "/fcar-command" },
      entry: "dist/index.js",
      has_api: true,
      presentation: "workspace",
      source: "user",
    },
  ];
  mocks.getConfig.mockReset();
  mocks.getConfig.mockResolvedValue({ dashboard: {} });
  mocks.logout.mockReset();
  mocks.sidebarStatusCalls.mockReset();
  mocks.usePlugins.mockClear();
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

async function renderAt(path: string) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
  });
}

describe("plugin-scoped dashboard shell", () => {
  it("mounts only the scoped plugin and account controls", async () => {
    await renderAt("/fcar-command");

    expect(container.textContent).toContain("plugin:fcar-command");
    expect(container.textContent).toContain("Matt");
    expect(container.textContent).not.toContain("Back to Hermes");
    expect(
      container.querySelector('[data-testid="profile-provider"]'),
    ).toBeNull();
    expect(mocks.getConfig).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("hermes:plugin-manifests")).toBeNull();
  });

  it("redirects privileged and unknown client routes to the plugin page", async () => {
    await renderAt("/config");

    expect(container.textContent).toContain("plugin:fcar-command");
    expect(container.textContent).not.toContain("Config");
  });

  it("fails closed before plugin discovery for an unknown surface", async () => {
    mocks.session.session = {
      ...mocks.session.session,
      surface: "dashboard:admin",
    };

    await renderAt("/");

    expect(container.textContent).toContain("Access denied");
    expect(mocks.usePlugins).not.toHaveBeenCalled();
  });
});

describe("workspace plugin presentation", () => {
  beforeEach(() => {
    mocks.session.session = {
      ...mocks.session.session,
      surface: "dashboard",
    };
    mocks.manifests = [
      {
        ...mocks.manifests[0],
        presentation: "workspace",
      },
    ];
  });

  it("renders the opted-in plugin route without native dashboard providers", async () => {
    await renderAt("/fcar-command");

    expect(container.textContent).toContain("plugin:fcar-command");
    expect(container.textContent).toContain("Back to Hermes");
    expect(
      container.querySelector('[data-testid="profile-provider"]'),
    ).toBeNull();
    expect(mocks.sidebarStatusCalls).not.toHaveBeenCalled();
  });

  it("uses the full Hermes app on normal routes", async () => {
    await renderAt("/sessions");

    expect(
      container.querySelector('[data-testid="profile-provider"]'),
    ).not.toBeNull();
    expect(mocks.sidebarStatusCalls).toHaveBeenCalled();
  });

  it("does not activate workspace presentation for unknown values", async () => {
    mocks.manifests = [
      {
        ...mocks.manifests[0],
        presentation: "unknown",
      },
    ];

    await renderAt("/fcar-command");

    expect(
      container.querySelector('[data-testid="profile-provider"]'),
    ).not.toBeNull();
  });
});
