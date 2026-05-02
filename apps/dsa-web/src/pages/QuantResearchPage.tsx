import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { quantResearchApi } from '../api/quantResearch';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  PageHeader,
  SectionCard,
  Select,
} from '../components/common';
import { cn } from '../utils/cn';
import type {
  BuiltinFactor,
  FactorEvaluationResult,
  ResearchBacktestRebalance,
  ResearchBacktestResult,
  ResearchBacktestStrategy,
  QuantStatus,
} from '../types/quantResearch';

// =====================================================================
// Layout primitives
// =====================================================================

const FORM_INPUT_CLASS =
  'input-surface input-focus-glow h-11 w-full rounded-xl border bg-transparent px-4 text-sm transition-all focus:outline-none disabled:cursor-not-allowed disabled:opacity-60';
const TEXTAREA_CLASS =
  'input-surface input-focus-glow w-full rounded-xl border bg-transparent px-4 py-3 text-sm transition-all focus:outline-none disabled:cursor-not-allowed disabled:opacity-60';

const PIE_COLORS = ['#00d4ff', '#00ff88', '#ffaa00', '#ff7a45', '#7f8cff', '#ff4466', '#9b8cff', '#16e0c1'];
const STRATEGY_OPTIONS: { value: ResearchBacktestStrategy; label: string; hint: string }[] = [
  { value: 'top_k_long_only', label: 'Top-K 多头', hint: '按因子排序持有 Top-K，等权多头' },
  { value: 'quantile_long_short', label: '分位多空（模拟）', hint: '多顶分位 / 空底分位（仅研究模拟，不可下单）' },
  { value: 'equal_weight_baseline', label: '等权基准', hint: '忽略因子，等权持仓 — 用作对照' },
];
const REBALANCE_OPTIONS: { value: ResearchBacktestRebalance; label: string }[] = [
  { value: 'daily', label: '每日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
];

type TabKey = 'factor' | 'backtest';

// =====================================================================
// Number formatting (NaN-safe)
// =====================================================================

function fmtNumber(value: number | null | undefined, digits = 4): string {
  if (value == null || Number.isNaN(value)) return '--';
  return Number(value).toFixed(digits);
}

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function fmtRawPct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(digits)}%`;
}

function fmtSigned(value: number | null | undefined, digits = 4): string {
  if (value == null || Number.isNaN(value)) return '--';
  const v = Number(value);
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`;
}

function parseCommaList(raw: string): string[] {
  return raw
    .split(/[\s,，;；]+/u)
    .map((token) => token.trim().toUpperCase())
    .filter((token) => token.length > 0);
}

function defaultStartDate(): string {
  const d = new Date();
  d.setDate(d.getDate() - 180);
  return d.toISOString().slice(0, 10);
}

function defaultEndDate(): string {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}

// =====================================================================
// Drawdown computation (cumulative from NAV curve)
// =====================================================================

function buildNavSeries(curve: ResearchBacktestResult['navCurve']): Array<{ date: string; nav: number; drawdown: number }> {
  if (!curve || curve.length === 0) return [];
  let peak = curve[0]?.nav ?? 0;
  return curve.map((point) => {
    const nav = Number(point.nav) || 0;
    if (nav > peak) peak = nav;
    const drawdown = peak > 0 ? (nav - peak) / peak : 0;
    return { date: point.date, nav, drawdown };
  });
}

// =====================================================================
// Stat (small KPI) — declared early so the per-tab result panels below
// can reference it without const-before-use risk.
// =====================================================================

const Stat: React.FC<{ label: string; value: string; accent?: boolean; mono?: boolean }> = ({
  label,
  value,
  accent,
  mono,
}) => (
  <Card padding="sm" variant="bordered" className="gap-1">
    <span className="label-uppercase">{label}</span>
    <span
      className={cn(
        'mt-1 block text-base font-semibold',
        accent ? 'text-cyan' : 'text-foreground',
        mono ? 'font-mono text-xs' : '',
      )}
    >
      {value}
    </span>
  </Card>
);

// =====================================================================
// Disabled banner
// =====================================================================

const DisabledBanner: React.FC<{ status: QuantStatus | null }> = ({ status }) => (
  <InlineAlert
    variant="info"
    title="量化研究实验室未启用"
    message={
      <div className="space-y-2">
        <p>
          {status?.message
            || '当前部署关闭了 Quant Research Lab。运维需要在 系统设置 → 量化研究 中将 QUANT_RESEARCH_ENABLED 设为 true，或在 .env 配置后重新部署。'}
        </p>
        <p className="text-xs opacity-75">
          页面仍然可见，便于研究人员熟悉 UI；启用后所有调用会立即生效。
        </p>
      </div>
    }
  />
);

// =====================================================================
// Research disclaimer (shown on every tab)
// =====================================================================

