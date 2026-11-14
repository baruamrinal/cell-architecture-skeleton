/* eslint-disable */
import * as types from './graphql';
import type { TypedDocumentNode as DocumentNode } from '@graphql-typed-document-node/core';

/**
 * Map of all GraphQL operations in the project.
 *
 * This map has several performance disadvantages:
 * 1. It is not tree-shakeable, so it will include all operations in the project.
 * 2. It is not minifiable, so the string of a GraphQL query will be multiple times inside the bundle.
 * 3. It does not support dead code elimination, so it will add unused operations.
 *
 * Therefore it is highly recommended to use the babel or swc plugin for production.
 * Learn more about it here: https://the-guild.dev/graphql/codegen/plugins/presets/preset-client#reducing-bundle-size
 */
type Documents = {
    "\n  query Me {\n    me {\n      tenantId\n      cellId\n    }\n  }\n": typeof types.MeDocument,
    "\n  query Users($first: Int!, $after: String) {\n    users(first: $first, after: $after) {\n      items {\n        id\n        email\n        name\n        createdAt\n        orders(first: 5) {\n          id\n          status\n        }\n      }\n      nextCursor\n    }\n  }\n": typeof types.UsersDocument,
    "\n  query User($id: ID!) {\n    user(id: $id) {\n      id\n      email\n      name\n      createdAt\n    }\n    orders(userId: $id, first: 50) {\n      items {\n        id\n        status\n        totalCents\n        createdAt\n        lines {\n          sku\n          quantity\n          unitPriceCents\n        }\n      }\n      nextCursor\n    }\n  }\n": typeof types.UserDocument,
    "\n  query Orders($first: Int!, $after: String) {\n    orders(first: $first, after: $after) {\n      items {\n        id\n        userId\n        status\n        totalCents\n        createdAt\n      }\n      nextCursor\n    }\n  }\n": typeof types.OrdersDocument,
    "\n  mutation CreateUser($input: UserInput!, $key: String) {\n    createUser(input: $input, idempotencyKey: $key) {\n      id\n      email\n      name\n    }\n  }\n": typeof types.CreateUserDocument,
    "\n  mutation DeleteUser($id: ID!) {\n    deleteUser(id: $id)\n  }\n": typeof types.DeleteUserDocument,
    "\n  mutation CreateOrder($input: OrderInput!, $key: String) {\n    createOrder(input: $input, idempotencyKey: $key) {\n      id\n      status\n      totalCents\n    }\n  }\n": typeof types.CreateOrderDocument,
    "\n  mutation CancelOrder($id: ID!) {\n    cancelOrder(id: $id) {\n      id\n      status\n    }\n  }\n": typeof types.CancelOrderDocument,
};
const documents: Documents = {
    "\n  query Me {\n    me {\n      tenantId\n      cellId\n    }\n  }\n": types.MeDocument,
    "\n  query Users($first: Int!, $after: String) {\n    users(first: $first, after: $after) {\n      items {\n        id\n        email\n        name\n        createdAt\n        orders(first: 5) {\n          id\n          status\n        }\n      }\n      nextCursor\n    }\n  }\n": types.UsersDocument,
    "\n  query User($id: ID!) {\n    user(id: $id) {\n      id\n      email\n      name\n      createdAt\n    }\n    orders(userId: $id, first: 50) {\n      items {\n        id\n        status\n        totalCents\n        createdAt\n        lines {\n          sku\n          quantity\n          unitPriceCents\n        }\n      }\n      nextCursor\n    }\n  }\n": types.UserDocument,
    "\n  query Orders($first: Int!, $after: String) {\n    orders(first: $first, after: $after) {\n      items {\n        id\n        userId\n        status\n        totalCents\n        createdAt\n      }\n      nextCursor\n    }\n  }\n": types.OrdersDocument,
    "\n  mutation CreateUser($input: UserInput!, $key: String) {\n    createUser(input: $input, idempotencyKey: $key) {\n      id\n      email\n      name\n    }\n  }\n": types.CreateUserDocument,
    "\n  mutation DeleteUser($id: ID!) {\n    deleteUser(id: $id)\n  }\n": types.DeleteUserDocument,
    "\n  mutation CreateOrder($input: OrderInput!, $key: String) {\n    createOrder(input: $input, idempotencyKey: $key) {\n      id\n      status\n      totalCents\n    }\n  }\n": types.CreateOrderDocument,
    "\n  mutation CancelOrder($id: ID!) {\n    cancelOrder(id: $id) {\n      id\n      status\n    }\n  }\n": types.CancelOrderDocument,
};

