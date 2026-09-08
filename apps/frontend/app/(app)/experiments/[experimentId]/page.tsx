'use client';

import * as React from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import { PageHeader } from '@/components/page-header';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { StatusBadge } from '@/components/status-badge';
import { LoadingState, EmptyState } from '@/components/ui/state-feedback';
import { Button } from '@/components/ui/button';
import { PipelineStepper } from '@/components/simulation/pipeline-stepper';
import { ApprovalPanel } from '@/components/simulation/approval-panel';
import { CanaryLivePanel } from '@/components/simulation/canary-live-panel';
import { useToast } from '@/components/app-providers';
import {
  useExperimentDetailQuery,
  useApproveExperimentMutation,
  useRejectExperimentMutation,
} from '@/hooks/use-experiments';
import { useConnectionsQuery } from '@/hooks/use-connections';
import { absoluteTime, deltaPct, relativeTime } from '@/lib/format';

export default function ExperimentDetailPage() {
  const params = useParams<{ experimentId: string }>();
  const { data: exp, isLoading, isError } = useExperimentDetailQuery(params.experimentId);
  const { data: connections = [] } = useConnectionsQuery();
  const { toast } = useToast();

  const approveMutation = useApproveExperimentMutation();
  const rejectMutation = useRejectExperimentMutation();

  const conn = connections.find((c) => c.id === exp?.connectionId);

  if (isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader title="Experiment detail" />
        <LoadingState message="Loading statistical verification and policy verdict..." />
      </div>
    );
  }

  if (isError || !exp) {
    return (
      <div className="space-y-6">
        <PageHeader title="Experiment not found" />
        <EmptyState
          title="This experiment does not exist"
          description="It may have been archived or deleted."
          action={
            <Button asChild variant="outline" size="sm">
              <Link href="/experiments">Back to history</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const completed = exp.outcome === 'COMMIT' || exp.outcome === 'ROLLBACK';
  const canaryRunning = exp.approvalState === 'APPROVED' && exp.outcome === 'IN_PROGRESS';
  const awaitingApproval =
    exp.approvalState === 'PENDING_APPROVAL' && exp.outcome === 'AWAITING_APPROVAL';

  async function handleApprove() {
    if (!exp) return;
    try {
      await approveMutation.mutateAsync({ id: exp.id, notes: 'Approved by Lead DBA' });
      toast({
        kind: 'success',
        title: 'Approval recorded',
        description: 'Canary deployment started. The audit trail has been updated.',
      });
    } catch (err: unknown) {
      toast({
        kind: 'danger',
        title: 'Approval failed',
        description: err instanceof Error ? err.message : 'Could not record approval.',
      });
    }
  }

  async function handleReject() {
    if (!exp) return;
    try {
      await rejectMutation.mutateAsync({ id: exp.id, reason: 'Rejected by operator' });
      toast({
        kind: 'warning',
        title: 'Experiment rejected',
        description: 'No deployment will proceed. The candidate optimization has been marked rejected.',
      });
    } catch (err: unknown) {
      toast({
        kind: 'danger',
        title: 'Rejection failed',
        description: err instanceof Error ? err.message : 'Could not record rejection.',
      });
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumb={
          <Link href="/experiments" className="inline-flex items-center gap-1 hover:text-foreground">
            <ArrowLeft className="h-3 w-3" /> Optimization history
          </Link>
        }
        title={`Experiment ${exp.id}`}
        description={`Candidate optimization verification for ${conn?.name || exp.connectionId}.`}
      />

      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={exp.verdict} dot />
        <StatusBadge status={exp.outcome} />
        <span className="text-xs text-muted-foreground">
          Created {relativeTime(exp.createdAtISO)} ({absoluteTime(exp.createdAtISO)})
        </span>
      </div>

      <PipelineStepper currentStage={exp.currentStage} completed={completed} />

      {awaitingApproval && (
        <ApprovalPanel
          verdict={exp.verdict}
          onApprove={handleApprove}
          onReject={handleReject}
        />
      )}

      {canaryRunning && (
        <CanaryLivePanel
          experimentId={exp.id}
          outcome={exp.outcome === 'COMMIT' || exp.outcome === 'ROLLBACK' ? exp.outcome : 'COMMIT'}
          rollbackReason={exp.rollbackReason}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>Baseline vs Candidate Benchmark</CardTitle>
          <CardDescription>
            Bootstrap confidence intervals (N=10,000 resamples). Significance: {exp.significance}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="pb-2 text-left font-medium">Metric</th>
                  <th className="pb-2 text-right font-medium">Baseline</th>
                  <th className="pb-2 text-right font-medium">Candidate</th>
                  <th className="pb-2 text-right font-medium">Delta</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {exp.comparisons.map((c) => {
                  const d = deltaPct(c.baseline, c.candidate, c.betterWhenLower);
                  return (
                    <tr key={c.metric}>
                      <td className="py-2.5 font-medium">{c.metric}</td>
                      <td className="py-2.5 text-right font-mono text-muted-foreground">
                        {c.baseline} {c.unit}
                      </td>
                      <td className="py-2.5 text-right font-mono font-medium">
                        {c.candidate} {c.unit}
                      </td>
                      <td className="py-2.5 text-right font-mono">
                        <span className={d.improved ? 'text-success font-medium' : 'text-danger font-medium'}>
                          {d.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <ExplainDiffCard before={exp.explainBefore} after={exp.explainAfter} />
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Arc A: EXPLAIN before/after side-by-side diff card.
// Reads the plan JSON captured by shadow_lab_worker around the candidate
// install and shows the two plans in monospace side-by-side, with a small
// Total Cost delta pulled from Plan['Total Cost'] on each.
// ────────────────────────────────────────────────────────────────────────
function _extractTotalCost(plan: unknown): number | null {
  if (!plan || typeof plan !== 'object') return null;
  // Postgres EXPLAIN (FORMAT JSON) shape: [{ "Plan": { "Total Cost": ... } }].
  const arr = Array.isArray(plan) ? plan : [plan];
  const first = arr[0] as Record<string, unknown> | undefined;
  const p = first && (first['Plan'] as Record<string, unknown> | undefined);
  const cost = p && p['Total Cost'];
  return typeof cost === 'number' ? cost : null;
}

function _formatPlan(plan: unknown): string {
  if (plan == null) return '';
  try {
    return JSON.stringify(plan, null, 2);
  } catch {
    return String(plan);
  }
}

function ExplainDiffCard({ before, after }: { before?: unknown; after?: unknown }) {
  const hasBefore = before != null && before !== '';
  const hasAfter = after != null && after !== '';

  if (!hasBefore && !hasAfter) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>EXPLAIN before vs after</CardTitle>
          <CardDescription>
            No plan capture available for this experiment. The shadow-pool worker
            records EXPLAIN plans around the candidate install; this card populates
            once a shadow-pool run has completed.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const costBefore = _extractTotalCost(before);
  const costAfter = _extractTotalCost(after);
  const delta =
    costBefore != null && costAfter != null ? costAfter - costBefore : null;
  const deltaPctVal =
    costBefore != null && costAfter != null && costBefore > 0
      ? ((costAfter - costBefore) / costBefore) * 100
      : null;
  const improved = delta != null && delta < 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>EXPLAIN before vs after</CardTitle>
        <CardDescription>
          Planner cost + tree captured by the shadow-pool worker for the workload
          query, before and after the candidate optimization was installed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {delta != null && (
          <div className="mb-3 flex flex-wrap items-baseline gap-4 text-xs">
            <span className="text-muted-foreground">Total Cost</span>
            <span className="font-mono">
              before {costBefore?.toFixed(2) ?? '—'}
            </span>
            <span className="font-mono">
              after {costAfter?.toFixed(2) ?? '—'}
            </span>
            <span
              className={
                improved
                  ? 'font-mono font-medium text-success'
                  : 'font-mono font-medium text-danger'
              }
            >
              Δ {delta > 0 ? '+' : ''}
              {delta.toFixed(2)}
              {deltaPctVal != null && (
                <span className="ml-1">
                  ({deltaPctVal > 0 ? '+' : ''}
                  {deltaPctVal.toFixed(1)}%)
                </span>
              )}
            </span>
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">Before</div>
            <pre className="max-h-96 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed">
              {hasBefore ? _formatPlan(before) : '(no plan captured)'}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">After</div>
            <pre className="max-h-96 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed">
              {hasAfter ? _formatPlan(after) : '(no plan captured)'}
            </pre>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
