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
  /** EP-1-3: hide model switching/adding affordances (UX only). */
  modelReadonly: boolean;
  load: () => Promise<void>;
}

let loadPromise: Promise<void> | null = null;

export const useHubPermissionsStore = create<HubPermissionsStore>((set) => ({
  status: "idle",
  role: null,
  deniedGroups: [],
  deniedRouteIds: null,
  modelReadonly: false,
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
          modelReadonly: payload?.model_readonly === true,
        });
      })
      .catch(() =>
        // Direct-runtime deployment (B6/EP-2-9): the runtime may pin
        // a restricted profile via QWENPAW_CONSOLE_PROFILE=restricted.
        // Probe it before degrading to the full menu.
        request<HubPermissions>("/console/profile")
          .then((payload) => {
            set({
              status: "ready",
              role: payload?.role ?? "user",
              deniedGroups: Array.isArray(payload?.denied_groups)
                ? payload.denied_groups
                : [],
              deniedRouteIds: Array.isArray(payload?.denied_routes)
                ? payload.denied_routes
                : [],
              modelReadonly: payload?.model_readonly !== false,
            });
          })
          .catch(() => {
            // Standard deployment, old backend, or transient failure:
            // keep the full menu. The hub proxy still enforces ACL.
            set({ status: "error", deniedRouteIds: null });
          }),
      )
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

/** EP-1-3: true when the signed-in user must not touch model config. */
export function useModelReadonly(): boolean {
  return useHubPermissionsStore((state) => state.modelReadonly);
}
