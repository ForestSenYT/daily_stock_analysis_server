/**
 * AI sandbox + training-label API client.
 *
 * Phase A invariant: never hits live trading paths. All endpoints
 * 503 when AI_SANDBOX_ENABLED=false; the panel hides itself in that
 * case via `getStatus()`.
 */

import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  AISandboxStatus,
  LabelKind,
  LabelSourceKind,
  PnlComputeResult,
  SandboxExecutionList,
  SandboxMetrics,
  TrainingLabel,
  TrainingLabelList,
  TrainingLabelStats,
} from '../types/aiSandbox';

export const aiSandboxApi = {
  getStatus: async (): Promise<AISandboxStatus> => {
    const r = await apiClient.get<Record<string, unknown>>(
      '/api/v1/ai-sandbox/status',
    );
    return toCamelCase<AISandboxStatus>(r.data);
  },

  runOnce: async (
    symbol: string,
    promptVersion?: string,
  ): Promise<Record<string, unknown>> => {
    const r = await apiClient.post<Record<string, unknown>>(
      '/api/v1/ai-sandbox/run-once',
      { symbol, prompt_version: promptVersion ?? null },
    );
    return r.data;
  },

  runBatch: async (
    symbols: string[],
    promptVersion?: string,
  ): Promise<{ submitted: string[]; held: string[]; skipped: string[]; total: number }> => {
    const r = await apiClient.post<Record<string, unknown>>(
      '/api/v1/ai-sandbox/run-batch',
      { symbols, prompt_version: promptVersion ?? null },
    );
    return r.data as {
      submitted: string[]; held: string[]; skipped: string[]; total: number;
    };
  },

  listExecutions: async (params: {
    agentRunId?: string;
    symbol?: string;
    status?: 'pending' | 'filled' | 'blocked' | 'failed';
    promptVersion?: string;
    limit?: number;
  } = {}): Promise<SandboxExecutionList> => {
    const query: Record<string, string | number> = {};
    if (params.agentRunId) query.agent_run_id = params.agentRunId;
    if (params.symbol) query.symbol = params.symbol;
    if (params.status) query.status = params.status;
    if (params.promptVersion) query.prompt_version = params.promptVersion;
    if (params.limit !== undefined) query.limit = params.limit;
    const r = await apiClient.get<Record<string, unknown>>(
      '/api/v1/ai-sandbox/executions', { params: query },
    );
    return toCamelCase<SandboxExecutionList>(r.data);
  },

  getMetrics: async (params: {
    sinceDays?: number;
    promptVersion?: string;
    symbol?: string;
  } = {}): Promise<SandboxMetrics> => {
    const query: Record<string, string | number> = {};
    if (params.sinceDays !== undefined) query.since_days = params.sinceDays;
    if (params.promptVersion) query.prompt_version = params.promptVersion;
    if (params.symbol) query.symbol = params.symbol;
    const r = await apiClient.get<Record<string, unknown>>(
      '/api/v1/ai-sandbox/metrics', { params: query },
    );
    return toCamelCase<SandboxMetrics>(r.data);
  },

  computePnl: async (limit = 50): Promise<PnlComputeResult> => {
    const r = await apiClient.post<Record<string, unknown>>(
      `/api/v1/ai-sandbox/pnl/compute?limit=${limit}`,
    );
    return toCamelCase<PnlComputeResult>(r.data);
  },
};

export const aiTrainingApi = {
  upsertLabel: async (
    sourceKind: LabelSourceKind,
    sourceId: number,
    label: LabelKind,
    outcomeText?: string,
    userNotes?: string,
  ): Promise<TrainingLabel> => {
    const r = await apiClient.post<Record<string, unknown>>(
      '/api/v1/ai-training/labels',
      {
        source_kind: sourceKind,
        source_id: sourceId,
        label,
        outcome_text: outcomeText ?? null,
        user_notes: userNotes ?? null,
      },
    );
    return toCamelCase<TrainingLabel>(r.data);
  },

  deleteLabel: async (
    sourceKind: LabelSourceKind,
    sourceId: number,
  ): Promise<{ deleted: boolean }> => {
    const r = await apiClient.delete<Record<string, unknown>>(
      `/api/v1/ai-training/labels?source_kind=${sourceKind}&source_id=${sourceId}`,
    );
    return r.data as { deleted: boolean };
  },

  listLabels: async (params: {
    sourceKind?: LabelSourceKind;
    label?: LabelKind;
    limit?: number;
  } = {}): Promise<TrainingLabelList> => {
    const query: Record<string, string | number> = {};
    if (params.sourceKind) query.source_kind = params.sourceKind;
    if (params.label) query.label = params.label;
    if (params.limit !== undefined) query.limit = params.limit;
    const r = await apiClient.get<Record<string, unknown>>(
      '/api/v1/ai-training/labels', { params: query },
    );
    return toCamelCase<TrainingLabelList>(r.data);
  },

  getStats: async (): Promise<TrainingLabelStats> => {
    const r = await apiClient.get<Record<string, unknown>>(
      '/api/v1/ai-training/labels/stats',
    );
    return toCamelCase<TrainingLabelStats>(r.data);
  },
};
