import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse } from "msw";
import type { ReactNode } from "react";
import { Provider } from "urql";
import { describe, expect, it } from "vitest";

import { CSRF_HEADER, createGraphqlClient } from "../client";
import { graphql, order, problem, user, usersPage } from "../test/fixtures";
import { server } from "../test/setup";
import { Orders } from "./Orders";
import { UserDetail } from "./UserDetail";
import { Users } from "./Users";

function withClient(node: ReactNode) {
  const client = createGraphqlClient(new URL("/graphql", window.location.origin).toString());
  return render(<Provider value={client}>{node}</Provider>);
}

describe("Users", () => {
  it("lists the tenant's users and pages through them", async () => {
    const seen: (string | null)[] = [];
    server.use(
      graphql.query("Users", ({ variables }) => {
        seen.push((variables.after as string | undefined) ?? null);
        return variables.after
          ? HttpResponse.json({ data: { users: { items: [user("usr_3")], nextCursor: null } } })
          : HttpResponse.json({
              data: { users: { items: [user("usr_1"), user("usr_2")], nextCursor: "c2" } },
            });
      }),
    );
    withClient(<Users />);
    expect(await screen.findByText("usr_1@acme.test")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(await screen.findByText("usr_3@acme.test")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /next/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /first page/i }));
    expect(await screen.findByText("usr_1@acme.test")).toBeInTheDocument();
    expect(seen).toContain("c2");
  });

  it("shows an empty tenant", async () => {
    server.use(usersPage([]));
    withClient(<Users />);
    expect(await screen.findByText(/no users yet/i)).toBeInTheDocument();
  });

  it("creates a user with an idempotency key and the anti-CSRF header, as a POST", async () => {
    const created: { input: unknown; key: unknown; method: string; csrf: string | null }[] = [];
    server.use(
      usersPage([]),
      graphql.mutation("CreateUser", ({ variables, request }) => {
        created.push({
          input: variables.input,
          key: variables.key,
          method: request.method,
          csrf: request.headers.get(CSRF_HEADER),
        });
        return HttpResponse.json({ data: { createUser: { id: "usr_9", email: "a@acme.test", name: "A" } } });
      }),
    );
    withClient(<Users />);
    await userEvent.type(await screen.findByLabelText(/email/i), "a@acme.test");
    await userEvent.type(screen.getByLabelText(/^name/i), "A");
    await userEvent.click(screen.getByRole("button", { name: /add user/i }));
    await waitFor(() => expect(created).toHaveLength(1));
    const [call] = created;
    expect(call).toMatchObject({ input: { email: "a@acme.test", name: "A" }, method: "POST", csrf: "1" });
    expect(typeof call?.key).toBe("string");
    expect(await screen.findByRole("status")).toHaveTextContent(/added a@acme.test/i);
  });

  it("explains a refused create, e.g. while the tenant is being moved", async () => {
    server.use(
      usersPage([]),
      graphql.mutation("CreateUser", () => problem("tenant_migrating", 503, 5)),
    );
    withClient(<Users />);
    await userEvent.type(await screen.findByLabelText(/email/i), "a@acme.test");
    await userEvent.type(screen.getByLabelText(/^name/i), "A");
    await userEvent.click(screen.getByRole("button", { name: /add user/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/being moved/i);
    expect(alert).toHaveTextContent(/5 s/);
  });

  it("deletes a user", async () => {
    const deleted: unknown[] = [];
    server.use(
      usersPage([user("usr_1")]),
      graphql.mutation("DeleteUser", ({ variables }) => {
        deleted.push(variables.id);
        return HttpResponse.json({ data: { deleteUser: true } });
      }),
    );
    withClient(<Users />);
    const row = (await screen.findByText("usr_1@acme.test")).closest("tr") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /delete/i }));
    await waitFor(() => expect(deleted).toEqual(["usr_1"]));
  });

  it("shows a rate limit with when to retry", async () => {
    server.use(graphql.query("Users", () => problem("rate_limited", 429, 3)));
    withClient(<Users />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/too many requests/i);
    expect(alert).toHaveTextContent(/3 s/);
  });
});

