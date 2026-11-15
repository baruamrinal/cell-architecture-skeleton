import { Client, cacheExchange, fetchExchange, type CombinedError } from "urql";

/** mgr refuses mutations without it, so a cross-site form can't trigger one. */
export const CSRF_HEADER = "X-Cellarch-CSRF";

/**
 * The GraphQL client. Queries go as GET, mutations as POST: during a tenant move the
 * router and the cell pause writes by method, so reads keep working. The session is
 * the HttpOnly cookie of this tenant's host; requests stay on that host.
 */
export function createGraphqlClient(url: string): Client {
  return new Client({
    url,
    exchanges: [cacheExchange, fetchExchange],
    preferGetMethod: "within-url-limit",
    fetchOptions: { credentials: "same-origin", headers: { [CSRF_HEADER]: "1" } },
  });
}

export interface Problem {
  code: string;
  message: string;
  retryAfter: number | null;
  status: number | null;
}

interface Extensions {
  code?: unknown;
  status?: unknown;
  retryAfter?: unknown;
}

/** The API's error shape (errors[].extensions), whichever layer refused the request. */
export function describe(error: CombinedError): Problem {
  const first = error.graphQLErrors[0];
  const extensions = (first?.extensions ?? {}) as Extensions;
  const response = error.response as Response | undefined;
  const status = typeof extensions.status === "number" ? extensions.status : (response?.status ?? null);
  return {
    code: typeof extensions.code === "string" ? extensions.code : error.networkError ? "network_error" : "error",
    message: first?.message ?? error.networkError?.message ?? error.message,
    retryAfter: typeof extensions.retryAfter === "number" ? extensions.retryAfter : null,
    status,
  };
}

export function isSignedOut(problem: Problem): boolean {
  return problem.code === "unauthenticated" || problem.status === 401;
}

/** One key per user action: a retry of the same action sends the same key. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
