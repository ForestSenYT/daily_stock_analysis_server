/**
 * Firstrade read-only broker API client.
 *
 * Reuses the shared axios `apiClient` (auth + 401 redirect + parsed
 * error envelope) — does not introduce a parallel HTTP stack.
 * Responses go through the same `toCamelCase` transform every other
 * API client uses, so we always see camelCase keys on the frontend.
 *
 * **Read-only**: this module exposes no place_order / cancel_order
 * surface. The corresponding endpoints don't exist on the backend
 * either; if you find yourself adding one here, stop.
 */

import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  BrokerSnapshotResponse,
  BrokerStatus,
  FirstradeLoginResponse,
  FirstradeSyncResponse,
} from '../types/broker';

export const brokerApi = {
  /**
   * Master flag + login state + last sync run metadata.
   * Safe to call when the feature is disabled — never raises 5xx.
   */
  getStatus: async (): Promise<BrokerStatus> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/broker/firstrade/status',
    );
    return toCamelCase<BrokerStatus>(response.data);
  },

  /**
   * Open or resume an FTSession. Returns ``status: "mfa_required"``
   * when the vendor SDK signals a verification code is needed.
   */
  login: async (): Promise<FirstradeLoginResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/broker/firstrade/login',
      {},
    );
    return toCamelCase<FirstradeLoginResponse>(response.data);
  },

  /**
   * Submit the MFA code obtained out-of-band. Throws via the shared
   * axios interceptor on 409 (``session_lost``) — the panel handles
   * that by resetting back to the "login" state.
   */
  verifyMfa: async (code: string): Promise<FirstradeLoginResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/broker/firstrade/login/verify',
      { code },
    );
    return toCamelCase<FirstradeLoginResponse>(response.data);
  },

  /**
   * Pull a fresh snapshot from Firstrade into the local SQLite.
   * Always writes a sync_run row; the response's `status` field
   * disambiguates success vs failure.
   */
  sync: async (params: { dateRange?: string } = {}): Promise<FirstradeSyncResponse> => {
    const body: Record<string, unknown> = {};
    if (params.dateRange) {
      body.date_range = params.dateRange;
    }
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/broker/firstrade/sync',
      body,
    );
    return toCamelCase<FirstradeSyncResponse>(response.data);
  },

  /**
   * Full local snapshot: accounts + balances + positions + orders +
   * transactions. The agent tool consumes the same data via the agent
   * registry; this method is for the WebUI panel.
   */
  getSnapshot: async (): Promise<BrokerSnapshotResponse> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/broker/firstrade/snapshot',
    );
    return toCamelCase<BrokerSnapshotResponse>(response.data);
  },
};
