import { createContext, useContext } from "react";

/**
 * Whether changes are paused: while the tenant is being moved to another cell it can
 * read everything, and the cell refuses every change (`tenant_migrating`). The screens
 * disable their change buttons and say why, rather than let a click fail.
 */
export interface ReadOnly {
  readOnly: boolean;
  /** Why changes are paused, shown on every disabled button. */
  reason: string | undefined;
}

export const ReadOnlyContext = createContext<ReadOnly>({ readOnly: false, reason: undefined });

export function useReadOnly(): ReadOnly {
  return useContext(ReadOnlyContext);
}
