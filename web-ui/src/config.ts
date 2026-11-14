/**
 * Runtime configuration, read from /config.json when the app starts (never baked in at
 * build time), so one image serves every environment. The chart provides the file.
 */
export interface SignInConfig {
  /** "dev": the development-only identity provider (one button, no password). */
  provider: string;
  signInUrl: string;
  signOutUrl: string;
}

export interface Config {
  graphqlPath: string;
  signIn: SignInConfig;
}

export const DEFAULT_CONFIG: Config = {
  graphqlPath: "/graphql",
  signIn: { provider: "dev", signInUrl: "/auth/dev/sign-in", signOutUrl: "/auth/sign-out" },
};

export async function loadConfig(fetchImpl: typeof fetch = fetch): Promise<Config> {
  try {
    const response = await fetchImpl("/config.json", { cache: "no-store" });
    if (!response.ok) return DEFAULT_CONFIG;
    const loaded = (await response.json()) as Partial<Config>;
    return {
      graphqlPath: loaded.graphqlPath ?? DEFAULT_CONFIG.graphqlPath,
      signIn: { ...DEFAULT_CONFIG.signIn, ...loaded.signIn },
    };
  } catch {
    return DEFAULT_CONFIG;
  }
}

/** The tenant this address belongs to: "acme" for acme.platform.com or acme.localhost. */
export function tenantFromHost(hostname: string): string | null {
  const [first, ...rest] = hostname.toLowerCase().split(".");
  return first && rest.length > 0 ? first : null;
}
