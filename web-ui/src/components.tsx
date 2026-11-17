import type { CombinedError } from "urql";

import { describe } from "./client";
import { explain } from "./format";

export function ErrorNotice({ error }: { error: CombinedError | string | null | undefined }) {
  if (!error) return null;
  const text = typeof error === "string" ? error : explain(describe(error));
  return (
    <p role="alert" className="alert">
      {text}
    </p>
  );
}

export function Notice({ text }: { text: string | null }) {
  return text ? (
    <p role="status" className="notice">
      {text}
    </p>
  ) : null;
}

interface PagerProps {
  nextCursor: string | null | undefined;
  onNext: (cursor: string) => void;
  onFirst: (() => void) | null;
}

/** Cursor paging: "Next" while there is more; "First page" once past the first. */
export function Pager({ nextCursor, onNext, onFirst }: PagerProps) {
  return (
    <nav aria-label="pages" className="pager">
      {onFirst && (
        <button type="button" onClick={onFirst}>
          First page
        </button>
      )}
      {nextCursor && (
        <button type="button" onClick={() => onNext(nextCursor)}>
          Next
        </button>
      )}
    </nav>
  );
}
