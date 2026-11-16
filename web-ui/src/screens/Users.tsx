import { type FormEvent, useState } from "react";
import { useMutation, useQuery } from "urql";

import { newIdempotencyKey } from "../client";
import { ErrorNotice, Notice, Pager } from "../components";
import { formatDate } from "../format";
import { CreateUserMutation, DeleteUserMutation, UsersQuery } from "../operations";
import { useReadOnly } from "../readOnly";

const PAGE_SIZE = 10;

function ordersLabel(count: number): string {
  if (count === 0) return "No orders yet";
  return count === 1 ? "1 order" : `${count} orders`;
}

export function Users() {
  const [after, setAfter] = useState<string | null>(null);
  const [{ data, error, fetching }] = useQuery({
    query: UsersQuery,
    variables: { first: PAGE_SIZE, after },
  });
  const [created, createUser] = useMutation(CreateUserMutation);
  const [deleted, deleteUser] = useMutation(DeleteUserMutation);
  const { readOnly, reason } = useReadOnly();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  async function add(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    const result = await createUser({ input: { email, name }, key: newIdempotencyKey() });
    if (result.data) {
      setNotice(`Added ${result.data.createUser.email}.`);
      setEmail("");
      setName("");
    }
  }

  const users = data?.users.items ?? [];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Users</h1>
          <p>The people in this tenant. Their data is kept in this tenant&apos;s own database.</p>
        </div>
      </div>
      <ErrorNotice error={error ?? created.error ?? deleted.error} />
      <Notice text={notice} />

      <form className="card form-card" aria-label="New user" onSubmit={(event) => void add(event)}>
        <h2>New user</h2>
        <label>
          Name <input required value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          Email <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <button type="submit" className="primary" disabled={readOnly || created.fetching} title={reason}>
          Add user
        </button>
      </form>

      <section className="card" aria-label="User list">
        {fetching && !data && <p className="empty">Loading…</p>}
        {data && users.length === 0 && <p className="empty">No users yet.</p>}
        {users.length > 0 && (
          <div className="table-box">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Recent orders</th>
                  <th>Created</th>
                  <th className="actions">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td className="strong">{u.name}</td>
                    <td>
                      <a href={`#/users/${encodeURIComponent(u.id)}`}>{u.email}</a>
                    </td>
                    <td>{ordersLabel(u.orders.length)}</td>
                    <td>{formatDate(u.createdAt)}</td>
                    <td className="actions">
                      <button
                        type="button"
                        className="danger"
                        disabled={readOnly}
                        title={reason}
                        onClick={() => void deleteUser({ id: u.id })}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager
          nextCursor={data?.users.nextCursor}
          onNext={setAfter}
          onFirst={after ? () => setAfter(null) : null}
        />
      </section>
    </>
  );
}
