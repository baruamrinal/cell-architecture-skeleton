import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { ME, config, graphql, order, problem, signedIn, user, usersPage } from "./test/fixtures";
import { server } from "./test/setup";

describe("signing in", () => {
  it("asks a signed-out visitor to sign in for this host's tenant, then shows the app", async () => {
    let signedInYet = false;
    server.use(
      config,
      graphql.query("Me", () =>
        signedInYet ? HttpResponse.json({ data: { me: ME } }) : problem("unauthenticated", 401),
      ),
      http.post("/auth/dev/sign-in", () => {
        signedInYet = true;
        return HttpResponse.json({ tenant_id: "acme", expires_in: 3600 });
      }),
      usersPage(),
    );
    render(<App />);

    const button = await screen.findByRole("button", { name: /sign in to acme/i });
    expect(screen.getByText(/development only/i)).toBeInTheDocument();
    await userEvent.click(button);

    expect(await screen.findByText("usr_1@acme.test")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "acme" })).toBeInTheDocument();
    expect(screen.getByText("served by cell-1")).toBeInTheDocument();
  });

  it("explains a failed sign-in", async () => {
    server.use(
      config,
      graphql.query("Me", () => problem("unauthenticated", 401)),
      http.post("/auth/dev/sign-in", () =>
        HttpResponse.json({ error: "tenant_host_required", message: "no" }, { status: 400 }),
      ),
    );
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not sign in/i);
  });

  it("signs out", async () => {
    let signedOut = false;
    server.use(
      config,
      graphql.query("Me", () =>
        signedOut ? problem("unauthenticated", 401) : HttpResponse.json({ data: { me: ME } }),
      ),
      http.post("/auth/sign-out", () => {
        signedOut = true;
        return new HttpResponse(null, { status: 204 });
      }),
      usersPage(),
    );
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: /sign out/i }));
    expect(await screen.findByRole("button", { name: /sign in to acme/i })).toBeInTheDocument();
  });
});

describe("navigation and errors", () => {
  it("moves between users, a user and orders", async () => {
    server.use(
      config,
      signedIn,
      usersPage([user("usr_1")]),
      graphql.query("User", () =>
        HttpResponse.json({
          data: { user: user("usr_1"), orders: { items: [order("ord_1")], nextCursor: null } },
        }),
      ),
      graphql.query("Orders", () =>
        HttpResponse.json({ data: { orders: { items: [order("ord_9")], nextCursor: null } } }),
      ),
    );
    render(<App />);
    await userEvent.click(await screen.findByRole("link", { name: "usr_1@acme.test" }));
    expect(await screen.findByRole("heading", { name: /USR_1/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Orders" }));
    expect(await screen.findByText("ord_9")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Users" }));
    expect(await screen.findByText("usr_1@acme.test")).toBeInTheDocument();
  });

  it("shows when the app can't reach the platform", async () => {
    server.use(config, graphql.query("Me", () => problem("cell_unavailable", 503, 2)));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  });

  it("starts with defaults when /config.json is missing", async () => {
    server.use(
      http.get("/config.json", () => new HttpResponse(null, { status: 404 })),
      signedIn,
      usersPage(),
    );
    render(<App />);
    expect(await screen.findByText("usr_1@acme.test")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/loading/i)).not.toBeInTheDocument());
  });
});
