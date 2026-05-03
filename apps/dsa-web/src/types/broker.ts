/**
 * TypeScript types for the Firstrade broker read-only API
 * (`/api/v1/broker/firstrade/*`).
 *
 * Mirrors `api/v1/schemas/broker.py` after `toCamelCase` on the
 * frontend boundary. These shapes intentionally do NOT include
 * credentials, cookies, full account numbers, or any vendor `raw`
 * payloads — the backend strips those before they reach the wire.
 */

export type BrokerStatusKind =
  | 'ok'
  | 'not_enabled'
  | 'not_installed'
  | 'login_required'
  | 'mfa_required'
  | 'session_lost'
  | 'failed'
  | 'no_snapshot'
  | 'stale';

export interface BrokerLastSyncRun {
  id: number;
  broker: string;
  status: string;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  accountCount: number;
  positionCount: number;
  orderCount: number;
  transactionCount: number;
  error: Record<string, unknown> | null;
}

export interface BrokerStatus {
  status: BrokerStatusKind | string;
  broker: string;
  enabled: boolean;
  loggedIn?: boolean;
  readOnly?: boolean;
  lastSync?: BrokerLastSyncRun | null;
  llmDataScope?: string;
  message?: string | null;
}

export interface FirstradeLoginResponse {
  status: BrokerStatusKind | string;
  broker: string;
  message?: string | null;
  accountCount: number;
}

export interface FirstradeSyncResponse {
  status: BrokerStatusKind | string;
  broker: string;
  message?: string | null;
  asOf?: string | null;
  accountCount: number;
  balanceCount: number;
  positionCount: number;
  orderCount: number;
  transactionCount: number;
}

export interface BrokerSnapshotAccount {
  accountAlias: string;
  accountLast4: string;
  accountHash: string;
  asOf: string | null;
}

/**
 * Snapshot rows from `/api/v1/broker/firstrade/snapshot` carry the
 * actual position fields inside ``payload`` (nested) — that matches
 * how :class:`BrokerSnapshotRepository._row_to_dict` shapes them.
 * Top-level fields are the indexed columns; everything else is in
 * ``payload``.
 */
export interface BrokerSnapshotPositionPayload {
  symbol?: string | null;
  quantity?: number | null;
  marketValue?: number | null;
  avgCost?: number | null;
  lastPrice?: number | null;
  unrealizedPnl?: number | null;
  dayChange?: number | null;
  dayChangePct?: number | null;
  currency?: string | null;
  asOf?: string | null;
  [key: string]: unknown;
}

export interface BrokerSnapshotPosition {
  id?: number;
  broker?: string;
  snapshotType?: string;
  accountHash: string;
  accountLast4: string;
  accountAlias: string;
  entityHash?: string | null;
  symbol: string | null;
  asOf: string | null;
  payload: BrokerSnapshotPositionPayload;
}

export interface BrokerSnapshotBalance {
  accountAlias: string;
  accountHash: string;
  cash: number | null;
  buyingPower: number | null;
  totalValue: number | null;
  currency: string;
  asOf: string | null;
}

export interface BrokerSnapshotOrder {
  accountAlias: string;
  symbol: string;
  orderIdHash: string;
  orderStatus: string | null;
  orderSide: string | null;
  orderType: string | null;
  orderQuantity: number | null;
  filledQuantity: number | null;
  limitPrice: number | null;
  asOf: string | null;
}

export interface BrokerSnapshotTransaction {
  accountAlias: string;
  transactionIdHash: string;
  symbol: string;
  transactionType: string | null;
  tradeDate: string | null;
  settleDate: string | null;
  amount: number | null;
  quantity: number | null;
  currency: string;
}

export interface BrokerSnapshotResponse {
  status: BrokerStatusKind | string;
  broker: string;
  message?: string | null;
  asOf?: string | null;
  lastSync?: BrokerLastSyncRun | null;
  accounts: BrokerSnapshotAccount[];
  balances: BrokerSnapshotBalance[];
  positions: BrokerSnapshotPosition[];
  orders: BrokerSnapshotOrder[];
  transactions: BrokerSnapshotTransaction[];
}
