"use client";

import { useEffect, useState } from 'react';
import { getRolloutPhase, type RolloutPhaseSnapshot } from '@/lib/api/forecasts';

const PHASE_LABEL: Record<string, string> = {
  rule_based: 'Phase 1 · Rule-Based',
  supervised: 'Phase 2 · Supervised',
  bandit_shadow: 'Phase 3 · Bandit (Shadow)',
  offline_evaluated: 'Phase 4 · Bandit (Live)',
};

const PHASE_TONE: Record<string, string> = {
  rule_based: 'bg-muted text-foreground/80 border-border',
  supervised: 'bg-primary/10 text-primary border-primary/30',
  bandit_shadow: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
  offline_evaluated: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
};

export function RolloutPhaseBadge() {
  const [snap, setSnap] = useState<RolloutPhaseSnapshot | null>(null);

  useEffect(() => {
    let alive = true;
    getRolloutPhase().then((s) => {
      if (alive) setSnap(s);
    });
    return () => {
      alive = false;
    };
  }, []);

  if (!snap) return null;

  const label = PHASE_LABEL[snap.current_phase] ?? snap.current_phase;
  const tone = PHASE_TONE[snap.current_phase] ?? PHASE_TONE.rule_based;
  const showProgress = snap.progress_pct < 100 && snap.next_threshold > snap.labelled_experiments;

  return (
    <div
      className={`inline-flex flex-col gap-1 rounded-md border px-3 py-2 text-xs ${tone}`}
      title={
        snap.bandit_live
          ? 'Bandit outputs are live in production.'
          : 'Bandit outputs are advisory only until Phase 4.'
      }
    >
      <div className="flex items-center gap-2 font-medium">
        <span className="h-2 w-2 rounded-full bg-current" />
        <span>{label}</span>
      </div>
      {showProgress && (
        <div className="flex items-center gap-2">
          <div className="h-1 w-24 overflow-hidden rounded bg-current/20">
            <div
              className="h-full bg-current"
              style={{ width: `${snap.progress_pct}%` }}
            />
          </div>
          <span className="tabular-nums opacity-70">
            {snap.labelled_experiments}/{snap.next_threshold}
          </span>
        </div>
      )}
    </div>
  );
}
