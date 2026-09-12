import { describe, expect, it } from "vitest";
import type { MenuItem } from "../../plugins/registry/types";
import {
  deniedPathsForRoutes,
  filterMenuForPermissions,
  isPathDenied,
  normalizeRoutePath,
} from "./permissions";

const items: MenuItem[] = [
  { id: "core.chat", location: "primary.agentScoped", label: "Chat" },
  {
    id: "core.workspace-group",
    location: "primary.agentScoped",
    label: "Workspace",
    isGroup: true,
    __children: [
      { id: "core.workspace", location: "primary.agentScoped", label: "Files" },
      { id: "core.files", location: "primary.agentScoped", label: "Files" },
    ],
  } as MenuItem,
  {
    id: "core.settings-group",
    location: "primary.settings",
    label: "Settings",
    isGroup: true,
    __children: [
      { id: "core.models", location: "primary.settings", label: "Models" },
      { id: "core.backups", location: "primary.settings", label: "Backups" },
    ],
  } as MenuItem,
  { id: "core.import", location: "primary.agentScoped", label: "Import" },
];

describe("filterMenuForPermissions", () => {
  it("passes everything through when permissions are unknown", () => {
    expect(filterMenuForPermissions(items, null)).toEqual(items);
  });

  it("passes everything through when the deny-list is empty (admin)", () => {
    expect(filterMenuForPermissions(items, new Set())).toEqual(items);
  });

  it("drops denied leaf items and keeps allowed siblings", () => {
    const filtered = filterMenuForPermissions(items, new Set(["core.import"]));
    expect(filtered.map((item) => item.id)).toEqual([
      "core.chat",
      "core.workspace-group",
      "core.settings-group",
    ]);
  });

  it("drops a group whose children are all denied", () => {
    const filtered = filterMenuForPermissions(
      items,
      new Set(["core.workspace", "core.files"]),
    );
    expect(filtered.map((item) => item.id)).toEqual([
      "core.chat",
      "core.settings-group",
      "core.import",
    ]);
  });

  it("keeps a group when at least one child remains", () => {
    const filtered = filterMenuForPermissions(items, new Set(["core.models"]));
    const group = filtered.find((item) => item.id === "core.settings-group") as
      | { __children?: MenuItem[] }
      | undefined;
    expect(group).toBeDefined();
    expect(group?.__children?.map((child) => child.id)).toEqual([
      "core.backups",
    ]);
  });

  it("denying a group id drops it even if children are not listed", () => {
    const filtered = filterMenuForPermissions(
      items,
      new Set(["core.settings-group"]),
    );
    expect(filtered.map((item) => item.id)).not.toContain(
      "core.settings-group",
    );
  });
});

describe("route guard helpers", () => {
  it("normalizes wildcard and trailing slashes", () => {
    expect(normalizeRoutePath("/settings/*")).toBe("/settings");
    expect(normalizeRoutePath("/files/")).toBe("/files");
    expect(normalizeRoutePath("/")).toBe("/");
  });

  it("maps denied menu ids to registry paths", () => {
    const routes = new Map<string, string>([
      ["core.files", "/files"],
      ["core.settings-center", "/settings/*"],
      ["core.chat", "/chat/*"],
    ]);
    const denied = deniedPathsForRoutes(
      routes,
      new Set(["core.files", "core.settings-center", "core.unknown"]),
    );
    expect([...denied].sort()).toEqual(["/files", "/settings"]);
    expect(deniedPathsForRoutes(routes, null).size).toBe(0);
  });

  it("matches exact and nested paths only", () => {
    const denied = new Set(["/files", "/settings"]);
    expect(isPathDenied("/files", denied)).toBe(true);
    expect(isPathDenied("/files/sub", denied)).toBe(true);
    expect(isPathDenied("/settings/general", denied)).toBe(true);
    expect(isPathDenied("/fileserver", denied)).toBe(false);
    expect(isPathDenied("/chat/x", denied)).toBe(false);
    expect(isPathDenied("/filesx", denied)).toBe(false);
  });
});
