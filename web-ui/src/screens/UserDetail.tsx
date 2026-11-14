import { type FormEvent, useState } from "react";
import { useMutation, useQuery } from "urql";

import { newIdempotencyKey } from "../client";
import { ErrorNotice, Notice } from "../components";
import { formatCents, parseCents } from "../format";
import { CancelOrderMutation, CreateOrderMutation, UserQuery } from "../operations";

export function UserDetail({ userId }: { userId: string }) {
  const [{ data, error, fetching }] = useQuery({ query: UserQuery, variables: { id: userId } });
  const [placed, placeOrder] = useMutation(CreateOrderMutation);
  const [cancelled, cancelOrder] = useMutation(CancelOrderMutation);
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

  if (fetching && !data) return <p>Loading…</p>;
  const user = data?.user;
  return (
    <section>
      <ErrorNotice error={error ?? problem ?? placed.error ?? cancelled.error} />
      <Notice text={notice} />
      {data && !user && <p>No such user.</p>}
      {user && (
        <>
          <h2>
            {user.name} <small>{user.email}</small>
          </h2>
          <h3>Orders</h3>
          <table>
            <tbody>
              {data.orders.items.map((o) => (
                <tr key={o.id}>
                  <td>{o.id}</td>
                  <td>{o.status}</td>
                  <td>{formatCents(o.totalCents)}</td>
                  <td>{o.lines.map((l) => `${l.quantity} × ${l.sku}`).join(", ")}</td>
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
          <form onSubmit={(event) => void place(event)}>
            <h3>Place an order</h3>
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
            <button type="submit" disabled={placed.fetching}>
              Place order
            </button>
          </form>
        </>
      )}
    </section>
  );
}
