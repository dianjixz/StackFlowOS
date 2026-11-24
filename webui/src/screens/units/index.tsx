/**
 * Units screen (`#/units`) — the three-tier unit explorer.
 *
 * Layout: health overview → filters → grouped tables (system / service /
 * worker) → a drawer with the descriptor, dependency graph, recent events and
 * the write actions the descriptor declares.
 *
 * The list itself comes from the live store snapshot (`/api/state` folded by
 * SSE); `/api/units/summary` only adds the authoritative totals, and the
 * detail drawer reads `/api/units/{id}` when the backend offers it.  A missing
 * detail endpoint degrades to snapshot data with a calm note instead of an
 * error loop.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Icon } from '@astryxdesign/core/Icon';
import { Section } from '@astryxdesign/core/Section';
import { Selector } from '@astryxdesign/core/Selector';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Table, pixel, proportional, type TableColumn } from '@astryxdesign/core/Table';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Token } from '@astryxdesign/core/Token';
import { ArrowPathIcon, CpuChipIcon } from '@heroicons/react/24/outline';
import { api, isForbidden, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { getState, loadState, useStore } from '../../state/store';
import type { Project, SfoEvent, SfoState, Unit } from '../../state/store';
import { ago, statusLabel, statusTone } from '../../ui/format';
import { Page } from '../../ui/Page';
import type { ScreenProps } from '../types';
import { UnitDetailDrawer } from './UnitDetailDrawer';
import {
  TIERS,
  abilityNames,
  asUnitView,
  dotVariant,
  findUnit,
  isOnline,
  isProblem,
  matchesQuery,
  normalizeSummary,
  projectName,
  runtimeText,
  tierOf,
  tokenColor,
  unitId,
  unitState,
  unitStatusLabel,
  unitTone,
  type DetailState,
  type UnitSummary,
  type UnitView,
} from './model';

/* stable selectors: never recreate them per render */
const selUnits = (state: SfoState): Unit[] => state.units;
const selProjects = (state: SfoState): Project[] => state.projects;
const selEvents = (state: SfoState): SfoEvent[] => state.events;
const selError = (state: SfoState): unknown => state.error;

interface SelectOption {
  value: string;
  label: string;
}

function AbilityTokens({ unit }: { unit: Unit }): ReactNode {
  const names = abilityNames(unit);
  if (!names.length) return <Text size="xsm" color="secondary">—</Text>;
  const shown = names.slice(0, 3);
  return (
    <HStack gap={1} wrap="wrap">
      {shown.map((name) => (
        <Token key={name} size="sm" label={name} color="blue" />
      ))}
      {names.length > shown.length ? (
        <Token
          size="sm"
          color="gray"
          label={t('units.table.more_abilities', { n: names.length - shown.length })}
        />
      ) : null}
    </HStack>
  );
}

function Stat({ label, value }: { label: string; value: string | number }): ReactNode {
  return (
    <VStack gap={0.5}>
      <Text size="xsm" color="secondary">{label}</Text>
      <Text size="2xl" weight="semibold" hasTabularNumbers>{String(value)}</Text>
    </VStack>
  );
}

