/**
 * ScheduleCloudCard
 * -----------------
 * Manages the GCP Cloud Scheduler job that triggers daily watchlist analysis
 * on Cloud Run. Lives under the "system" category alongside legacy schedule
 * fields. Two input modes:
 *   - Simple:   pick HH:MM + days-of-week chips → derives cron
 *   - Advanced: edit raw 5-field cron expression
 *
 * Source-of-truth for cron / timezone is `runtime.env` (managed via the rest
 * of the system-config form). This card just reflects them into Cloud
 * Scheduler with one click and shows the job's live state.
 */
import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { scheduleApi, type ScheduleStatus } from '../../api/systemConfig';
import { getParsedApiError } from '../../api/error';
import { Badge, Button, InlineAlert, Input, StatusDot } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';

interface ScheduleCloudCardProps {
  /** Current cron value from the system-config form (`SCHEDULE_CRON`). */
  cron: string;
  /** Current timezone value from the form (`SCHEDULE_TIMEZONE`). */
  timezone: string;
  /** Current `SCHEDULE_ENABLED` value from the form. */
  enabled: boolean;
  /**
   * Called when the user edits cron / timezone via the simple-mode helpers.
   * The parent should reflect this back into `SCHEDULE_CRON` /
   * `SCHEDULE_TIMEZONE` config fields and persist via the standard
   * SystemConfig save flow.
   */
  onChange?: (next: { cron?: string; timezone?: string }) => void;
}

type Tone = 'idle' | 'loading' | 'success' | 'error';

interface ActionState {
  tone: Tone;
  text?: string;
}

const DAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** Try to parse a cron of the form `M H * * <DAYS>` into simple mode. */
function parseSimpleCron(cron: string): { time: string; days: number[] } | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [m, h, dom, mon, dow] = parts;
  if (dom !== '*' || mon !== '*') return null;
  const minute = Number(m);
  const hour = Number(h);
  if (Number.isNaN(minute) || Number.isNaN(hour)) return null;
  if (minute < 0 || minute > 59 || hour < 0 || hour > 23) return null;
  const days = parseDaysField(dow);
  if (!days) return null;
  return {
    time: `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`,
    days,
  };
}

function parseDaysField(field: string): number[] | null {
  if (field === '*') return [0, 1, 2, 3, 4, 5, 6];
  const out = new Set<number>();
  for (const segment of field.split(',')) {
    if (/^\d+$/.test(segment)) {
      out.add(Number(segment));
      continue;
    }
    const range = segment.split('-');
    if (range.length === 2 && /^\d+$/.test(range[0]) && /^\d+$/.test(range[1])) {
      const lo = Number(range[0]);
      const hi = Number(range[1]);
      if (lo > hi) return null;
      for (let i = lo; i <= hi; i++) out.add(i);
      continue;
    }
    return null;
  }
  return [...out].filter((d) => d >= 0 && d <= 6).sort((a, b) => a - b);
}

function buildSimpleCron(time: string, days: number[]): string {
  const [h, m] = time.split(':');
  const dow = days.length === 0
    ? '*'
    : days.length === 7
    ? '*'
    : days.sort((a, b) => a - b).join(',');
  return `${Number(m)} ${Number(h)} * * ${dow}`;
}

function ScheduleStateBadge({ status }: { status: ScheduleStatus | null }) {
  if (!status) {
    return <Badge variant="default">Loading…</Badge>;
  }
  if (!status.exists) {
    return <Badge variant="warning">Not created</Badge>;
  }
  switch ((status.state || '').toUpperCase()) {
    case 'ENABLED':
      return <Badge variant="success">Active</Badge>;
    case 'PAUSED':
      return <Badge variant="warning">Paused</Badge>;
    case 'UPDATE_FAILED':
      return <Badge variant="danger">Update failed</Badge>;
    case 'DISABLED':
      return <Badge variant="default">Disabled</Badge>;
    default:
      return <Badge variant="default">{status.state || 'Unknown'}</Badge>;
  }
}

