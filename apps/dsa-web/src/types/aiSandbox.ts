/**
 * Types for AI sandbox + training-label APIs.
 * Mirrors `api/v1/schemas/ai_sandbox.py` after `toCamelCase`.
 */

export interface AISandboxStatus {
  status: 'disabled' | 'ready' | string;
  message?: string | null;
  maxPositionValue?: number | null;
  maxPositionPct?: number | null;
  maxDailyTurnover?: number | null;
  symbolAllowlist: string[];
  paperSlippageBps?: number | null;
  daemonEnabled?: boolean | null;
  daemonIntervalMinutes?: number | null;
  daemonWatchlist: string[];
}

export interface PnlHorizonsView {
  horizon_1d?: number | null;
  horizon_3d?: number | null;
  horizon_7d?: number | null;
  horizon_30d?: number | null;
  computed_at?: string | null;
}

export interface SandboxExecution {
  id: number;
  requestUid: string;
  symbol: string;
  side: string;
  orderType: string;
  quantity: number;
  fillPrice?: number | null;
  fillQuantity?: number | null;
  fillTime?: string | null;
  status: string;
  riskDecision?: string | null;
  agentRunId?: string | null;
  promptVersion?: string | null;
  confidenceScore?: number | null;
  reasoningText?: string | null;
  modelUsed?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  pnlHorizons?: PnlHorizonsView | null;
  requestedAt?: string | null;
  pnlComputedAt?: string | null;
}

export interface SandboxExecutionList {
  items: SandboxExecution[];
  count: number;
}

export interface SandboxMetrics {
  totalExecutions: number;
  filledCount: number;
  withPnlCount: number;
  winRate1d?: number | null;
  winRate7d?: number | null;
  avgPnl1dPct?: number | null;
  avgPnl7dPct?: number | null;
  filters: Record<string, unknown>;
}

export interface PnlComputeResult {
  scanned: number;
  computed: number;
  skipped: number;
}

export type LabelKind = 'correct' | 'incorrect' | 'unclear';
export type LabelSourceKind = 'analysis_history' | 'ai_sandbox';

export interface TrainingLabel {
  id: number;
  sourceKind: LabelSourceKind | string;
  sourceId: number;
  label: LabelKind | string;
  outcomeText?: string | null;
  userNotes?: string | null;
  createdBy?: string | null;
  createdAt?: string | null;
}

export interface TrainingLabelList {
  items: TrainingLabel[];
  count: number;
}

export interface TrainingLabelStats {
  total: number;
  correct: number;
  incorrect: number;
  unclear: number;
  fromAnalysisHistory: number;
  fromAiSandbox: number;
}
