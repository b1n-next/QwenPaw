import { useMemo } from "react";
import { create } from "zustand";

import { request } from "../api/request";
import type { HubPermissions } from "../layouts/registry/permissions";

/**
 * Hub role permissions for the signed-in user (UX only — the hub proxy
 * ACL is the real security boundary). One fetch per page load; failures
 * degrade to "no filtering" and never block rendering.
 */

type LoadStatus = "idle" | "loading" | "ready" | "error";

interface HubPermissionsStore {
  status: LoadStatus;
  role: string | null;
  deniedGroups: string[];
  /** null = permissions unknown → console must not filter menus. */
  deniedRouteIds: string[] | null;
  load: () => Promise<void>;
}

let loadPromise: Promise<void> | null = null;

export const useHubPermissionsStore = create<HubPermissionsStore>((set) => ({
  status: "idle",
  role: null,
  deniedGroups: [],
  deniedRouteIds: null,
  load: async () => {
    if (loadPromise) return loadPromise;
    set({ status: "loading" });
    loadPromise = request<HubPermissions>("/hub/me/permissions")
      .then((payload) => {
        set({
          status: "ready",
          role: payload?.role ?? null,
          deniedGroups: Array.isArray(payload?.denied_groups)
            ? payload.denied_groups
            : [],
          deniedRouteIds: Array.isArray(payload?.denied_routes)
            ? payload.denied_routes
            : [],
        });
      })
      .catch(() => {
        // Standard deployment, old backend, or transient failure: keep
        // the full menu. The hub proxy still enforces the real ACL.
        set({ status: "error", deniedRouteIds: null });
      })
      .finally(() => {
        loadPromise = null;
      });
    return loadPromise;
  },
}));

/** Convenience selector: stable Set for filtering, null when unknown. */
export function useDeniedRouteIds(): Set<string> | null {
  const ids = useHubPermissionsStore((state) => state.deniedRouteIds);
  return useMemo(() => (ids ? new Set(ids) : null), [ids]);
}
