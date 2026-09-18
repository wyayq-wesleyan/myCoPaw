import { useCallback, useEffect, useMemo, useState } from "react";
import { DatePicker, Segmented, Select } from "antd";
import { useTranslation } from "react-i18next";
import dayjs, { type Dayjs } from "dayjs";
import { Column } from "@ant-design/plots";
import { useTheme } from "../../../contexts/ThemeContext";
import api from "../../../api";
import type { AccountUsageSummary } from "../../../api/types/tokenUsage";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { PageHeader } from "@/components/PageHeader";
import { formatCompact } from "../../../utils/formatNumber";
import { LoadingState } from "../TokenUsage/components";
import {
  buildAccountRows,
  filterAccountRows,
  metricValue,
  sumUsage,
  type AccountUsageRow,
} from "./aggregate";
import styles from "./index.module.less";

type Metric = "total" | "prompt" | "completion" | "calls";
type ChartMetric = Metric | "promptCompletion";

function AccountUsagePage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [summary, setSummary] = useState<AccountUsageSummary | null>(null);
  const [startDate, setStartDate] = useState<Dayjs>(
    dayjs().subtract(30, "day"),
  );
  const [endDate, setEndDate] = useState<Dayjs>(dayjs());
  // Accounts the administrator removed from the view; empty = all selected.
  const [excluded, setExcluded] = useState<string[]>([]);
  const [chartMetric, setChartMetric] = useState<ChartMetric>("total");

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      // Always fetch every account so the picker can list all of them and
      // toggling one is instant; narrowing happens client-side below.
      const data = await api.getAccountUsage({
        start_date: startDate.format("YYYY-MM-DD"),
        end_date: endDate.format("YYYY-MM-DD"),
      });
      setSummary(data);
    } catch (err) {
      console.error("Failed to load account usage:", err);
      message.error(t("accountUsage.loadFailed"));
      setSummary(null);
      setError(true);
    } finally {
      setLoading(false);
    }
  }, [startDate, endDate, message, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const allRows = useMemo<AccountUsageRow[]>(
    () =>
      buildAccountRows(summary, t("accountUsage.unattributed")),
    [summary, t],
  );

  const visibleRows = useMemo(
    () => filterAccountRows(allRows, excluded),
    [allRows, excluded],
  );

  const selectedAccounts = useMemo(
    () => visibleRows.map((row) => row.account),
    [visibleRows],
  );

  const totals = useMemo(() => sumUsage(visibleRows), [visibleRows]);

  const chartConfig = useMemo(() => {
    if (visibleRows.length === 0) return null;

    const promptLabel = t("tokenUsage.promptTokens");
    const completionLabel = t("tokenUsage.completionTokens");
    const data = visibleRows.flatMap((row) => {
      if (chartMetric === "promptCompletion") {
        return [
          { account: row.label, metric: promptLabel, value: row.prompt_tokens },
          {
            account: row.label,
            metric: completionLabel,
            value: row.completion_tokens,
          },
        ];
      }
      const value = metricValue(row, chartMetric);
      return [
        { account: row.label, metric: t(`accountUsage.metric.${chartMetric}`), value },
      ];
    });

    return {
      data,
      xField: "account",
      yField: "value",
      colorField: "metric",
      stack: chartMetric === "promptCompletion",
      group: chartMetric !== "promptCompletion",
      autoFit: true,
      height: 340,
      theme: isDark ? "dark" : "light",
      columnWidthRatio: 0.6,
      axis: {
        x: {
          title: null,
          labelAutoRotate: true,
          labelAutoHide: false,
        },
        y: {
          title: null,
          labelFormatter: (value: number) => formatCompact(value),
          grid: {
            line: {
              style: {
                stroke: isDark
                  ? "rgba(255, 255, 255, 0.05)"
                  : "rgba(0, 0, 0, 0.04)",
              },
            },
          },
        },
      },
      tooltip: {
        items: [
          (datum: { value: number; metric: string }) => ({
            name: datum.metric,
            value: formatCompact(datum.value),
          }),
        ],
      },
      legend: { position: "top" as const, itemMarker: "circle" },
    };
  }, [visibleRows, chartMetric, isDark, t]);

  const pickerOptions = useMemo(
    () => allRows.map((row) => ({ label: row.label, value: row.account })),
    [allRows],
  );

  const pageHeader = (
    <PageHeader parent={t("nav.settings")} current={t("accountUsage.title")} />
  );

  if (loading) {
    return (
      <div className={styles.container}>
        {pageHeader}
        <LoadingState message={t("common.loading", "Loading...")} />
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        {pageHeader}
        <LoadingState
          message={t("accountUsage.loadFailed")}
          error
          onRetry={fetchData}
        />
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {pageHeader}

      <div className={styles.content}>
        <div className={styles.toolbar}>
          <DatePicker.RangePicker
            value={[startDate, endDate]}
            onChange={(dates) => {
              if (!dates?.[0] || !dates?.[1]) return;
              setStartDate(dates[0]);
              setEndDate(dates[1]);
            }}
            disabledDate={(current) =>
              current && current.isAfter(dayjs(), "day")
            }
          />
          <Select
            mode="multiple"
            className={styles.accountPicker}
            placeholder={t("accountUsage.allAccounts")}
            value={selectedAccounts}
            options={pickerOptions}
            maxTagCount="responsive"
            allowClear
            onChange={(selected: string[]) => {
              const chosen = new Set(selected);
              setExcluded(
                allRows
                  .map((row) => row.account)
                  .filter((account) => !chosen.has(account)),
              );
            }}
          />
          <Segmented
            value={chartMetric}
            onChange={(value) => setChartMetric(value as ChartMetric)}
            options={[
              { label: t("accountUsage.metric.total"), value: "total" },
              { label: t("accountUsage.metric.prompt"), value: "prompt" },
              {
                label: t("accountUsage.metric.completion"),
                value: "completion",
              },
              { label: t("accountUsage.metric.calls"), value: "calls" },
              {
                label: t("accountUsage.metric.promptCompletion"),
                value: "promptCompletion",
              },
            ]}
          />
        </div>

        <div className={styles.summaryCards}>
          <div className={styles.card}>
            <div className={styles.cardValue}>
              {formatCompact(totals.calls)}
            </div>
            <div className={styles.cardLabel}>{t("tokenUsage.totalCalls")}</div>
          </div>
          <div className={styles.card}>
            <div className={styles.cardValue}>
              {formatCompact(totals.prompt)}
            </div>
            <div className={styles.cardLabel}>
              {t("tokenUsage.promptTokens")}
            </div>
          </div>
          <div className={styles.card}>
            <div className={styles.cardValue}>
              {formatCompact(totals.completion)}
            </div>
            <div className={styles.cardLabel}>
              {t("tokenUsage.completionTokens")}
            </div>
          </div>
          <div className={styles.card}>
            <div className={styles.cardValue}>
              {formatCompact(totals.total)}
            </div>
            <div className={styles.cardLabel}>{t("tokenUsage.totalTokens")}</div>
          </div>
        </div>

        <div className={styles.chartCard}>
          <div className={styles.chartTitle}>
            {t("accountUsage.chartTitle")}
          </div>
          {chartConfig ? (
            <Column {...chartConfig} />
          ) : (
            <div className={styles.empty}>{t("accountUsage.noData")}</div>
          )}
        </div>

        <table className={styles.table}>
          <thead>
            <tr>
              <th>{t("accountUsage.account")}</th>
              <th className={styles.numeric}>{t("tokenUsage.totalCalls")}</th>
              <th className={styles.numeric}>{t("tokenUsage.promptTokens")}</th>
              <th className={styles.numeric}>
                {t("tokenUsage.completionTokens")}
              </th>
              <th className={styles.numeric}>{t("tokenUsage.totalTokens")}</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row) => (
              <tr key={row.key}>
                <td>{row.label}</td>
                <td className={styles.numeric}>
                  {formatCompact(row.call_count)}
                </td>
                <td className={styles.numeric}>
                  {formatCompact(row.prompt_tokens)}
                </td>
                <td className={styles.numeric}>
                  {formatCompact(row.completion_tokens)}
                </td>
                <td className={styles.numeric}>
                  {formatCompact(row.total_tokens)}
                </td>
              </tr>
            ))}
            {visibleRows.length === 0 && (
              <tr>
                <td colSpan={5} className={styles.emptyCell}>
                  {t("accountUsage.noData")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default AccountUsagePage;
