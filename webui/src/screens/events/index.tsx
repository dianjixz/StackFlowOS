/**
 * Events screen (`#/events`) — live system console / audit stream.
 *
 * Source of truth is the store: the `/api/state` snapshot seeded `events` and
 * the SSE reducer keeps appending to it (`lastSeq` tracks the cursor).  This
 * screen only filters that live list — no polling — so every control here is
 * local: type, project, search, row limit, pause and clear-view.
 *
 * Clearing is deliberately a view operation: it hides everything up to the
 * current cursor locally and never calls the backend.
 */

import { useMemo, useState, type ReactNode } from 'react';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Icon } from '@astryxdesign/core/Icon';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Selector } from '@astryxdesign/core/Selector';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import {
  Table,
  pixel,
  proportional,
  useTableRowExpansion,
  type TableColumn,
} from '@astryxdesign/core/Table';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Timestamp } from '@astryxdesign/core/Timestamp';
import { BoltIcon, PauseIcon, PlayIcon, TrashIcon } from '@heroicons/react/24/outline';
import { t } from '../../i18n';
import { useStore } from '../../state/store';
import type { Project, SfoEvent, SfoState } from '../../state/store';
import { eventSummary, fmtDateTime, toneForType } from '../../ui/format';
import { toast } from '../../ui/toast';
import { Page } from '../../ui/Page';
import type { ScreenProps } from '../types';
import {
  EVENT_TYPES,
  LIMITS,
  dotVariant,
  matchesSearch,
  matchesType,
  projectLabel,
  toEventRow,
  type EventRow,
  type EventTypeFilter,
} from './model';

/* stable selectors: never recreate them per render */
const selEvents = (state: SfoState): SfoEvent[] => state.events;
const selLastSeq = (state: SfoState): number => state.lastSeq;
const selProjects = (state: SfoState): Project[] => state.projects;
const selSse = (state: SfoState): SfoState['sse'] => state.sse;

const SSE_VARIANTS = {
  live: 'success',
  connecting: 'warning',
  reconnecting: 'warning',
  offline: 'neutral',
} as const;

function ExpandedEvent({ row, projects }: { row: EventRow; projects: Project[] }): ReactNode {
  const json = JSON.stringify(row.data || {}, null, 2);
  return (
    <VStack gap={3} padding={3}>
      <MetadataList orientation="horizontal" label={{ position: 'top' }}>
        <MetadataListItem label={t('events.detail.id')}>
          <Text type="code" size="xsm">{row.id || '—'}</Text>
        </MetadataListItem>
        <MetadataListItem label={t('events.detail.seq')}>
          <Text size="xsm" hasTabularNumbers>#{row.seq}</Text>
        </MetadataListItem>
        <MetadataListItem label={t('events.detail.source')}>
          <Text size="xsm">{row.source}</Text>
        </MetadataListItem>
        <MetadataListItem label={t('events.detail.project')}>
          <Text size="xsm">{projectLabel(projects, row.project_id)}</Text>
        </MetadataListItem>
        <MetadataListItem label={t('events.detail.ts')}>
          <Text size="xsm">{fmtDateTime(row.ts)}</Text>
        </MetadataListItem>
      </MetadataList>
      <CodeBlock
        code={json}
        language="json"
        title={t('events.detail.data')}
        size="sm"
        width="100%"
        maxHeight={300}
      />
    </VStack>
  );
}

