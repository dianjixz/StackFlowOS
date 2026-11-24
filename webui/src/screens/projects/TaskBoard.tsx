/**
 * TaskBoard.tsx — the project task board: five status columns, per-task status
 * changes, plus the detail panel (checklist, artifacts, notes) for the task the
 * operator selected.
 */

import { useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { ClickableCard } from '@astryxdesign/core/ClickableCard';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack, Section, StackItem, VStack } from '@astryxdesign/core/Layout';
import { Grid } from '@astryxdesign/core/Grid';
import { Heading, Text } from '@astryxdesign/core/Text';
import { IconButton } from '@astryxdesign/core/IconButton';
import { List, ListItem } from '@astryxdesign/core/List';
import { Selector } from '@astryxdesign/core/Selector';
import { Spinner } from '@astryxdesign/core/Spinner';
import { TextInput } from '@astryxdesign/core/TextInput';
import {
  ArrowPathIcon,
  PlusIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { fmtDateTime, roleLabel, statusLabel } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import {
  TASK_STATUSES,
  checkProgress,
  isDependencyBlocked,
  openDepCount,
  priorityLabel,
  taskDeps,
  taskTitle,
} from './data';
import type { Task } from './types';
import { Hint, SectionHead, StatusBadge, StatusDotLabel } from './ui';

const TASK_TRANSITIONS: Record<string, string[]> = {
  todo: ['doing', 'blocked'],
  doing: ['review', 'blocked', 'todo'],
  review: ['done', 'todo', 'blocked'],
  blocked: ['todo', 'doing'],
  done: [],
};

const TASK_STATUS_OPTIONS = (status: string): { value: string; label: string }[] => {
  const list = [status, ...(TASK_TRANSITIONS[status] || [])];
  return list.map((value) => ({ value, label: statusLabel(value) }));
};

/* ------------------------------------------------------------------ */
/* task card                                                           */
/* ------------------------------------------------------------------ */

function TaskCard({
  task,
  tasks,
  canWrite,
  isSelected,
  isBusy,
  onSelect,
  onChangeStatus,
}: {
  task: Task;
  tasks: Task[];
  canWrite: boolean;
  isSelected: boolean;
  isBusy: boolean;
  onSelect: () => void;
  onChangeStatus: (status: string) => void;
}): ReactNode {
  const title = taskTitle(task);
  const progress = checkProgress(task);
  const deps = taskDeps(task);
  const blocked = isDependencyBlocked(task, tasks);
  const priority = Number(task.priority) || 0;

  return (
    <ClickableCard
      label={t('prj.task.select', { title })}
      onClick={onSelect}
      variant={isSelected ? 'muted' : 'default'}
      padding={3}
    >
      <VStack gap={2}>
        <Text type="body" size="sm" weight="semibold" maxLines={2}>{title}</Text>
        <HStack gap={1.5} align="center" wrap="wrap">
          <Text size="2xs" color="secondary" hasTabularNumbers>{task.id}</Text>
          {task.role ? <Badge variant="neutral" label={roleLabel(task.role)} /> : null}
          {priority > 0 ? <Badge variant="info" label={priorityLabel(priority)} /> : null}
          {progress.total > 0 ? (
            <Text size="2xs" color="secondary" hasTabularNumbers>
              ✓ {t('prj.task.checks', { done: progress.done, total: progress.total })}
            </Text>
          ) : null}
          {deps.length ? (
            <Badge
              variant={blocked ? 'error' : 'success'}
              label={`${t('deps.count', { n: deps.length })}${blocked ? ` · ${t('deps.open', { n: openDepCount(task, tasks) })}` : ''}`}
            />
          ) : null}
        </HStack>
        {canWrite && task.status !== 'done' ? (
          <Selector
            label={t('prj.task.status')}
            isLabelHidden
            size="sm"
            variant="secondary"
            value={task.status}
            options={TASK_STATUS_OPTIONS(task.status)}
            onChange={(next) => { if (next) onChangeStatus(next); }}
            isLoading={isBusy}
          />
        ) : (
          <HStack><StatusBadge status={task.status} /></HStack>
        )}
      </VStack>
    </ClickableCard>
  );
}

/* ------------------------------------------------------------------ */
/* task detail                                                         */
/* ------------------------------------------------------------------ */

export function TaskDetail({
  task,
  tasks,
  canWrite,
  onClose,
  onMutated,
}: {
  task: Task;
  tasks: Task[];
  canWrite: boolean;
  onClose: () => void;
  onMutated: () => Promise<void> | void;
}): ReactNode {
  const [checkBusy, setCheckBusy] = useState('');
  const [note, setNote] = useState('');
  const [noteBusy, setNoteBusy] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);

  const checks = Array.isArray(task.checks) ? task.checks : [];
  const notes = Array.isArray(task.notes) ? task.notes : [];
  const artifacts = Array.isArray(task.artifacts) ? task.artifacts : [];
  const deps = taskDeps(task);
  const byId = new Map(tasks.map((item) => [item.id, item]));

  const changeStatus = async (status: string): Promise<void> => {
    if (!status || status === task.status) return;
    setStatusBusy(true);
    try {
      await api.patchTask(task.id, { status });
      toast(t('prj.task.status.moved', { status: statusLabel(status) }), 'info');
      await onMutated();
    } catch (cause) {
      toastError(cause);
    } finally {
      setStatusBusy(false);
    }
  };

  const toggleCheck = async (itemId: string, done: boolean): Promise<void> => {
    setCheckBusy(itemId);
    try {
      await api.checkTask(task.id, { item_id: itemId, done });
      await onMutated();
    } catch (cause) {
      toastError(cause);
    } finally {
      setCheckBusy('');
    }
  };

  const addNote = async (): Promise<void> => {
    const text = note.trim();
    if (!text) {
      toast(t('prj.task.note.required'), 'error');
      return;
    }
    setNoteBusy(true);
    try {
      await api.addTaskNote(task.id, text);
      setNote('');
      toast(t('prj.task.note.done'), 'info');
      await onMutated();
    } catch (cause) {
      toastError(cause);
    } finally {
      setNoteBusy(false);
    }
  };

  return (
    <Card padding={4}>
      <VStack gap={4}>
        <HStack gap={3} align="start" justify="between" wrap="wrap">
          <VStack gap={1}>
            <HStack gap={2} align="center" wrap="wrap">
              <Heading level={3}>{taskTitle(task)}</Heading>
              <StatusBadge status={task.status} />
            </HStack>
            <HStack gap={2} align="center" wrap="wrap">
              <Text size="xsm" color="secondary" hasTabularNumbers>{task.id}</Text>
              {task.role ? <Badge variant="neutral" label={roleLabel(task.role)} /> : null}
              {task.assignee ? <Text size="xsm" color="secondary">{`@${task.assignee}`}</Text> : null}
              {(Number(task.priority) || 0) > 0 ? (
                <Badge variant="info" label={t('prj.task.priority', { n: Number(task.priority) })} />
              ) : null}
              {task.error ? <Badge variant="error" label={task.error} /> : null}
              {task.status === 'done' ? (
                <Text size="2xs" color="secondary">{t('prj.board.done.locked')}</Text>
              ) : null}
            </HStack>
            <Text size="2xs" color="secondary">
              {t('prj.task.meta', {
                created: fmtDateTime(task.created_at),
                updated: fmtDateTime(task.updated_at),
              })}
            </Text>
          </VStack>
          <HStack gap={2} align="center" wrap="wrap">
            {canWrite && task.status !== 'done' ? (
              <Selector
                label={t('prj.task.status')}
                size="sm"
                value={task.status}
                options={TASK_STATUS_OPTIONS(task.status)}
                onChange={(next) => { if (next) void changeStatus(next); }}
                isLoading={statusBusy}
              />
            ) : null}
            <IconButton
              label={t('prj.task.close')}
              variant="ghost"
              size="sm"
              icon={<XMarkIcon width={18} height={18} aria-hidden="true" />}
              onClick={onClose}
            />
          </HStack>
        </HStack>

        {task.description ? (
          <Text type="body">{task.description}</Text>
        ) : (
          <Hint>{t('prj.task.description.empty')}</Hint>
        )}

        {deps.length ? (
          <VStack gap={1.5}>
            <Text size="sm" weight="semibold">{t('deps.depends_on')}</Text>
            <HStack gap={2} align="center" wrap="wrap">
              {deps.map((depId) => {
                const dep = byId.get(depId);
                return (
                  <Badge
                    key={depId}
                    variant={dep && dep.status === 'done' ? 'success' : 'error'}
                    label={dep ? taskTitle(dep) : `${depId} · ${t('prj.deps.missing')}`}
                  />
                );
              })}
            </HStack>
          </VStack>
        ) : null}

        <Grid columns={{ minWidth: 280, max: 2 }} gap={4}>
          <VStack gap={2}>
            <Text size="sm" weight="semibold">{t('prj.task.checklist')}</Text>
            {checks.length === 0 ? <Hint>{t('prj.task.checklist.empty')}</Hint> : (
              <VStack gap={1.5}>
                {checks.map((check) => (
                  <CheckboxInput
                    key={check.id}
                    label={check.text || check.id}
                    description={check.note || undefined}
                    value={Boolean(check.done)}
                    isDisabled={!canWrite}
                    isLoading={checkBusy === check.id}
                    onChange={(next) => { void toggleCheck(check.id, next); }}
                  />
                ))}
              </VStack>
            )}
            <Text size="sm" weight="semibold">{t('prj.task.artifacts')}</Text>
            {artifacts.length === 0 ? <Hint>{t('prj.task.artifacts.empty')}</Hint> : (
              <List density="compact" hasDividers>
                {artifacts.map((artifact, index) => (
                  <ListItem
                    key={`${artifact.path || artifact.title || index}`}
                    label={artifact.title || artifact.path || '—'}
                    description={artifact.path || ''}
                    endContent={<Badge variant="neutral" label={artifact.type || t('prj.task.artifact.file')} />}
                  />
                ))}
              </List>
            )}
          </VStack>

          <VStack gap={2}>
            <Text size="sm" weight="semibold">{t('prj.task.notes')}</Text>
            {notes.length === 0 ? <Hint>{t('prj.task.notes.empty')}</Hint> : (
              <VStack gap={2}>
                {notes.map((entry, index) => (
                  <Card key={`${entry.ts || index}-${index}`} variant="muted" padding={3}>
                    <VStack gap={1}>
                      <HStack gap={2} align="center" wrap="wrap">
                        <Text size="2xs" color="secondary">{entry.author || '—'}</Text>
                        <Text size="2xs" color="secondary">{fmtDateTime(entry.ts)}</Text>
                      </HStack>
                      <Text type="supporting">{entry.text || ''}</Text>
                    </VStack>
                  </Card>
                ))}
              </VStack>
            )}
            {canWrite ? (
              <HStack gap={2} align="end">
                <StackItem size="fill">
                  <TextInput
                    label={t('prj.task.notes')}
                    isLabelHidden
                    placeholder={t('prj.task.note.placeholder')}
                    value={note}
                    onChange={setNote}
                    onEnter={() => { void addNote(); }}
                    width="100%"
                  />
                </StackItem>
                <Button
                  label={t('prj.task.note.add')}
                  size="sm"
                  isLoading={noteBusy}
                  clickAction={addNote}
                />
              </HStack>
            ) : null}
          </VStack>
        </Grid>
      </VStack>
    </Card>
  );
}

