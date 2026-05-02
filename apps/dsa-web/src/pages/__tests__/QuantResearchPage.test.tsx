import type React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import QuantResearchPage from '../QuantResearchPage';

const {
  getStatus,
  getCapabilities,
  listFactors,
  evaluateFactor,
  runBacktest,
} = vi.hoisted(() => ({
  getStatus: vi.fn(),
  getCapabilities: vi.fn(),
  listFactors: vi.fn(),
  evaluateFactor: vi.fn(),
  runBacktest: vi.fn(),
}));

vi.mock('../../api/quantResearch', () => ({
  quantResearchApi: {
    getStatus,
    getCapabilities,
    listFactors,
    evaluateFactor,
    runBacktest,
  },
}));

// Recharts depends on layout APIs that jsdom doesn't implement (see the
// existing PortfolioPage test). Flatten every chart primitive to a div so
// the page renders cleanly under jsdom.
vi.mock('recharts', () => {
  const Pass = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  const Empty = () => null;
  return {
    ResponsiveContainer: Pass,
    LineChart: Pass,
    AreaChart: Pass,
    BarChart: Pass,
    PieChart: Pass,
    Pie: Pass,
    CartesianGrid: Empty,
    Cell: Empty,
    Legend: Empty,
    Line: Empty,
    Area: Empty,
    Bar: Empty,
    ReferenceLine: Empty,
    Tooltip: Empty,
    XAxis: Empty,
    YAxis: Empty,
  };
});

const sampleFactors = [
  {
    id: 'ma_ratio_5_20',
    name: 'MA Ratio 5/20',
    description: '短期 vs 长期均线比率',
    expectedDirection: 'positive',
    lookbackDays: 21,
  },
  {
    id: 'return_5d',
    name: '5-Day Return',
    description: '过去 5 天收益率',
    expectedDirection: 'unknown',
    lookbackDays: 6,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  // Default: lab is fully enabled with two builtin factors. Individual
  // tests override these mocks for the disabled / error paths.
  getStatus.mockResolvedValue({
    enabled: true,
    status: 'ready',
    message: 'Quant Research Lab is live.',
    phase: 'phase-7-web-workbench',
  });
  getCapabilities.mockResolvedValue({
    enabled: true,
    capabilities: [],
  });
  listFactors.mockResolvedValue({
    enabled: true,
    builtins: sampleFactors,
  });
  evaluateFactor.mockResolvedValue({
    enabled: true,
    runId: 'eval-run-1',
    factor: { name: 'MA Ratio 5/20', builtinId: 'ma_ratio_5_20' },
    factorKind: 'builtin',
    stockPool: ['AAPL'],
    startDate: '2026-01-01',
    endDate: '2026-04-30',
    forwardWindow: 5,
    quantileCount: 5,
    coverage: {
      requestedStocks: ['AAPL'],
      coveredStocks: ['AAPL'],
      missingStocks: [],
      requestedDays: 86,
      totalObservations: 86,
      missingObservations: 0,
      missingRate: 0,
    },
    metrics: {
      ic: [0.12, 0.05, -0.04],
      rankIc: [0.11, 0.04, -0.05],
      dailyIcCount: 3,
      dailyRankIcCount: 3,
      icMean: 0.043,
      icStd: 0.08,
      icir: 0.54,
      rankIcMean: 0.033,
      quantileCount: 5,
      quantileReturns: { '1': -0.012, '2': -0.005, '3': 0.001, '4': 0.008, '5': 0.014 },
      longShortSpread: 0.026,
      factorTurnover: 0.31,
      autocorrelation: 0.62,
    },
    diagnostics: ['evaluator_version=phase-2'],
    assumptions: { no_lookahead: true },
  });
  runBacktest.mockResolvedValue({
    enabled: true,
    runId: 'bt-run-1',
    strategy: 'top_k_long_only',
    factorKind: 'builtin',
    factorId: 'ma_ratio_5_20',
    expression: null,
    stockPool: ['AAPL'],
    startDate: '2026-01-01',
    endDate: '2026-04-30',
    rebalanceFrequency: 'weekly',
    navCurve: [{ date: '2026-01-02', nav: 1000000 }],
    metrics: {
      totalReturn: 0.05,
      annualizedReturn: 0.18,
      annualizedVolatility: 0.16,
      sharpe: 1.1,
      sortino: 1.4,
      calmar: 0.7,
      maxDrawdown: -0.07,
      winRate: 0.55,
      turnover: 0.12,
      costDrag: 0.0034,
      benchmarkReturn: null,
      excessReturn: null,
      informationRatio: null,
    },
    diagnostics: {
      dataCoverage: {},
      missingSymbols: [],
      insufficientHistorySymbols: [],
      rebalanceCount: 17,
      lookaheadBiasGuard: true,
      assumptions: {},
    },
    positions: [],
    createdAt: '2026-05-01T00:00:00Z',
  });
});