export default function UnitsScreen({ id }: ScreenProps): ReactNode {
  const units = useStore(selUnits);
  const projects = useStore(selProjects);
  const events = useStore(selEvents);
  const error = useStore(selError);

  const [summary, setSummary] = useState<UnitSummary | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [status, setStatus] = useState('');
  const [kind, setKind] = useState('');
  const [tier, setTier] = useState('');
  const [project, setProject] = useState('');
  const [query, setQuery] = useState('');

  const [selectedId, setSelectedId] = useState<string | null>(id || null);
  const [detail, setDetail] = useState<UnitView | null>(null);
  const [detailState, setDetailState] = useState<DetailState>('loading');
  const [note, setNote] = useState('');
  const [detailEvents, setDetailEvents] = useState<SfoEvent[]>([]);

  const loadSummary = useCallback(async (): Promise<void> => {
    try {
      const data = await api.getUnitsSummary();
      setSummary(normalizeSummary(data));
    } catch {
      // Older backends have no summary endpoint; counts fall back to the list.
    }
  }, []);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const loadDetail = useCallback(async (unitId: string): Promise<void> => {
    setDetail(findUnit(getState().units, unitId));
    setDetailState('loading');
    setNote('');
    setDetailEvents([]);
    try {
      const data = await api.getUnit(unitId);
      const payload = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>;
      const rawUnit = payload.unit ?? payload.descriptor
        ?? ((payload.work_id || payload.unit_id) ? payload : null);
      const nextUnit = asUnitView(rawUnit as Unit | null);
      if (nextUnit) setDetail(nextUnit);
      const rawEvents = [payload.events, payload.recent_events, payload.state_events]
        .find((candidate) => Array.isArray(candidate));
      setDetailEvents(Array.isArray(rawEvents) ? (rawEvents as SfoEvent[]) : []);
      setDetailState('ready');
    } catch (detailError) {
      const seed = findUnit(getState().units, unitId);
      if (isUnsupported(detailError)) {
        setDetailState('unavailable');
        setNote(t('units.detail.unavailable'));
      } else if (isForbidden(detailError)) {
        setDetailState('forbidden');
        setNote(t('units.detail.forbidden'));
      } else {
        setDetailState('error');
        setNote(t('units.detail.error', {
          message: detailError instanceof Error ? detailError.message : String(detailError),
        }));
      }
      setDetail(seed);
    }
  }, []);

  useEffect(() => {
    setSelectedId(id || null);
  }, [id]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const refresh = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      await loadState().catch(() => null);
    } finally {
      setRefreshing(false);
    }
    void loadSummary();
  }, [loadSummary]);

  const statusOptions = useMemo<SelectOption[]>(() => {
    const values = [...new Set(units.map((unit) => unitState(unit)).filter(Boolean))].sort();
    return [
      { value: '', label: t('units.filter.status.all') },
      ...values.map((value) => ({ value, label: statusLabel(value) })),
    ];
  }, [units]);

  const kindOptions = useMemo<SelectOption[]>(() => {
    const values = [...new Set(units.map((unit) => String(unit.kind || '')).filter(Boolean))].sort();
    return [
      { value: '', label: t('units.filter.kind.all') },
      ...values.map((value) => ({ value, label: value })),
    ];
  }, [units]);

  const tierOptions = useMemo<SelectOption[]>(() => [
    { value: '', label: t('units.filter.tier.all') },
    ...TIERS.map((value) => ({ value, label: t(`units.tier.${value}`) })),
  ], []);

  const projectOptions = useMemo<SelectOption[]>(() => [
    { value: '', label: t('units.filter.project.all') },
    ...[...projects]
      .sort((left, right) => left.name.localeCompare(right.name))
      .map((item) => ({ value: item.id, label: item.name })),
  ], [projects]);

  const filtered = useMemo(() => units.filter((unit) => {
    if (status && unitState(unit) !== status) return false;
    if (kind && String(unit.kind || '') !== kind) return false;
    if (tier && tierOf(unit) !== tier) return false;
    if (project && String(unit.project_id || '') !== project) return false;
    return matchesQuery(unit, query);
  }), [units, status, kind, tier, project, query]);

  const groups = useMemo(() => TIERS
    .map((tierKey) => ({
      tier: tierKey,
      units: filtered.filter((unit) => tierOf(unit) === tierKey),
    }))
    .filter((group) => group.units.length > 0), [filtered]);

  const liveUnit = useMemo(
    () => (selectedId ? findUnit(units, selectedId) : null),
    [units, selectedId],
  );

  const drawerEvents = useMemo(() => {
    if (!selectedId) return [];
    const local = events.filter((event) => {
      if (!String(event.type || '').startsWith('unit')) return false;
      const data = (event.data || {}) as Record<string, unknown>;
      return String(data.unit_id || data.work_id || data.id || '') === selectedId;
    });
    const byKey = new Map<string, SfoEvent>();
    for (const event of [...detailEvents, ...local]) {
      byKey.set(event.id || `seq-${event.seq}`, event);
    }
    return [...byKey.values()]
      .sort((left, right) => (Number(left.seq) || 0) - (Number(right.seq) || 0))
      .slice(-12);
  }, [detailEvents, events, selectedId]);

  const columns = useMemo<TableColumn<Unit>[]>(() => [
    {
      key: 'state',
      header: t('units.table.status'),
      width: pixel(112),
      renderCell: (unit) => (
        <HStack gap={1.5} align="center">
          <StatusDot
            variant={dotVariant(unitTone(unit))}
            label={unitStatusLabel(unit)}
            isPulsing={unitState(unit) === 'online'}
          />
          <Text size="xsm">{unitStatusLabel(unit)}</Text>
        </HStack>
      ),
    },
    {
      key: 'unit',
      header: t('units.table.unit'),
      width: proportional(2, { minWidth: 200 }),
      renderCell: (unit) => {
        const unitName = unitId(unit);
        return (
          <VStack gap={0.5} hAlign="start">
            <Button
              label={unitName}
              variant="ghost"
              size="sm"
              width="fit-content"
              onClick={() => setSelectedId(unitName)}
            >
              <Text type="code" size="sm">{unitName}</Text>
            </Button>
            <Text size="xsm" color="secondary" maxLines={1}>
              {[unit.label, unit.kind].filter(Boolean).join(' · ') || '—'}
            </Text>
          </VStack>
        );
      },
    },
    {
      key: 'runtime',
      header: t('units.table.runtime'),
      width: proportional(1.4, { minWidth: 130 }),
      renderCell: (unit) => <Text size="xsm" color="secondary">{runtimeText(unit)}</Text>,
    },
    {
      key: 'abilities',
      header: t('units.table.abilities'),
      width: proportional(2, { minWidth: 220 }),
      renderCell: (unit) => <AbilityTokens unit={unit} />,
    },
    {
      key: 'project',
      header: t('units.table.project'),
      width: proportional(1, { minWidth: 100 }),
      renderCell: (unit) => <Text size="sm">{projectName(projects, unit.project_id)}</Text>,
    },
    {
      key: 'last_seen',
      header: t('units.table.last_seen'),
      width: pixel(116),
      renderCell: (unit) => (
        <Text size="xsm" color="secondary" hasTabularNumbers>{ago(unit.last_seen)}</Text>
      ),
    },
  ], [projects]);

  const total = Number(summary?.total ?? units.length);
  const online = Number(summary?.online ?? units.filter(isOnline).length);
  const problems = units.filter(isProblem).length;
  const kinds = new Set(units.map((unit) => String(unit.kind || '—'))).size;
  const healthKey = summary?.critical_bad ? 'down' : problems ? 'degraded' : units.length ? 'ok' : 'unknown';
  const healthTone = healthKey === 'ok' ? 'success' : healthKey === 'down' ? 'error' : healthKey === 'degraded' ? 'warning' : 'neutral';

  const statusCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const unit of units) {
      const state = unitState(unit);
      counts.set(state, (counts.get(state) || 0) + 1);
    }
    return [...counts.entries()].sort((left, right) => right[1] - left[1]);
  }, [units]);

  const hasFilters = Boolean(status || kind || tier || project || query);
  const clearFilters = (): void => {
    setStatus('');
    setKind('');
    setTier('');
    setProject('');
    setQuery('');
  };

  const errorMessage = error instanceof Error ? error.message : String(error || '');

  let body: ReactNode;
  if (!units.length) {
    body = error ? (
      <EmptyState
        title={t('units.empty.broken.title')}
        description={errorMessage ? `${t('units.empty.broken.text')}\n${errorMessage}` : t('units.empty.broken.text')}
        icon={<Icon icon={CpuChipIcon} size="lg" />}
        actions={<Button label={t('units.empty.retry')} variant="primary" isLoading={refreshing} onClick={() => void refresh()} />}
      />
    ) : (
      <EmptyState
        title={t('units.empty.none.title')}
        description={t('units.empty.none.text')}
        icon={<Icon icon={CpuChipIcon} size="lg" />}
        actions={<Button label={t('units.empty.retry')} variant="secondary" isLoading={refreshing} onClick={() => void refresh()} />}
      />
    );
  } else if (!filtered.length) {
    body = (
      <EmptyState
        title={t('units.empty.filtered.title')}
        description={t('units.empty.filtered.text')}
        icon={<Icon icon={CpuChipIcon} size="lg" />}
        actions={<Button label={t('units.filter.clear')} variant="secondary" onClick={clearFilters} />}
      />
    );
  } else {
    body = (
      <VStack gap={4}>
        {groups.map((group) => {
          const onlineInTier = group.units.filter(isOnline).length;
          return (
            <Section key={group.tier} padding={0} dividers={['bottom']}>
              <HStack justify="between" align="center" gap={3} wrap="wrap" padding={4}>
                <VStack gap={0.5}>
                  <HStack gap={2} align="center">
                    <Heading level={3}>{t(`units.tier.${group.tier}`)}</Heading>
                    <Badge label={String(group.units.length)} variant="neutral" />
                  </HStack>
                  <Text size="xsm" color="secondary">{t(`units.tier.${group.tier}.hint`)}</Text>
                </VStack>
                <Text size="sm" color="secondary">
                  {t('units.tier.count', { online: onlineInTier, total: group.units.length })}
                </Text>
              </HStack>
              <Table
                data={group.units}
                columns={columns}
                idKey={(unit) => unitId(unit)}
                density="compact"
                dividers="rows"
                hasHover
                textOverflow="truncate"
                verticalAlign="middle"
              />
            </Section>
          );
        })}
      </VStack>
    );
  }

  return (
    <>
      <Page
        title={t('app.units')}
        description={t('app.sub.units')}
        actions={(
          <Button
            label={t('units.refresh')}
            icon={<Icon icon={ArrowPathIcon} size="sm" />}
            variant="secondary"
            size="sm"
            isLoading={refreshing}
            onClick={() => void refresh()}
          />
        )}
      >
        <VStack gap={4}>
          <Card padding={4}>
            <HStack justify="between" align="center" gap={4} wrap="wrap">
              <HStack gap={6} align="center" wrap="wrap">
                <Stat label={t('units.overview.total')} value={total} />
                <Stat label={t('units.overview.online')} value={online} />
                <Stat label={t('units.overview.problems')} value={problems} />
                <Stat label={t('units.overview.kinds')} value={kinds} />
                <VStack gap={0.5}>
                  <Text size="xsm" color="secondary">{t('units.overview.health')}</Text>
                  <HStack gap={1.5} align="center">
                    <StatusDot variant={healthTone} label={t(`units.health.${healthKey}`)} />
                    <Text size="sm">{t(`units.health.${healthKey}`)}</Text>
                  </HStack>
                </VStack>
              </HStack>
              <VStack gap={1.5}>
                <Text size="xsm" color="secondary">{t('units.overview.status')}</Text>
                {statusCounts.length ? (
                  <HStack gap={1.5} align="center" wrap="wrap">
                    {statusCounts.map(([state, count]) => (
                      <Token
                        key={state}
                        size="sm"
                        color={tokenColor(statusTone(state))}
                        label={`${statusLabel(state)} ${count}`}
                      />
                    ))}
                  </HStack>
                ) : (
                  <Text size="sm" color="secondary">{t('units.overview.empty_status')}</Text>
                )}
              </VStack>
            </HStack>
          </Card>

          <HStack gap={3} align="end" wrap="wrap">
            <Selector
              label={t('units.filter.status')}
              options={statusOptions}
              value={status}
              onChange={(value) => setStatus(value || '')}
              size="sm"
              width={160}
            />
            <Selector
              label={t('units.filter.kind')}
              options={kindOptions}
              value={kind}
              onChange={(value) => setKind(value || '')}
              size="sm"
              width={150}
            />
            <Selector
              label={t('units.filter.tier')}
              options={tierOptions}
              value={tier}
              onChange={(value) => setTier(value || '')}
              size="sm"
              width={150}
            />
            <Selector
              label={t('units.filter.project')}
              options={projectOptions}
              value={project}
              onChange={(value) => setProject(value || '')}
              size="sm"
              width={190}
            />
            <TextInput
              label={t('units.filter.search')}
              isLabelHidden
              type="search"
              value={query}
              onChange={setQuery}
              placeholder={t('units.search.placeholder')}
              startIcon="search"
              size="sm"
              hasClear
              width={320}
            />
            {hasFilters ? (
              <Button label={t('units.filter.clear')} variant="ghost" size="sm" onClick={clearFilters} />
            ) : null}
          </HStack>

          {error && units.length ? (
            <Banner
              status="warning"
              title={t('units.stale')}
              description={errorMessage || undefined}
              collapsible={false}
              container="section"
            />
          ) : null}

          {body}
        </VStack>
      </Page>

      <UnitDetailDrawer
        isOpen={selectedId !== null}
        unitId={selectedId}
        unit={detail}
        liveUnit={liveUnit}
        detailState={detailState}
        note={note}
        events={drawerEvents}
        units={units}
        projects={projects}
        onOpenChange={(open) => { if (!open) setSelectedId(null); }}
        onReload={() => {
          if (selectedId) void loadDetail(selectedId);
          void loadSummary();
        }}
      />
    </>
  );
}
