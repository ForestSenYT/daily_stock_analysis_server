import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { tradingApi } from '../../api/trading';
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
  OrderResult,
  OrderSide,
  OrderTypeKind,
  RiskFlag,
  TradeExecution,
  TradingMode,
  TradingStatus,
} from '../../types/trading';

/**
 * Trading execution panel (Phase A — paper only).
 *
 * UX state machine:
 *   loading → disabled (panel hidden entirely)
 *   loading → ready (paper mode) → form submitted → result toast
 *   loading → ready (live mode) → submit returns 503 + error toast
 *
 * Hard rules:
 *   - When `status.mode === 'disabled'` the panel renders NULL — the
 *     entire feature is dormant.
 *   - The user is the only entity that can call ``submit`` in Phase A.
 *     The agent's ``propose_trade`` tool returns an intent dict;
 *     this panel can pre-populate from such an intent (future), but
 *     the request_uid is always re-generated per submission to keep
 *     idempotency tight.
 *   - Order entry stays in component state only — never written to
 *     localStorage / sessionStorage / indexedDB.
 */

type PanelMode = 'loading' | 'disabled' | 'ready';

function _newRequestUid(prefix: string): string {
  // Compact 24-char UUID4 — backend min_length=8.
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 24)
      : Math.random().toString(36).slice(2, 14) + Date.now().toString(36);
  return `${prefix}-${random}`;
}

