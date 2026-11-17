import { useEffect, useMemo, useState } from "react";
import { Provider, useQuery } from "urql";

import { createGraphqlClient, describe, isSignedOut } from "./client";
import { ErrorNotice } from "./components";
import { type Config, loadConfig, tenantFromHost } from "./config";
import { isMoving, MainNav, MovingNotice, PageFoot, type Screen, TopBar } from "./layout";
import { MeQuery } from "./operations";
import { ReadOnlyContext } from "./readOnly";
import { Orders } from "./screens/Orders";
import { UserDetail } from "./screens/UserDetail";
import { Users } from "./screens/Users";
import { YourCell } from "./screens/YourCell";

/** While the tenant is being moved, how often the app asks whether the move is done. */
export const MOVE_CHECK_MS = 10_000;

interface AppProps {
  moveCheckMs?: number;
}

/** The app for one tenant: whichever tenant this address (acme.platform.com) belongs to. */
export function App({ moveCheckMs = MOVE_CHECK_MS }: AppProps) {
  const [config, setConfig] = useState<Config | null>(null);
  useEffect(() => {
    void loadConfig().then(setConfig);
  }, []);
  const client = useMemo(
    () => config && createGraphqlClient(new URL(config.graphqlPath, window.location.origin).toString()),
    [config],
  );
  if (!config || !client) return <p className="loading">Loading…</p>;
  return (
    <Provider value={client}>
      <Shell config={config} moveCheckMs={moveCheckMs} />
    </Provider>
  );
}

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

function Shell({ config, moveCheckMs }: { config: Config; moveCheckMs: number }) {
  const [{ data, error, fetching }, refetchMe] = useQuery({ query: MeQuery, requestPolicy: "network-only" });
  const [signInProblem, setSignInProblem] = useState<string | null>(null);
  const hash = useHash();
  const tenant = tenantFromHost(window.location.hostname) ?? "this tenant";
  const moving = data ? isMoving(data.me) : false;

  // A move ends without the tenant doing anything: keep asking until it has.
  useEffect(() => {
    if (!moving) return undefined;
    const timer = window.setInterval(() => refetchMe({ requestPolicy: "network-only" }), moveCheckMs);
    return () => window.clearInterval(timer);
  }, [moving, moveCheckMs, refetchMe]);

  async function post(url: string): Promise<boolean> {
    const response = await fetch(new URL(url, window.location.origin), {
      method: "POST",
      credentials: "same-origin",
    });
    refetchMe({ requestPolicy: "network-only" });
    return response.ok;
  }

  if (fetching && !data && !error) return <p className="loading">Loading…</p>;
  if (error && isSignedOut(describe(error))) {
    return (
      <main className="card sign-in">
        <span className="tenant-mark" aria-hidden="true">
          {tenant.slice(0, 1)}
        </span>
        <h1>{tenant}</h1>
        <p className="muted">{window.location.host}</p>
        <ErrorNotice error={signInProblem} />
        <button
          type="button"
          className="primary"
          onClick={() =>
            void post(config.signIn.signInUrl).then((ok) =>
              setSignInProblem(ok ? null : "Could not sign in. Use your tenant's own address."),
            )
          }
        >
          Sign in to {tenant}
        </button>
        {config.signIn.provider === "dev" && <p className="muted">Development only: no password needed.</p>}
      </main>
    );
  }
  if (!data) return <ErrorNotice error={error} />;

  const profile = data.me;
  const userMatch = /^#\/users\/(.+)$/.exec(hash);
  let current: Screen = "users";
  let screen = <Users />;
  if (hash === "#/orders") {
    current = "orders";
    screen = <Orders />;
  } else if (hash === "#/cell") {
    current = "cell";
    screen = <YourCell profile={profile} />;
  } else if (userMatch?.[1]) {
    screen = <UserDetail userId={decodeURIComponent(userMatch[1])} />;
  }
  const readOnly = {
    readOnly: moving,
    reason: moving ? `Paused while ${profile.tenantId} is being moved` : undefined,
  };

  return (
    <ReadOnlyContext.Provider value={readOnly}>
      <TopBar profile={profile} onSignOut={() => void post(config.signIn.signOutUrl)} />
      {moving && <MovingNotice tenant={profile.tenantId} />}
      <div className="frame">
        <MainNav current={current} />
        <main className="content">
          <ErrorNotice error={error} />
          {screen}
          <PageFoot profile={profile} />
        </main>
      </div>
    </ReadOnlyContext.Provider>
  );
}
