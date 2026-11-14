import { graphql } from "./gql";

export const MeQuery = graphql(`
  query Me {
    me {
      tenantId
      cellId
    }
  }
`);

export const UsersQuery = graphql(`
  query Users($first: Int!, $after: String) {
    users(first: $first, after: $after) {
      items {
        id
        email
        name
        createdAt
        orders(first: 5) {
          id
          status
        }
      }
      nextCursor
    }
  }
`);

export const UserQuery = graphql(`
  query User($id: ID!) {
    user(id: $id) {
      id
      email
      name
      createdAt
    }
    orders(userId: $id, first: 50) {
      items {
        id
        status
        totalCents
        createdAt
        lines {
          sku
          quantity
          unitPriceCents
        }
      }
      nextCursor
    }
  }
`);

export const OrdersQuery = graphql(`
  query Orders($first: Int!, $after: String) {
    orders(first: $first, after: $after) {
      items {
        id
        userId
        status
        totalCents
        createdAt
      }
      nextCursor
    }
  }
`);

export const CreateUserMutation = graphql(`
  mutation CreateUser($input: UserInput!, $key: String) {
    createUser(input: $input, idempotencyKey: $key) {
      id
      email
      name
    }
  }
`);

export const DeleteUserMutation = graphql(`
  mutation DeleteUser($id: ID!) {
    deleteUser(id: $id)
  }
`);

export const CreateOrderMutation = graphql(`
  mutation CreateOrder($input: OrderInput!, $key: String) {
    createOrder(input: $input, idempotencyKey: $key) {
      id
      status
      totalCents
    }
  }
`);

export const CancelOrderMutation = graphql(`
  mutation CancelOrder($id: ID!) {
    cancelOrder(id: $id) {
      id
      status
    }
  }
`);
