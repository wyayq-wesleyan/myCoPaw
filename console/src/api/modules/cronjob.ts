import { request } from "../request";
import { getAgentScopedApiPath } from "../authHeaders";
import type {
  CronDispatchTargetsResponse,
  CronJobExecutionRecord,
  CronJobSpecInput,
  CronJobSpecOutput,
  CronJobView,
} from "../types";

export const cronJobApi = {
  listCronJobs: () =>
    request<CronJobSpecOutput[]>(getAgentScopedApiPath("/cron/jobs")),

  createCronJob: (spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>(getAgentScopedApiPath("/cron/jobs"), {
      method: "POST",
      body: JSON.stringify(spec),
    }),

  getCronJob: (jobId: string) =>
    request<CronJobView>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}`),
    ),

  replaceCronJob: (jobId: string, spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}`),
      {
        method: "PUT",
        body: JSON.stringify(spec),
      },
    ),

  deleteCronJob: (jobId: string) =>
    request<void>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}`),
      { method: "DELETE" },
    ),

  pauseCronJob: (jobId: string) =>
    request<void>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/pause`),
      { method: "POST" },
    ),

  resumeCronJob: (jobId: string) =>
    request<void>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/resume`),
      { method: "POST" },
    ),

  runCronJob: (jobId: string) =>
    request<void>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/run`),
      { method: "POST" },
    ),

  triggerCronJob: (jobId: string) =>
    request<void>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/run`),
      { method: "POST" },
    ),

  getCronJobState: (jobId: string) =>
    request<unknown>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/state`),
    ),

  getCronJobHistory: (jobId: string) =>
    request<CronJobExecutionRecord[]>(
      getAgentScopedApiPath(`/cron/jobs/${encodeURIComponent(jobId)}/history`),
    ),

  listCronDispatchTargets: (params?: {
    channel?: string;
    keyword?: string;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.channel) searchParams.append("channel", params.channel);
    if (params?.keyword) searchParams.append("keyword", params.keyword);
    const query = searchParams.toString();
    return request<CronDispatchTargetsResponse>(
      getAgentScopedApiPath(
        `/cron/dispatch-targets${query ? `?${query}` : ""}`,
      ),
    );
  },
};