describe("UserDetail", () => {
  const detail = (orders = [order("ord_1")]) =>
    graphql.query("User", () =>
      HttpResponse.json({ data: { user: user("usr_1"), orders: { items: orders, nextCursor: null } } }),
    );

  it("shows the user and their orders, and places an order", async () => {
    const placed: unknown[] = [];
    server.use(
      detail(),
      graphql.mutation("CreateOrder", ({ variables }) => {
        placed.push(variables);
        return HttpResponse.json({ data: { createOrder: { id: "ord_2", status: "placed", totalCents: 300 } } });
      }),
    );
    withClient(<UserDetail userId="usr_1" />);
    expect(await screen.findByRole("heading", { name: /USR_1/ })).toBeInTheDocument();
    expect(screen.getByText("ord_1")).toBeInTheDocument();
    expect(screen.getByText("$25.00")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/sku/i), "pen");
    await userEvent.clear(screen.getByLabelText(/quantity/i));
    await userEvent.type(screen.getByLabelText(/quantity/i), "3");
    await userEvent.clear(screen.getByLabelText(/unit price/i));
    await userEvent.type(screen.getByLabelText(/unit price/i), "1.00");
    await userEvent.click(screen.getByRole("button", { name: /place order/i }));
    await waitFor(() => expect(placed).toHaveLength(1));
    expect(placed[0]).toMatchObject({
      input: { userId: "usr_1", lines: [{ sku: "pen", quantity: 3, unitPriceCents: 100 }] },
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/placed ord_2/i);
  });

  it("cancels an order", async () => {
    const cancelled: unknown[] = [];
    server.use(
      detail(),
      graphql.mutation("CancelOrder", ({ variables }) => {
        cancelled.push(variables.id);
        return HttpResponse.json({ data: { cancelOrder: { id: "ord_1", status: "cancelled" } } });
      }),
    );
    withClient(<UserDetail userId="usr_1" />);
    await userEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(cancelled).toEqual(["ord_1"]));
  });

  it("says when the user doesn't exist (or belongs to another tenant)", async () => {
    server.use(
      graphql.query("User", () =>
        HttpResponse.json({ data: { user: null, orders: { items: [], nextCursor: null } } }),
      ),
    );
    withClient(<UserDetail userId="usr_x" />);
    expect(await screen.findByText(/no such user/i)).toBeInTheDocument();
  });

  it("refuses a malformed price before sending anything", async () => {
    server.use(detail([]));
    withClient(<UserDetail userId="usr_1" />);
    await userEvent.type(await screen.findByLabelText(/sku/i), "pen");
    await userEvent.clear(screen.getByLabelText(/unit price/i));
    await userEvent.type(screen.getByLabelText(/unit price/i), "abc");
    await userEvent.click(screen.getByRole("button", { name: /place order/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/price/i);
  });

  it("shows errors from placing and cancelling", async () => {
    server.use(
      detail(),
      graphql.mutation("CreateOrder", () => problem("unknown_user", 422)),
      graphql.mutation("CancelOrder", () => problem("order_already_cancelled", 409)),
    );
    withClient(<UserDetail userId="usr_1" />);
    await userEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/refused: order_already_cancelled/);
    await userEvent.type(screen.getByLabelText(/sku/i), "pen");
    await userEvent.click(screen.getByRole("button", { name: /place order/i }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/refused: unknown_user/),
    );
  });
});

describe("Orders", () => {
  it("lists orders, pages through them and cancels one", async () => {
    const cancelled: unknown[] = [];
    server.use(
      graphql.query("Orders", ({ variables }) =>
        variables.after
          ? HttpResponse.json({ data: { orders: { items: [order("ord_3", "usr_1", "cancelled")], nextCursor: null } } })
          : HttpResponse.json({ data: { orders: { items: [order("ord_1")], nextCursor: "c" } } }),
      ),
      graphql.mutation("CancelOrder", ({ variables }) => {
        cancelled.push(variables.id);
        return HttpResponse.json({ data: { cancelOrder: { id: "ord_1", status: "cancelled" } } });
      }),
    );
    withClient(<Orders />);
    await userEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(cancelled).toEqual(["ord_1"]));
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(await screen.findByText("ord_3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();
  });

  it("shows an empty list and errors", async () => {
    server.use(
      graphql.query("Orders", () => HttpResponse.json({ data: { orders: { items: [], nextCursor: null } } })),
    );
    withClient(<Orders />);
    expect(await screen.findByText(/no orders yet/i)).toBeInTheDocument();
  });

  it("shows when listing fails", async () => {
    server.use(graphql.query("Orders", () => problem("cell_overloaded", 503, 1)));
    withClient(<Orders />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/busy/i);
  });
});
