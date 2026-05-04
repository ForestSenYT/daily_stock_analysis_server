import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { aiSandboxApi, aiTrainingApi } from '../../api/aiSandbox';
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
  AISandboxStatus,
  LabelKind,
  LabelSourceKind,
  SandboxExecution,
  SandboxMetrics,
  TrainingLabelStats,
} from '../../types/aiSandbox';

/**
 * AI Sandbox panel — Phase A+C foundation.
 *
 * Three sections:
 *   1. Status + thresholds + run-once trigger
 *   2. Recent executions table with reasoning + PnL horizons
 *   3. Training-label dataset stats + quick label form
 *
 * Hidden entirely when ``status.status === 'disabled'``.
 */

type Mode = 'loading' | 'disabled' | 'ready';

function _fmt(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function _fmtPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(2)}%`;
}

function _fmtRate(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

const AISandboxPanel: React.FC = () => {
  const [mode, setMode] = useState<Mode>('loading');
  const [status, setStatus] = useState<AISandboxStatus | null>(null);
  const [executions, setExecutions] = useState<SandboxExecution[]>([]);
  const [metrics, setMetrics] = useState<SandboxMetrics | null>(null);
  const [labelStats, setLabelStats] = useState<TrainingLabelStats | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [info, setInfo] = useState<{ tone: 'success' | 'warning' | 'info'; text: string } | null>(
    null,
  );
  const [busy, setBusy] = useState<'run-once' | 'compute-pnl' | 'label' | null>(
    null,
  );

  const [symbol, setSymbol] = useState('');
  const [labelSourceKind, setLabelSourceKind] = useState<LabelSourceKind>('ai_sandbox');
  const [labelSourceId, setLabelSourceId] = useState('');
  const [labelKind, setLabelKind] = useState<LabelKind>('correct');
  const [labelOutcome, setLabelOutcome] = useState('');

  const refreshAll = useCallback(async () => {
    try {
      const s = await aiSandboxApi.getStatus();
      setStatus(s);
      if (s.status === 'disabled') {
        setMode('disabled');
        return;
      }
      setMode('ready');
      const [list, m, ls] = await Promise.all([
        aiSandboxApi.listExecutions({ limit: 15 }),
        aiSandboxApi.getMetrics(),
        aiTrainingApi.getStats(),
      ]);
      setExecutions(list.items || []);
      setMetrics(m);
      setLabelStats(ls);
    } catch (err) {
      setError(getParsedApiError(err));
      setMode('disabled');
    }
  }, []);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  const handleRunOnce = useCallback(async () => {
    if (!symbol.trim()) return;
    setBusy('run-once');
    setError(null);
    setInfo(null);
    try {
      const r = await aiSandboxApi.runOnce(symbol.trim().toUpperCase());
      const status = r.status as string;
      if (status === 'submitted') {
        setInfo({ tone: 'success', text: `已提交：${symbol.toUpperCase()}` });
      } else if (status === 'hold') {
        setInfo({ tone: 'info', text: `AI 决定 HOLD（不下单）` });
      } else {
        setInfo({ tone: 'warning', text: `跳过：${(r as { message?: string }).message ?? status}` });
      }
      await refreshAll();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [symbol, refreshAll]);

  const handleComputePnl = useCallback(async () => {
    setBusy('compute-pnl');
    setError(null);
    setInfo(null);
    try {
      const r = await aiSandboxApi.computePnl(50);
      setInfo({
        tone: 'success',
        text: `P&L 回算：scanned=${r.scanned}, computed=${r.computed}, skipped=${r.skipped}`,
      });
      await refreshAll();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [refreshAll]);

  const handleLabel = useCallback(async () => {
    const sid = Number(labelSourceId);
    if (!sid || sid < 1) return;
    setBusy('label');
    setError(null);
    setInfo(null);
    try {
      await aiTrainingApi.upsertLabel(
        labelSourceKind,
        sid,
        labelKind,
        labelOutcome.trim() || undefined,
      );
      setInfo({
        tone: 'success',
        text: `已标注：${labelSourceKind}#${sid} → ${labelKind}`,
      });
      setLabelSourceId('');
      setLabelOutcome('');
      const next = await aiTrainingApi.getStats();
      setLabelStats(next);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [labelSourceKind, labelSourceId, labelKind, labelOutcome]);

  const winRate1d = useMemo(() => metrics?.winRate1d ?? null, [metrics]);
  const winRate7d = useMemo(() => metrics?.winRate7d ?? null, [metrics]);

  if (mode === 'loading' || mode === 'disabled') return null;

  return (
    <Card padding="md">
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">
            AI 训练沙盒（Phase A+C · 仅纸面 · 完全隔离）
          </h2>
          <p className="mt-1 text-xs text-secondary-text">
            Forward simulation：让 AI 自己跑决策、用现行情模拟成交、独立审计表，
            <strong className="mx-1">不进 portfolio_trades / trade_executions</strong>。
            P&L 1/3/7/30 日 horizon 后台回算用于评估 prompt / 模型表现。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="info">SANDBOX</Badge>
          {status?.daemonEnabled ? (
            <Badge variant="success">daemon ON</Badge>
          ) : (
            <Badge variant="default">daemon OFF</Badge>
          )}
        </div>
      </header>

      {error ? <ApiErrorAlert error={error} className="mb-3" /> : null}
      {info ? (
        <InlineAlert variant={info.tone} message={info.text} className="mb-3" />
      ) : null}

      {/* Metrics summary */}
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <SandboxStat
          label="总执行"
          value={String(metrics?.totalExecutions ?? 0)}
        />
        <SandboxStat
          label="已成交"
          value={String(metrics?.filledCount ?? 0)}
        />
        <SandboxStat
          label="1日胜率"
          value={_fmtRate(winRate1d)}
        />
        <SandboxStat
          label="7日平均收益"
          value={_fmtPct(metrics?.avgPnl7dPct)}
        />
      </div>

      {/* Run once form */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Input
          placeholder="股票代码（如 AAPL）"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="max-w-[12rem]"
        />
        <Button
          variant="primary"
          size="sm"
          isLoading={busy === 'run-once'}
          loadingText="跑中..."
          onClick={() => void handleRunOnce()}
          disabled={!symbol.trim()}
        >
          让 AI 跑一次
        </Button>
        <Button
          variant="secondary"
          size="sm"
          isLoading={busy === 'compute-pnl'}
          loadingText="算中..."
          onClick={() => void handleComputePnl()}
        >
          回算 P&L
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void refreshAll()}
        >
          刷新
        </Button>
      </div>

      {/* Recent executions */}
      {executions.length === 0 ? (
        <EmptyState
          title="尚无沙盒执行历史"
          description="点上面「让 AI 跑一次」触发首次决策。"
          className="mb-3 bg-card/45"
        />
      ) : (
        <div className="mb-3 overflow-x-auto rounded-xl border border-border/40 bg-card/30">
          <table className="w-full min-w-[720px] text-xs">
            <thead className="text-muted-text">
              <tr className="text-left">
                <th className="px-3 py-2">时间</th>
                <th className="px-3 py-2">代号</th>
                <th className="px-3 py-2">方向</th>
                <th className="px-3 py-2 text-right">数量</th>
                <th className="px-3 py-2 text-right">成交价</th>
                <th className="px-3 py-2 text-right">1日</th>
                <th className="px-3 py-2 text-right">7日</th>
                <th className="px-3 py-2">状态</th>
              </tr>
            </thead>
            <tbody>
              {executions.map((row) => {
                const h1 = row.pnlHorizons?.horizon_1d ?? null;
                const h7 = row.pnlHorizons?.horizon_7d ?? null;
                return (
                  <tr key={row.id} className="border-t border-border/40">
                    <td className="px-3 py-2 font-mono text-muted-text">
                      {row.requestedAt
                        ? new Date(row.requestedAt).toLocaleTimeString('zh-CN')
                        : '--'}
                    </td>
                    <td className="px-3 py-2 font-mono text-foreground">
                      {row.symbol}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={cn(
                          'rounded px-2 py-0.5 text-xs',
                          row.side === 'buy'
                            ? 'bg-success/10 text-success'
                            : 'bg-danger/10 text-danger',
                        )}
                      >
                        {row.side === 'buy' ? '买入' : '卖出'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {_fmt(row.quantity, 0)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {_fmt(row.fillPrice)}
                    </td>
                    <td
                      className={cn(
                        'px-3 py-2 text-right font-mono',
                        h1 == null
                          ? 'text-muted-text'
                          : h1 >= 0
                            ? 'text-success'
                            : 'text-danger',
                      )}
                    >
                      {_fmtPct(h1)}
                    </td>
                    <td
                      className={cn(
                        'px-3 py-2 text-right font-mono',
                        h7 == null
                          ? 'text-muted-text'
                          : h7 >= 0
                            ? 'text-success'
                            : 'text-danger',
                      )}
                    >
                      {_fmtPct(h7)}
                    </td>
                    <td className="px-3 py-2">
                      <span className="text-secondary-text">{row.status}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Training labels section */}
      <div className="mt-4 rounded-xl border border-border/40 bg-card/20 p-3">
        <h3 className="mb-2 text-sm font-semibold text-foreground">
          训练数据标注
        </h3>
        {labelStats ? (
          <p className="mb-3 text-xs text-secondary-text">
            已标注 {labelStats.total} 条 ·
            <span className="mx-1 text-success">正确 {labelStats.correct}</span>
            ·
            <span className="mx-1 text-danger">错误 {labelStats.incorrect}</span>
            ·
            <span className="mx-1 text-muted-text">不明 {labelStats.unclear}</span>
            ·
            <span className="mx-1">来源：分析 {labelStats.fromAnalysisHistory} / 沙盒 {labelStats.fromAiSandbox}</span>
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
          <select
            className="rounded-xl border border-border/40 bg-card/60 px-3 py-2 text-sm"
            value={labelSourceKind}
            onChange={(e) => setLabelSourceKind(e.target.value as LabelSourceKind)}
          >
            <option value="ai_sandbox">沙盒执行</option>
            <option value="analysis_history">分析报告</option>
          </select>
          <Input
            placeholder="source_id"
            value={labelSourceId}
            type="number"
            onChange={(e) => setLabelSourceId(e.target.value)}
          />
          <select
            className="rounded-xl border border-border/40 bg-card/60 px-3 py-2 text-sm"
            value={labelKind}
            onChange={(e) => setLabelKind(e.target.value as LabelKind)}
          >
            <option value="correct">✓ 正确</option>
            <option value="incorrect">✗ 错误</option>
            <option value="unclear">? 不明</option>
          </select>
          <Button
            variant="primary"
            size="sm"
            isLoading={busy === 'label'}
            loadingText="标注中..."
            onClick={() => void handleLabel()}
            disabled={!labelSourceId}
          >
            标注
          </Button>
        </div>
        <Input
          placeholder="实际后续表现（可选，例如：5 日后 +3.2%）"
          value={labelOutcome}
          onChange={(e) => setLabelOutcome(e.target.value)}
          className="mt-2"
        />
      </div>
    </Card>
  );
};

const SandboxStat: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <Card padding="sm" variant="bordered">
    <span className="label-uppercase">{label}</span>
    <span className="mt-1 block text-base font-semibold text-foreground">
      {value}
    </span>
  </Card>
);

export default AISandboxPanel;
