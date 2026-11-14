import { useState } from "react";
import { useMutation, useQuery } from "urql";

import { ErrorNotice, Pager } from "../components";
import { formatCents } from "../format";
import { CancelOrderMutation, OrdersQuery } from "../operations";

const PAGE_SIZE = 20;

export function Orders() {
  const [after, setAfter] = useState<string | null>(null);
  const [{ data, error, fetching }] = useQuery({
    query: OrdersQuery,
    variables: { first: PAGE_SIZE, after },
  });
  const [cancelled, cancelOrder] = useMutation(CancelOrderMutation);
  const orders = data?.orders.items ?? [];
  return (
    <section>
      <h2>Orders</h2>
      <ErrorNotice error={error ?? cancelled.error} />
      {fetching && !data && <p>Loading…</p>}
      {data && orders.length === 0 && <p>No orders yet.</p>}
      {orders.length > 0 && (
        <table>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td>{o.id}</td>
                <td>
                  <a href={`#/users/${encodeURIComponent(o.userId)}`}>{o.userId}</a>
                </td>
                <td>{o.status}</td>
                <td>{formatCents(o.totalCents)}</td>
                <td>
                  {o.status === "placed" && (
                    <button type="button" onClick={() => void cancelOrder({ id: o.id })}>
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Pager
        nextCursor={data?.orders.nextCursor}
        onNext={setAfter}
        onFirst={after ? () => setAfter(null) : null}
      />
    </section>
  );
}