export const ScheduleCloudCard: React.FC<ScheduleCloudCardProps> = ({
  cron,
  timezone,
  enabled,
  onChange,
}) => {
  const [advanced, setAdvanced] = useState(false);
  const [status, setStatus] = useState<ScheduleStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [syncState, setSyncState] = useState<ActionState>({ tone: 'idle' });
  const [actionState, setActionState] = useState<ActionState>({ tone: 'idle' });

  // Simple mode parses cron into time + days; flips to advanced if the cron
  // is too complex to round-trip.
  const simple = useMemo(() => parseSimpleCron(cron), [cron]);
  useEffect(() => {
    if (!simple) {
      setAdvanced(true);
    }
  }, [simple]);

  const refreshStatus = useCallback(async () => {
    try {
      setStatusError(null);
      const result = await scheduleApi.getStatus();
      setStatus(result);
    } catch (error) {
      const parsed = getParsedApiError(error);
      setStatusError(parsed.message || 'Failed to load scheduler status');
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const handleSimpleTimeChange = (next: string) => {
    if (!simple) return;
    onChange?.({ cron: buildSimpleCron(next, simple.days) });
  };

  const handleSimpleDayToggle = (day: number) => {
    if (!simple) return;
    const next = simple.days.includes(day)
      ? simple.days.filter((d) => d !== day)
      : [...simple.days, day];
    onChange?.({ cron: buildSimpleCron(simple.time, next) });
  };

  const handleAdvancedCronChange = (raw: string) => {
    onChange?.({ cron: raw });
  };

  const handleSync = async () => {
    setSyncState({ tone: 'loading', text: 'Syncing…' });
    try {
      const result = await scheduleApi.sync();
      setStatus(result.job);
      setSyncState({
        tone: 'success',
        text: `Synced: ${result.cron} (${result.timeZone})`,
      });
    } catch (error) {
      const parsed = getParsedApiError(error);
      setSyncState({ tone: 'error', text: parsed.message || 'Sync failed' });
    }
  };

  const handleRunNow = async () => {
    setActionState({ tone: 'loading', text: 'Triggering…' });
    try {
      await scheduleApi.runNow();
      setActionState({
        tone: 'success',
        text: 'Triggered. Check Cloud Run logs / your inbox.',
      });
      await refreshStatus();
    } catch (error) {
      const parsed = getParsedApiError(error);
      setActionState({
        tone: 'error',
        text: parsed.message || 'Run-now failed',
      });
    }
  };

  const handlePauseResume = async () => {
    const isPaused = (status?.state || '').toUpperCase() === 'PAUSED';
    setActionState({
      tone: 'loading',
      text: isPaused ? 'Resuming…' : 'Pausing…',
    });
    try {
      const result = isPaused
        ? await scheduleApi.resume()
        : await scheduleApi.pause();
      if (result.job) setStatus(result.job);
      setActionState({
        tone: 'success',
        text: isPaused ? 'Resumed' : 'Paused',
      });
    } catch (error) {
      const parsed = getParsedApiError(error);
      setActionState({
        tone: 'error',
        text: parsed.message || 'Action failed',
      });
    }
  };

  const isPaused = (status?.state || '').toUpperCase() === 'PAUSED';
  const exists = status?.exists ?? false;

  return (
    <SettingsSectionCard
      title="Cloud Scheduler"
      description="Run the daily watchlist analysis on a managed GCP cron. Cloud Scheduler triggers POST /analyze; the server reads STOCK_LIST from runtime.env and emails the report."
    >
      <div className="space-y-4">
        {/* Status row */}
        <div className="flex items-center gap-3">
          <ScheduleStateBadge status={status} />
          {status?.exists && (
            <span className="text-xs text-slate-400">
              {status.schedule} · {status.timeZone}
            </span>
          )}
          {status?.nextRunTime && (
            <span className="text-xs text-slate-500">
              next: {status.nextRunTime}
            </span>
          )}
        </div>

        {statusError && <InlineAlert variant="danger" message={statusError} />}

        {/* Mode toggle */}
        <div className="flex items-center gap-3 text-xs">
          <button
            type="button"
            onClick={() => setAdvanced(false)}
            className={`px-2 py-1 rounded ${!advanced ? 'bg-sky-600 text-white' : 'text-slate-400'}`}
            disabled={!simple}
          >
            Simple
          </button>
          <button
            type="button"
            onClick={() => setAdvanced(true)}
            className={`px-2 py-1 rounded ${advanced ? 'bg-sky-600 text-white' : 'text-slate-400'}`}
          >
            Advanced (cron)
          </button>
          {!simple && (
            <span className="text-amber-400">
              Current cron is too complex for Simple mode; using Advanced.
            </span>
          )}
        </div>

        {/* Editor */}
        {!advanced && simple ? (
          <div className="space-y-3">
            <div>
              <label className="text-xs text-slate-400 block mb-1">Time (HH:MM)</label>
              <Input
                type="time"
                value={simple.time}
                onChange={(e) => handleSimpleTimeChange(e.target.value)}
              />
            </div>
            <div>
              <label className="text-xs text-slate-400 block mb-1">Days of week</label>
              <div className="flex gap-1 flex-wrap">
                {DAY_LABELS.map((label, idx) => {
                  const active = simple.days.includes(idx);
                  return (
                    <button
                      key={label}
                      type="button"
                      onClick={() => handleSimpleDayToggle(idx)}
                      className={`px-2 py-1 rounded text-xs ${
                        active
                          ? 'bg-sky-600 text-white'
                          : 'bg-slate-700 text-slate-300'
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="text-xs text-slate-500">
              Generates cron: <code>{cron}</code>
            </div>
          </div>
        ) : (
          <div>
            <label className="text-xs text-slate-400 block mb-1">
              Cron (5 fields: minute hour day-of-month month day-of-week)
            </label>
            <Input
              value={cron}
              onChange={(e) => handleAdvancedCronChange(e.target.value)}
              placeholder="0 6 * * 2-6"
            />
          </div>
        )}

        <div>
          <label className="text-xs text-slate-400 block mb-1">Timezone (IANA)</label>
          <Input
            value={timezone}
            onChange={(e) => onChange?.({ timezone: e.target.value })}
            placeholder="Asia/Shanghai"
          />
        </div>

        {/* Actions */}
        <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-700">
          <Button onClick={handleSync} disabled={syncState.tone === 'loading'}>
            {syncState.tone === 'loading' ? 'Syncing…' : 'Sync to Cloud Scheduler'}
          </Button>
          <Button
            variant="secondary"
            onClick={handleRunNow}
            disabled={!exists || actionState.tone === 'loading'}
          >
            Run now
          </Button>
          <Button
            variant="secondary"
            onClick={handlePauseResume}
            disabled={!exists || actionState.tone === 'loading'}
          >
            {isPaused ? 'Resume' : 'Pause'}
          </Button>
          {!enabled && (
            <Badge variant="default">SCHEDULE_ENABLED is off — sync will pause the job</Badge>
          )}
        </div>

        {/* Status messages */}
        {syncState.tone !== 'idle' && syncState.text && (
          <div className="flex items-center gap-2 text-xs">
            <StatusDot
              tone={
                syncState.tone === 'loading'
                  ? 'warning'
                  : syncState.tone === 'success'
                  ? 'success'
                  : 'danger'
              }
              pulse={syncState.tone === 'loading'}
            />
            <span>{syncState.text}</span>
          </div>
        )}
        {actionState.tone !== 'idle' && actionState.text && (
          <div className="flex items-center gap-2 text-xs">
            <StatusDot
              tone={
                actionState.tone === 'loading'
                  ? 'warning'
                  : actionState.tone === 'success'
                  ? 'success'
                  : 'danger'
              }
              pulse={actionState.tone === 'loading'}
            />
            <span>{actionState.text}</span>
          </div>
        )}
      </div>
    </SettingsSectionCard>
  );
};

export default ScheduleCloudCard;