describe('QuantResearchPage', () => {
  it('renders the persistent research-only disclaimer regardless of flag state', async () => {
    render(<QuantResearchPage />);
    // Surface always carries the "research only, no auto-trading" disclaimer.
    expect(await screen.findByText('研究专用 · 非投资建议')).toBeInTheDocument();
  });

  it('reads the master flag from /api/v1/quant/status on mount', async () => {
    render(<QuantResearchPage />);
    await waitFor(() => {
      expect(getStatus).toHaveBeenCalledTimes(1);
      expect(listFactors).toHaveBeenCalledTimes(1);
    });
  });

  it('shows enabled badge with the live phase string when the lab is on', async () => {
    render(<QuantResearchPage />);
    expect(await screen.findByText('phase-7-web-workbench')).toBeInTheDocument();
  });

  it('renders the disabled banner and disables the run button when QUANT_RESEARCH_ENABLED is off', async () => {
    getStatus.mockResolvedValueOnce({
      enabled: false,
      status: 'not_enabled',
      message: 'Quant Research Lab is disabled.',
      phase: 'phase-7-web-workbench',
    });
    listFactors.mockResolvedValueOnce({ enabled: false, builtins: [] });

    render(<QuantResearchPage />);

    expect(await screen.findByText('未启用')).toBeInTheDocument();
    // The disabled banner is rendered both at the page level and inside the
    // active tab — `getAllByText` lets the test stay correct if either copy
    // moves around in the future.
    expect(screen.getAllByText('量化研究实验室未启用').length).toBeGreaterThan(0);
    const runButton = screen.getByRole('button', { name: '运行评估' });
    expect(runButton).toBeDisabled();
  });

  it('exposes Factor Lab and Research Backtest tabs and lets the user switch', async () => {
    render(<QuantResearchPage />);

    // Factor Lab is the default tab.
    expect(await screen.findByRole('button', { name: '运行评估' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Research Backtest/ }));
    // Switching swaps the run button for the backtest variant.
    expect(await screen.findByRole('button', { name: '运行回测' })).toBeInTheDocument();
  });

  it('rejects an empty stock pool without calling the backend', async () => {
    render(<QuantResearchPage />);
    const runButton = await screen.findByRole('button', { name: '运行评估' });

    // Wipe the default stock list and click run.
    const stocksInput = screen.getByPlaceholderText(/AAPL, MSFT, NVDA/);
    fireEvent.change(stocksInput, { target: { value: '   ' } });
    fireEvent.click(runButton);

    expect(await screen.findByText('股票池为空')).toBeInTheDocument();
    expect(evaluateFactor).not.toHaveBeenCalled();
  });

  it('runs factor evaluation against the backend and renders metrics', async () => {
    render(<QuantResearchPage />);
    const runButton = await screen.findByRole('button', { name: '运行评估' });

    // The page lazily populates `builtinId` once factors arrive (a
    // useEffect). Wait until the built-in factor's description appears
    // before clicking — otherwise the form short-circuits with a "请选择
    // 内置因子" error and never calls the API.
    expect(await screen.findByText('短期 vs 长期均线比率')).toBeInTheDocument();

    fireEvent.click(runButton);

    await waitFor(() => {
      expect(evaluateFactor).toHaveBeenCalledTimes(1);
    });

    // The IC stats appear in the result panel after the run resolves.
    expect(await screen.findByText('IC Mean')).toBeInTheDocument();
    expect(await screen.findByText('Long-Short Spread')).toBeInTheDocument();
    // Run id is rendered as a mono-spaced stat for cross-tab correlation.
    expect(await screen.findByText('eval-run-1')).toBeInTheDocument();
  });

  it('runs research backtest from the second tab and renders metrics', async () => {
    render(<QuantResearchPage />);

    fireEvent.click(await screen.findByRole('button', { name: /Research Backtest/ }));
    const runButton = await screen.findByRole('button', { name: '运行回测' });

    // Explicitly pick the builtin factor instead of relying on the
    // useEffect race; the user-style `change` event on the select
    // synchronously calls `setBuiltinFactorId`.
    const factorSelect = await screen.findByLabelText('选择内置因子');
    fireEvent.change(factorSelect, { target: { value: 'ma_ratio_5_20' } });

    fireEvent.click(runButton);

    await waitFor(() => {
      expect(runBacktest).toHaveBeenCalledTimes(1);
    });

    // Sharpe / Total Return / lookahead guard chip all show up post-run.
    expect(await screen.findByText('Sharpe')).toBeInTheDocument();
    expect(await screen.findByText('Total Return')).toBeInTheDocument();
    expect(await screen.findByText('no-lookahead')).toBeInTheDocument();
  });
});
