import { useMemo } from "react";

import { flattenMenu } from "@/layouts/registry/adapter";
import { filterMenuForAgentCapabilities } from "@/layouts/registry/capabilities";
import { filterMenuForPermissions } from "@/layouts/registry/permissions";
import { partitionSidebarEntries } from "@/layouts/registry/sidebarEntries";
import { useMenuItems, useRoutes } from "@/plugins/registry/hooks";
import { useAgentStore } from "@/stores/agentStore";
import { useDeniedRouteIds } from "@/stores/hubPermissionsStore";

export function useSidebarEntryGroups() {
  const routes = useRoutes();
  const rawAgentMenu = useMenuItems("primary.agentScoped");
  const rawSettingsMenu = useMenuItems("primary.settings");
  const { selectedAgent, agents } = useAgentStore();
  const currentAgent = agents.find((agent) => agent.id === selectedAgent);
  const deniedRouteIds = useDeniedRouteIds();

  return useMemo(() => {
    const capabilities = currentAgent
      ? {
          ...currentAgent.backend_capabilities,
          workspace_ui:
            currentAgent.backend === "qwenpaw"
              ? currentAgent.backend_capabilities?.workspace_ui ?? true
              : false,
        }
      : undefined;
    return partitionSidebarEntries(
      flattenMenu(
        filterMenuForPermissions(
          filterMenuForAgentCapabilities(rawAgentMenu, capabilities),
          deniedRouteIds,
        ),
        routes,
        18,
      ),
      flattenMenu(
        filterMenuForPermissions(rawSettingsMenu, deniedRouteIds),
        routes,
        18,
      ),
    );
  }, [currentAgent, deniedRouteIds, rawAgentMenu, rawSettingsMenu, routes]);
}
