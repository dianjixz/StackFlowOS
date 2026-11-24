/**
 * Roster.tsx — `#/agents`: the agent roster, grouped by project, with
 * project/role/status filters, dispatch (`POST /api/agents`) and terminate.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Page } from '../../ui/Page';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Button } from '@astryxdesign/core/Button';
import { IconButton } from '@astryxdesign/core/IconButton';
import { Badge } from '@astryxdesign/core/Badge';
import { Divider } from '@astryxdesign/core/Divider';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { ClickableCard } from '@astryxdesign/core/ClickableCard';
import { Grid } from '@astryxdesign/core/Grid';
import { Selector } from '@astryxdesign/core/Selector';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { Card } from '@astryxdesign/core/Card';
import {
  ArrowPathIcon,
  FolderIcon,
  PlusIcon,
  TrashIcon,
  UserGroupIcon,
} from '@heroicons/react/24/outline';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { useStore, loadState, type Agent, type Project, type SfoState } from '../../state/store';
import { useCanWrite } from '../../session';
import { ago, clip, roleLabel, statusLabel } from '../../ui/format';
import { toastError } from '../../ui/toast';
import {
  AgentAvatar,
  ROLE_ORDER,
  StatusBadge,
  agentName,
  isTerminal,
} from './common';
import { SpawnDialog } from './SpawnDialog';
import { normalizeTasks, type AgentTask } from './types';

const selAgents = (s: SfoState): Agent[] => s.agents;
const selProjects = (s: SfoState): Project[] => s.projects;

const STATUS_ORDER = ['spawning', 'idle', 'working', 'waiting', 'blocked', 'failed', 'done', 'dead'];

function statusRank(status: string): number {
  const index = STATUS_ORDER.indexOf(status);
  return index === -1 ? STATUS_ORDER.length : index;
}

interface AgentGroup {
  key: string;
  title: string;
  project: Project | null;
  agents: Agent[];
}

export function AgentRosterScreen(): ReactNode {
  const canWrite = useCanWrite();
  const agents = useStore(selAgents);
  const projects = useStore(selProjects);

  const [projectFilter, setProjectFilter] = useState('all');
  const [roleFilter, setRoleFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [spawnOpen, setSpawnOpen] = useState(false);
  const [killTarget, setKillTarget] = useState<Agent | null>(null);
  const [killing, setKilling] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [taskMap, setTaskMap] = useState<Record<string, AgentTask[]>>({});

  /* Current-task titles: one fetch per project that actually has agents. */
  const projectIds = useMemo(
    () => [...new Set(agents.map((agent) => agent.project_id).filter((id): id is string => Boolean(id)))].sort(),
    [agents],
  );
  const projectIdsKey = projectIds.join(',');
  useEffect(() => {
    if (!projectIds.length) {
      setTaskMap({});
      return undefined;
    }
    let alive = true;
    void Promise.all(
      projectIds.map(async (id) => {
        try {
          return [id, normalizeTasks(await api.getTasks(id))] as const;
        } catch (error) {
          console.warn('[agents] could not load tasks', id, error);
          return [id, [] as AgentTask[]] as const;
        }
      }),
    ).then((entries) => {
      if (alive) setTaskMap(Object.fromEntries(entries));
    });
    return () => { alive = false; };
    // projectIdsKey is the stable dependency; the array itself is recreated.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectIdsKey]);

  const projectOptions = useMemo(() => {
    const options: { value: string; label: string }[] = [
      { value: 'all', label: t('agents.roster.filter.all') },
      ...projects.map((project) => ({ value: project.id, label: project.name || project.id })),
    ];
    if (agents.some((agent) => !agent.project_id)) {
      options.push({ value: 'none', label: t('agents.roster.unassigned') });
    }
    return options;
  }, [projects, agents]);

  const roleOptions = useMemo(() => {
    const present = new Set(agents.map((agent) => agent.role));
    const ordered = ROLE_ORDER.filter((role) => present.has(role));
    const extra = [...present].filter((role) => !ROLE_ORDER.includes(role)).sort();
    return [
      { value: 'all', label: t('agents.roster.filter.all') },
      ...ordered.concat(extra).map((role) => ({ value: role, label: roleLabel(role) })),
    ];
  }, [agents]);

  const statusOptions = useMemo(() => {
    const present = [...new Set(agents.map((agent) => agent.status))].sort(
      (a, b) => statusRank(a) - statusRank(b),
    );
    return [
      { value: 'all', label: t('agents.roster.filter.all') },
      ...present.map((status) => ({ value: status, label: statusLabel(status) })),
    ];
  }, [agents]);

  const filtered = useMemo(() => agents.filter((agent) => {
    if (projectFilter === 'none' && agent.project_id) return false;
    if (projectFilter !== 'all' && projectFilter !== 'none' && agent.project_id !== projectFilter) return false;
    if (roleFilter !== 'all' && agent.role !== roleFilter) return false;
    if (statusFilter !== 'all' && agent.status !== statusFilter) return false;
    return true;
  }), [agents, projectFilter, roleFilter, statusFilter]);

  const groups = useMemo<AgentGroup[]>(() => {
    const byProject = new Map<string, Agent[]>();
    for (const agent of filtered) {
      const key = agent.project_id || '__none__';
      const rows = byProject.get(key);
      if (rows) rows.push(agent);
      else byProject.set(key, [agent]);
    }
    const result: AgentGroup[] = [];
    for (const project of projects) {
      const rows = byProject.get(project.id);
      if (rows?.length) {
        result.push({ key: project.id, title: project.name || project.id, project, agents: rows });
        byProject.delete(project.id);
      }
    }
    for (const [key, rows] of byProject) {
      result.push({
        key,
        title: key === '__none__' ? t('agents.roster.unassigned') : key,
        project: projects.find((project) => project.id === key) || null,
        agents: rows,
      });
    }
    return result;
  }, [filtered, projects]);

  const hasFilters = projectFilter !== 'all' || roleFilter !== 'all' || statusFilter !== 'all';

  const resetFilters = (): void => {
    setProjectFilter('all');
    setRoleFilter('all');
    setStatusFilter('all');
  };

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await loadState();
    } catch (error) {
      toastError(error);
    } finally {
      setRefreshing(false);
    }
  };

  const taskFor = (agent: Agent): AgentTask | null => {
    if (!agent.task_id) return null;
    const rows = taskMap[agent.project_id || ''];
    if (!rows) return null;
    return rows.find((task) => task.id === agent.task_id) || null;
  };

  const renderCard = (agent: Agent): ReactNode => {
    const name = agentName(agent);
    const task = taskFor(agent);
    const taskText = task
      ? clip(task.title || task.id, 34)
      : agent.task_id
        ? clip(agent.task_id, 24)
        : t('agents.roster.card.no_task');
    return (
      <ClickableCard
        key={agent.id}
        href={`#/agents/${encodeURIComponent(agent.id)}`}
        label={name}
        padding={3}
      >
        <VStack gap={2}>
          <HStack justify="between" align="start" gap={2}>
            <HStack gap={2} align="center">
              <AgentAvatar agent={agent} size="md" />
              <VStack gap={0}>
                <Text weight="semibold" maxLines={1}>{name}</Text>
                <Text size="xsm" color="secondary">{roleLabel(agent.role)} · {clip(agent.id, 20)}</Text>
              </VStack>
            </HStack>
            <HStack gap={1} align="center">
              <StatusBadge status={agent.status} />
              {canWrite && !isTerminal(agent.status) ? (
                <IconButton
                  label={t('agents.roster.card.kill')}
                  variant="ghost"
                  size="sm"
                  icon={<TrashIcon width={16} height={16} />}
                  onClick={(event) => {
                    event.stopPropagation();
                    setKillTarget(agent);
                  }}
                />
              ) : null}
            </HStack>
          </HStack>

          <Text size="sm" color="secondary" maxLines={2}>
            {agent.summary || t('agents.roster.card.no_task')}
          </Text>
          {agent.error ? (
            <Text size="xsm" color="secondary" maxLines={1}>
              {t('agents.roster.card.error')}: {clip(agent.error, 80)}
            </Text>
          ) : null}

          <Divider />

          <HStack gap={2} align="center" wrap="wrap">
            <Badge label={clip(agent.model || t('agents.none'), 20)} variant="neutral" />
            <Text size="xsm" color="secondary">
              {t('agents.roster.card.task')}: {taskText}
            </Text>
          </HStack>
          <HStack justify="between" align="center" gap={2}>
            <Text size="xsm" color="secondary">
              {t('agents.roster.card.last_seen')}{' '}
              {agent.last_seen ? ago(agent.last_seen) : t('agents.none')}
            </Text>
            <Text size="xsm" color="secondary">{t('agents.roster.card.open')} →</Text>
          </HStack>
        </VStack>
      </ClickableCard>
    );
  };

  let body: ReactNode;
  if (!agents.length) {
    body = (
      <Card padding={6}>
        <EmptyState
          icon={<UserGroupIcon width={40} height={40} />}
          title={t('agents.roster.empty.title')}
          description={t('agents.roster.empty.text')}
          headingLevel={3}
          actions={canWrite ? (
            <Button
              label={t('agents.roster.empty.action')}
              variant="primary"
              icon={<PlusIcon width={16} height={16} />}
              onClick={() => setSpawnOpen(true)}
            />
          ) : null}
        />
      </Card>
    );
  } else if (!filtered.length) {
    body = (
      <Card padding={6}>
        <EmptyState
          icon={<UserGroupIcon width={40} height={40} />}
          title={t('agents.roster.no_match.title')}
          description={t('agents.roster.no_match.text')}
          headingLevel={3}
          actions={<Button label={t('agents.roster.reset_filters')} variant="secondary" onClick={resetFilters} />}
        />
      </Card>
    );
  } else {
    body = (
      <VStack gap={5}>
        {groups.map((group) => (
          <VStack key={group.key} gap={2}>
            <HStack gap={2} align="center" wrap="wrap">
              <FolderIcon width={18} height={18} aria-hidden="true" />
              <Heading level={3}>{group.title}</Heading>
              {group.project ? <StatusBadge status={group.project.status} /> : null}
              <Badge
                label={t('agents.roster.group.count', { n: group.agents.length })}
                variant="neutral"
              />
            </HStack>
            <Grid columns={{ minWidth: 320, max: 3 }} gap={3}>
              {group.agents.map(renderCard)}
            </Grid>
          </VStack>
        ))}
      </VStack>
    );
  }

  return (
    <>
      <Page
        title={t('agents.roster.title')}
        description={t('agents.roster.description')}
        actions={(
          <>
            {canWrite ? (
              <Button
                label={t('agents.roster.spawn')}
                variant="primary"
                icon={<PlusIcon width={16} height={16} />}
                onClick={() => setSpawnOpen(true)}
              />
            ) : null}
            <Button
              label={t('agents.roster.refresh')}
              variant="ghost"
              icon={<ArrowPathIcon width={16} height={16} />}
              isLoading={refreshing}
              onClick={() => { void refresh(); }}
            />
          </>
        )}
      >
        <VStack gap={4}>
          <HStack gap={2} align="end" wrap="wrap">
            <Selector
              label={t('agents.roster.filter.project')}
              size="sm"
              width={190}
              options={projectOptions}
              value={projectFilter}
              onChange={(value) => setProjectFilter(value || 'all')}
            />
            <Selector
              label={t('agents.roster.filter.role')}
              size="sm"
              width={160}
              options={roleOptions}
              value={roleFilter}
              onChange={(value) => setRoleFilter(value || 'all')}
            />
            <Selector
              label={t('agents.roster.filter.status')}
              size="sm"
              width={160}
              options={statusOptions}
              value={statusFilter}
              onChange={(value) => setStatusFilter(value || 'all')}
            />
            {hasFilters ? (
              <Button
                label={t('agents.roster.reset_filters')}
                variant="ghost"
                size="sm"
                onClick={resetFilters}
              />
            ) : null}
          </HStack>
          {body}
        </VStack>
      </Page>

      <SpawnDialog isOpen={spawnOpen} onOpenChange={setSpawnOpen} projects={projects} />

      <AlertDialog
        isOpen={Boolean(killTarget)}
        onOpenChange={(open) => { if (!open) setKillTarget(null); }}
        title={t('agents.kill.title', { name: agentName(killTarget) })}
        description={t('agents.kill.text')}
        actionLabel={t('agents.kill.confirm')}
        cancelLabel={t('agents.kill.cancel')}
        actionVariant="destructive"
        isActionLoading={killing}
        onAction={() => {
          if (!killTarget) return;
          setKilling(true);
          void api.killAgent(killTarget.id)
            .then(async () => {
              await loadState({ quiet: true }).catch(() => null);
              setKillTarget(null);
            })
            .catch((error) => toastError(error))
            .finally(() => setKilling(false));
        }}
      />
    </>
  );
}
