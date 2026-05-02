/**
 * Quant Research Lab API client.
 *
 * Reuses the shared `apiClient` (axios + auth + parsed-error
 * interceptor); does NOT introduce a parallel HTTP stack. Every
 * response goes through `toCamelCase` so callers see camelCase keys.
 *
 * ``getStatus`` swallows 401/network errors only via the existing
 * interceptor; other endpoints surface ``ParsedApiError`` exactly
 * like the rest of the app.
 */

import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  FactorEvaluationRequest,
  FactorEvaluationResult,
  FactorRegistryResponse,
  QuantCapabilities,
  QuantStatus,
  ResearchBacktestRequest,
  ResearchBacktestResult,
} from '../types/quantResearch';

// ----- request mapping helpers --------------------------------------
// Backend expects snake_case; we keep the front-end camelCase
// surface and translate at the boundary. Avoids a second key-mapping
// dependency.

function buildFactorEvaluationBody(req: FactorEvaluationRequest): Record<string, unknown> {
  const factor: Record<string, unknown> = {};
  if (req.factor.name) factor.name = req.factor.name;
  if (req.factor.builtinId) factor.builtin_id = req.factor.builtinId;
  if (req.factor.expression) factor.expression = req.factor.expression;
  return {
    factor,
    stocks: req.stocks,
    start_date: req.startDate,
    end_date: req.endDate,
    forward_window: req.forwardWindow,
    quantile_count: req.quantileCount,
  };
}

function buildBacktestBody(req: ResearchBacktestRequest): Record<string, unknown> {
  const body: Record<string, unknown> = {
    strategy: req.strategy,
    stocks: req.stocks,
    start_date: req.startDate,
    end_date: req.endDate,
    rebalance_frequency: req.rebalanceFrequency,
    quantile_count: req.quantileCount ?? 5,
  };
  if (req.builtinFactorId) body.builtin_factor_id = req.builtinFactorId;
  if (req.expression) body.expression = req.expression;
  if (req.factorName) body.factor_name = req.factorName;
  if (req.topK != null) body.top_k = req.topK;
  if (req.initialCash != null) body.initial_cash = req.initialCash;
  if (req.commissionBps != null) body.commission_bps = req.commissionBps;
  if (req.slippageBps != null) body.slippage_bps = req.slippageBps;
  if (req.minHoldingDays != null) body.min_holding_days = req.minHoldingDays;
  if (req.benchmark) body.benchmark = req.benchmark;
  return body;
}

// ----- API surface ---------------------------------------------------

export const quantResearchApi = {
  /**
   * Master flag + phase string. Safe to call regardless of the flag
   * state; never raises 5xx for a healthy service.
   */
  getStatus: async (): Promise<QuantStatus> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/quant/status',
    );
    return toCamelCase<QuantStatus>(response.data);
  },

  /**
   * Capability inventory — drives the SPA "what's live in this build"
   * banner and (later) per-tab disabled hints.
   */
  getCapabilities: async (): Promise<QuantCapabilities> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/quant/capabilities',
    );
    return toCamelCase<QuantCapabilities>(response.data);
  },

  /**
   * Built-in factor catalog. Returns `{enabled: false, builtins: []}`
   * when the lab is disabled — safe to render as an empty selector.
   */
  listFactors: async (): Promise<FactorRegistryResponse> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/quant/factors',
    );
    return toCamelCase<FactorRegistryResponse>(response.data);
  },

  /**
   * IC / RankIC / quantile evaluation. Throws ParsedApiError on
   * validation / disabled / 5xx so callers can render `ApiErrorAlert`.
   */
  evaluateFactor: async (
    request: FactorEvaluationRequest,
  ): Promise<FactorEvaluationResult> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/quant/factors/evaluate',
      buildFactorEvaluationBody(request),
    );
    return toCamelCase<FactorEvaluationResult>(response.data);
  },

  /**
   * Run a research backtest. Synchronous — the shared axios timeout
   * (30s) bounds it, and the backend caps the workload before
   * starting (≤ 50 stocks, ≤ 366 days).
   */
  runBacktest: async (
    request: ResearchBacktestRequest,
  ): Promise<ResearchBacktestResult> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/quant/backtests/run',
      buildBacktestBody(request),
    );
    return toCamelCase<ResearchBacktestResult>(response.data);
  },
};
