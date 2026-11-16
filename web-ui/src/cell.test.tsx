import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { config, graphql, me, order, user, usersPage } from "./test/fixtures";
import { server } from "./test/setup";

const MOVING = me({ status: "MOVING" });

function signedInAs(profile: ReturnType<typeof me>) {
  return graphql.query("Me", () => HttpResponse.json({ data: { me: profile } }));
}

describe("A90: the tenant's cell, in the tenant's words", () => {
  it("A90 every screen shows the tenant, its address and its cell", async () => {
    server.use(config, signedInAs(me()), usersPage());
    render(<App />);

    const banner = await screen.findByRole("banner");
    expect(within(banner).getByText("acme")).toBeInTheDocument();
    expect(within(banner).getByText("acme.localhost:8080")).toBeInTheDocument();
    const badge = within(banner).getByRole("link", { name: /cell-1/ });
    expect(badge).toHaveTextContent(/shared cell/i);
    expect(badge).toHaveTextContent(/active/i);
    expect(badge).toHaveAttribute("href", "#/cell");

    const nav = screen.getByRole("navigation", { name: /main/i });
    for (const name of ["Users", "Orders", "Your cell"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { level: 1, name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toHaveTextContent(/served by cell-1/i);
  });

  it("A90 a dedicated cell says so", async () => {
    server.use(
      config,
      signedInAs(me({ cell: { id: "cell-acme", type: "DEDICATED", region: "eu-west-1" } })),
      usersPage(),
    );
    render(<App />);
    expect(await screen.findByRole("link", { name: /cell-acme/ })).toHaveTextContent(
      /dedicated cell/i,
    );
  });

  it("A90 'Your cell' explains the cell the tenant lives in", async () => {
    server.use(config, signedInAs(me({ cell: { id: "cell-1", type: "SHARED", region: "eu-west-1" } })));
    window.location.hash = "#/cell";
    render(<App />);

    expect(await screen.findByRole("heading", { level: 1, name: "Your cell" })).toBeInTheDocument();
    const facts = screen.getByRole("region", { name: /cell facts/i });
    expect(facts).toHaveTextContent("cell-1");
    expect(facts).toHaveTextContent(/shared/i);
    expect(facts).toHaveTextContent("eu-west-1");
    expect(facts).toHaveTextContent(/active/i);
    expect(screen.getByRole("heading", { name: /how a request reaches acme/i })).toBeInTheDocument();
    expect(screen.getByText(/cells never call each other/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /if acme is moved/i })).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: /move steps/i })).getAllByRole("listitem");
    expect(steps.map((s) => s.querySelector("strong")?.textContent)).toEqual([
      "Pause changes",
      "Copy",
      "Switch",
      "Done",
    ]);
    expect(
      within(screen.getByRole("navigation", { name: /main/i })).getByRole("link", { name: "Your cell" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("A90 'Your cell' for a dedicated cell, during a move", async () => {
    server.use(
      config,
      signedInAs(me({ status: "MOVING", cell: { id: "cell-acme", type: "DEDICATED", region: "local" } })),
    );
    window.location.hash = "#/cell";
    render(<App moveCheckMs={60_000} />);
    const facts = await screen.findByRole("region", { name: /cell facts/i });
    expect(facts).toHaveTextContent(/only acme lives here/i);
    expect(facts).toHaveTextContent(/being moved/i);
    expect(facts).toHaveTextContent(/reading only/i);
  });

  it("A90 while the tenant is moved, it is read-only, says why, and checks again by itself", async () => {
    let checks = 0;
    server.use(
      config,
      graphql.query("Me", () => {
        checks += 1;
        return HttpResponse.json({ data: { me: checks < 3 ? MOVING : me() } });
      }),
      usersPage([user("usr_1")]),
    );
    render(<App moveCheckMs={20} />);

    const notice = await screen.findByRole("status", { name: /being moved/i });
    expect(notice).toHaveTextContent(/acme is being moved to another cell/i);
    expect(notice).toHaveTextContent(/you can browse everything/i);
    expect(screen.getByRole("link", { name: /cell-1/ })).toHaveTextContent(/moving · read-only/i);

    const row = (await screen.findByText("usr_1@acme.test")).closest("tr") as HTMLElement;
    expect(within(row).getByRole("button", { name: /delete/i })).toBeDisabled();
    const add = screen.getByRole("button", { name: /add user/i });
    expect(add).toBeDisabled();
    expect(add).toHaveAccessibleDescription(/paused while acme is being moved/i);

    // The move finishes: the banner goes and changes are possible again.
    await waitFor(() => expect(screen.queryByRole("status", { name: /being moved/i })).toBeNull());
    expect(screen.getByRole("button", { name: /add user/i })).toBeEnabled();
    expect(checks).toBeGreaterThanOrEqual(3);
  });

  it("A90 orders and a user's page are read-only during a move too", async () => {
    server.use(
      config,
      signedInAs(MOVING),
      graphql.query("Orders", () =>
        HttpResponse.json({ data: { orders: { items: [order("ord_1")], nextCursor: null } } }),
      ),
      graphql.query("User", () =>
        HttpResponse.json({
          data: { user: user("usr_1"), orders: { items: [order("ord_1")], nextCursor: null } },
        }),
      ),
    );
    window.location.hash = "#/orders";
    render(<App moveCheckMs={60_000} />);
    expect(await screen.findByRole("button", { name: /cancel/i })).toBeDisabled();

    await userEvent.click(screen.getByRole("link", { name: "usr_1" }));
    expect(await screen.findByRole("button", { name: /place order/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
  });
});