function LaneHead({ status, count }: { status: string; count: number }): ReactNode {
  return (
    <HStack gap={2} align="center" justify="between">
      <StatusDotLabel status={status} />
      <Badge variant="neutral" label={String(count)} />
    </HStack>
  );
}

/* ------------------------------------------------------------------ */
/* board section                                                       */
/* ------------------------------------------------------------------ */

export interface TaskBoardProps {
  tasks: Task[];
  isLoading: boolean;
  error: string;
  canWrite: boolean;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onReload: () => Promise<void> | void;
  onOpenCreate: () => void;
}

export function TaskBoardSection({
  tasks,
  isLoading,
  error,
  canWrite,
  selectedId,
  onSelect,
  onReload,
  onOpenCreate,
}: TaskBoardProps): ReactNode {
  const [busyTask, setBusyTask] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  const changeStatus = async (task: Task, status: string): Promise<void> => {
    if (!status || status === task.status) return;
    setBusyTask(task.id);
    try {
      await api.patchTask(task.id, { status });
      toast(t('prj.task.status.moved', { status: statusLabel(status) }), 'info');
      await onReload();
    } catch (cause) {
      toastError(cause);
    } finally {
      setBusyTask('');
    }
  };

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await onReload();
    } finally {
      setRefreshing(false);
    }
  };

  const columns = TASK_STATUSES
    .map((status) => ({
      status,
      tasks: tasks
        .filter((task) => task.status === status)
        .sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0)),
    }))
    .filter((column) => column.tasks.length > 0);
  const emptyStatuses = TASK_STATUSES.filter((status) => !columns.some((column) => column.status === status));

  const renderTaskCard = (task: Task): ReactNode => (
    <TaskCard
      key={task.id}
      task={task}
      tasks={tasks}
      canWrite={canWrite}
      isSelected={task.id === selectedId}
      isBusy={busyTask === task.id}
      onSelect={() => onSelect(task.id === selectedId ? null : task.id)}
      onChangeStatus={(next) => { void changeStatus(task, next); }}
    />
  );

  return (
    <Section padding={0}>
      <VStack gap={4}>
        <SectionHead
          level={2}
          title={t('prj.board.title')}
          hint={isLoading ? t('prj.board.loading') : t('prj.board.hint', { n: tasks.length })}
          action={(
            <>
              <Button
                label={t('prj.board.refresh')}
                variant="ghost"
                size="sm"
                icon={<ArrowPathIcon width={18} height={18} aria-hidden="true" />}
                isLoading={refreshing}
                clickAction={refresh}
              />
              {canWrite ? (
                <Button
                  label={t('prj.board.add')}
                  variant="secondary"
                  size="sm"
                  icon={<PlusIcon width={18} height={18} aria-hidden="true" />}
                  onClick={onOpenCreate}
                />
              ) : null}
            </>
          )}
        />

        {isLoading ? (
          <HStack gap={2} align="center" padding={4}>
            <Spinner size="sm" />
            <Text size="sm" color="secondary">{t('prj.board.loading')}</Text>
          </HStack>
        ) : error ? (
          <Hint isWarning>{t('prj.board.error', { message: error })}</Hint>
        ) : tasks.length === 0 ? (
          <EmptyState
            title={t('prj.board.empty.title')}
            description={t('prj.board.empty.text')}
            headingLevel={3}
            isCompact
            actions={canWrite ? (
              <Button label={t('prj.board.add')} variant="primary" onClick={onOpenCreate} />
            ) : undefined}
          />
        ) : (
          <VStack gap={3}>
            {/* Only non-empty status groups get a column: with sparse data a
                fixed five-column board wastes most of the width on empty
                "空" cells and squeezes the real work into a narrow strip.
                A single group reads as a card grid instead of one 1100px lane. */}
            {emptyStatuses.length ? (
              <HStack gap={3} align="center" wrap="wrap">
                <Text size="2xs" color="secondary">{t('prj.board.empty.statuses')}</Text>
                {emptyStatuses.map((status) => (
                  <StatusDotLabel key={status} status={status} />
                ))}
              </HStack>
            ) : null}
            {columns.length === 1 ? (
              <VStack gap={2}>
                <LaneHead status={columns[0].status} count={columns[0].tasks.length} />
                <Grid columns={{ minWidth: 300, max: 3 }} gap={3}>
                  {columns[0].tasks.map(renderTaskCard)}
                </Grid>
              </VStack>
            ) : (
              <Grid columns={{ minWidth: 260, max: Math.min(columns.length, 5) }} gap={3}>
                {columns.map(({ status, tasks: columnTasks }) => (
                  <VStack key={status} gap={2}>
                    <LaneHead status={status} count={columnTasks.length} />
                    {columnTasks.map(renderTaskCard)}
                  </VStack>
                ))}
              </Grid>
            )}
          </VStack>
        )}
      </VStack>
    </Section>
  );
}
