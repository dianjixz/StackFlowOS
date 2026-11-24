/**
 * UnitDetailDrawer.tsx — the unit inspector.
 *
 * Shows the descriptor, runtime placement, dependencies (requires/wants
 * graph), declared abilities, endpoints, recent events and the write actions
 * the descriptor actually declares.  Actions are hidden for viewers and every
 * failure is reported through `toastError` (no silent failures, no retry
 * loops).
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { Divider } from '@astryxdesign/core/Divider';
import { Drawer, DrawerHeader } from '@astryxdesign/core/Drawer';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { List, ListItem } from '@astryxdesign/core/List';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Spinner } from '@astryxdesign/core/Spinner';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Timestamp } from '@astryxdesign/core/Timestamp';
import { Token } from '@astryxdesign/core/Token';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { CpuChipIcon } from '@heroicons/react/24/outline';
import { Icon } from '@astryxdesign/core/Icon';
import { api, isConflict } from '../../api/client';
import { t } from '../../i18n';
import { useCanWrite } from '../../session';
import type { Project, SfoEvent, Unit } from '../../state/store';
import { ago, fmtDateTime, statusLabel, toneForType } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import {
  abilityNames,
  actionLabel,
  actionNeedsConfirmation,
  dotVariant,
  leaseText,
  projectName,
  runtimeText,
  tierOf,
  unitEventLine,
  unitTone,
  type DetailState,
  type UnitView,
} from './model';
import { DependencyGraph } from './DependencyGraph';

interface UnitDetailDrawerProps {
  isOpen: boolean;
  unitId: string | null;
  /** Descriptor from the detail endpoint, or the snapshot unit while loading. */
  unit: UnitView | null;
  /** Latest snapshot row, so status/health stay live while the drawer is open. */
  liveUnit: UnitView | null;
  detailState: DetailState;
  note: string;
  events: SfoEvent[];
  units: Unit[];
  projects: Project[];
  onOpenChange: (isOpen: boolean) => void;
  onReload: () => void;
}

function Block({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <VStack gap={2}>
      <Heading level={3}>{title}</Heading>
      {children}
    </VStack>
  );
}