export default function EventsScreen(_props: ScreenProps): ReactNode {
  const events = useStore(selEvents);
  const lastSeq = useStore(selLastSeq);
  const projects = useStore(selProjects);
  const sse = useStore(selSse);

  const [type, setType] = useState<EventTypeFilter>('all');
  const [project, setProject] = useState('');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState('200');
  const [paused, setPaused] = useState(false);
  const [frozenSeq, setFrozenSeq] = useState(0);
  const [clearedSeq, setClearedSeq] = useState(0);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());

  const rows = useMemo(() => events.map(toEventRow), [events]);
  const limitNum = Number(limit) || 200;

  const expansion = useTableRowExpansion<EventRow>({
    expandedKeys,
    onToggle: (key) => {
      setExpandedKeys((previous) => {
        const next = new Set(previous);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    },
    getRowKey: (row) => row.key,
    getRowLabel: (row) => row.type,
    hasRowClickExpansion: true,
    renderExpanded: (row) => <ExpandedEvent row={row} projects={projects} />,
  });

  const projectOptions = useMemo(() => {
    const ids = new Set<string>(projects.map((item) => item.id));
    for (const row of rows) if (row.project_id) ids.add(row.project_id);
    return [
      { value: '', label: t('events.filter.project.all') },
      ...[...ids].map((id) => ({ value: id, label: projectLabel(projects, id) })),
    ];
  }, [projects, rows]);

  const limitOptions = useMemo(
    () => LIMITS.map((value) => ({ value: String(value), label: t('events.limit.option', { n: value }) })),
    [],
  );

  const needle = query.trim().toLowerCase();

  const eligible = useMemo(() => rows.filter((row) => {
    if (clearedSeq && row.seq <= clearedSeq) return false;
    if (!matchesType(row, type)) return false;
    if (project && row.project_id !== project) return false;
    return matchesSearch(row, needle);
  }), [rows, clearedSeq, type, project, needle]);

  const filtered = useMemo(() => {
    const newestFirst = [...eligible].sort((left, right) => right.seq - left.seq);
    const frozen = paused ? newestFirst.filter((row) => row.seq <= frozenSeq) : newestFirst;
    return frozen.slice(0, limitNum);
  }, [eligible, paused, frozenSeq, limitNum]);

  const maxSeq = useMemo(() => rows.reduce((max, row) => Math.max(max, row.seq), 0), [rows]);
  const newSincePause = paused ? eligible.filter((row) => row.seq > frozenSeq).length : 0;
  const hasFilters = type !== 'all' || Boolean(project) || Boolean(query);

  const togglePause = (): void => {
    if (paused) {
      setPaused(false);
      return;
    }
    setFrozenSeq(Math.max(lastSeq, maxSeq));
    setPaused(true);
  };

  const clearView = (): void => {
    const cursor = Math.max(lastSeq, maxSeq);
    setClearedSeq(cursor);
    setExpandedKeys(new Set());
    if (paused) setFrozenSeq(cursor);
    toast(t('events.cleared'));
  };

  const clearFilters = (): void => {
    setType('all');
    setProject('');
    setQuery('');
  };

  const columns = useMemo<TableColumn<EventRow>[]>(() => [
    {
      key: 'seq',
      header: t('events.table.seq'),
      width: pixel(84),
      align: 'end',
      renderCell: (row) => (
        <Text type="code" size="xsm" hasTabularNumbers>{String(row.seq)}</Text>
      ),
    },
    {
      key: 'type',
      header: t('events.table.type'),
      width: proportional(1.6, { minWidth: 150 }),
      renderCell: (row) => (
        <HStack gap={1.5} align="center">
          <StatusDot variant={dotVariant(toneForType(row.type))} label={row.type} />
          <Text type="code" size="xsm" maxLines={1}>{row.type}</Text>
        </HStack>
      ),
    },
    {
      key: 'source',
      header: t('events.table.source'),
      width: proportional(1.2, { minWidth: 100 }),
      renderCell: (row) => <Text size="xsm" maxLines={1}>{row.source}</Text>,
    },
    {
      key: 'project',
      header: t('events.table.project'),
      width: proportional(1, { minWidth: 90 }),
      renderCell: (row) => (
        <Text size="xsm" color="secondary" maxLines={1}>{projectLabel(projects, row.project_id)}</Text>
      ),
    },
    {
      key: 'time',
      header: t('events.table.time'),
      width: pixel(108),
      renderCell: (row) => (
        <Timestamp value={Math.floor(row.ts / 1000)} format="system_time" color="secondary" />
      ),
    },
    {
      key: 'summary',
      header: t('events.table.summary'),
      width: proportional(3, { minWidth: 220 }),
      renderCell: (row) => <Text size="sm" maxLines={2}>{eventSummary(row)}</Text>,
    },
  ], [projects]);

  const emptyState = events.length === 0 ? (
    <EmptyState
      title={t('events.empty.none.title')}
      description={t('events.empty.none.text')}
      icon={<Icon icon={BoltIcon} size="lg" />}
      isCompact
    />
  ) : clearedSeq > 0 && eligible.length === 0 && !hasFilters ? (
    <EmptyState
      title={t('events.empty.cleared.title')}
      description={t('events.empty.cleared.text')}
      icon={<Icon icon={BoltIcon} size="lg" />}
      isCompact
    />
  ) : (
    <EmptyState
      title={t('events.empty.filtered.title')}
      description={t('events.empty.filtered.text')}
      icon={<Icon icon={BoltIcon} size="lg" />}
      isCompact
      actions={hasFilters ? (
        <Button label={t('events.filter.clear')} variant="secondary" size="sm" onClick={clearFilters} />
      ) : undefined}
    />
  );

  return (
    <Page
      title={t('app.events')}
      description={t('app.sub.events')}
      actions={(
        <HStack gap={2} align="center" wrap="wrap">
          <Button
            label={paused ? t('events.resume') : t('events.pause')}
            icon={<Icon icon={paused ? PlayIcon : PauseIcon} size="sm" />}
            variant={paused ? 'primary' : 'secondary'}
            size="sm"
            onClick={togglePause}
          />
          <Button
            label={t('events.clear')}
            icon={<Icon icon={TrashIcon} size="sm" />}
            variant="ghost"
            size="sm"
            isDisabled={events.length === 0}
            onClick={clearView}
          />
        </HStack>
      )}
    >
      <VStack gap={3}>
        <SegmentedControl
          label={t('events.filter.type')}
          value={type}
          onChange={(value) => setType(value as EventTypeFilter)}
          size="sm"
          layout="hug"
        >
          {EVENT_TYPES.map((key) => (
            <SegmentedControlItem key={key} value={key} label={t(`events.type.${key}`)} />
          ))}
        </SegmentedControl>

        <HStack gap={3} align="end" wrap="wrap">
          <Selector
            label={t('events.filter.project')}
            options={projectOptions}
            value={project}
            onChange={(value) => setProject(value || '')}
            size="sm"
            width={200}
          />
          <TextInput
            label={t('events.filter.search')}
            isLabelHidden
            type="search"
            value={query}
            onChange={setQuery}
            placeholder={t('events.search.placeholder')}
            startIcon="search"
            size="sm"
            hasClear
            width={320}
          />
          <Selector
            label={t('events.filter.limit')}
            options={limitOptions}
            value={limit}
            onChange={(value) => setLimit(value || '200')}
            size="sm"
            width={130}
          />
          {hasFilters ? (
            <Button label={t('events.filter.clear')} variant="ghost" size="sm" onClick={clearFilters} />
          ) : null}
        </HStack>

        <HStack gap={3} align="center" wrap="wrap">
          <HStack gap={1.5} align="center">
            <StatusDot
              variant={SSE_VARIANTS[sse] || 'neutral'}
              label={t(`events.connection.${sse}`)}
              isPulsing={sse === 'live' && !paused}
            />
            <Text size="xsm" color="secondary">{t(`events.connection.${sse}`)}</Text>
          </HStack>
          <Text size="xsm" color="secondary">
            {t('events.count', { shown: filtered.length, total: eligible.length })}
          </Text>
          <Text size="xsm" color="secondary" hasTabularNumbers>
            {t('events.last_seq', { seq: lastSeq })}
          </Text>
          {paused ? <Badge label={t('events.paused')} variant="warning" /> : null}
          {paused && newSincePause > 0 ? (
            <Badge label={t('events.new_since_pause', { n: newSincePause })} variant="info" />
          ) : null}
          {paused ? <Text size="xsm" color="secondary">{t('events.paused_hint')}</Text> : null}
        </HStack>

        <Table
          data={filtered}
          columns={columns}
          idKey={(row) => row.key}
          density="compact"
          dividers="rows"
          hasHover
          textOverflow="truncate"
          verticalAlign="middle"
          plugins={{ expansion }}
          emptyState={emptyState}
        />
      </VStack>
    </Page>
  );
}
