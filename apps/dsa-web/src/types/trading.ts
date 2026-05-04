/**
 * TypeScript types for the trading framework Phase A
 * (`/api/v1/trading/*`).
 *
 * Mirrors `api/v1/schemas/trading.py` after `toCamelCase` on the
 * frontend boundary. Phase A is paper-only; live mode is wired but
 * stubs out at the executor — endpoints return 503 with
 * `error_code='LIVE_NOT_IMPLEMENTED'` if `TRADING_MODE=live`.
 */

export type TradingMode = 'disabled' | 'paper' | 'live';
export type OrderSide = 'buy' | 'sell';
export type OrderTypeKind = 'market' | 'limit';
export type TimeInForceKind = 'day' | 'gtc';
export type ExecutionStatusKind =
  | 'pending'
  | 'filled'
  | 'blocked'
  | 'failed';
export type RiskSeverityKind = 'info' | 'warning' | 'block';

export interface TradingStatus {
  status: 'disabled' | 'ready' | string;
  mode: TradingMode | string;
  message?: string | null;
  paperAccountId?: number | null;
  maxPositionValue?: number | null;
  maxPositionPct?: number | null;
  maxDailyTurnover?: number | null;
  symbolAllowlist: string[];
  symbolDenylist: string[];
  marketHoursStrict?: boolean | null;
  notificationEnabled?: boolean | null;
}

export interface OrderSubmitPayload {
  symbol: string;
  side: OrderSide;
  quantity: number;
  orderType: OrderTypeKind;
  limitPrice?: number | null;
  timeInForce?: TimeInForceKind;
  accountId?: number | null;
  market?: 'us' | 'cn' | 'hk' | null;
  currency?: string | null;
  note?: string | null;
  requestUid: string;
  source?: 'ui' | 'agent' | 'strategy';
  agentSessionId?: string | null;
}

export interface RiskFlag {
  code: string;
  severity: RiskSeverityKind | string;
  message: string;
  detail?: Record<string, unknown>;
}

export interface RiskAssessment {
  decision: 'allow' | 'block' | string;
  flags: RiskFlag[];
  evaluatedAt: string;
  configSnapshot: Record<string, unknown>;
}

export interface OrderResult {
  request: Record<string, unknown>;
  status: ExecutionStatusKind | string;
  mode: TradingMode | string;
  fillPrice?: number | null;
  fillQuantity?: number | null;
  fillTime?: string | null;
  realisedFee?: number;
  realisedTax?: number;
  riskAssessment?: RiskAssessment | null;
  portfolioTradeId?: number | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  quotePayload?: Record<string, unknown> | null;
}

export interface TradeExecution {
  id: number;
  requestUid: string;
  mode: string;
  source: string;
  symbol: string;
  side: string;
  orderType: string;
  quantity: number;
  limitPrice?: number | null;
  accountId?: number | null;
  market?: string | null;
  currency?: string | null;
  status: string;
  riskDecision?: string | null;
  riskFlags: RiskFlag[];
  fillPrice?: number | null;
  fillQuantity?: number | null;
  realisedFee?: number;
  realisedTax?: number;
  portfolioTradeId?: number | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  agentSessionId?: string | null;
  requestedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
  requestPayload: Record<string, unknown>;
  resultPayload?: Record<string, unknown> | null;
}

export interface TradeExecutionList {
  items: TradeExecution[];
  count: number;
}
