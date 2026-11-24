/**
 * Approvals screen — the human gate in front of risky capability calls.
 *
 * Frame: page header (pending count + refresh) → pending queue → auto-approval
 * rules → already-decided requests.  The queue comes from the live store
 * snapshot (SSE refreshes it); rules and decisions are read from their own
 * endpoints because the snapshot does not carry them.
 *
 * Every write action is hidden/disabled for the `viewer` role and the server
 * remains the authority: a refused write surfaces the backend's message.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { IconButton } from '@astryxdesign/core/IconButton';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Banner } from '@astryxdesign/core/Banner';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Spinner } from '@astryxdesign/core/Spinner';
import { Table, pixel, proportional, type TableColumn } from '@astryxdesign/core/Table';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Divider } from '@astryxdesign/core/Divider';
import { Section } from '@astryxdesign/core/Section';
import { Heading, Text } from '@astryxdesign/core/Text';
import { ArrowPathIcon, PlusIcon, ShieldCheckIcon, TrashIcon } from '@heroicons/react/24/outline';
import { api, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { useCanWrite } from '../../session';
import { loadState, useStore, type SfoState } from '../../state/store';
import { fmtCountdown, fmtDateTime, statusLabel, statusTone } from '../../ui/format';
import { Page } from '../../ui/Page';
import { toast, toastError } from '../../ui/toast';
import { ApprovalDetailDialog } from './ApprovalDetailDialog';
import { RuleDialog } from './RuleDialog';
import { badgeVariant, modeLabel, scopeInline, RiskBadge, useNow } from './common';
import type { ApprovalRow, DecidedFilter, DecidedRow, RuleRow } from './types';

/* Stable selectors: a new array every render would loop `useSyncExternalStore`. */
const selApprovals = (s: SfoState): ApprovalRow[] => s.approvals as unknown as ApprovalRow[];
const selProjects = (s: SfoState): SfoState['projects'] => s.projects;

function rowsOf<T>(payload: unknown, key: string): T[] {
  const record = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  const rows = record[key];
  if (!Array.isArray(rows)) return [];
  return rows.filter((row): row is T => Boolean(row) && typeof row === 'object');
}

const DECIDED_FILTERS: DecidedFilter[] = ['all', 'approved', 'denied', 'expired'];
const MAX_DECIDED = 50;

