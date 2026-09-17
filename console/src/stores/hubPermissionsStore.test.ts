import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const requestMock = vi.fn();

vi.mock("../api/request", () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));

const loadStore = async () => {
  const mod = await import("./hubPermissionsStore");
  const store = mod.useHubPermissionsStore;
  await store.getState().load();
  const state = store.getState();
  return {
    status: state.status,
    role: state.role,
    deniedGroups: state.deniedGroups,
    deniedRouteIds: state.deniedRouteIds,
    modelReadonly: state.modelReadonly,
  };
};

describe("hubPermissionsStore degradation chain", () => {
  beforeEach(() => {
    requestMock.mockReset();
  });
  afterEach(() => {
    vi.resetModules();
  });

  it("uses the hub payload when present", async () => {
    requestMock.mockResolvedValueOnce({
      role: "admin",
      denied_groups: [],
      denied_routes: [],
      model_readonly: false,
    });
    const state = await loadStore();
    expect(state.status).toBe("ready");
    expect(state.role).toBe("admin");
    expect(requestMock).toHaveBeenCalledTimes(1);
    expect(requestMock).toHaveBeenCalledWith("/hub/me/permissions");
  });

  it("falls back to the runtime restricted profile (B6)", async () => {
    requestMock
      .mockRejectedValueOnce(new Error("404"))
      .mockResolvedValueOnce({
        profile: "restricted",
        role: "user",
        denied_groups: ["workspace"],
        denied_routes: ["core.import"],
        model_readonly: true,
      });
    const state = await loadStore();
    expect(requestMock).toHaveBeenCalledTimes(2);
    expect(requestMock).toHaveBeenNthCalledWith(2, "/console/profile");
    expect(state.status).toBe("ready");
    expect(state.role).toBe("user");
    expect(state.deniedGroups).toEqual(["workspace"]);
    expect(state.deniedRouteIds).toEqual(["core.import"]);
    expect(state.modelReadonly).toBe(true);
  });

  it("keeps the full menu when both probes fail", async () => {
    requestMock
      .mockRejectedValueOnce(new Error("404"))
      .mockRejectedValueOnce(new Error("404"));
    const state = await loadStore();
    expect(state.status).toBe("error");
    expect(state.deniedRouteIds).toBeNull();
  });

  it("treats a restricted profile without model flag as readonly", async () => {
    requestMock
      .mockRejectedValueOnce(new Error("404"))
      .mockResolvedValueOnce({
        profile: "restricted",
        role: "user",
        denied_groups: [],
        denied_routes: [],
      });
    const state = await loadStore();
    expect(state.modelReadonly).toBe(true);
  });
});