const ResearchDisclaimer: React.FC = () => (
  <InlineAlert
    variant="warning"
    title="研究专用 · 非投资建议"
    message={
      <span>
        此模块输出 IC / 分位收益 / 模拟净值与回撤等
        <strong className="mx-1">研究指标</strong>
        ，不会自动下单、不会写入持仓、不构成投资建议。多空策略的空头腿仅为
        <strong className="mx-1">模拟</strong>
        ，请勿据此进行实盘融券或下单。
      </span>
    }
  />
);

// =====================================================================
// Tab nav
// =====================================================================

const TabButton: React.FC<{
  label: string;
  active: boolean;
  onClick: () => void;
}> = ({ label, active, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    className={cn(
      'inline-flex h-10 items-center rounded-xl border px-4 text-sm font-medium transition-all',
      active
        ? 'border-cyan/40 bg-cyan/10 text-cyan shadow-glow-cyan'
        : 'border-border/60 bg-card/40 text-secondary-text hover:border-cyan/30 hover:text-foreground',
    )}
  >
    {label}
  </button>
);

// =====================================================================
// Factor Lab tab
// =====================================================================

interface FactorLabTabProps {
  enabled: boolean;
  factors: BuiltinFactor[];
}

const FactorLabTab: React.FC<FactorLabTabProps> = ({ enabled, factors }) => {
  const [stocksRaw, setStocksRaw] = useState('AAPL, MSFT, NVDA, AMD, GOOG, META, AMZN, TSLA');
  const [startDate, setStartDate] = useState<string>(defaultStartDate);
  const [endDate, setEndDate] = useState<string>(defaultEndDate);
  const [factorMode, setFactorMode] = useState<'builtin' | 'expression'>('builtin');
  const [builtinId, setBuiltinId] = useState<string>('');
  const [expression, setExpression] = useState<string>('');
  const [factorName, setFactorName] = useState<string>('');
  const [forwardWindow, setForwardWindow] = useState<string>('5');
  const [quantileCount, setQuantileCount] = useState<string>('5');

  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<FactorEvaluationResult | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);

  // Initialise builtinId once factors arrive.
  useEffect(() => {
    if (!builtinId && factors.length > 0) {
      setBuiltinId(factors[0].id);
    }
  }, [factors, builtinId]);

  const builtinOptions = useMemo(
    () =>
      factors.map((f) => ({
        value: f.id,
        label: `${f.id} · ${f.name}`,
      })),
    [factors],
  );

  const handleEvaluate = useCallback(async () => {
    setIsRunning(true);
    setError(null);
    try {
      const stocks = parseCommaList(stocksRaw);
      if (stocks.length === 0) {
        setError({
          title: '股票池为空',
          message: '请输入至少一个股票代码（用逗号或空格分隔）。',
          rawMessage: 'empty stock pool',
          category: 'missing_params',
        });
        return;
      }
      const factor: { name?: string; builtinId?: string; expression?: string } = {};
      if (factorName.trim()) factor.name = factorName.trim();
      if (factorMode === 'builtin') {
        if (!builtinId) {
          setError({
            title: '请选择内置因子',
            message: '使用「内置因子」模式时必须选择一个 builtin id。',
            rawMessage: 'missing builtin id',
            category: 'missing_params',
          });
          return;
        }
        factor.builtinId = builtinId;
      } else {
        const expr = expression.trim();
        if (!expr) {
          setError({
            title: '请输入因子表达式',
            message: '使用「自定义表达式」模式时必须填写 AST 白名单语法的表达式。',
            rawMessage: 'missing expression',
            category: 'missing_params',
          });
          return;
        }
        factor.expression = expr;
      }
      const evalRes = await quantResearchApi.evaluateFactor({
        factor,
        stocks,
        startDate,
        endDate,
        forwardWindow: Number(forwardWindow) || 5,
        quantileCount: Number(quantileCount) || 5,
      });
      setResult(evalRes);
    } catch (err) {
      console.error('factor evaluation failed', err);
      setError(getParsedApiError(err));
      setResult(null);
    } finally {
      setIsRunning(false);
    }
  }, [
    stocksRaw,
    startDate,
    endDate,
    forwardWindow,
    quantileCount,
    factorMode,
    builtinId,
    expression,
    factorName,
  ]);

  const icSeries = useMemo(() => {
    if (!result) return [];
    const ic = result.metrics.ic ?? [];
    const rankIc = result.metrics.rankIc ?? [];
    return ic.map((value, idx) => ({
      idx: idx + 1,
      ic: value,
      rankIc: rankIc[idx] ?? null,
    }));
  }, [result]);

  const quantileSeries = useMemo(() => {
    if (!result) return [];
    const map = result.metrics.quantileReturns ?? {};
    return Object.entries(map)
      .map(([key, value]) => ({
        bucket: `Q${key}`,
        value: value == null || Number.isNaN(value) ? 0 : Number(value),
      }))
      .sort((a, b) => Number(a.bucket.slice(1)) - Number(b.bucket.slice(1)));
  }, [result]);

  return (
    <div className="space-y-6">
      <SectionCard
        title="因子评估"
        subtitle="Factor Lab"
        actions={
          <Button
            variant="primary"
            isLoading={isRunning}
            loadingText="评估中..."
            onClick={() => void handleEvaluate()}
            disabled={!enabled}
          >
            运行评估
          </Button>
        }
      >
        {!enabled ? <DisabledBanner status={null} /> : null}

        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <label className="mb-2 block text-sm font-medium text-foreground">股票池</label>
            <input
              className={FORM_INPUT_CLASS}
              value={stocksRaw}
              onChange={(e) => setStocksRaw(e.target.value)}
              placeholder="AAPL, MSFT, NVDA, ... （逗号或空格分隔，≤ 50）"
              disabled={isRunning || !enabled}
            />
            <p className="mt-2 text-xs text-secondary-text">
              支持 A 股 / 港股 / 美股代码；后端会做规范化（OHLCV 缺失股票将进入 missing_stocks 列表）。
            </p>
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">开始日期</label>
            <input
              type="date"
              className={FORM_INPUT_CLASS}
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">结束日期</label>
            <input
              type="date"
              className={FORM_INPUT_CLASS}
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">前向收益窗口（交易日）</label>
            <input
              type="number"
              min={1}
              max={60}
              className={FORM_INPUT_CLASS}
              value={forwardWindow}
              onChange={(e) => setForwardWindow(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">分位数</label>
            <input
              type="number"
              min={2}
              max={10}
              className={FORM_INPUT_CLASS}
              value={quantileCount}
              onChange={(e) => setQuantileCount(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>

          <div className="md:col-span-2">
            <span className="label-uppercase">因子来源</span>
            <div className="mt-2 inline-flex rounded-xl border border-border/60 bg-card/40 p-1">
              <button
                type="button"
                onClick={() => setFactorMode('builtin')}
                className={cn(
                  'rounded-lg px-3 py-1.5 text-xs',
                  factorMode === 'builtin' ? 'bg-cyan/15 text-cyan' : 'text-secondary-text',
                )}
              >
                内置因子
              </button>
              <button
                type="button"
                onClick={() => setFactorMode('expression')}
                className={cn(
                  'rounded-lg px-3 py-1.5 text-xs',
                  factorMode === 'expression' ? 'bg-cyan/15 text-cyan' : 'text-secondary-text',
                )}
              >
                自定义表达式
              </button>
            </div>
          </div>

          {factorMode === 'builtin' ? (
            <div className="md:col-span-2">
              <Select
                label="选择内置因子"
                value={builtinId}
                onChange={(value) => setBuiltinId(value)}
                options={builtinOptions}
                disabled={isRunning || !enabled || builtinOptions.length === 0}
                placeholder={builtinOptions.length === 0 ? '暂无内置因子，可在「自定义表达式」中输入' : '请选择'}
              />
              {builtinId ? (
                <p className="mt-2 text-xs text-secondary-text">
                  {factors.find((f) => f.id === builtinId)?.description || ''}
                </p>
              ) : null}
            </div>
          ) : (
            <div className="md:col-span-2">
              <label className="mb-2 block text-sm font-medium text-foreground">表达式（AST 白名单）</label>
              <textarea
                className={cn(TEXTAREA_CLASS, 'min-h-[5rem] font-mono')}
                value={expression}
                onChange={(e) => setExpression(e.target.value)}
                placeholder="例如: div(close, mean(close, 20)) - 1"
                disabled={isRunning || !enabled}
              />
              <p className="mt-2 text-xs text-secondary-text">
                只允许 OHLCV 列引用（open / high / low / close / volume / amount / pct_chg / ma5 / ma10 / ma20 / volume_ratio）和白名单算子函数（mean / std / lag / shift / diff / pct_change / zscore / log / abs / max / min / div）。任何危险节点会被拒绝。
              </p>
            </div>
          )}

          <div className="md:col-span-2">
            <label className="mb-2 block text-sm font-medium text-foreground">显示名称（可选）</label>
            <input
              className={FORM_INPUT_CLASS}
              value={factorName}
              onChange={(e) => setFactorName(e.target.value)}
              placeholder="例如：MA20 偏离度"
              disabled={isRunning || !enabled}
            />
          </div>
        </div>

        {error ? <ApiErrorAlert error={error} className="mt-4" /> : null}
      </SectionCard>

      {result ? (
        <FactorEvaluationResultPanel result={result} icSeries={icSeries} quantileSeries={quantileSeries} />
      ) : (
        <EmptyState
          title="尚无评估结果"
          description="填写表单并点击「运行评估」后，此处会显示 IC / 分位收益 / 覆盖率与诊断信息。"
          className="bg-card/45"
        />
      )}
    </div>
  );
};

const FactorEvaluationResultPanel: React.FC<{
  result: FactorEvaluationResult;
  icSeries: Array<{ idx: number; ic: number | null; rankIc: number | null }>;
  quantileSeries: Array<{ bucket: string; value: number }>;
}> = ({ result, icSeries, quantileSeries }) => (
  <div className="space-y-4">
    <SectionCard title="IC 概览" subtitle="Information Coefficient">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="IC Mean" value={fmtNumber(result.metrics.icMean)} />
        <Stat label="IC Std" value={fmtNumber(result.metrics.icStd)} />
        <Stat label="ICIR" value={fmtNumber(result.metrics.icir)} />
        <Stat label="RankIC Mean" value={fmtNumber(result.metrics.rankIcMean)} />
        <Stat label="Long-Short Spread" value={fmtSigned(result.metrics.longShortSpread)} />
        <Stat label="Factor Turnover" value={fmtNumber(result.metrics.factorTurnover)} />
        <Stat label="Autocorrelation" value={fmtNumber(result.metrics.autocorrelation)} />
        <Stat label="Daily IC #" value={String(result.metrics.dailyIcCount)} />
      </div>

      {icSeries.length > 0 ? (
        <div className="mt-6 h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={icSeries} margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border) / 0.4)" />
              <XAxis dataKey="idx" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
              <YAxis stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{
                  background: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: 8,
                  fontSize: 12,
                }}
                formatter={(value) =>
                  typeof value === 'number' ? value.toFixed(4) : '--'
                }
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 2" />
              <Line type="monotone" dataKey="ic" stroke="#00d4ff" dot={false} strokeWidth={1.5} name="IC" />
              <Line type="monotone" dataKey="rankIc" stroke="#ffaa00" dot={false} strokeWidth={1.5} name="RankIC" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="mt-4 text-xs text-secondary-text">未返回 IC 序列（可能覆盖率不足）。</p>
      )}
    </SectionCard>

    <SectionCard title="分位收益" subtitle="Quantile Returns">
      {quantileSeries.length > 0 ? (
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={quantileSeries} margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border) / 0.4)" />
              <XAxis dataKey="bucket" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
              <YAxis stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{
                  background: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: 8,
                  fontSize: 12,
                }}
                formatter={(value) => Number(value).toFixed(4)}
              />
              <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 2" />
              <Bar dataKey="value" name="平均前向收益" radius={[6, 6, 0, 0]}>
                {quantileSeries.map((entry, idx) => (
                  <Cell
                    key={entry.bucket}
                    fill={entry.value >= 0 ? '#00ff88' : '#ff4466'}
                    fillOpacity={0.4 + 0.6 * (idx / Math.max(quantileSeries.length - 1, 1))}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="text-xs text-secondary-text">未返回分位结果。</p>
      )}
    </SectionCard>

    <SectionCard title="覆盖率与诊断" subtitle="Coverage & Diagnostics">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Stat label="Run ID" value={result.runId} mono />
        <Stat label="Factor" value={result.factor.name || result.factor.builtinId || result.factor.expression || '--'} />
        <Stat label="Forward Window" value={`${result.forwardWindow} 个交易日`} />
        <Stat label="Coverage" value={`${result.coverage.coveredStocks.length} / ${result.coverage.requestedStocks.length}`} />
        <Stat label="Missing Rate" value={fmtPct(result.coverage.missingRate, 1)} />
        <Stat label="Total Observations" value={String(result.coverage.totalObservations)} />
      </div>
      {result.coverage.missingStocks.length > 0 ? (
        <div className="mt-4 rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <span className="font-semibold">缺失股票：</span>
          <span className="ml-1">{result.coverage.missingStocks.join(', ')}</span>
        </div>
      ) : null}
      {result.diagnostics.length > 0 ? (
        <div className="mt-3">
          <span className="label-uppercase">诊断信息</span>
          <ul className="mt-2 space-y-1 text-xs text-secondary-text">
            {result.diagnostics.map((line, idx) => (
              <li key={idx}>· {line}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </SectionCard>
  </div>
);

// =====================================================================
// Research Backtest tab
// =====================================================================

interface ResearchBacktestTabProps {
  enabled: boolean;
  factors: BuiltinFactor[];
}

const ResearchBacktestTab: React.FC<ResearchBacktestTabProps> = ({ enabled, factors }) => {
  const [stocksRaw, setStocksRaw] = useState('AAPL, MSFT, NVDA, AMD, GOOG, META, AMZN, TSLA');
  const [startDate, setStartDate] = useState<string>(defaultStartDate);
  const [endDate, setEndDate] = useState<string>(defaultEndDate);
  const [strategy, setStrategy] = useState<ResearchBacktestStrategy>('top_k_long_only');
  const [rebalance, setRebalance] = useState<ResearchBacktestRebalance>('weekly');
  const [factorMode, setFactorMode] = useState<'builtin' | 'expression'>('builtin');
  const [builtinFactorId, setBuiltinFactorId] = useState<string>('');
  const [expression, setExpression] = useState<string>('');
  const [topK, setTopK] = useState<string>('3');
  const [quantileCount, setQuantileCount] = useState<string>('5');
  const [initialCash, setInitialCash] = useState<string>('1000000');
  const [commissionBps, setCommissionBps] = useState<string>('10');
  const [slippageBps, setSlippageBps] = useState<string>('5');
  const [benchmark, setBenchmark] = useState<string>('');

  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<ResearchBacktestResult | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);

  useEffect(() => {
    if (!builtinFactorId && factors.length > 0) {
      setBuiltinFactorId(factors[0].id);
    }
  }, [factors, builtinFactorId]);

  const handleRun = useCallback(async () => {
    setIsRunning(true);
    setError(null);
    try {
      const stocks = parseCommaList(stocksRaw);
      if (stocks.length === 0) {
        setError({
          title: '股票池为空',
          message: '请输入至少一个股票代码（用逗号或空格分隔）。',
          rawMessage: 'empty stock pool',
          category: 'missing_params',
        });
        return;
      }
      const factorPart: { builtinFactorId?: string; expression?: string } = {};
      if (strategy !== 'equal_weight_baseline') {
        if (factorMode === 'builtin') {
          if (!builtinFactorId) {
            setError({
              title: '请选择内置因子',
              message: '当前策略需要因子；请选择 builtin 或填写自定义表达式。',
              rawMessage: 'missing factor',
              category: 'missing_params',
            });
            return;
          }
          factorPart.builtinFactorId = builtinFactorId;
        } else {
          const expr = expression.trim();
          if (!expr) {
            setError({
              title: '请输入因子表达式',
              message: '当前策略需要因子；请填写 AST 白名单语法的表达式。',
              rawMessage: 'missing expression',
              category: 'missing_params',
            });
            return;
          }
          factorPart.expression = expr;
        }
      }
      const res = await quantResearchApi.runBacktest({
        strategy,
        stocks,
        startDate,
        endDate,
        rebalanceFrequency: rebalance,
        ...factorPart,
        topK: topK ? Number(topK) : undefined,
        quantileCount: quantileCount ? Number(quantileCount) : 5,
        initialCash: initialCash ? Number(initialCash) : undefined,
        commissionBps: commissionBps ? Number(commissionBps) : undefined,
        slippageBps: slippageBps ? Number(slippageBps) : undefined,
        benchmark: benchmark.trim() || undefined,
      });
      setResult(res);
    } catch (err) {
      console.error('research backtest failed', err);
      setError(getParsedApiError(err));
      setResult(null);
    } finally {
      setIsRunning(false);
    }
  }, [
    stocksRaw,
    startDate,
    endDate,
    strategy,
    rebalance,
    factorMode,
    builtinFactorId,
    expression,
    topK,
    quantileCount,
    initialCash,
    commissionBps,
    slippageBps,
    benchmark,
  ]);

  const showFactor = strategy !== 'equal_weight_baseline';
  const showTopK = strategy === 'top_k_long_only';
  const showQuantile = strategy === 'quantile_long_short';

  return (
    <div className="space-y-6">
      <SectionCard
        title="研究回测"
        subtitle="Research Backtest"
        actions={
          <Button
            variant="primary"
            isLoading={isRunning}
            loadingText="回测中..."
            onClick={() => void handleRun()}
            disabled={!enabled}
          >
            运行回测
          </Button>
        }
      >
        {!enabled ? <DisabledBanner status={null} /> : null}

        <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <label className="mb-2 block text-sm font-medium text-foreground">股票池</label>
            <input
              className={FORM_INPUT_CLASS}
              value={stocksRaw}
              onChange={(e) => setStocksRaw(e.target.value)}
              placeholder="AAPL, MSFT, NVDA, ... （逗号或空格分隔）"
              disabled={isRunning || !enabled}
            />
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">开始日期</label>
            <input
              type="date"
              className={FORM_INPUT_CLASS}
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">结束日期</label>
            <input
              type="date"
              className={FORM_INPUT_CLASS}
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>

          <div>
            <Select
              label="策略类型"
              value={strategy}
              onChange={(value) => setStrategy(value as ResearchBacktestStrategy)}
              options={STRATEGY_OPTIONS.map((opt) => ({ value: opt.value, label: opt.label }))}
              disabled={isRunning || !enabled}
            />
            <p className="mt-2 text-xs text-secondary-text">
              {STRATEGY_OPTIONS.find((opt) => opt.value === strategy)?.hint || ''}
            </p>
          </div>
          <div>
            <Select
              label="调仓频率"
              value={rebalance}
              onChange={(value) => setRebalance(value as ResearchBacktestRebalance)}
              options={REBALANCE_OPTIONS.map((opt) => ({ value: opt.value, label: opt.label }))}
              disabled={isRunning || !enabled}
            />
          </div>

          {showFactor ? (
            <>
              <div className="md:col-span-2">
                <span className="label-uppercase">因子来源</span>
                <div className="mt-2 inline-flex rounded-xl border border-border/60 bg-card/40 p-1">
                  <button
                    type="button"
                    onClick={() => setFactorMode('builtin')}
                    className={cn(
                      'rounded-lg px-3 py-1.5 text-xs',
                      factorMode === 'builtin' ? 'bg-cyan/15 text-cyan' : 'text-secondary-text',
                    )}
                  >
                    内置因子
                  </button>
                  <button
                    type="button"
                    onClick={() => setFactorMode('expression')}
                    className={cn(
                      'rounded-lg px-3 py-1.5 text-xs',
                      factorMode === 'expression' ? 'bg-cyan/15 text-cyan' : 'text-secondary-text',
                    )}
                  >
                    自定义表达式
                  </button>
                </div>
              </div>

              {factorMode === 'builtin' ? (
                <div className="md:col-span-2">
                  <Select
                    label="选择内置因子"
                    value={builtinFactorId}
                    onChange={(v) => setBuiltinFactorId(v)}
                    options={factors.map((f) => ({ value: f.id, label: `${f.id} · ${f.name}` }))}
                    disabled={isRunning || !enabled || factors.length === 0}
                    placeholder={factors.length === 0 ? '暂无内置因子' : '请选择'}
                  />
                </div>
              ) : (
                <div className="md:col-span-2">
                  <label className="mb-2 block text-sm font-medium text-foreground">表达式（AST 白名单）</label>
                  <textarea
                    className={cn(TEXTAREA_CLASS, 'min-h-[5rem] font-mono')}
                    value={expression}
                    onChange={(e) => setExpression(e.target.value)}
                    placeholder="例如: div(close, mean(close, 20)) - 1"
                    disabled={isRunning || !enabled}
                  />
                </div>
              )}
            </>
          ) : null}

          {showTopK ? (
            <div>
              <label className="mb-2 block text-sm font-medium text-foreground">Top-K</label>
              <input
                type="number"
                min={1}
                className={FORM_INPUT_CLASS}
                value={topK}
                onChange={(e) => setTopK(e.target.value)}
                disabled={isRunning || !enabled}
              />
            </div>
          ) : null}
          {showQuantile ? (
            <div>
              <label className="mb-2 block text-sm font-medium text-foreground">分位数</label>
              <input
                type="number"
                min={2}
                max={10}
                className={FORM_INPUT_CLASS}
                value={quantileCount}
                onChange={(e) => setQuantileCount(e.target.value)}
                disabled={isRunning || !enabled}
              />
            </div>
          ) : null}

          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">初始资金</label>
            <input
              type="number"
              min={0}
              className={FORM_INPUT_CLASS}
              value={initialCash}
              onChange={(e) => setInitialCash(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">手续费 (bps)</label>
            <input
              type="number"
              min={0}
              max={1000}
              className={FORM_INPUT_CLASS}
              value={commissionBps}
              onChange={(e) => setCommissionBps(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">滑点 (bps)</label>
            <input
              type="number"
              min={0}
              max={1000}
              className={FORM_INPUT_CLASS}
              value={slippageBps}
              onChange={(e) => setSlippageBps(e.target.value)}
              disabled={isRunning || !enabled}
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-medium text-foreground">基准（可选）</label>
            <input
              className={FORM_INPUT_CLASS}
              value={benchmark}
              onChange={(e) => setBenchmark(e.target.value.toUpperCase())}
              placeholder="如：SPY / QQQ"
              disabled={isRunning || !enabled}
            />
          </div>
        </div>

        {error ? <ApiErrorAlert error={error} className="mt-4" /> : null}
      </SectionCard>

      {result ? (
        <ResearchBacktestResultPanel result={result} />
      ) : (
        <EmptyState
          title="尚无回测结果"
          description="填写表单并点击「运行回测」后，此处会显示净值曲线、回撤、调仓权重与回测指标。"
          className="bg-card/45"
        />
      )}
    </div>
  );
};

const ResearchBacktestResultPanel: React.FC<{ result: ResearchBacktestResult }> = ({ result }) => {
  const navSeries = useMemo(() => buildNavSeries(result.navCurve), [result]);
  const lastPosition = useMemo(() => {
    const positions = result.positions || [];
    return positions.length > 0 ? positions[positions.length - 1] : null;
  }, [result]);
  const weightPie = useMemo(() => {
    if (!lastPosition) return [];
    return Object.entries(lastPosition.weights || {})
      .filter(([, weight]) => Math.abs(Number(weight) || 0) > 1e-6)
      .map(([symbol, weight]) => ({
        name: symbol,
        value: Math.abs(Number(weight) || 0),
        signed: Number(weight) || 0,
      }))
      .sort((a, b) => b.value - a.value);
  }, [lastPosition]);

  return (
    <div className="space-y-4">
      <SectionCard title="回测指标" subtitle="Metrics">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Total Return" value={fmtPct(result.metrics.totalReturn)} accent />
          <Stat label="Annualized Return" value={fmtPct(result.metrics.annualizedReturn)} accent />
          <Stat label="Annualized Vol" value={fmtPct(result.metrics.annualizedVolatility)} />
          <Stat label="Sharpe" value={fmtNumber(result.metrics.sharpe, 2)} />
          <Stat label="Sortino" value={fmtNumber(result.metrics.sortino, 2)} />
          <Stat label="Calmar" value={fmtNumber(result.metrics.calmar, 2)} />
          <Stat label="Max Drawdown" value={fmtPct(result.metrics.maxDrawdown)} />
          <Stat label="Win Rate" value={fmtPct(result.metrics.winRate)} />
          <Stat label="Turnover" value={fmtNumber(result.metrics.turnover, 2)} />
          <Stat label="Cost Drag" value={fmtPct(result.metrics.costDrag)} />
          <Stat label="Excess Return" value={fmtPct(result.metrics.excessReturn)} />
          <Stat label="Information Ratio" value={fmtNumber(result.metrics.informationRatio, 2)} />
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-secondary-text">
          <Badge variant="default">strategy: {result.strategy}</Badge>
          <Badge variant="default">rebalance: {result.rebalanceFrequency}</Badge>
          {result.factorId ? <Badge variant="default">factor: {result.factorId}</Badge> : null}
          {result.diagnostics.lookaheadBiasGuard ? (
            <Badge variant="success">no-lookahead</Badge>
          ) : (
            <Badge variant="warning">lookahead?</Badge>
          )}
        </div>
      </SectionCard>

      <SectionCard title="净值曲线" subtitle="NAV Curve">
        {navSeries.length > 0 ? (
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={navSeries} margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
                <defs>
                  <linearGradient id="quantNavFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#00d4ff" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="#00d4ff" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border) / 0.4)" />
                <XAxis dataKey="date" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} minTickGap={32} />
                <YAxis stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--card))',
                    border: '1px solid hsl(var(--border))',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  formatter={(value) => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}
                />
                <Area type="monotone" dataKey="nav" stroke="#00d4ff" fill="url(#quantNavFill)" strokeWidth={1.8} name="NAV" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-xs text-secondary-text">未返回净值序列。</p>
        )}
      </SectionCard>

      <SectionCard title="回撤曲线" subtitle="Drawdown">
        {navSeries.length > 0 ? (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={navSeries} margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
                <defs>
                  <linearGradient id="quantDrawdownFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#ff4466" stopOpacity={0.05} />
                    <stop offset="100%" stopColor="#ff4466" stopOpacity={0.4} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border) / 0.4)" />
                <XAxis dataKey="date" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} minTickGap={32} />
                <YAxis
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  tickFormatter={(value) => `${(Number(value) * 100).toFixed(0)}%`}
                />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--card))',
                    border: '1px solid hsl(var(--border))',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  formatter={(value) => fmtPct(typeof value === 'number' ? value : Number(value), 2)}
                />
                <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 2" />
                <Area
                  type="monotone"
                  dataKey="drawdown"
                  stroke="#ff4466"
                  fill="url(#quantDrawdownFill)"
                  strokeWidth={1.5}
                  name="Drawdown"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-xs text-secondary-text">未返回净值序列。</p>
        )}
      </SectionCard>

      <SectionCard title="最近调仓权重" subtitle="Latest Rebalance">
        {lastPosition && weightPie.length > 0 ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={weightPie}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={100}
                    innerRadius={50}
                    paddingAngle={2}
                  >
                    {weightPie.map((entry, idx) => (
                      <Cell
                        key={entry.name}
                        fill={entry.signed >= 0 ? PIE_COLORS[idx % PIE_COLORS.length] : '#ff4466'}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      background: 'hsl(var(--card))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(value) => fmtPct(typeof value === 'number' ? value : Number(value), 2)}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="overflow-auto rounded-xl border border-border/40 bg-card/30 p-3">
              <p className="text-xs text-secondary-text">
                调仓日：<span className="font-mono">{lastPosition.date}</span>
              </p>
              <p className="mt-1 text-xs text-secondary-text">
                NAV: <span className="font-mono">{Number(lastPosition.nav).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</span>
              </p>
              <p className="mt-1 text-xs text-secondary-text">
                成本扣减: <span className="font-mono">{Number(lastPosition.costDeducted).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</span>
              </p>
              <table className="mt-3 w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-text">
                    <th className="pb-1">代码</th>
                    <th className="pb-1 text-right">权重</th>
                  </tr>
                </thead>
                <tbody>
                  {weightPie.map((row) => (
                    <tr key={row.name} className="border-t border-border/30">
                      <td className="py-1 font-mono text-foreground">{row.name}</td>
                      <td
                        className={cn(
                          'py-1 text-right font-mono',
                          row.signed >= 0 ? 'text-success' : 'text-danger',
                        )}
                      >
                        {fmtRawPct(row.signed * 100)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-3 text-xs text-warning">
                负权重为模拟空头腿，仅作为研究分析口径，不会下单。
              </p>
            </div>
          </div>
        ) : (
          <p className="text-xs text-secondary-text">未返回调仓快照。</p>
        )}
      </SectionCard>

      <SectionCard title="诊断" subtitle="Diagnostics">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Run ID" value={result.runId} mono />
          <Stat label="Rebalance Count" value={String(result.diagnostics.rebalanceCount)} />
          <Stat label="Missing Symbols" value={String(result.diagnostics.missingSymbols.length)} />
          <Stat label="Insufficient History" value={String(result.diagnostics.insufficientHistorySymbols.length)} />
        </div>
        {(result.diagnostics.missingSymbols.length > 0
          || result.diagnostics.insufficientHistorySymbols.length > 0) && (
          <div className="mt-3 rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs text-warning">
            {result.diagnostics.missingSymbols.length > 0 ? (
              <div>缺失股票：{result.diagnostics.missingSymbols.join(', ')}</div>
            ) : null}
            {result.diagnostics.insufficientHistorySymbols.length > 0 ? (
              <div>历史不足：{result.diagnostics.insufficientHistorySymbols.join(', ')}</div>
            ) : null}
          </div>
        )}
      </SectionCard>
    </div>
  );
};

// =====================================================================
// Main page
// =====================================================================

const QuantResearchPage: React.FC = () => {
  useEffect(() => {
    document.title = '量化研究 - DSA';
  }, []);

  const [activeTab, setActiveTab] = useState<TabKey>('factor');
  const [status, setStatus] = useState<QuantStatus | null>(null);
  const [factors, setFactors] = useState<BuiltinFactor[]>([]);
  const [statusError, setStatusError] = useState<ParsedApiError | null>(null);
  const [isLoadingMeta, setIsLoadingMeta] = useState(true);

  const enabled = Boolean(status?.enabled);

  const fetchMeta = useCallback(async () => {
    setIsLoadingMeta(true);
    setStatusError(null);
    try {
      const [statusRes, factorsRes] = await Promise.all([
        quantResearchApi.getStatus(),
        quantResearchApi.listFactors().catch(() => ({ enabled: false, builtins: [] })),
      ]);
      setStatus(statusRes);
      setFactors(factorsRes.builtins ?? []);
    } catch (err) {
      console.error('quant lab status load failed', err);
      setStatusError(getParsedApiError(err));
    } finally {
      setIsLoadingMeta(false);
    }
  }, []);

  useEffect(() => {
    void fetchMeta();
  }, [fetchMeta]);

  return (
    <AppPage>
      <PageHeader
        eyebrow="QUANT RESEARCH LAB"
        title="量化研究"
        description="基于 OHLCV 的因子探索、研究级回测与组合风险研究。研究专用，永远不会自动下单或写入持仓。"
        actions={
          <div className="flex items-center gap-2">
            {isLoadingMeta ? (
              <Badge variant="default">加载中…</Badge>
            ) : enabled ? (
              <Badge variant="success" glow>
                {status?.phase || 'enabled'}
              </Badge>
            ) : (
              <Badge variant="warning">未启用</Badge>
            )}
            <Button variant="secondary" size="sm" onClick={() => void fetchMeta()}>
              刷新
            </Button>
          </div>
        }
      />

      <div className="mt-6 space-y-4">
        <ResearchDisclaimer />
        {!enabled && !isLoadingMeta ? <DisabledBanner status={status} /> : null}
        {statusError ? <ApiErrorAlert error={statusError} /> : null}

        <div className="flex flex-wrap gap-2">
          <TabButton label="Factor Lab · 因子评估" active={activeTab === 'factor'} onClick={() => setActiveTab('factor')} />
          <TabButton label="Research Backtest · 研究回测" active={activeTab === 'backtest'} onClick={() => setActiveTab('backtest')} />
        </div>

        {activeTab === 'factor' ? (
          <FactorLabTab enabled={enabled} factors={factors} />
        ) : (
          <ResearchBacktestTab enabled={enabled} factors={factors} />
        )}
      </div>
    </AppPage>
  );
};

export default QuantResearchPage;
