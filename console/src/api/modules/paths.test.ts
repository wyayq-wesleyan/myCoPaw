import { afterEach, describe, expect, it, vi } from "vitest";
import { pathTree } from "./paths";

/**
 * The failure that a deploy shows before the app is restarted: new static assets,
 * old backend, so the paths endpoint 404s.
 *
 * The panel used to render the raw response body, and the user saw
 * `{"detail":"Not Found"}` — technically accurate and completely unactionable.
 * These tests pin that the message names the cause instead.
 */
describe("paths API failure messages", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("explains a 404 as a stale backend rather than echoing the body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"Not Found"}', { status: 404 })),
    );

    await expect(pathTree()).rejects.toThrow(/重启应用/);
    // The raw JSON must never reach the user.
    await expect(pathTree()).rejects.not.toThrow(/\{"detail"/);
  });

  it("maps an expired session to a re-login hint", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 401 })),
    );
    await expect(pathTree()).rejects.toThrow(/重新登录/);
  });

  it("keeps the status and the detail for any other failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    const err = await pathTree().catch((e) => e);
    expect(err.status).toBe(500);
    expect(err.message).toContain("500");
  });
});
