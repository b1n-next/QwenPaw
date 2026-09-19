/**
 * mobileChatOnly.test.ts — B7 pure-logic tests.
 */
import { describe, expect, it } from "vitest";
import { isPathMobileAllowed } from "./mobileChatOnly";

describe("isPathMobileAllowed (B7 chat-centric mobile view)", () => {
  it("keeps the conversation surface", () => {
    expect(isPathMobileAllowed("/chat")).toBe(true);
    expect(isPathMobileAllowed("/chat/session-1")).toBe(true);
  });

  it("keeps the approval inbox reachable on mobile", () => {
    expect(isPathMobileAllowed("/inbox")).toBe(true);
    expect(isPathMobileAllowed("/inbox?tab=approvals")).toBe(true);
  });

  it("redirects management and admin surfaces", () => {
    for (const path of [
      "/",
      "/settings/models",
      "/files",
      "/skills",
      "/market",
      "/hub",
      "/debug",
      "/cron-jobs",
    ]) {
      expect(isPathMobileAllowed(path)).toBe(false);
    }
  });
});
