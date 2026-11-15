import { CombinedError } from "urql";
import { describe, expect, it } from "vitest";

import { describe as describeProblem, isSignedOut, newIdempotencyKey } from "./client";
import { DEFAULT_CONFIG, loadConfig, tenantFromHost } from "./config";
import { explain, formatCents, parseCents } from "./format";

describe("config", () => {
  it("merges /config.json over the defaults", async () => {
    const fetchImpl = (async () =>
      new Response(JSON.stringify({ signIn: { signInUrl: "/x" } }))) as unknown as typeof fetch;
    const loaded = await loadConfig(fetchImpl);
    expect(loaded.graphqlPath).toBe("/graphql");
    expect(loaded.signIn).toEqual({ ...DEFAULT_CONFIG.signIn, signInUrl: "/x" });
  });

  it("falls back to the defaults when /config.json can't be read", async () => {
    const failing = (async () => {
      throw new TypeError("offline");
    }) as unknown as typeof fetch;
    expect(await loadConfig(failing)).toEqual(DEFAULT_CONFIG);
  });

  it.each([
    ["acme.localhost", "acme"],
    ["ACME.platform.com", "acme"],
    ["localhost", null],
  ])("the tenant of %s is %s", (host, tenant) => {
    expect(tenantFromHost(host)).toBe(tenant);
  });
});

describe("errors", () => {
  it("reads the API's error shape", () => {
    const error = new CombinedError({
      graphQLErrors: [{ message: "slow down", extensions: { code: "rate_limited", status: 429, retryAfter: 4 } }],
    });
    expect(describeProblem(error)).toEqual({ code: "rate_limited", message: "slow down", retryAfter: 4, status: 429 });
  });

  it("handles network failures and unknown shapes", () => {
    const network = describeProblem(new CombinedError({ networkError: new Error("offline") }));
    expect(network.code).toBe("network_error");
    const odd = describeProblem(new CombinedError({ graphQLErrors: [{ message: "x" }] }));
    expect(odd).toMatchObject({ code: "error", retryAfter: null, status: null });
    expect(isSignedOut({ code: "x", message: "", retryAfter: null, status: 401 })).toBe(true);
  });

  it.each([
    ["tenant_migrating", /being moved/],
    ["rate_limited", /too many requests/i],
    ["cell_overloaded", /busy/],
    ["cell_unavailable", /unavailable/],
    ["network_error", /can't reach/i],
    ["something_else", /refused: something_else/],
  ])("explains %s", (code, expected) => {
    expect(explain({ code, message: `refused: ${code}`, retryAfter: null, status: 500 })).toMatch(expected);
  });

  it("idempotency keys are unique per action", () => {
    expect(newIdempotencyKey()).not.toEqual(newIdempotencyKey());
  });
});

describe("money", () => {
  it.each([
    ["1", 100],
    ["12.5", 1250],
    ["0.99", 99],
  ])("parses %s as %s cents", (text, cents) => {
    expect(parseCents(text)).toBe(cents);
  });

  it.each(["", "abc", "-1", "1.234"])("refuses %s", (text) => {
    expect(parseCents(text)).toBeNull();
  });

  it("formats cents", () => {
    expect(formatCents(2500)).toBe("$25.00");
  });
});
