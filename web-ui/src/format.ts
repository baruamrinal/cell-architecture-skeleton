import type { Problem } from "./client";

const MONEY = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function formatCents(cents: number): string {
  return MONEY.format(cents / 100);
}

/** "12.5" -> 1250; null for anything that isn't a non-negative amount with ≤ 2 decimals. */
export function parseCents(text: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text.trim());
  if (!match) return null;
  const [, whole = "0", fraction = ""] = match;
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

function retry(problem: Problem): string {
  return problem.retryAfter ? ` Try again in ${problem.retryAfter} s.` : " Try again shortly.";
}

/** What a refusal means for the person using the UI. */
export function explain(problem: Problem): string {
  switch (problem.code) {
    case "tenant_migrating":
      return `Your data is being moved to another cell, so it is read-only for a moment.${retry(problem)}`;
    case "rate_limited":
      return `Too many requests.${retry(problem)}`;
    case "cell_overloaded":
      return `The service is busy.${retry(problem)}`;
    case "cell_unavailable":
    case "mgr_unavailable":
    case "tenant_stack_unavailable":
    case "cell_unreachable":
      return `The service is unavailable.${retry(problem)}`;
    case "network_error":
      return "Can't reach the service. Check your connection.";
    default:
      return problem.message;
  }
}
