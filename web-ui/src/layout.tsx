import type { ReactNode } from "react";

import type { MeQuery } from "./gql/graphql";

export type Profile = MeQuery["me"];

export type Screen = "users" | "orders" | "cell";

/** "Shared cell" or "Dedicated cell": what kind of cell the tenant lives in. */
export function cellKind(profile: Profile): string {
  return profile.cell.type === "DEDICATED" ? "Dedicated" : "Shared";
}

export function isMoving(profile: Profile): boolean {
  return profile.status === "MOVING";
}

const ICONS: Record<Screen, ReactNode> = {
  users: (
    <>
      <circle cx="9" cy="8" r="4" />
      <path d="M2 21c0-4 3-7 7-7s7 3 7 7" />
      <path d="M17 11a3 3 0 1 0 0-6" />
      <path d="M22 21c0-3-2-5-4-6" />
    </>
  ),
  orders: (
    <>
      <path d="M6 2h12l2 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z" />
      <path d="M4 7h16" />
      <path d="M9 12h6" />
    </>
  ),
  cell: <CellShape />,
};

function CellShape() {
  return (
    <>
      <path d="M12 2l9 5v10l-9 5-9-5V7z" />
      <path d="M12 12l9-5" />
      <path d="M12 12v10" />
      <path d="M12 12L3 7" />
    </>
  );
}

export function Icon({ name, size = 18 }: { name: Screen; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  );
}

/** The cell the tenant lives in, on every screen; it opens "Your cell". */
export function CellBadge({ profile }: { profile: Profile }) {
  const moving = isMoving(profile);
  return (
    <a href="#/cell" className={moving ? "cell-badge moving" : "cell-badge"}>
      <span className={moving ? "dot moving" : "dot"} />
      <span className="mono">{profile.cell.id}</span>
      <span className="muted">
        {moving ? "Moving · read-only" : `${cellKind(profile)} cell · Active`}
      </span>
    </a>
  );
}

export function TopBar({ profile, onSignOut }: { profile: Profile; onSignOut: () => void }) {
  return (
    <header className="topbar">
      <div className="tenant">
        <span className="tenant-mark" aria-hidden="true">
          {profile.tenantId.slice(0, 1)}
        </span>
        <span className="tenant-name">
          <strong>{profile.tenantId}</strong>
          <span>{window.location.host}</span>
        </span>
      </div>
      <CellBadge profile={profile} />
      <div className="spacer" />
      <div className="account">
        <button type="button" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </header>
  );
}

/** While the tenant is moved: what still works, and that the page will notice the end. */
export function MovingNotice({ tenant }: { tenant: string }) {
  return (
    <div role="status" aria-label={`${tenant} is being moved`} className="moving-notice">
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        aria-hidden="true"
      >
        <rect x="5" y="11" width="14" height="10" rx="2" />
        <path d="M8 11V7a4 4 0 0 1 8 0v4" />
      </svg>
      <p>
        <strong>{tenant} is being moved to another cell.</strong> You can browse everything;
        adding, deleting and cancelling are paused until the move is done. This page checks
        again by itself.
      </p>
      <a href="#/cell">What&apos;s happening</a>
    </div>
  );
}

const SCREENS: { screen: Screen; label: string; href: string }[] = [
  { screen: "users", label: "Users", href: "#/users" },
  { screen: "orders", label: "Orders", href: "#/orders" },
  { screen: "cell", label: "Your cell", href: "#/cell" },
];

export function MainNav({ current }: { current: Screen }) {
  return (
    <nav aria-label="Main" className="main-nav">
      {SCREENS.map(({ screen, label, href }) => (
        <a key={screen} href={href} aria-current={screen === current ? "page" : undefined}>
          <Icon name={screen} />
          {label}
        </a>
      ))}
    </nav>
  );
}

export function PageFoot({ profile }: { profile: Profile }) {
  return (
    <footer className="page-foot">
      <span>
        Served by <span className="mono">{profile.cell.id}</span>
      </span>
      <span>
        {cellKind(profile)} cell in <span className="mono">{profile.cell.region}</span>
      </span>
    </footer>
  );
}