/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 *
 *
 * @example
 * ```ts
 * const query = graphql(`query GetUser($id: ID!) { user(id: $id) { name } }`);
 * ```
 *
 * The query argument is unknown!
 * Please regenerate the types.
 */
export function graphql(source: string): unknown;

/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Me {\n    me {\n      tenantId\n      cellId\n    }\n  }\n"): (typeof documents)["\n  query Me {\n    me {\n      tenantId\n      cellId\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Users($first: Int!, $after: String) {\n    users(first: $first, after: $after) {\n      items {\n        id\n        email\n        name\n        createdAt\n        orders(first: 5) {\n          id\n          status\n        }\n      }\n      nextCursor\n    }\n  }\n"): (typeof documents)["\n  query Users($first: Int!, $after: String) {\n    users(first: $first, after: $after) {\n      items {\n        id\n        email\n        name\n        createdAt\n        orders(first: 5) {\n          id\n          status\n        }\n      }\n      nextCursor\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query User($id: ID!) {\n    user(id: $id) {\n      id\n      email\n      name\n      createdAt\n    }\n    orders(userId: $id, first: 50) {\n      items {\n        id\n        status\n        totalCents\n        createdAt\n        lines {\n          sku\n          quantity\n          unitPriceCents\n        }\n      }\n      nextCursor\n    }\n  }\n"): (typeof documents)["\n  query User($id: ID!) {\n    user(id: $id) {\n      id\n      email\n      name\n      createdAt\n    }\n    orders(userId: $id, first: 50) {\n      items {\n        id\n        status\n        totalCents\n        createdAt\n        lines {\n          sku\n          quantity\n          unitPriceCents\n        }\n      }\n      nextCursor\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  query Orders($first: Int!, $after: String) {\n    orders(first: $first, after: $after) {\n      items {\n        id\n        userId\n        status\n        totalCents\n        createdAt\n      }\n      nextCursor\n    }\n  }\n"): (typeof documents)["\n  query Orders($first: Int!, $after: String) {\n    orders(first: $first, after: $after) {\n      items {\n        id\n        userId\n        status\n        totalCents\n        createdAt\n      }\n      nextCursor\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CreateUser($input: UserInput!, $key: String) {\n    createUser(input: $input, idempotencyKey: $key) {\n      id\n      email\n      name\n    }\n  }\n"): (typeof documents)["\n  mutation CreateUser($input: UserInput!, $key: String) {\n    createUser(input: $input, idempotencyKey: $key) {\n      id\n      email\n      name\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation DeleteUser($id: ID!) {\n    deleteUser(id: $id)\n  }\n"): (typeof documents)["\n  mutation DeleteUser($id: ID!) {\n    deleteUser(id: $id)\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CreateOrder($input: OrderInput!, $key: String) {\n    createOrder(input: $input, idempotencyKey: $key) {\n      id\n      status\n      totalCents\n    }\n  }\n"): (typeof documents)["\n  mutation CreateOrder($input: OrderInput!, $key: String) {\n    createOrder(input: $input, idempotencyKey: $key) {\n      id\n      status\n      totalCents\n    }\n  }\n"];
/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "\n  mutation CancelOrder($id: ID!) {\n    cancelOrder(id: $id) {\n      id\n      status\n    }\n  }\n"): (typeof documents)["\n  mutation CancelOrder($id: ID!) {\n    cancelOrder(id: $id) {\n      id\n      status\n    }\n  }\n"];

export function graphql(source: string) {
  return (documents as any)[source] ?? {};
}

export type DocumentType<TDocumentNode extends DocumentNode<any, any>> = TDocumentNode extends DocumentNode<  infer TType,  any>  ? TType  : never;