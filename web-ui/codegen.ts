import type { CodegenConfig } from "@graphql-codegen/cli";

// Types for every query and mutation in src/, generated from mgr's published schema
// (the file mgr's tests keep in step with its code). Run `pnpm codegen` after either
// changes; the generated files are committed.
const config: CodegenConfig = {
  schema: "../src/cellarch/mgr/schema.graphql",
  documents: ["src/**/*.{ts,tsx}", "!src/gql/**"],
  generates: {
    "src/gql/": {
      preset: "client",
      config: { useTypeImports: true, enumsAsTypes: true },
      presetConfig: { fragmentMasking: false },
    },
  },
};

export default config;