export function UnitDetailDrawer({
  isOpen,
  unitId: selectedId,
  unit,
  liveUnit,
  detailState,
  note,
  events,
  units,
  projects,
  onOpenChange,
  onReload,
}: UnitDetailDrawerProps): ReactNode {
  const writable = useCanWrite();
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);

  useEffect(() => {
    setBusy(null);
    setConfirmAction(null);
  }, [selectedId]);

  // The descriptor carries actions/health/dependencies while the snapshot row
  // carries the freshest status; keep both without letting a stale descriptor
  // win over a live heartbeat.
  const current = unit
    ? {
        ...unit,
        ...(liveUnit
          ? { status: liveUnit.status, state: liveUnit.state, last_seen: liveUnit.last_seen }
          : {}),
      }
    : liveUnit;
  const status = unitTone(current);
  const stateLabel = statusLabel((current as UnitView | null)?.state || current?.status);
  const tier = tierOf(current);
  const project = projectName(projects, current?.project_id);

  const declaredActions = useMemo(() => {
    const actions = Array.isArray(current?.actions) ? current.actions.map(String).filter(Boolean) : [];
    return actions.length ? actions : ['probe'];
  }, [current]);

  const running = async (action: string): Promise<void> => {
    if (!selectedId || !writable) return;
    setBusy(action);
    try {
      if (action === 'probe') {
        await api.probeUnit(selectedId);
        toast(t('units.actions.probe_done'));
      } else if (action === 'kill') {
        await api.killUnit(selectedId);
        toast(t('units.actions.kill_done', { id: selectedId }));
        setConfirmAction(null);
        onOpenChange(false);
        return;
      } else {
        await api.unitAction(selectedId, action);
        toast(t('units.actions.done', { action: actionLabel(action) }));
      }
      onReload();
    } catch (error) {
      if (isConflict(error)) toast(t('units.actions.conflict'), 'error');
      else toastError(error);
      onReload();
    } finally {
      setBusy(null);
      setConfirmAction(null);
    }
  };

  const askOrRun = (action: string): void => {
    if (!writable) return;
    if (actionNeedsConfirmation(action)) {
      setConfirmAction(action);
      return;
    }
    void running(action);
  };

  const healthMode = planHealth(current);
  const health = healthMode === 'ok'
    ? <Badge label={t('units.detail.health_ok')} variant="success" />
    : healthMode === 'bad'
      ? <Badge label={t('units.detail.health_bad')} variant="error" />
      : null;

  const title = selectedId ? `${t('units.detail.title')} · ${selectedId}` : t('units.detail.title');
  const subtitle = current
    ? [current.label || current.kind || '—', `${t(`units.tier.${tier}`)} / ${current.kind || '—'}`, project]
      .filter(Boolean)
      .join(' · ')
    : t('units.detail.title');

  const showSpinner = detailState === 'loading' && !unit && !liveUnit;
  const showMissing = !current && !showSpinner;

  return (
    <>
      <Drawer
        isOpen={isOpen}
        onOpenChange={onOpenChange}
        label={title}
        width={560}
        padding={4}
      >
        <DrawerHeader
          title={selectedId || t('units.detail.title')}
          subtitle={subtitle}
          onOpenChange={onOpenChange}
          hasDivider
        />
        <VStack gap={4}>
          {note ? (
            <Banner
              status={detailState === 'forbidden' || detailState === 'error' ? 'warning' : 'info'}
              title={note}
              collapsible={false}
              container="section"
            />
          ) : null}

          {showSpinner ? (
            <HStack gap={2} align="center" padding={4} justify="center">
              <Spinner size="md" label={t('units.detail.loading')} />
            </HStack>
          ) : null}

          {showMissing ? (
            <EmptyState
              title={t('units.detail.not_found.title')}
              description={t('units.detail.not_found.text')}
              icon={<Icon icon={CpuChipIcon} size="lg" />}
              isCompact
              actions={<Button label={t('units.empty.retry')} variant="secondary" size="sm" onClick={onReload} />}
            />
          ) : null}

          {current ? (
            <>
              <HStack gap={2} align="center" wrap="wrap">
                <StatusDot
                  variant={dotVariant(status)}
                  label={stateLabel}
                  isPulsing={String((current as UnitView).state || current.status || '') === 'online'}
                />
                <Text size="sm">{stateLabel}</Text>
                <Token size="sm" label={t(`units.tier.${tier}`)} />
                <Token size="sm" label={String(current.kind || '—')} color="gray" />
                {health}
                {current.activity ? <Token size="sm" label={String(current.activity)} color="blue" /> : null}
              </HStack>

              {(current as UnitView).reason ? (
                <Text size="sm" color="secondary">
                  {t('units.detail.reason')}: {String((current as UnitView).reason)}
                </Text>
              ) : null}

              <Block title={t('units.detail.actions')}>
                {writable ? (
                  <HStack gap={2} wrap="wrap">
                    {declaredActions.map((action) => (
                      <Button
                        key={action}
                        label={actionLabel(action)}
                        variant="secondary"
                        size="sm"
                        isLoading={busy === action}
                        isDisabled={busy !== null && busy !== action}
                        onClick={() => askOrRun(action)}
                      />
                    ))}
                    <Button
                      label={t('units.actions.kill')}
                      variant="destructive"
                      size="sm"
                      isLoading={busy === 'kill'}
                      isDisabled={busy !== null && busy !== 'kill'}
                      onClick={() => askOrRun('kill')}
                    />
                  </HStack>
                ) : (
                  <Text size="sm" color="secondary">{t('units.actions.viewer')}</Text>
                )}
              </Block>

              <Divider />

              <Block title={t('units.detail.meta')}>
                <MetadataList columns={2} label={{ position: 'top' }}>
                  <MetadataListItem label={t('units.detail.runtime')}>
                    <Text size="sm">{runtimeText(current)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.transport')}>
                    <Text size="sm">{String(current.transport || '—')}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.host')}>
                    <Text size="sm">{String(current.host || '—')}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.pid')}>
                    <Text size="sm" hasTabularNumbers>{String(current.pid ?? '—')}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.project')}>
                    <Text size="sm">{project}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.started')}>
                    <Text size="sm">{fmtDateTime(current.started_at ?? (current as UnitView).since)}</Text>
                  </MetadataListItem>
                  <MetadataListItem label={t('units.detail.last_seen')}>
                    <Text size="sm">{ago(current.last_seen)}</Text>
                  </MetadataListItem>
                  {Number(current.restart_count) > 0 ? (
                    <MetadataListItem label={t('units.detail.restarts')}>
                      <Text size="sm" hasTabularNumbers>{String(current.restart_count)}</Text>
                    </MetadataListItem>
                  ) : null}
                  {(current as UnitView).supervisor ? (
                    <MetadataListItem label={t('units.detail.supervisor')}>
                      <Text size="sm">{String((current as UnitView).supervisor)}</Text>
                    </MetadataListItem>
                  ) : null}
                  <MetadataListItem label={t('units.detail.lease')}>
                    <Text size="sm">{leaseText(current)}</Text>
                  </MetadataListItem>
                  {(current as UnitView).health?.last_latency_ms !== undefined ? (
                    <MetadataListItem label={t('units.detail.latency_label')}>
                      <Text size="sm" hasTabularNumbers>
                        {t('units.detail.latency', { ms: String((current as UnitView).health?.last_latency_ms) })}
                      </Text>
                    </MetadataListItem>
                  ) : null}
                </MetadataList>
              </Block>

              <Divider />

              <Block title={t('units.detail.deps')}>
                <DependencyGraph unit={unit || liveUnit} units={units} />
              </Block>

              <Divider />

              <Block title={t('units.detail.abilities')}>
                {abilityNames(current).length ? (
                  <HStack gap={1} wrap="wrap">
                    {abilityNames(current).map((name) => (
                      <Token key={name} size="sm" label={name} color="blue" />
                    ))}
                  </HStack>
                ) : (
                  <Text size="sm" color="secondary">{t('units.detail.no_abilities')}</Text>
                )}
              </Block>

              {current.rpc_address || current.events_address ? (
                <>
                  <Divider />
                  <Block title={t('units.detail.endpoints')}>
                    <MetadataList columns={1} label={{ position: 'start', width: 110 }}>
                      {current.rpc_address ? (
                        <MetadataListItem label={t('units.detail.rpc')}>
                          <Text type="code" size="xsm" wordBreak="break-all">{String(current.rpc_address)}</Text>
                        </MetadataListItem>
                      ) : null}
                      {current.events_address ? (
                        <MetadataListItem label={t('units.detail.events')}>
                          <Text type="code" size="xsm" wordBreak="break-all">{String(current.events_address)}</Text>
                        </MetadataListItem>
                      ) : null}
                    </MetadataList>
                  </Block>
                </>
              ) : null}

              <Divider />

              <Block title={t('units.detail.recent_events')}>
                {events.length ? (
                  <List density="compact" hasDividers>
                    {[...events].reverse().map((event) => (
                      <ListItem
                        key={event.id || `seq-${event.seq}`}
                        label={String(event.type || 'event')}
                        description={unitEventLine(event)}
                        startContent={(
                          <StatusDot
                            variant={dotVariant(toneForType(event.type))}
                            label={String(event.type || 'event')}
                          />
                        )}
                        endContent={(
                          <Timestamp
                            value={Math.floor(Number(event.ts) / 1000)}
                            format="system_time"
                            color="secondary"
                          />
                        )}
                      />
                    ))}
                  </List>
                ) : (
                  <Text size="sm" color="secondary">{t('units.detail.no_events')}</Text>
                )}
              </Block>

              <Divider />

              <Block title={t('units.detail.descriptor')}>
                <CodeBlock
                  code={JSON.stringify(unit || current || {}, null, 2)}
                  language="json"
                  title={t('units.detail.descriptor')}
                  size="sm"
                  width="100%"
                  maxHeight={320}
                  isWrapped
                  isCollapsible
                  collapsibleThreshold={18}
                />
              </Block>
            </>
          ) : null}
        </VStack>
      </Drawer>

      <AlertDialog
        isOpen={confirmAction !== null}
        onOpenChange={(open) => { if (!open) setConfirmAction(null); }}
        title={confirmAction === 'kill'
          ? t('units.actions.kill_title')
          : t('units.actions.confirm_title', { action: actionLabel(confirmAction || '') })}
        description={confirmAction === 'kill'
          ? t('units.actions.kill_text', { id: selectedId || '' })
          : t('units.actions.confirm_text', { id: selectedId || '', action: actionLabel(confirmAction || '') })}
        actionLabel={confirmAction === 'kill' ? t('units.actions.kill') : actionLabel(confirmAction || '')}
        cancelLabel={t('shell.cancel')}
        actionVariant={confirmAction === 'kill' ? 'destructive' : 'primary'}
        isActionLoading={busy !== null && busy === confirmAction}
        onAction={() => { if (confirmAction) void running(confirmAction); }}
      />
    </>
  );
}

/** Descriptor health drives the small badge; a missing field means "unknown". */
function planHealth(unit: UnitView | null): 'ok' | 'bad' | 'unknown' {
  if (!unit) return 'unknown';
  const health = unit.health;
  if (health && typeof health.ok === 'boolean') return health.ok ? 'ok' : 'bad';
  return 'unknown';
}
