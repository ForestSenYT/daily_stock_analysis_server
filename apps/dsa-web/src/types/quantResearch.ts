/**
 * Quant Research Lab API type definitions.
 *
 * Mirrors `src/quant_research/schemas.py` (backend pydantic models).
 * snake_case → camelCase happens in `api/utils.toCamelCase`, so the
 * shapes here are camelCase.
 */

// ============ Status / capabilities ============

export type QuantStatusKind = 'not_enabled' | 'ready' | 'operational';

export interface QuantStatus {
  enabled: boolean;
  status: QuantStatusKind | string;
  message: string;
  phase: string;
}

export interface QuantCapability {
  name: string;
  title: string;
  available: boolean;
  phase: string;
  description: string;
  endpoints: string[];
  requiresOptionalDeps: string[];
}

export interface QuantCapabilities {
  enabled: boolean;
  capabilities: QuantCapability[];
}

// ============ Factor registry ============

export interface BuiltinFactor {
  id: string;
  name: string;
  description: string;
  expectedDirection: string;
  lookbackDays: number;
}

export interface FactorRegistryResponse {
  enabled: boolean;
  builtins: BuiltinFactor[];
}

// ============ Factor evaluation ============

export interface FactorEvaluationRequest {
  factor: {
    name?: string;
    builtinId?: string;
    expression?: string;
  };
  stocks: string[];
  startDate: string;
  endDate: string;
  forwardWindow: number;
  quantileCount: number;
}

export interface FactorCoverageReport {
  requestedStocks: string[];
  coveredStocks: string[];
  missingStocks: string[];
  requestedDays: number;
  totalObservations: number;
  missingObservations: number;
  missingRate: number | null;
}

export interface FactorMetricSummary {
  ic: Array<number | null>;
  rankIc: Array<number | null>;
  dailyIcCount: number;
  dailyRankIcCount: number;
  icMean: number | null;
  icStd: number | null;
  icir: number | null;
  rankIcMean: number | null;
  quantileCount: number;
  quantileReturns: Record<string, number | null>;
  longShortSpread: number | null;
  factorTurnover: number | null;
  autocorrelation: number | null;
}

export interface FactorEvaluationResult {
  enabled: boolean;
  runId: string;
  factor: { name?: string; builtinId?: string; expression?: string };
  factorKind: 'builtin' | 'expression';
  stockPool: string[];
  startDate: string;
  endDate: string;
  forwardWindow: number;
  quantileCount: number;
  coverage: FactorCoverageReport;
  metrics: FactorMetricSummary;
  diagnostics: string[];
  assumptions: Record<string, unknown>;
}

// ============ Research backtest ============

export type ResearchBacktestStrategy =
  | 'top_k_long_only'
  | 'quantile_long_short'
  | 'equal_weight_baseline';

export type ResearchBacktestRebalance = 'daily' | 'weekly' | 'monthly';

export interface ResearchBacktestRequest {
  strategy: ResearchBacktestStrategy;
  stocks: string[];
  startDate: string;
  endDate: string;
  rebalanceFrequency: ResearchBacktestRebalance;
  builtinFactorId?: string;
  expression?: string;
  factorName?: string;
  topK?: number;
  quantileCount?: number;
  initialCash?: number;
  commissionBps?: number;
  slippageBps?: number;
  minHoldingDays?: number;
  benchmark?: string;
}

export interface ResearchBacktestMetrics {
  totalReturn: number | null;
  annualizedReturn: number | null;
  annualizedVolatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  maxDrawdown: number | null;
  winRate: number | null;
  turnover: number | null;
  costDrag: number | null;
  benchmarkReturn: number | null;
  excessReturn: number | null;
  informationRatio: number | null;
}

export interface ResearchBacktestPositionSnapshot {
  date: string;
  weights: Record<string, number>;
  nav: number;
  cashReserve: number;
  costDeducted: number;
}

export interface ResearchBacktestDiagnostics {
  dataCoverage: Record<string, unknown>;
  missingSymbols: string[];
  insufficientHistorySymbols: string[];
  rebalanceCount: number;
  lookaheadBiasGuard: boolean;
  assumptions: Record<string, unknown>;
}

export interface ResearchBacktestResult {
  enabled: boolean;
  runId: string;
  strategy: ResearchBacktestStrategy | string;
  factorKind: 'builtin' | 'expression' | 'n/a' | string;
  factorId: string | null;
  expression: string | null;
  stockPool: string[];
  startDate: string;
  endDate: string;
  rebalanceFrequency: ResearchBacktestRebalance | string;
  navCurve: Array<{ date: string; nav: number; [key: string]: unknown }>;
  metrics: ResearchBacktestMetrics;
  diagnostics: ResearchBacktestDiagnostics;
  positions: ResearchBacktestPositionSnapshot[];
  createdAt: string;
}
