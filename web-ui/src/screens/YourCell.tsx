import { cellKind, Icon, isMoving, type Profile } from "../layout";

function Arrow() {
  return (
    <span className="arrow" aria-hidden="true">
      <svg width="28" height="16" viewBox="0 0 28 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M2 8h22" />
        <path d="M18 2l6 6-6 6" />
      </svg>
    </span>
  );
}

/**
 * The cell architecture in the tenant's words: which cell it lives in, how a request
 * reaches it, what isolation gives it, and what a move means. Everything shown comes
 * from the tenant's own cell (`me`), never from the control plane.
 */
export function YourCell({ profile }: { profile: Profile }) {
  const tenant = profile.tenantId;
  const moving = isMoving(profile);
  const kind = cellKind(profile);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Your cell</h1>
          <p>
            The platform is split into independent cells. {tenant} lives in exactly one of them,
            with its own services and its own database. A problem in another cell can&apos;t reach{" "}
            {tenant}, and {tenant}&apos;s data stays in its cell.
          </p>
        </div>
      </div>

      <section aria-label="Cell facts">
        <dl className="facts">
          <div className="card fact">
            <dt>Cell</dt>
            <dd className="mono">{profile.cell.id}</dd>
          </div>
          <div className="card fact">
            <dt>Type</dt>
            <dd>
              <span>
                {kind}
                <span className="hint">
                  {kind === "Dedicated" ? `Only ${tenant} lives here` : "Several tenants, each fully separate"}
                </span>
              </span>
            </dd>
          </div>
          <div className="card fact">
            <dt>Region</dt>
            <dd>
              <span>
                <span className="mono">{profile.cell.region}</span>
                <span className="hint">Where {tenant}&apos;s data is stored</span>
              </span>
            </dd>
          </div>
          <div className="card fact">
            <dt>Status</dt>
            <dd>
              <span className={moving ? "dot moving" : "dot"} />
              <span>
                {moving ? "Being moved" : "Active"}
                <span className="hint">{moving ? "Reading only, for now" : "Reading and changes allowed"}</span>
              </span>
            </dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="path-heading" className="card section">
        <div>
          <h2 id="path-heading">How a request reaches {tenant}</h2>
          <p>
            Every page you open takes this path. The router only decides which cell; everything
            else happens inside {tenant}&apos;s cell.
          </p>
        </div>
        <div className="path">
          <div className="hop">
            <strong>Your browser</strong>
            <span className="mono">{window.location.host}</span>
          </div>
          <Arrow />
          <div className="hop">
            <strong>Router</strong>
            <span>
              Checks your sign-in is for {tenant}, then sends {tenant} to {profile.cell.id}
            </span>
          </div>
          <Arrow />
          <div className="cell-box">
            <strong>
              <Icon name="cell" size={16} />
              Cell {profile.cell.id}
            </strong>
            <div className="parts">
              <div className="part">
                <strong>Gateway</strong>
                <span>Admits only the tenants it hosts</span>
              </div>
              <div className="part">
                <strong>Console API</strong>
                <span>Serves this app</span>
              </div>
              <div className="part">
                <strong>{tenant}&apos;s services</strong>
                <span>Accounts and orders</span>
              </div>
              <div className="part">
                <strong>{tenant}&apos;s database</strong>
                <span>Only {tenant}&apos;s data</span>
              </div>
            </div>
          </div>
        </div>
        <p className="others">
          <span className="chip" aria-hidden="true">
            other cell
          </span>
          <span className="chip" aria-hidden="true">
            other cell
          </span>
          <span>
            Other cells run side by side, each with its own servers and database. Cells never
            call each other.
          </span>
        </p>
      </section>

      <section aria-label="What isolation means" className="promises">
        <div className="card padded">
          <h3>Your data stays yours</h3>
          <p>
            {tenant} has its own database. Each service signs in with its own login that reaches
            only its part of it.
          </p>
        </div>
        <div className="card padded">
          <h3>Failures stay contained</h3>
          <p>
            An outage, or a busy tenant, in another cell doesn&apos;t slow {tenant} down. Each
            cell can fail on its own.
          </p>
        </div>
        <div className="card padded">
          <h3>Room to grow</h3>
          <p>
            When {tenant} needs it, it can move to a dedicated cell of its own, or to a cell in
            your own cloud account.
          </p>
        </div>
      </section>

      <section aria-labelledby="move-heading" className="card section">
        <div>
          <h2 id="move-heading">If {tenant} is moved to another cell</h2>
          <p>
            A move keeps your address and your data. You can keep reading throughout; changes
            pause until the switch.
          </p>
        </div>
        <ol className="steps" aria-label="Move steps">
          <li>
            <strong>Pause changes</strong>
            <span>{tenant} becomes read-only</span>
          </li>
          <li>
            <strong>Copy</strong>
            <span>The data is copied to the new cell</span>
          </li>
          <li>
            <strong>Switch</strong>
            <span>The router sends {tenant} to the new cell</span>
          </li>
          <li>
            <strong>Done</strong>
            <span>Changes open again; the old copy is removed</span>
          </li>
        </ol>
        <p>If anything goes wrong, the move is undone and {tenant} stays where it was.</p>
      </section>
    </>
  );
}
