import { describe, expect, it } from "vitest";
import {
  buildAccountRows,
  filterAccountRows,
  metricValue,
  sumUsage,
  UNATTRIBUTED_KEY,
} from "./aggregate";
import type { AccountUsageSummary } from "../../../api/types/tokenUsage";

const LABEL = "未归属（历史数据）";

function summary(
  partial: Partial<AccountUsageSummary>,
): AccountUsageSummary {
  return {
    total_prompt_tokens: 0,
    total_completion_tokens: 0,
    total_calls: 0,
    by_user: {},
    by_date: {},
    unattributed: {
      prompt_tokens: 0,
      completion_tokens: 0,
      call_count: 0,
    },
    ...partial,
  };
}

function account(userId: string, prompt: number, completion: number, calls: number) {
  return {
    user_id: userId,
    prompt_tokens: prompt,
    completion_tokens: completion,
    call_count: calls,
    by_date: {},
    by_model: {},
  };
}

describe("buildAccountRows", () => {
  it("flattens accounts into rows sorted by total tokens", () => {
    const rows = buildAccountRows(
      summary({
        by_user: {
          alice: account("alice", 100, 10, 2),
          bob: account("bob", 900, 90, 5),
        },
      }),
      LABEL,
    );

    expect(rows.map((row) => row.account)).toEqual(["bob", "alice"]);
    expect(rows[0].total_tokens).toBe(990);
    expect(rows[1].total_tokens).toBe(110);
  });

  it("keeps unattributed usage as its own row so rows cover the totals", () => {
    const rows = buildAccountRows(
      summary({
        by_user: { alice: account("alice", 100, 10, 2) },
        unattributed: {
          prompt_tokens: 500,
          completion_tokens: 50,
          call_count: 3,
        },
      }),
      LABEL,
    );

    const unattributed = rows.find((row) => row.key === UNATTRIBUTED_KEY);
    expect(unattributed).toBeDefined();
    expect(unattributed?.label).toBe(LABEL);
    expect(unattributed?.total_tokens).toBe(550);
    // Heaviest first puts the unattributed bucket ahead of alice.
    expect(rows.map((row) => row.account)).toEqual([
      UNATTRIBUTED_KEY,
      "alice",
    ]);
  });

  it("omits the unattributed row when there is none", () => {
    const rows = buildAccountRows(
      summary({ by_user: { alice: account("alice", 1, 1, 1) } }),
      LABEL,
    );

    expect(rows.some((row) => row.key === UNATTRIBUTED_KEY)).toBe(false);
  });

  it("handles a missing summary and empty accounts", () => {
    expect(buildAccountRows(null, LABEL)).toEqual([]);
    expect(buildAccountRows(summary({}), LABEL).map((r) => r.account)).not.toContain(
      UNATTRIBUTED_KEY,
    );
  });
});

describe("filterAccountRows", () => {
  const rows = buildAccountRows(
    summary({
      by_user: {
        alice: account("alice", 100, 10, 2),
        bob: account("bob", 200, 20, 3),
        carol: account("carol", 300, 30, 4),
      },
    }),
    LABEL,
  );

  it("returns every row when nothing is excluded (the default)", () => {
    expect(filterAccountRows(rows, [])).toHaveLength(3);
  });

  it("drops only the excluded accounts", () => {
    const kept = filterAccountRows(rows, ["bob"]);
    expect(kept.map((row) => row.account)).toEqual(["carol", "alice"]);
  });

  it("can exclude several accounts at once", () => {
    const kept = filterAccountRows(rows, ["bob", "carol"]);
    expect(kept.map((row) => row.account)).toEqual(["alice"]);
  });

  it("returns nothing when every account is excluded", () => {
    expect(filterAccountRows(rows, ["alice", "bob", "carol"])).toEqual([]);
  });
});

describe("sumUsage", () => {
  const rows = buildAccountRows(
    summary({
      by_user: {
        alice: account("alice", 100, 10, 2),
        bob: account("bob", 200, 20, 3),
      },
    }),
    LABEL,
  );

  it("totals the visible rows", () => {
    expect(sumUsage(rows)).toEqual({
      prompt: 300,
      completion: 30,
      total: 330,
      calls: 5,
    });
  });

  it("totals only what is still visible after filtering", () => {
    const visible = filterAccountRows(rows, ["bob"]);
    expect(sumUsage(visible)).toEqual({
      prompt: 100,
      completion: 10,
      total: 110,
      calls: 2,
    });
  });

  it("returns zeros for an empty selection", () => {
    expect(sumUsage([])).toEqual({
      prompt: 0,
      completion: 0,
      total: 0,
      calls: 0,
    });
  });
});

describe("metricValue", () => {
  const row = {
    key: "alice",
    account: "alice",
    label: "alice",
    prompt_tokens: 100,
    completion_tokens: 10,
    total_tokens: 110,
    call_count: 7,
  };

  it("selects each chart metric", () => {
    expect(metricValue(row, "total")).toBe(110);
    expect(metricValue(row, "prompt")).toBe(100);
    expect(metricValue(row, "completion")).toBe(10);
    expect(metricValue(row, "calls")).toBe(7);
  });

  it("defaults to total tokens", () => {
    expect(metricValue(row, "total")).toBe(row.total_tokens);
  });
});
