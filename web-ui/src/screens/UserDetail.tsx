import { type FormEvent, useState } from "react";
import { useMutation, useQuery } from "urql";

import { newIdempotencyKey } from "../client";
import { ErrorNotice, Notice } from "../components";
import { formatCents, formatDate, parseCents } from "../format";
import { CancelOrderMutation, CreateOrderMutation, UserQuery } from "../operations";
import { useReadOnly } from "../readOnly";
import { StatusPill } from "./Orders";

export function UserDetail({ userId }: { userId: string }) {
  const [{ data, error, fetching }] = useQuery({ query: UserQuery, variables: { id: userId } });
  const [placed, placeOrder] = useMutation(CreateOrderMutation);
  const [cancelled, cancelOrder] = useMutation(CancelOrderMutation);
  const { readOnly, reason } = useReadOnly();
  const [sku, setSku] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("0.00");
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function place(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    const unitPriceCents = parseCents(price);
    if (unitPriceCents === null) {
      setProblem("Enter the unit price as an amount, e.g. 12.50.");
      return;
    }
    setProblem(null);
    const result = await placeOrder({
      input: { userId, lines: [{ sku, quantity: Number(quantity), unitPriceCents }] },
      key: newIdempotencyKey(),
    });
    if (result.data) setNotice(`Placed ${result.data.createOrder.id}.`);
  }

  if (fetching && !data) return <p className="loading">Loading…</p>;
  const user = data?.user;
  return (
    <>
      <p>
        <a href="#/users">← All users</a>
      </p>
      <ErrorNotice error={error ?? problem ?? placed.error ?? cancelled.error} />
      <Notice text={notice} />
      {data && !user && <p className="card empty">No such user.</p>}
      {user && (
        <>
          <div className="page-head">
            <div>
              <h1>{user.name}</h1>
              <p>
                {user.email} · since {formatDate(user.createdAt)}
              </p>
            </div>
          </div>

          <section className="card" aria-labelledby="orders-heading">
            <h2 id="orders-heading" className="section-title">
              Orders
            </h2>
            {data.orders.items.length === 0 ? (
              <p className="empty">No orders yet.</p>
            ) : (
              <div className="table-box">
                <table>
                  <thead>
                    <tr>
                      <th>Order</th>
                      <th>Items</th>
                      <th className="number">Total</th>
                      <th>Status</th>
                      <th className="actions">
                        <span className="visually-hidden">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.orders.items.map((o) => (
                      <tr key={o.id}>
                        <td className="mono">{o.id}</td>
                        <td>{o.lines.map((l) => `${l.quantity} × ${l.sku}`).join(", ")}</td>
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
          </section>

          <form className="card form-card" aria-label="Place an order" onSubmit={(event) => void place(event)}>
            <h2>Place an order</h2>
            <label>
              SKU <input required value={sku} onChange={(e) => setSku(e.target.value)} />
            </label>
            <label>
              Quantity{" "}
              <input type="number" min={1} required value={quantity} onChange={(e) => setQuantity(e.target.value)} />
            </label>
            <label>
              Unit price <input required value={price} onChange={(e) => setPrice(e.target.value)} />
            </label>
            <button type="submit" className="primary" disabled={readOnly || placed.fetching} title={reason}>
              Place order
            </button>
          </form>
        </>
      )}
    </>
  );
}
