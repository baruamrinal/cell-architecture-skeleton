import { useEffect, useMemo, useState } from "react";
import { Provider, useQuery } from "urql";

import { createGraphqlClient, describe, isSignedOut } from "./client";
import { ErrorNotice } from "./components";
import { type Config, loadConfig, tenantFromHost } from "./config";
import { MeQuery } from "./operations";
import { Orders } from "./screens/Orders";
import { UserDetail } from "./screens/UserDetail";
import { Users } from "./screens/Users";

/** The app for one tenant: whichever tenant this address (acme.platform.com) belongs to. */
export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  useEffect(() => {
    void loadConfig().then(setConfig);
  }, []);
  const client = useMemo(
    () => config && createGraphqlClient(new URL(config.graphqlPath, window.location.origin).toString()),
    [config],
  );
  if (!config || !client) return <p>Loading…</p>;
  return (
    <Provider value={client}>
      <Shell config={config} />
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

function Shell({ config }: { config: Config }) {
  const [{ data, error, fetching }, refetchMe] = useQuery({ query: MeQuery, requestPolicy: "network-only" });
  const [signInProblem, setSignInProblem] = useState<string | null>(null);
  const hash = useHash();
  const tenant = tenantFromHost(window.location.hostname) ?? "this tenant";

  async function post(url: string): Promise<boolean> {
    const response = await fetch(new URL(url, window.location.origin), {
      method: "POST",
      credentials: "same-origin",
    });
    refetchMe({ requestPolicy: "network-only" });
    return response.ok;
  }

  if (fetching && !data && !error) return <p>Loading…</p>;
  if (error && isSignedOut(describe(error))) {
    return (
      <main>
        <h1>{tenant}</h1>
        <ErrorNotice error={signInProblem} />
        <button
          type="button"
          onClick={() =>
            void post(config.signIn.signInUrl).then((ok) =>
              setSignInProblem(ok ? null : "Could not sign in. Use your tenant's own address."),
            )
          }
        >
          Sign in to {tenant}
        </button>
        {config.signIn.provider === "dev" && <p>Development only: no password needed.</p>}
      </main>
    );
  }
  if (error || !data) return <ErrorNotice error={error} />;

  const userMatch = /^#\/users\/(.+)$/.exec(hash);
  let screen = <Users />;
  if (hash === "#/orders") screen = <Orders />;
  else if (userMatch?.[1]) screen = <UserDetail userId={decodeURIComponent(userMatch[1])} />;

  return (
    <main>
      <header>
        <h1>{data.me.tenantId}</h1>
        <small>served by {data.me.cellId}</small>
        <nav>
          <a href="#/users">Users</a> <a href="#/orders">Orders</a>{" "}
          <button type="button" onClick={() => void post(config.signIn.signOutUrl)}>
            Sign out
          </button>
        </nav>
      </header>
      {screen}
    </main>
  );
}
