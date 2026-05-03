import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { brokerApi } from '../../api/broker';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import {
  ApiErrorAlert,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  Input,
} from '../common';
import { cn } from '../../utils/cn';
import type {
  BrokerSnapshotResponse,
  BrokerStatus,
} from '../../types/broker';

/**
 * Read-only Firstrade sync panel.
 *
 * UX state machine:
 *   disabled        — feature flag is false
 *   not_installed   — flag is on but `firstrade` package missing
 *   logged_out      — no FTSession (default)
 *   mfa_required    — login() returned mfa_required
 *   syncing         — sync_now() running
 *   synced (stale)  — got snapshot; stale flag if as_of older than threshold
 *
 * **Hard rules** the panel enforces at the UI layer (defence-in-depth
 * on top of the backend redaction):
 *   - No order / cancel buttons. The corresponding endpoints don't
 *     exist server-side either.
 *   - Username / password / MFA / pin are kept ONLY in component state
 *     while the panel is mounted; we never write them to localStorage,
 *     sessionStorage, indexedDB, or any persistent surface.
 *   - The panel never logs the snapshot payload to the console; the
 *     backend redacts but a careless `console.log(response)` would
 *     still surface enough metadata to be uncomfortable.
 */

type PanelMode =
  | 'loading_status'
  | 'disabled'
  | 'not_installed'
  | 'logged_out'
  | 'mfa_required'
  | 'logged_in'
  | 'syncing';

const STALE_THRESHOLD_SECONDS = 3600; // 1h, matches agent tool default

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(2)}%`;
}

function formatSigned(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '--';
  const v = Number(value);
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(2)}`;
}

function formatSignedPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '--';
  const v = Number(value);
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(2)}%`;
}

function ageInSeconds(asOf: string | null | undefined): number | null {
  if (!asOf) return null;
  const parsed = Date.parse(asOf);
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, Math.floor((Date.now() - parsed) / 1000));
}

function describeAge(seconds: number | null): string {
  if (seconds == null) return '--';
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

const FirstradeSyncPanel: React.FC = () => {
  const [mode, setMode] = useState<PanelMode>('loading_status');
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [snapshot, setSnapshot] = useState<BrokerSnapshotResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [mfaCode, setMfaCode] = useState('');
  const [busy, setBusy] = useState<'login' | 'verify' | 'sync' | null>(null);
  const [info, setInfo] = useState<{ tone: 'success' | 'warning' | 'info'; text: string } | null>(
    null,
  );

  // ---- Loaders ------------------------------------------------------

  const refreshStatus = useCallback(async () => {
    try {
      const next = await brokerApi.getStatus();
      setStatus(next);
      if (!next.enabled) {
        setMode('disabled');
        return;
      }
      if (next.status === 'not_installed') {
        setMode('not_installed');
        return;
      }
      setMode(next.loggedIn ? 'logged_in' : 'logged_out');
    } catch (err) {
      setError(getParsedApiError(err));
      setMode('logged_out');
    }
  }, []);

  const refreshSnapshot = useCallback(async () => {
    try {
      const next = await brokerApi.getSnapshot();
      setSnapshot(next);
    } catch (err) {
      setError(getParsedApiError(err));
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    void refreshSnapshot();
  }, [refreshStatus, refreshSnapshot]);

  // ---- Actions ------------------------------------------------------

  const handleLogin = useCallback(async () => {
    setBusy('login');
    setError(null);
    setInfo(null);
    try {
      const result = await brokerApi.login();
      if (result.status === 'mfa_required') {
        setMode('mfa_required');
        setInfo({
          tone: 'info',
          text: '已发送 / 需要 MFA 验证码。请在下方输入收到的验证码。',
        });
      } else if (result.status === 'ok') {
        setMode('logged_in');
        setInfo({ tone: 'success', text: '登录成功，账户已就绪。' });
        await refreshStatus();
      } else {
        setError({
          title: 'Firstrade 登录失败',
          message: result.message || '登录返回了未知状态。',
          rawMessage: result.message || result.status,
          category: 'unknown',
        });
      }
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [refreshStatus]);

  const handleVerifyMfa = useCallback(async () => {
    if (!mfaCode.trim()) return;
    setBusy('verify');
    setError(null);
    setInfo(null);
    try {
      const result = await brokerApi.verifyMfa(mfaCode.trim());
      if (result.status === 'ok') {
        setMode('logged_in');
        setMfaCode('');
        setInfo({ tone: 'success', text: 'MFA 验证通过。' });
        await refreshStatus();
      } else {
        setError({
          title: 'MFA 验证失败',
          message: result.message || '验证码错误，请重试。',
          rawMessage: result.message || result.status,
          category: 'unknown',
        });
      }
    } catch (err) {
      const parsed = getParsedApiError(err);
      // 409 = session_lost — reset the flow.
      if (parsed.status === 409) {
        setMode('logged_out');
        setMfaCode('');
        setInfo({
          tone: 'warning',
          text: 'MFA 会话已过期（云实例可能被回收）。请重新登录。',
        });
      } else {
        setError(parsed);
      }
    } finally {
      setBusy(null);
    }
  }, [mfaCode, refreshStatus]);

  const handleSyncNow = useCallback(async () => {
    setBusy('sync');
    setMode('syncing');
    setError(null);
    setInfo(null);
    try {
      const result = await brokerApi.sync({});
      if (result.status === 'ok') {
        setInfo({
          tone: 'success',
          text: `同步完成：${result.accountCount} 个账户、${result.positionCount} 个持仓、${result.orderCount} 个订单、${result.transactionCount} 条交易记录。`,
        });
      } else if (result.status === 'login_required') {
        setMode('logged_out');
        setInfo({ tone: 'warning', text: '会话失效，请先重新登录。' });
        return;
      } else {
        setError({
          title: '同步失败',
          message: result.message || '同步未返回成功状态。',
          rawMessage: result.message || result.status,
          category: 'unknown',
        });
      }
      await Promise.all([refreshStatus(), refreshSnapshot()]);
      setMode('logged_in');
    } catch (err) {
      setError(getParsedApiError(err));
      setMode('logged_in');
    } finally {
      setBusy(null);
    }
  }, [refreshStatus, refreshSnapshot]);

  // ---- Derived UI state --------------------------------------------

  const lastSync = status?.lastSync ?? null;
  const snapshotAge = useMemo(() => ageInSeconds(snapshot?.asOf), [snapshot]);
  const isStale = snapshotAge != null && snapshotAge > STALE_THRESHOLD_SECONDS;
  const totalPositions = snapshot?.positions?.length ?? 0;
  // Show ALL positions (not capped at 10) — the typical retail user
  // has 5-30 positions and wants the full picture for monitoring.
  // The API itself caps server-side; UI doesn't need a second limit.
  const previewPositions = useMemo(
    () => snapshot?.positions ?? [],
    [snapshot],
  );

  // Total notional across all positions (used for weight_pct).
  const totalMarketValue = useMemo(
    () => previewPositions.reduce(
      (acc, p) => acc + (p.payload?.marketValue ?? 0),
      0,
    ),
    [previewPositions],
  );

  // ---- Render -------------------------------------------------------

  return (
    <Card padding="md">
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">Firstrade 只读同步</h2>
          <p className="mt-1 text-xs text-secondary-text">
            将 Firstrade 账户、余额、持仓、订单、交易历史快照同步到本地数据库；
            <strong className="mx-1">研究专用 · 永远不会下单或撤单</strong>
            。Agent 只读取本地快照，不会自己登录或同步 Firstrade。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {status?.readOnly ? <Badge variant="success">read-only</Badge> : null}
          {mode === 'disabled' ? (
            <Badge variant="warning">未启用</Badge>
          ) : mode === 'not_installed' ? (
            <Badge variant="warning">缺少依赖</Badge>
          ) : mode === 'logged_in' ? (
            <Badge variant="success">已登录</Badge>
          ) : mode === 'mfa_required' ? (
            <Badge variant="info">需要 MFA</Badge>
          ) : mode === 'syncing' ? (
            <Badge variant="info">同步中</Badge>
          ) : (
            <Badge variant="default">未登录</Badge>
          )}
        </div>
      </header>

      {error ? <ApiErrorAlert error={error} className="mb-3" /> : null}
      {info ? (
        <InlineAlert
          variant={info.tone}
          message={info.text}
          className="mb-3"
        />
      ) : null}
      {isStale ? (
        <InlineAlert
          variant="warning"
          title="本地快照已过期"
          message={`最近同步在 ${describeAge(snapshotAge)}。如需基于最新数据决策，请点击「立即同步」。`}
          className="mb-3"
        />
      ) : null}

      {mode === 'disabled' ? (
        <EmptyState
          title="Firstrade 只读同步未启用"
          description="请在 .env 中设置 BROKER_FIRSTRADE_ENABLED=true 并配置 BROKER_ACCOUNT_HASH_SALT，安装 requirements-broker.txt 后重启服务。"
        />
      ) : mode === 'not_installed' ? (
        <EmptyState
          title="缺少 firstrade 依赖"
          description="请在服务器上运行 `pip install -r requirements-broker.txt` 后重启服务。"
        />
      ) : (
        <div className="space-y-3">
          {mode !== 'logged_in' && mode !== 'syncing' ? (
            <div className="space-y-2">
              <p className="text-xs text-secondary-text">
                登录信息从服务端 .env 读取（不会在浏览器存储）。点击下方按钮使用配置的凭证发起登录。
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  isLoading={busy === 'login'}
                  loadingText="登录中..."
                  onClick={() => void handleLogin()}
                >
                  登录 Firstrade
                </Button>
                {mode === 'mfa_required' ? (
                  <>
                    <Input
                      placeholder="请输入收到的 MFA 验证码"
                      value={mfaCode}
                      onChange={(e) => setMfaCode(e.target.value)}
                      type="text"
                      iconType="key"
                      className="max-w-[18rem]"
                    />
                    <Button
                      variant="secondary"
                      size="sm"
                      isLoading={busy === 'verify'}
                      loadingText="验证中..."
                      onClick={() => void handleVerifyMfa()}
                      disabled={mfaCode.trim().length < 4}
                    >
                      验证 MFA
                    </Button>
                  </>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              isLoading={busy === 'sync'}
              loadingText="同步中..."
              onClick={() => void handleSyncNow()}
              disabled={mode !== 'logged_in' && mode !== 'syncing'}
            >
              立即同步
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void refreshStatus();
                void refreshSnapshot();
              }}
            >
              刷新本地快照
            </Button>
            {lastSync?.startedAt ? (
              <span className="text-xs text-secondary-text">
                上次同步：{describeAge(ageInSeconds(lastSync.finishedAt ?? lastSync.startedAt))}
                ｜ 状态：{lastSync.status}
              </span>
            ) : null}
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <SnapshotStat label="账户" value={String(snapshot?.accounts?.length ?? 0)} />
            <SnapshotStat label="持仓" value={String(totalPositions)} />
            <SnapshotStat label="订单" value={String(snapshot?.orders?.length ?? 0)} />
          </div>

          {totalPositions === 0 ? (
            <EmptyState
              title="尚无本地持仓快照"
              description="完成一次「立即同步」后，这里会显示你的真实持仓概览（脱敏账户别名）。"
              className="bg-card/45"
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border/40 bg-card/30">
              <table className="w-full min-w-[680px] text-xs">
                <thead className="text-muted-text">
                  <tr className="text-left">
                    <th className="px-3 py-2">代号</th>
                    <th className="px-3 py-2 text-right">数量</th>
                    <th className="px-3 py-2 text-right">最后成交价</th>
                    <th className="px-3 py-2 text-right">变更$</th>
                    <th className="px-3 py-2 text-right">变更%</th>
                    <th className="px-3 py-2 text-right">市值</th>
                    <th className="px-3 py-2 text-right">浮盈</th>
                    <th className="px-3 py-2 text-right">权重</th>
                  </tr>
                </thead>
                <tbody>
                  {previewPositions.map((p) => {
                    const payload = p.payload || {};
                    const marketValue = payload.marketValue ?? null;
                    const weightPct =
                      marketValue != null && totalMarketValue > 0
                        ? (marketValue / totalMarketValue) * 100
                        : null;
                    const dayChange = payload.dayChange ?? null;
                    const dayChangePct = payload.dayChangePct ?? null;
                    const unrealized = payload.unrealizedPnl ?? null;
                    return (
                      <tr
                        key={`${p.accountHash}-${p.symbol}-${p.id ?? ''}`}
                        className="border-t border-border/40"
                      >
                        <td className="px-3 py-2 font-mono text-foreground">
                          {p.symbol || payload.symbol || '--'}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {formatNumber(payload.quantity ?? null, 0)}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {formatNumber(payload.lastPrice ?? null)}
                        </td>
                        <td
                          className={cn(
                            'px-3 py-2 text-right font-mono',
                            dayChange == null
                              ? 'text-muted-text'
                              : dayChange >= 0
                                ? 'text-success'
                                : 'text-danger',
                          )}
                        >
                          {formatSigned(dayChange)}
                        </td>
                        <td
                          className={cn(
                            'px-3 py-2 text-right font-mono',
                            dayChangePct == null
                              ? 'text-muted-text'
                              : dayChangePct >= 0
                                ? 'text-success'
                                : 'text-danger',
                          )}
                        >
                          {formatSignedPct(dayChangePct)}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {formatNumber(marketValue)}
                        </td>
                        <td
                          className={cn(
                            'px-3 py-2 text-right font-mono',
                            unrealized == null
                              ? 'text-muted-text'
                              : unrealized >= 0
                                ? 'text-success'
                                : 'text-danger',
                          )}
                        >
                          {formatNumber(unrealized)}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {formatPct(weightPct)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr className="border-t border-border/60 bg-card/20">
                    <td className="px-3 py-2 font-mono font-semibold">合计</td>
                    <td colSpan={4} />
                    <td className="px-3 py-2 text-right font-mono font-semibold">
                      {formatNumber(totalMarketValue)}
                    </td>
                    <td colSpan={2} />
                  </tr>
                </tfoot>
              </table>
            </div>
          )}

          <p className="text-xs text-warning">
            研究专用：本面板与 Agent 工具均
            <strong className="mx-1">不会下单、不会撤单、不会触发期权交易</strong>
            。仅展示来自最近一次同步的本地脱敏快照。
          </p>
        </div>
      )}
    </Card>
  );
};

const SnapshotStat: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <Card padding="sm" variant="bordered">
    <span className="label-uppercase">{label}</span>
    <span className="mt-1 block text-base font-semibold text-foreground">{value}</span>
  </Card>
);

export default FirstradeSyncPanel;