function _fmtNum(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

const TradingExecutionPanel: React.FC = () => {
  const [panelMode, setPanelMode] = useState<PanelMode>('loading');
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [executions, setExecutions] = useState<TradeExecution[]>([]);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [info, setInfo] = useState<{ tone: 'success' | 'warning' | 'info'; text: string } | null>(
    null,
  );
  const [busy, setBusy] = useState<'submit' | 'preview' | null>(null);

  // Form state
  const [symbol, setSymbol] = useState('');
  const [side, setSide] = useState<OrderSide>('buy');
  const [orderType, setOrderType] = useState<OrderTypeKind>('market');
  const [quantity, setQuantity] = useState('1');
  const [limitPrice, setLimitPrice] = useState('');
  const [accountId, setAccountId] = useState('');
  const [note, setNote] = useState('');

  const refreshStatus = useCallback(async () => {
    try {
      const next = await tradingApi.getStatus();
      setStatus(next);
      if (next.mode === 'disabled') {
        setPanelMode('disabled');
      } else {
        setPanelMode('ready');
      }
    } catch (err) {
      setError(getParsedApiError(err));
      setPanelMode('disabled');
    }
  }, []);

  const refreshExecutions = useCallback(async () => {
    try {
      const next = await tradingApi.listExecutions({ limit: 10 });
      setExecutions(next.items || []);
    } catch (err) {
      // Listing failure shouldn't take down the form
      // — surface as an inline warning only.
      const e = getParsedApiError(err);
      setInfo({ tone: 'warning', text: `加载历史失败：${e.message}` });
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    void refreshExecutions();
  }, [refreshStatus, refreshExecutions]);

  const _buildPayload = useCallback(
    (mode: 'submit' | 'preview') => {
      const qty = Number(quantity);
      const limit = orderType === 'limit' ? Number(limitPrice) : null;
      return {
        symbol: symbol.trim().toUpperCase(),
        side,
        quantity: qty,
        orderType,
        limitPrice: limit,
        timeInForce: 'day' as const,
        accountId: accountId ? Number(accountId) : null,
        market: 'us' as const,
        currency: 'USD',
        note: note.trim() || null,
        requestUid: _newRequestUid(mode),
        source: 'ui' as const,
      };
    },
    [symbol, side, orderType, quantity, limitPrice, accountId, note],
  );

  const handleSubmit = useCallback(async () => {
    setBusy('submit');
    setError(null);
    setInfo(null);
    try {
      const result = await tradingApi.submit(_buildPayload('submit'));
      void renderResultInfo(result, setInfo);
      await refreshExecutions();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [_buildPayload, refreshExecutions]);

  const handlePreview = useCallback(async () => {
    setBusy('preview');
    setError(null);
    setInfo(null);
    try {
      const assessment = await tradingApi.previewRisk(_buildPayload('preview'));
      const blocks = assessment.flags
        .filter((f) => f.severity === 'block')
        .map((f) => f.message);
      const tone = assessment.decision === 'block' ? 'warning' : 'info';
      setInfo({
        tone,
        text:
          assessment.decision === 'block'
            ? `风险预检阻断：${blocks.join('；') || '未通过风险检查'}`
            : `风险预检通过（${assessment.flags.length} 个标记，无 block）`,
      });
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setBusy(null);
    }
  }, [_buildPayload]);

  const modeBadge = useMemo(() => {
    const mode = (status?.mode || 'disabled') as TradingMode;
    if (mode === 'paper') {
      return <Badge variant="info">PAPER（模拟）</Badge>;
    }
    if (mode === 'live') {
      return <Badge variant="warning">LIVE（实盘）</Badge>;
    }
    return <Badge variant="default">未启用</Badge>;
  }, [status]);

  if (panelMode === 'loading') {
    return null;
  }
  if (panelMode === 'disabled') {
    return null;
  }

  return (
    <Card padding="md">
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">
            交易执行（Phase A · 仅纸面）
          </h2>
          <p className="mt-1 text-xs text-secondary-text">
            模拟成交：用最近行情模拟下单、写入持仓表（标记 source=&apos;paper&apos;），
            <strong className="mx-1">不会触达真实券商账户</strong>。每笔提交都过风险引擎并写审计日志。
          </p>
        </div>
        <div className="flex items-center gap-2">{modeBadge}</div>
      </header>

      {error ? <ApiErrorAlert error={error} className="mb-3" /> : null}
      {info ? (
        <InlineAlert variant={info.tone} message={info.text} className="mb-3" />
      ) : null}

      <div className="space-y-3">
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          <Input
            placeholder="股票代码（如 AAPL）"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          />
          <select
            className="rounded-xl border border-border/40 bg-card/60 px-3 py-2 text-sm"
            value={side}
            onChange={(e) => setSide(e.target.value as OrderSide)}
          >
            <option value="buy">买入</option>
            <option value="sell">卖出</option>
          </select>
          <Input
            placeholder="数量"
            value={quantity}
            type="number"
            onChange={(e) => setQuantity(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          <select
            className="rounded-xl border border-border/40 bg-card/60 px-3 py-2 text-sm"
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as OrderTypeKind)}
          >
            <option value="market">市价</option>
            <option value="limit">限价</option>
          </select>
          <Input
            placeholder={orderType === 'limit' ? '限价（必填）' : '限价（市价单不需要）'}
            value={limitPrice}
            type="number"
            onChange={(e) => setLimitPrice(e.target.value)}
            disabled={orderType !== 'limit'}
          />
          <Input
            placeholder={`账户 ID${
              status?.paperAccountId
                ? `（默认 ${status.paperAccountId}）`
                : '（必填）'
            }`}
            value={accountId}
            type="number"
            onChange={(e) => setAccountId(e.target.value)}
          />
        </div>
        <Input
          placeholder="备注（可选）"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            size="sm"
            isLoading={busy === 'submit'}
            loadingText="提交中..."
            onClick={() => void handleSubmit()}
            disabled={!symbol.trim() || !quantity}
          >
            提交（模拟成交）
          </Button>
          <Button
            variant="secondary"
            size="sm"
            isLoading={busy === 'preview'}
            loadingText="预检中..."
            onClick={() => void handlePreview()}
            disabled={!symbol.trim() || !quantity}
          >
            风险预检
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void refreshExecutions()}
          >
            刷新历史
          </Button>
        </div>

        {executions.length === 0 ? (
          <EmptyState
            title="尚无交易历史"
            description="提交一笔订单后，这里会显示最近 10 条审计记录。"
            className="bg-card/45"
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border/40 bg-card/30">
            <table className="w-full min-w-[640px] text-xs">
              <thead className="text-muted-text">
                <tr className="text-left">
                  <th className="px-3 py-2">时间</th>
                  <th className="px-3 py-2">代号</th>
                  <th className="px-3 py-2">方向</th>
                  <th className="px-3 py-2 text-right">数量</th>
                  <th className="px-3 py-2 text-right">成交价</th>
                  <th className="px-3 py-2">状态</th>
                </tr>
              </thead>
              <tbody>
                {executions.map((row) => (
                  <tr
                    key={row.id}
                    className="border-t border-border/40"
                  >
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
                      {_fmtNum(row.quantity, 0)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {row.fillPrice != null ? _fmtNum(row.fillPrice) : '--'}
                    </td>
                    <td className="px-3 py-2">{statusLabel(row)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="text-xs text-warning">
          仅纸面交易：不会调用任何真实券商下单接口。
          <strong className="mx-1">实盘模式（live）暂未实现</strong>，会直接 503。
        </p>
      </div>
    </Card>
  );
};

function renderResultInfo(
  result: OrderResult,
  setInfo: (info: { tone: 'success' | 'warning' | 'info'; text: string } | null) => void,
): void {
  if (result.status === 'filled') {
    setInfo({
      tone: 'success',
      text: `成交：fill_price=${result.fillPrice}, fill_qty=${result.fillQuantity}, portfolio_trade_id=${result.portfolioTradeId}`,
    });
  } else if (result.status === 'blocked') {
    const flags = (result.riskAssessment?.flags || [])
      .filter((f: RiskFlag) => f.severity === 'block')
      .map((f: RiskFlag) => f.message);
    setInfo({
      tone: 'warning',
      text: `被风险引擎拦截：${flags.join('；') || result.errorMessage || '未通过'}`,
    });
  } else if (result.status === 'failed') {
    setInfo({
      tone: 'warning',
      text: `失败：${result.errorCode || ''} ${result.errorMessage || ''}`,
    });
  } else {
    setInfo({ tone: 'info', text: `状态：${result.status}` });
  }
}

function statusLabel(row: TradeExecution): React.ReactNode {
  const map: Record<string, { label: string; cls: string }> = {
    pending: { label: '处理中', cls: 'bg-info/10 text-info' },
    filled: { label: '已成交', cls: 'bg-success/10 text-success' },
    blocked: { label: '已拦截', cls: 'bg-warning/10 text-warning' },
    failed: { label: '失败', cls: 'bg-danger/10 text-danger' },
  };
  const entry = map[row.status] || { label: row.status, cls: 'bg-card text-secondary-text' };
  return <span className={`rounded px-2 py-0.5 text-xs ${entry.cls}`}>{entry.label}</span>;
}

export default TradingExecutionPanel;
