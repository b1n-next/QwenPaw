/**
 * mobileChatOnly.ts — B7: thin/mobile clients get a chat-centric view.
 *
 * On viewports <= 768px everything except the conversation surface and
 * the approval inbox redirects to /chat. The full desktop experience
 * (management, config, hub admin) stays one window-resize away — this
 * is a UX focus decision, not an access-control one (the hub ACL
 * remains the enforcement plane).
 */

export const MOBILE_CHAT_ONLY_QUERY = "(max-width: 768px)";

/** Paths (by prefix) that stay reachable on mobile. */
const MOBILE_ALLOWED_PREFIXES = ["/chat", "/inbox"];

export function isPathMobileAllowed(pathname: string): boolean {
  const normalized = (pathname || "/").split("?")[0];
  return MOBILE_ALLOWED_PREFIXES.some((prefix) =>
    normalized.startsWith(prefix),
  );
}

export function isMobileChatOnlyViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(MOBILE_CHAT_ONLY_QUERY).matches
  );
}
