/**
 * Pure data shaping for the account usage page.
 *
 * Kept out of the component so the filtering/totalling rules — which the
 * "totals must agree with the chart" requirement depends on — can be tested
 * without rendering.
 */
import type { AccountUsageSummary } from "../../../../api/types/tokenUsage";

/** Local row key for usage that could not be attributed to an account. */
export const UNATTRIBUTED_KEY = "__unattributed__";

export interface AccountUsageRow {
  key: string;
  account: string;
  label: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  call_count: number;
}

export interface UsageTotals {
  prompt: number;
  completion: number;
  total: number;
  calls: number;
}

export const EMPTY_TOTALS: UsageTotals = {
  prompt: 0,
  completion: 0,
  total: 0,
  calls: 0,
};

/**
 * Flatten the summary into one row per account, heaviest first.
 *
 * Unattributed usage (recorded before the account dimension existed) is
 * surfaced as its own row rather than dropped, so the rows always sum back to
 * the server's totals.
 */
export function buildAccountRows(
  summary: AccountUsageSummary | null,
  unattributedLabel: string,
): AccountUsageRow[] {
  if (!summary) return [];

  const rows: AccountUsageRow[] = Object.values(summary.by_user ?? {}).map(
    (account) => {
      const prompt = account.prompt_tokens ?? 0;
      const completion = account.completion_tokens ?? 0;
      return {
        key: account.user_id,
        account: account.user_id,
        label: account.user_id,
        prompt_tokens: prompt,
        completion_tokens: completion,
        total_tokens: prompt + completion,
        call_count: account.call_count ?? 0,
      };
    },
  );

  const unattributed = summary.unattributed;
  if (unattributed && (unattributed.call_count ?? 0) > 0) {
    const prompt = unattributed.prompt_tokens ?? 0;
    const completion = unattributed.completion_tokens ?? 0;
    rows.push({
      key: UNATTRIBUTED_KEY,
      account: UNATTRIBUTED_KEY,
      label: unattributedLabel,
      prompt_tokens: prompt,
      completion_tokens: completion,
      total_tokens: prompt + completion,
      call_count: unattributed.call_count ?? 0,
    });
  }

  return rows.sort((a, b) => b.total_tokens - a.total_tokens);
}

/** Apply the administrator's account selection (a list of exclusions). */
export function filterAccountRows(
  rows: AccountUsageRow[],
  excluded: string[],
): AccountUsageRow[] {
  if (excluded.length === 0) return rows;
  const blocked = new Set(excluded);
  return rows.filter((row) => !blocked.has(row.account));
}

/**
 * Totals for the current selection.
 *
 * Derived from the visible rows — not the server's range totals — so the
 * summary cards always agree with the chart and the table.
 */
export function sumUsage(rows: AccountUsageRow[]): UsageTotals {
  return rows.reduce(
    (acc, row) => ({
      prompt: acc.prompt + row.prompt_tokens,
      completion: acc.completion + row.completion_tokens,
      total: acc.total + row.total_tokens,
      calls: acc.calls + row.call_count,
    }),
    { ...EMPTY_TOTALS },
  );
}

/** Value of a row for one chart metric. */
export function metricValue(
  row: AccountUsageRow,
  metric: "total" | "prompt" | "completion" | "calls",
): number {
  switch (metric) {
    case "prompt":
      return row.prompt_tokens;
    case "completion":
      return row.completion_tokens;
    case "calls":
      return row.call_count;
    default:
      return row.total_tokens;
  }
}
