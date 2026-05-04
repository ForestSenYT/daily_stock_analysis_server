/**
 * Trading framework API client (Phase A — paper only).
 *
 * Reuses the shared `apiClient` (auth + 401 redirect + parsed error
 * envelope). Responses go through `toCamelCase` so the frontend
 * always sees camelCase keys.
 *
 * Phase A: only `paper` is functional. `live` mode endpoints return
 * 503 with `error_code='LIVE_NOT_IMPLEMENTED'`. `disabled` mode
 * returns 503 from every endpoint and is the safe default.
 */

import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  OrderResult,
  OrderSubmitPayload,
  RiskAssessment,
  TradeExecutionList,
  TradingStatus,
} from '../types/trading';

function _payloadToBackend(
  body: OrderSubmitPayload,
): Record<string, unknown> {
  return {
    symbol: body.symbol,
    side: body.side,
    quantity: body.quantity,
    order_type: body.orderType,
    limit_price: body.limitPrice ?? null,
    time_in_force: body.timeInForce ?? 'day',
    account_id: body.accountId ?? null,
    market: body.market ?? null,
    currency: body.currency ?? null,
    note: body.note ?? null,
    request_uid: body.requestUid,
    source: body.source ?? 'ui',
    agent_session_id: body.agentSessionId ?? null,
  };
}

export const tradingApi = {
  /** Master status + thresholds. Safe to call when disabled. */
  getStatus: async (): Promise<TradingStatus> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/trading/status',
    );
    return toCamelCase<TradingStatus>(response.data);
  },

  /**
   * Submit an OrderRequest. Phase A returns 503 for live mode.
   * Idempotent via `requestUid`: duplicates → 409.
   */
  submit: async (body: OrderSubmitPayload): Promise<OrderResult> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/trading/submit',
      _payloadToBackend(body),
    );
    return toCamelCase<OrderResult>(response.data);
  },

  /** Run RiskEngine WITHOUT persisting an audit row. */
  previewRisk: async (
    body: OrderSubmitPayload,
  ): Promise<RiskAssessment> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/trading/risk/preview',
      _payloadToBackend(body),
    );
    return toCamelCase<RiskAssessment>(response.data);
  },

  /** Recent audit rows (paper / live filtered). */
  listExecutions: async (params: {
    mode?: 'paper' | 'live';
    accountId?: number;
    symbol?: string;
    status?: 'pending' | 'filled' | 'blocked' | 'failed';
    limit?: number;
  } = {}): Promise<TradeExecutionList> => {
    const query: Record<string, string | number> = {};
    if (params.mode) query.mode = params.mode;
    if (params.accountId !== undefined) query.account_id = params.accountId;
    if (params.symbol) query.symbol = params.symbol;
    if (params.status) query.status = params.status;
    if (params.limit !== undefined) query.limit = params.limit;
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/trading/executions',
      { params: query },
    );
    return toCamelCase<TradeExecutionList>(response.data);
  },
};
