import { useState } from "react";
import { useMutation, useQuery } from "urql";

import { ErrorNotice, Pager } from "../components";
import { formatCents, formatDate } from "../format";
import { CancelOrderMutation, OrdersQuery } from "../operations";
import { useReadOnly } from "../readOnly";

const PAGE_SIZE = 20;

export function StatusPill({ status }: { status: string }) {
  return <span className={status === "placed" ? "pill placed" : "pill"}>{status}</span>;
}

export function Orders() {
  const [after, setAfter] = useState<string | null>(null);
  const [{ data, error, fetching }] = useQuery({
    query: OrdersQuery,
    variables: { first: PAGE_SIZE, after },
  });
  const [cancelled, cancelOrder] = useMutation(CancelOrderMutation);
  const { readOnly, reason } = useReadOnly();
  const orders = data?.orders.items ?? [];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Orders</h1>
          <p>All of this tenant&apos;s orders, newest first.</p>
        </div>
      </div>
      <ErrorNotice error={error ?? cancelled.error} />
      <section className="card" aria-label="Order list">
        {fetching && !data && <p className="empty">Loading…</p>}
        {data && orders.length === 0 && <p className="empty">No orders yet.</p>}
        {orders.length > 0 && (
          <div className="table-box">
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Customer</th>
                  <th>Placed</th>
                  <th className="number">Total</th>
                  <th>Status</th>
                  <th className="actions">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id}>
                    <td className="mono">{o.id}</td>
                    <td>
                      <a href={`#/users/${encodeURIComponent(o.userId)}`}>{o.userId}</a>
                    </td>
                    <td>{formatDate(o.createdAt)}</td>
                    <td className="number">{formatCents(o.totalCents)}</td>
                    <td>
                      <StatusPill status={o.status} />
                    </td>
                    <td className="actions">
                      {o.status === "placed" && (
                        <button
                          type="button"
                          disabled={readOnly}
                          title={reason}
                          onClick={() => void cancelOrder({ id: o.id })}
                        >
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager
          nextCursor={data?.orders.nextCursor}
          onNext={setAfter}
          onFirst={after ? () => setAfter(null) : null}
        />
      </section>
    </>
  );
}
