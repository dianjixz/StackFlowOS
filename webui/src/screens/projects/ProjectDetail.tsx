/**
 * ProjectDetail.tsx — `#/projects/:id`: one project's header, task board,
 * dependency graph, workspace git, onboarding check and agents.
 *
 * The project card comes from the live `/api/state` snapshot (so SSE refreshes
 * update the header), while tasks / git / health are fetched per screen and
 * reloaded after every write.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { Divider } from '@astryxdesign/core/Divider';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { IconButton } from '@astryxdesign/core/IconButton';
import { Layout, LayoutContent, LayoutHeader, HStack, VStack } from '@astryxdesign/core/Layout';
import { List, ListItem } from '@astryxdesign/core/List';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { Avatar } from '@astryxdesign/core/Avatar';
import { Heading, Text } from '@astryxdesign/core/Text';
import { useImperativeAlertDialog } from '@astryxdesign/core/AlertDialog';
import {
  ArrowLeftIcon,
  ArrowPathIcon,
  CheckIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { navigate } from '../../shell/router';
import { loadState, useStore, type Agent, type SfoState } from '../../state/store';
import { useCanWrite } from '../../session';
import { ago, roleLabel } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import { Page } from '../../ui/Page';
import { CreateTaskDialog, DeleteProjectDialog } from './dialogs';
import { DependencyGraphSection } from './DependencyGraph';
import { GitPanel } from './GitPanel';
import { HealthPanel } from './HealthPanel';
import { TaskBoardSection, TaskDetail } from './TaskBoard';
import {
  milestoneCount,
  normalizeTaskList,
  projectCounts,
  projectStatus,
} from './data';
import type { ProjectCard, Task } from './types';
import { Hint, SectionHead, Stat, StatusBadge } from './ui';

const selProjects = (state: SfoState): ProjectCard[] => state.projects as ProjectCard[];
const selAgents = (state: SfoState): Agent[] => state.agents;
const ACTIVE_AGENT_STATUSES = new Set(['queued', 'spawning', 'idle', 'working', 'waiting']);

function progressVariant(status: string): 'accent' | 'success' | 'warning' {
  if (status === 'done' || status === 'archived') return 'success';
  if (status === 'degraded') return 'warning';
  return 'accent';
}

function modeLabel(project: ProjectCard): string {
  if (project.external) {
    return project.workspace_mode === 'attached-rw'
      ? t('prj.detail.mode.rw')
      : t('prj.detail.mode.ro');
  }
  return t('prj.detail.mode.managed');
}

function NotFound({ id }: { id: string }): ReactNode {
  return (
    <Page
      title={t('prj.detail.notfound.title')}
      description={t('app.sub.projects')}
      actions={(
        <Button
          label={t('prj.common.back')}
          variant="secondary"
          icon={<ArrowLeftIcon width={18} height={18} aria-hidden="true" />}
          onClick={() => navigate('projects')}
        />
      )}
    >
      <EmptyState
        title={t('prj.detail.notfound.title')}
        description={t('prj.detail.notfound.text', { id })}
        headingLevel={2}
        actions={(
          <Button label={t('common.retry')} variant="primary" onClick={() => { void loadState({ quiet: true }); }} />
        )}
      />
    </Page>
  );
}

export function ProjectDetail({ id }: { id: string }): ReactNode {
  const projects = useStore(selProjects);
  const storeAgents = useStore(selAgents);
  const canWrite = useCanWrite();
  const project = projects.find((item) => item.id === id);

  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [tasksError, setTasksError] = useState('');
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [createTaskOpen, setCreateTaskOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState('');
  const [healthRun, setHealthRun] = useState(0);
  const healthRef = useRef<HTMLElement | null>(null);
  const taskDetailRef = useRef<HTMLElement | null>(null);
  const alert = useImperativeAlertDialog();

  const loadTasks = useCallback(async (quiet = false): Promise<void> => {
    if (!quiet) setTasksLoading(true);
    setTasksError('');
    try {
      const data = await api.getTasks(id);
      setTasks(normalizeTaskList(data));
    } catch (cause) {
      setTasksError(cause instanceof Error ? cause.message : String(cause));
      toastError(cause);
    } finally {
      setTasksLoading(false);
    }
  }, [id]);

  useEffect(() => {
    setSelectedTaskId(null);
    void loadTasks();
  }, [loadTasks]);

  const refreshAll = useCallback(async (): Promise<void> => {
    await Promise.all([
      loadState({ quiet: true }).catch(() => null),
      loadTasks(true),
    ]);
  }, [loadTasks]);

  if (!project) return <NotFound id={id} />;

  const status = projectStatus(project);
  const counts = projectCounts(project);
  const progress = Math.max(0, Math.min(100, Math.round(Number(project.progress) || 0)));
  const name = project.name || project.id;
  const workspaceMode = modeLabel(project);
  const tags = Array.isArray(project.tags) ? project.tags : [];
  const milestones = milestoneCount(project);
  const writable = project.workspace_mode === 'attached-rw';

  const projectAgents = (Array.isArray(project.agents) && project.agents.length
    ? project.agents
    : storeAgents.filter((agent) => agent.project_id === project.id))
    .filter((agent) => agent.status !== 'dead');
  const activeAgents = projectAgents.filter((agent) => ACTIVE_AGENT_STATUSES.has(agent.status)).length;
  const pending = Number(project.pending_approvals) || 0;
  const leases = Array.isArray(project.leases) ? project.leases : [];

  const complete = async (): Promise<void> => {
    setActionBusy('complete');
    try {
      await api.completeProject(project.id);
      toast(t('prj.complete.done'), 'info');
      await refreshAll();
    } catch (cause) {
      toastError(cause);
    } finally {
      setActionBusy('');
    }
  };

  const archive = async (): Promise<void> => {
    setActionBusy('archive');
    try {
      const result = (await api.archiveProject(project.id)) as { agents_stopped?: number };
      toast(t('prj.archive.done', { n: Number(result?.agents_stopped) || 0 }), 'info');
      await refreshAll();
      navigate('projects');
    } catch (cause) {
      toastError(cause);
    } finally {
      setActionBusy('');
    }
  };

  const toggleAccess = async (): Promise<void> => {
    const allow = !writable;
    setActionBusy('access');
    try {
      await api.setProjectAccess(project.id, allow, true);
      toast(t(allow ? 'prj.access.done.rw' : 'prj.access.done.ro'), 'info');
      await refreshAll();
    } catch (cause) {
      toastError(cause);
    } finally {
      setActionBusy('');
    }
  };

  const runHealthcheck = (): void => {
    setHealthRun((value) => value + 1);
    window.requestAnimationFrame(() => {
      healthRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  };

  const selectTask = (taskId: string | null): void => {
    setSelectedTaskId(taskId);
    if (!taskId) return;
    window.requestAnimationFrame(() => {
      taskDetailRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  };

  const headerActions = canWrite ? (
    <>
      <IconButton
        label={t('common.refresh')}
        variant="ghost"
        size="sm"
        icon={<ArrowPathIcon width={18} height={18} aria-hidden="true" />}
        onClick={() => { void refreshAll(); }}
      />
      <Button
        label={t('prj.action.healthcheck')}
        variant="ghost"
        size="sm"
        onClick={runHealthcheck}
      />
      {!['done', 'archived'].includes(String(project.status)) ? (
        <Button
          label={t('prj.action.complete')}
          variant="primary"
          size="sm"
          icon={<CheckIcon width={18} height={18} aria-hidden="true" />}
          isDisabled={actionBusy === 'complete'}
          onClick={() => {
            alert.show({
              title: t('prj.complete.confirm.title', { name }),
              description: t('prj.complete.confirm.text'),
              actionLabel: t('prj.action.complete'),
              cancelLabel: t('common.cancel'),
              actionVariant: 'primary',
              isActionLoading: actionBusy === 'complete',
              onAction: () => { alert.hide(); void complete(); },
            });
          }}
        />
      ) : null}
      {project.status !== 'archived' ? (
        <Button
          label={t('prj.action.archive')}
          variant="ghost"
          size="sm"
          isDisabled={actionBusy === 'archive'}
          onClick={() => {
            alert.show({
              title: t('prj.archive.confirm.title', { name }),
              description: t('prj.archive.confirm.text'),
              actionLabel: t('prj.action.archive'),
              cancelLabel: t('common.cancel'),
              actionVariant: 'destructive',
              onAction: () => { alert.hide(); void archive(); },
            });
          }}
        />
      ) : null}
      {project.external ? (
        <Button
          label={t(writable ? 'prj.action.access.ro' : 'prj.action.access.rw')}
          variant="ghost"
          size="sm"
          isDisabled={actionBusy === 'access'}
          onClick={() => {
            alert.show({
              title: t(writable ? 'prj.access.confirm.ro.title' : 'prj.access.confirm.rw.title', { name }),
              description: t(writable ? 'prj.access.confirm.ro.text' : 'prj.access.confirm.rw.text'),
              actionLabel: t(writable ? 'prj.action.access.ro' : 'prj.action.access.rw'),
              cancelLabel: t('common.cancel'),
              actionVariant: writable ? 'destructive' : 'primary',
              onAction: () => { alert.hide(); void toggleAccess(); },
            });
          }}
        />
      ) : null}
      <Button
        label={t('prj.action.delete')}
        variant="destructive"
        size="sm"
        icon={<TrashIcon width={18} height={18} aria-hidden="true" />}
        onClick={() => setDeleteOpen(true)}
      />
    </>
  ) : (
    <Hint>{t('prj.detail.viewer_hint')}</Hint>
  );

  const agentsSection = (
    <VStack gap={3}>
      <SectionHead
        level={2}
        title={t('prj.agents.title')}
        hint={t('prj.agents.hint', { total: projectAgents.length, active: activeAgents })}
        action={pending > 0 ? (
          <Button
            label={t('prj.agents.approvals', { n: pending })}
            variant="secondary"
            size="sm"
            onClick={() => navigate('approvals')}
          />
        ) : (
          <Badge variant="neutral" label={t('prj.agents.approvals.none')} />
        )}
      />
      {projectAgents.length === 0 ? (
        <Hint>{t('prj.agents.empty')}</Hint>
      ) : (
        <List hasDividers>
          {projectAgents.map((agent) => (
            <ListItem
              key={agent.id}
              href={`#/agents/${encodeURIComponent(agent.id)}`}
              startContent={<Avatar name={agent.display_name || agent.id} size="sm" />}
              label={(
                <HStack gap={2} align="center" wrap="wrap">
                  <Text type="body" weight="semibold">{agent.display_name || agent.id}</Text>
                  <Badge variant="neutral" label={roleLabel(agent.role)} />
                  <StatusBadge status={agent.status} />
                </HStack>
              )}
              description={agent.summary || agent.error || undefined}
              endContent={agent.last_seen ? (
                <Text size="2xs" color="secondary">{t('prj.agents.last_seen', { time: ago(agent.last_seen) })}</Text>
              ) : undefined}
            />
          ))}
        </List>
      )}
      <HStack gap={3} align="center" wrap="wrap">
        <Text size="xsm" color="secondary">{t('prj.agents.leases', { n: leases.length })}</Text>
        {leases.length === 0 ? <Hint>{t('prj.agents.leases.empty')}</Hint> : null}
      </HStack>
    </VStack>
  );

  const selectedTask = tasks.find((task) => task.id === selectedTaskId) || null;

  return (
    <>
    <Layout
      height="fill"
      contentWidth={1400}
      header={(
        <LayoutHeader padding={4} hasDivider>
          <VStack gap={3}>
            <HStack gap={2} align="center" wrap="wrap">
              <Button
                label={t('prj.common.back')}
                variant="ghost"
                size="sm"
                icon={<ArrowLeftIcon width={18} height={18} aria-hidden="true" />}
                onClick={() => navigate('projects')}
              />
              <Text size="xsm" color="secondary" hasTabularNumbers>{project.id}</Text>
              <StatusBadge status={status} />
            </HStack>

            <HStack gap={4} align="start" justify="between" wrap="wrap">
              <VStack gap={1}>
                <Heading level={2}>{name}</Heading>
                <Text type="body" color="secondary" maxLines={2}>
                  {project.goal || project.brief || t('prj.detail.goal.empty')}
                </Text>
              </VStack>
              <VStack gap={1.5} width={280}>
                <HStack gap={2} align="end" justify="between">
                  <Text size="2xs" color="secondary">{t('prj.detail.progress')}</Text>
                  <Text size="sm" weight="semibold" hasTabularNumbers>{`${progress}%`}</Text>
                </HStack>
                <ProgressBar
                  label={`${name} · ${t('prj.detail.progress')}`}
                  isLabelHidden
                  value={progress}
                  variant={progressVariant(status)}
                />
              </VStack>
            </HStack>

            <HStack gap={2} align="center" wrap="wrap">
              <Text size="xsm" color="secondary">{t('prj.common.workspace')}</Text>
              <Text type="code" size="xsm" maxLines={1}>{project.workspace || '—'}</Text>
              <Badge variant="neutral" label={workspaceMode} />
            </HStack>

            <HStack gap={4} align="center" wrap="wrap">
              <Text size="xsm" color="secondary">{t('prj.common.updated', { time: ago(project.updated_at) })}</Text>
              {milestones > 0 ? (
                <Text size="xsm" color="secondary">{t('prj.detail.milestones', { n: milestones })}</Text>
              ) : null}
              {tags.map((tag) => <Badge key={tag} variant="neutral" label={tag} />)}
            </HStack>

            <HStack gap={2} align="center" wrap="wrap">{headerActions}</HStack>
          </VStack>
        </LayoutHeader>
      )}
      content={(
        <LayoutContent padding={4} role="main">
          <VStack gap={6}>
            <Card padding={4}>
              <HStack gap={6} align="center" wrap="wrap">
                <Stat label={t('prj.stat.total')} value={counts.total} />
                <Stat label={t('prj.stat.todo')} value={counts.todo} />
                <Stat label={t('prj.stat.doing')} value={counts.doing} tone="info" />
                <Stat label={t('prj.stat.review')} value={counts.review} tone="warning" />
                <Stat label={t('prj.stat.done')} value={counts.done} tone="success" />
                <Stat label={t('prj.stat.blocked')} value={counts.blocked} tone="danger" />
              </HStack>
            </Card>

            {selectedTask ? (
              <VStack ref={taskDetailRef} gap={0}>
                <TaskDetail
                  task={selectedTask}
                  tasks={tasks}
                  canWrite={canWrite}
                  onClose={() => selectTask(null)}
                  onMutated={refreshAll}
                />
              </VStack>
            ) : null}

            <Divider />
            <TaskBoardSection
              tasks={tasks}
              isLoading={tasksLoading}
              error={tasksError}
              canWrite={canWrite}
              selectedId={selectedTaskId}
              onSelect={selectTask}
              onReload={refreshAll}
              onOpenCreate={() => setCreateTaskOpen(true)}
            />

            <Divider />
            <DependencyGraphSection
              tasks={tasks}
              selectedId={selectedTaskId}
              onSelect={(taskId) => selectTask(taskId)}
            />

            <Divider />
            <GitPanel
              projectId={project.id}
              projectName={name}
              canWrite={canWrite}
              onMutated={refreshAll}
            />

            <Divider />
            <VStack ref={healthRef} gap={0}>
              <HealthPanel
                projectId={project.id}
                canWrite={canWrite}
                onMutated={refreshAll}
                runSignal={healthRun}
              />
            </VStack>

            <Divider />
            {agentsSection}
          </VStack>
        </LayoutContent>
      )}
    />

    {/* dialogs live outside the layout frame so their focus trap is stable */}
    <CreateTaskDialog
      isOpen={createTaskOpen}
      projectId={project.id}
      onOpenChange={setCreateTaskOpen}
      onDone={refreshAll}
    />
    <DeleteProjectDialog
      project={project}
      isOpen={deleteOpen}
      onOpenChange={setDeleteOpen}
      onDone={async () => {
        await loadState({ quiet: true }).catch(() => null);
        navigate('projects');
      }}
    />
    {alert.element}
    </>
  );
}
