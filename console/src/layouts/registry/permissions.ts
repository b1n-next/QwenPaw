import type { MenuItem } from "../../plugins/registry/types";

/** Shape of GET /api/hub/me/permissions (hub deployments only). */
export interface HubPermissions {
  role: string;
  denied_groups: string[];
  denied_routes: string[];
  /** EP-1-3: user role must not get model switching/adding affordances. */
  model_readonly?: boolean;
}

type MenuTreeItem = MenuItem & { __children?: MenuItem[] };

/**
 * Filter built-in menu items against the hub role deny-list.
 *
 * Mirrors `filterMenuForAgentCapabilities`: `deniedIds === null` means
 * "no hub permission info" (standard deployment, old backend, or fetch
 * failure) and passes everything through unchanged. Group headers are
 * dropped when every child has been filtered out.
 */
export function filterMenuForPermissions(
  items: MenuItem[],
  deniedIds: Set<string> | null,
): MenuItem[] {
  if (!deniedIds || deniedIds.size === 0) return items;

  return items.flatMap((item) => {
    if (deniedIds.has(item.id)) return [];

    const treeItem = item as MenuTreeItem;
    if (!treeItem.__children) return [item];

    const children = filterMenuForPermissions(treeItem.__children, deniedIds);
    // Group header left without any visible child is removed too.
    if (children.length === 0 && treeItem.isGroup) return [];
    return [{ ...treeItem, __children: children }];
  });
}

/**
 * Registry route paths (e.g. "/files", "/settings/*") that belong to a
 * denied menu route id — used by the route guard in MainLayout.
 */
export function deniedPathsForRoutes(
  routeIdsById: Map<string, string>,
  deniedIds: Set<string> | null,
): Set<string> {
  const deniedPaths = new Set<string>();
  if (!deniedIds) return deniedPaths;
  for (const id of deniedIds) {
    const path = routeIdsById.get(id);
    if (path) deniedPaths.add(normalizeRoutePath(path));
  }
  return deniedPaths;
}

/** "/settings/*" → "/settings"; "/files" → "/files". */
export function normalizeRoutePath(routePath: string): string {
  const withoutWildcard = routePath.replace(/\*+$/, "");
  return withoutWildcard.replace(/\/+$/, "") || "/";
}

/** True when *pathname* targets a denied page (exact or nested match). */
export function isPathDenied(
  pathname: string,
  deniedPaths: Set<string>,
): boolean {
  for (const denied of deniedPaths) {
    if (denied === "/") continue;
    if (pathname === denied || pathname.startsWith(`${denied}/`)) {
      return true;
    }
  }
  return false;
}
