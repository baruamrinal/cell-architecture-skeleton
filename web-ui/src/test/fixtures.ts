import { http, HttpResponse } from "msw";
import { graphql as gql } from "msw/graphql";

/** mgr's GraphQL endpoint on this tenant's host (as the browser calls it). */
export const graphql = gql.link("http://acme.localhost:8080/graphql");

export const ME = { tenantId: "acme", cellId: "cell-1" };

export function user(id: string, email = `${id}@acme.test`) {
  return {
    id,
    email,
    name: id.toUpperCase(),
    createdAt: "2026-11-14T10:00:00Z",
    orders: [] as { id: string; status: string }[],
  };
}

export function order(id: string, userId = "usr_1", status = "placed") {
  return {
    id,
    userId,
    status,
    totalCents: 2500,
    createdAt: "2026-11-14T10:00:00Z",
    lines: [{ sku: "book-1", quantity: 2, unitPriceCents: 1250 }],
  };
}

/** A GraphQL error as mgr, the gateway or the router sends it. */
export function problem(code: string, status: number, retryAfter?: number) {
  const extensions: Record<string, unknown> = { code, status };
  if (retryAfter !== undefined) extensions.retryAfter = retryAfter;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (retryAfter !== undefined) headers["Retry-After"] = String(retryAfter);
  const body = { errors: [{ message: `refused: ${code}`, extensions }], data: null };
  return new Response(JSON.stringify(body), { status, headers });
}

export const config = http.get("/config.json", () =>
  HttpResponse.json({
    graphqlPath: "/graphql",
    signIn: { provider: "dev", signInUrl: "/auth/dev/sign-in", signOutUrl: "/auth/sign-out" },
  }),
);

export const signedIn = graphql.query("Me", () => HttpResponse.json({ data: { me: ME } }));

export function usersPage(items = [user("usr_1"), user("usr_2")], nextCursor: string | null = null) {
  return graphql.query("Users", () => HttpResponse.json({ data: { users: { items, nextCursor } } }));
}
