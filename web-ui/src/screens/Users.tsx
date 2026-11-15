import { type FormEvent, useState } from "react";
import { useMutation, useQuery } from "urql";

import { newIdempotencyKey } from "../client";
import { ErrorNotice, Notice, Pager } from "../components";
import { CreateUserMutation, DeleteUserMutation, UsersQuery } from "../operations";

const PAGE_SIZE = 10;

export function Users() {
  const [after, setAfter] = useState<string | null>(null);
  const [{ data, error, fetching }] = useQuery({
    query: UsersQuery,
    variables: { first: PAGE_SIZE, after },
  });
  const [created, createUser] = useMutation(CreateUserMutation);
  const [deleted, deleteUser] = useMutation(DeleteUserMutation);
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
    <section>
      <h2>Users</h2>
      <ErrorNotice error={error ?? created.error ?? deleted.error} />
      <Notice text={notice} />
      {fetching && !data && <p>Loading…</p>}
      {data && users.length === 0 && <p>No users yet.</p>}
      {users.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Name</th>
              <th>Orders</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <a href={`#/users/${encodeURIComponent(u.id)}`}>{u.email}</a>
                </td>
                <td>{u.name}</td>
                <td>{u.orders.length}</td>
                <td>
                  <button type="button" onClick={() => void deleteUser({ id: u.id })}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Pager
        nextCursor={data?.users.nextCursor}
        onNext={setAfter}
        onFirst={after ? () => setAfter(null) : null}
      />
      <form onSubmit={(event) => void add(event)}>
        <h3>Add a user</h3>
        <label>
          Email <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Name <input required value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button type="submit" disabled={created.fetching}>
          Add user
        </button>
      </form>
    </section>
  );
}