export default function ApprovalsScreen(): ReactNode {
  const approvals = useStore(selApprovals);
  const projects = useStore(selProjects);
  const canWrite = useCanWrite();
  const now = useNow(1000);

  const [selected, setSelected] = useState<ApprovalRow | null>(null);
  const [ruleDialogOpen, setRuleDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<RuleRow | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [rules, setRules] = useState<RuleRow[]>([]);
  const [rulesLoading, setRulesLoading] = useState(true);
  const [rulesError, setRulesError] = useState('');

  const [filter, setFilter] = useState<DecidedFilter>('all');
  const [decided, setDecided] = useState<DecidedRow[]>([]);
  const [decidedTotal, setDecidedTotal] = useState(0);
  const [decidedLoading, setDecidedLoading] = useState(true);
  const [decidedError, setDecidedError] = useState('');

  const [refreshing, setRefreshing] = useState(false);

  const pending = useMemo(
    () => [...approvals].sort((a, b) => (Number(a.expires_at) || 0) - (Number(b.expires_at) || 0)),
    [approvals],
  );

  const loadRules = useCallback(async (): Promise<void> => {
    setRulesLoading(true);
    try {
      const payload = await api.getRules();
      setRules(rowsOf<RuleRow>(payload, 'rules'));
      setRulesError('');
    } catch (error) {
      setRules([]);
      setRulesError(isUnsupported(error) ? t('shell.unsupported') : (error instanceof Error ? error.message : String(error)));
    } finally {
      setRulesLoading(false);
    }
  }, []);

  const loadDecided = useCallback(async (): Promise<void> => {
    setDecidedLoading(true);
    setDecided([]);
    try {
      const payload = await api.getApprovals(filter === 'all' ? '' : filter);
      // `all` is the unfiltered list, which still contains pending requests:
      // this section is the decision history, so drop them.
      const rows = rowsOf<DecidedRow>(payload, 'approvals')
        .filter((row) => filter !== 'all' || row.status !== 'pending')
        .sort((a, b) => Number(b.decided_at || b.created_at || 0) - Number(a.decided_at || a.created_at || 0));
      setDecidedTotal(rows.length);
      setDecided(rows.slice(0, MAX_DECIDED));
      setDecidedError('');
    } catch (error) {
      setDecided([]);
      setDecidedTotal(0);
      setDecidedError(isUnsupported(error) ? t('shell.unsupported') : (error instanceof Error ? error.message : String(error)));
    } finally {
      setDecidedLoading(false);
    }
  }, [filter]);

  useEffect(() => { void loadRules(); }, [loadRules]);
  useEffect(() => { void loadDecided(); }, [loadDecided]);

  const refreshAll = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      await Promise.all([loadState({ quiet: true }), loadRules(), loadDecided()]);
    } finally {
      setRefreshing(false);
    }
  }, [loadDecided, loadRules]);

  const afterDecision = useCallback((): void => {
    void loadState({ quiet: true }).catch(() => undefined);
    void loadRules();
    void loadDecided();
  }, [loadDecided, loadRules]);

  const removeRule = async (): Promise<void> => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      await api.deleteRule(deleteTarget.id);
      toast(t('approvals.rule.deleted'), 'info');
      setDeleteTarget(null);
      await Promise.all([loadRules(), loadState({ quiet: true })]);
    } catch (error) {
      toastError(error);
    } finally {
      setDeleting(false);
    }
  };

  const pendingColumns: TableColumn<ApprovalRow>[] = [
    {
      key: 'risk',
      header: t('approvals.col.risk'),
      width: pixel(110),
      renderCell: (row) => <RiskBadge risk={row.risk} />,
    },
    {
      key: 'capability',
      header: t('approvals.col.capability'),
      width: proportional(2),
      renderCell: (row) => {
        const scope = scopeInline(row.scope);
        return (
          <VStack gap={0.5}>
            <Text type="code" size="sm" maxLines={1}>{row.capability || '—'}</Text>
            <Text size="xsm" color="secondary" maxLines={1}>
              {scope === '—' ? t('approvals.rule.scope_any') : scope}
            </Text>
          </VStack>
        );
      },
    },
    {
      key: 'principal',
      header: t('approvals.col.principal'),
      width: pixel(150),
      renderCell: (row) => <Text type="code" size="sm" maxLines={1}>{row.principal || '—'}</Text>,
    },
    {
      key: 'project',
      header: t('approvals.col.project'),
      width: pixel(170),
      renderCell: (row) => {
        const project = projects.find((item) => item.id === row.project_id);
        return (
          <VStack gap={0.5}>
            <Text size="sm">{project?.name || row.project_id || '—'}</Text>
            {row.task_id ? <Text type="code" size="xsm" color="secondary">{row.task_id}</Text> : null}
          </VStack>
        );
      },
    },
    {
      key: 'expires',
      header: t('approvals.col.expires'),
      width: pixel(90),
      renderCell: (row) => {
        const ttl = fmtCountdown(row.expires_at, now);
        if (ttl.expired) return <Badge label={t('approvals.detail.expired')} variant="error" />;
        return (
          <Text
            size="sm"
            color={ttl.urgent ? 'accent' : 'secondary'}
            hasTabularNumbers
          >
            {ttl.text}
          </Text>
        );
      },
    },
    {
      key: 'actions',
      header: t('approvals.col.actions'),
      align: 'end',
      width: pixel(120),
      renderCell: (row) => (
        <Button
          label={t('approvals.review')}
          variant="secondary"
          size="sm"
          onClick={() => setSelected(row)}
        />
      ),
    },
  ];

  const ruleColumns: TableColumn<RuleRow>[] = [
    {
      key: 'effect',
      header: t('approvals.col.effect'),
      width: pixel(90),
      renderCell: (row) => {
        const effect = String(row.effect || 'allow');
        return (
          <Badge
            label={effect.toUpperCase()}
            variant={effect === 'deny' ? 'error' : 'success'}
          />
        );
      },
    },
    {
      key: 'capability',
      header: t('approvals.col.capability'),
      width: proportional(2),
      renderCell: (row) => {
        const scope = scopeInline(row.scope);
        return (
          <VStack gap={0.5}>
            <Text type="code" size="sm" maxLines={1}>{row.capability || '*'}</Text>
            <Text size="xsm" color="secondary" maxLines={1}>
              → {row.principal || '*'}{scope === '—' ? '' : ` · ${scope}`}
            </Text>
          </VStack>
        );
      },
    },
    {
      key: 'risk_max',
      header: t('approvals.col.risk_max'),
      width: pixel(110),
      renderCell: (row) => <RiskBadge risk={row.risk_max || 'high'} />,
    },
    {
      key: 'expires',
      header: t('approvals.col.expires_at'),
      width: pixel(150),
      renderCell: (row) => (
        <Text size="sm" color="secondary">
          {row.expires_at ? fmtDateTime(row.expires_at) : t('approvals.rule.forever')}
        </Text>
      ),
    },
    {
      key: 'uses',
      header: t('approvals.col.uses_left'),
      width: pixel(100),
      renderCell: (row) => (
        <Text size="sm" color="secondary" hasTabularNumbers>
          {row.uses_left === null || row.uses_left === undefined
            ? t('approvals.rule.unlimited')
            : String(row.uses_left)}
        </Text>
      ),
    },
    {
      key: 'note',
      header: t('approvals.col.note'),
      width: proportional(2),
      renderCell: (row) => <Text size="sm" color="secondary" maxLines={2}>{row.note || '—'}</Text>,
    },
  ];

  if (canWrite) {
    ruleColumns.push({
      key: 'actions',
      header: t('approvals.col.actions'),
      align: 'end',
      width: pixel(100),
      renderCell: (row) => (
        <IconButton
          label={t('approvals.rule.delete')}
          icon={<TrashIcon />}
          variant="ghost"
          size="sm"
          tooltip={t('approvals.rule.delete')}
          onClick={() => setDeleteTarget(row)}
        />
      ),
    });
  }

  const decidedColumns: TableColumn<DecidedRow>[] = [
    {
      key: 'status',
      header: t('approvals.col.status'),
      width: pixel(100),
      renderCell: (row) => (
        <Badge label={statusLabel(row.status)} variant={badgeVariant(statusTone(row.status))} />
      ),
    },
    {
      key: 'capability',
      header: t('approvals.col.capability'),
      width: pixel(160),
      renderCell: (row) => <Text type="code" size="sm" maxLines={1}>{row.capability || '—'}</Text>,
    },
    {
      key: 'principal',
      header: t('approvals.col.principal'),
      width: pixel(150),
      renderCell: (row) => <Text type="code" size="sm" maxLines={1}>{row.principal || '—'}</Text>,
    },
    {
      key: 'mode',
      header: t('approvals.col.mode'),
      width: pixel(100),
      renderCell: (row) => <Text size="sm" color="secondary">{modeLabel(row.mode)}</Text>,
    },
    {
      key: 'decided_by',
      header: t('approvals.col.decided_by'),
      width: pixel(150),
      renderCell: (row) => <Text type="code" size="sm" maxLines={1}>{row.decided_by || '—'}</Text>,
    },
    {
      key: 'decided_at',
      header: t('approvals.col.decided_at'),
      width: pixel(160),
      renderCell: (row) => (
        <Text size="sm" color="secondary">{row.decided_at ? fmtDateTime(row.decided_at) : '—'}</Text>
      ),
    },
    {
      key: 'note',
      header: t('approvals.col.note'),
      width: proportional(1),
      renderCell: (row) => <Text size="sm" color="secondary" maxLines={2}>{row.note || '—'}</Text>,
    },
  ];

  return (
    <Page
      title={t('approvals.title')}
      description={pending.length
        ? t('approvals.sub.pending', { n: pending.length })
        : t('approvals.sub.none')}
      actions={(
        <Button
          label={t('approvals.refresh')}
          variant="ghost"
          size="sm"
          icon={<ArrowPathIcon />}
          isLoading={refreshing}
          onClick={() => { void refreshAll(); }}
        />
      )}
    >
      <VStack gap={4}>
        {!canWrite ? (
          <Banner
            status="warning"
            title={t('shell.viewer_hint')}
            description={t('approvals.viewer_hint')}
            container="section"
          />
        ) : null}

        <Section variant="transparent" padding={0}>
          <VStack gap={2}>
            <HStack gap={2} align="center" justify="between" wrap="wrap">
              <VStack gap={0.5}>
                <HStack gap={2} align="center">
                  <Heading level={3}>{t('approvals.queue.title')}</Heading>
                  {pending.length ? <Badge label={String(pending.length)} variant="warning" /> : null}
                </HStack>
                <Text size="sm" color="secondary">
                  {t('approvals.queue.hint', { n: pending.length })}
                </Text>
              </VStack>
            </HStack>
            <Table
              data={pending}
              columns={pendingColumns}
              idKey="id"
              density="compact"
              hasHover
              textOverflow="truncate"
              emptyState={(
                <EmptyState
                  title={t('approvals.queue.empty.title')}
                  description={t('approvals.queue.empty.text')}
                  icon={<ShieldCheckIcon />}
                  headingLevel={4}
                  isCompact
                />
              )}
            />
          </VStack>
        </Section>

        <Divider />

        <Section variant="transparent" padding={0}>
          <VStack gap={2}>
            <HStack gap={3} align="start" justify="between" wrap="wrap">
              <VStack gap={0.5}>
                <Heading level={3}>{t('approvals.rules.title')}</Heading>
                <Text size="sm" color="secondary">{t('approvals.rules.hint', { n: rules.length })}</Text>
              </VStack>
              {canWrite ? (
                <Button
                  label={t('approvals.rules.add')}
                  variant="secondary"
                  size="sm"
                  icon={<PlusIcon />}
                  onClick={() => setRuleDialogOpen(true)}
                />
              ) : null}
            </HStack>
            {rulesError ? (
              <Banner status="error" title={t('approvals.rules.error')} description={rulesError} container="section" />
            ) : null}
            {rulesLoading && rules.length === 0 ? (
              <HStack padding={4} align="center" justify="center"><Spinner size="sm" /></HStack>
            ) : (
              <Table
                data={rules}
                columns={ruleColumns}
                idKey="id"
                density="compact"
                hasHover
                textOverflow="truncate"
                emptyState={(
                  <EmptyState
                    title={t('approvals.rules.empty.title')}
                    description={t('approvals.rules.empty.text')}
                    headingLevel={4}
                    isCompact
                  />
                )}
              />
            )}
          </VStack>
        </Section>

        <Divider />

        <Section variant="transparent" padding={0}>
          <VStack gap={2}>
            <HStack gap={3} align="start" justify="between" wrap="wrap">
              <VStack gap={0.5}>
                <Heading level={3}>{t('approvals.decisions.title')}</Heading>
                <Text size="sm" color="secondary">
                  {t('approvals.decisions.hint', { n: decided.length, total: decidedTotal })}
                </Text>
              </VStack>
              <SegmentedControl
                label={t('approvals.decisions.filter')}
                size="sm"
                value={filter}
                onChange={(value) => setFilter(value as DecidedFilter)}
              >
                {DECIDED_FILTERS.map((key) => (
                  <SegmentedControlItem
                    key={key}
                    value={key}
                    label={t(`approvals.decisions.filter.${key}`)}
                  />
                ))}
              </SegmentedControl>
            </HStack>
            {decidedError ? (
              <Banner status="error" title={t('approvals.decisions.error')} description={decidedError} container="section" />
            ) : null}
            {decidedLoading && decided.length === 0 ? (
              <HStack padding={4} align="center" justify="center"><Spinner size="sm" /></HStack>
            ) : (
              <Table
                data={decided}
                columns={decidedColumns}
                idKey="id"
                density="compact"
                hasHover
                textOverflow="truncate"
                emptyState={(
                  <EmptyState
                    title={t('approvals.decisions.empty')}
                    headingLevel={4}
                    isCompact
                  />
                )}
              />
            )}
          </VStack>
        </Section>
      </VStack>

      <ApprovalDetailDialog
        approval={selected}
        projects={projects}
        canWrite={canWrite}
        onClose={() => setSelected(null)}
        onDecided={afterDecision}
      />

      <RuleDialog
        isOpen={ruleDialogOpen}
        onClose={() => setRuleDialogOpen(false)}
        onCreated={() => {
          void loadRules();
          void loadDecided();
          void loadState({ quiet: true }).catch(() => undefined);
        }}
      />

      <AlertDialog
        isOpen={Boolean(deleteTarget)}
        onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
        title={t('approvals.rule.delete_confirm.title')}
        description={t('approvals.rule.delete_confirm.text', {
          capability: deleteTarget?.capability || '*',
          effect: deleteTarget?.effect || 'allow',
        })}
        actionLabel={t('approvals.rule.delete_confirm.action')}
        cancelLabel={t('shell.cancel')}
        isActionLoading={deleting}
        onAction={() => { void removeRule(); }}
      />
    </Page>
  );
}
