/**
 * ProjectList.tsx — `#/projects`: every project grouped by status, with the
 * create / attach entry points.
 */

import { useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { ClickableCard } from '@astryxdesign/core/ClickableCard';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { Divider } from '@astryxdesign/core/Divider';
import { Grid } from '@astryxdesign/core/Grid';
import {
  ArrowPathIcon,
  FolderPlusIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { t } from '../../i18n';
import { navigate } from '../../shell/router';
import { useStore, loadState, type SfoState } from '../../state/store';
import { useCanWrite } from '../../session';
import { Page } from '../../ui/Page';
import { ago } from '../../ui/format';
import { toastError } from '../../ui/toast';
import { AttachWorkspaceDialog, CreateProjectDialog } from './dialogs';
import {
  filterOptions,
  groupProjects,
  projectCounts,
  projectStatus,
  sortProjects,
} from './data';
import type { ProjectCard } from './types';
import { SectionHead, StatusBadge, Hint } from './ui';

const selProjects = (state: SfoState): ProjectCard[] => state.projects as ProjectCard[];

function progressVariant(status: string): 'accent' | 'success' | 'warning' {
  if (status === 'done' || status === 'archived') return 'success';
  if (status === 'degraded') return 'warning';
  return 'accent';
}

function ProjectRow({ project }: { project: ProjectCard }): ReactNode {
  const counts = projectCounts(project);
  const progress = Math.max(0, Math.min(100, Math.round(Number(project.progress) || 0)));
  const agents = Array.isArray(project.agents) ? project.agents.length : 0;
  const pending = Number(project.pending_approvals) || 0;
  const status = projectStatus(project);
  const name = project.name || project.id;
  const description = project.goal || project.brief || '';
  const tags = Array.isArray(project.tags) ? project.tags : [];

  return (
    <ClickableCard
      label={t('prj.list.open', { name })}
      href={`#/projects/${encodeURIComponent(project.id)}`}
      padding={4}
    >
      <HStack gap={4} align="center" justify="between" wrap="wrap">
        <VStack gap={1.5}>
          <HStack gap={2} align="center" wrap="wrap">
            <Text type="body" weight="semibold">{name}</Text>
            <StatusBadge status={status} />
            {tags.map((tag) => <Badge key={tag} variant="neutral" label={tag} />)}
          </HStack>
          {description ? (
            <Text type="supporting" color="secondary" maxLines={2}>{description}</Text>
          ) : null}
          <HStack gap={3} align="center" wrap="wrap">
            <Text size="xsm" color="secondary">{t('prj.common.tasks', { n: counts.total })}</Text>
            <Text size="xsm" color="secondary">{t('prj.common.agents', { n: agents })}</Text>
            {pending > 0 ? <Badge variant="warning" label={t('prj.common.approvals', { n: pending })} /> : null}
            <Text size="xsm" color="secondary">{t('prj.common.updated', { time: ago(project.updated_at) })}</Text>
          </HStack>
        </VStack>
        <VStack gap={1} width={220}>
          <HStack gap={2} align="center" justify="between">
            <Text size="2xs" color="secondary">{t('prj.detail.progress')}</Text>
            <Text size="xsm" color="secondary" hasTabularNumbers>{progress}%</Text>
          </HStack>
          <ProgressBar
            label={`${name} · ${t('prj.detail.progress')}`}
            isLabelHidden
            value={progress}
            variant={progressVariant(status)}
          />
        </VStack>
      </HStack>
    </ClickableCard>
  );
}

export function ProjectList(): ReactNode {
  const projects = useStore(selProjects);
  const canWrite = useCanWrite();
  const [filter, setFilter] = useState('all');
  const [createOpen, setCreateOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const statuses = filterOptions(projects);
  const visible = filter === 'all'
    ? projects
    : projects.filter((project) => projectStatus(project) === filter);
  const groups = groupProjects(visible);

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await loadState({ quiet: true });
    } catch (cause) {
      toastError(cause);
    } finally {
      setRefreshing(false);
    }
  };

  const afterCreate = (projectId: string): void => {
    if (projectId) navigate(`projects/${projectId}`);
  };

  const actions = (
    <>
      <Button
        label={t('common.refresh')}
        variant="ghost"
        size="sm"
        icon={<ArrowPathIcon width={18} height={18} aria-hidden="true" />}
        isLoading={refreshing}
        clickAction={refresh}
      />
      {canWrite ? (
        <>
          <Button
            label={t('prj.list.attach')}
            variant="secondary"
            size="sm"
            icon={<FolderPlusIcon width={18} height={18} aria-hidden="true" />}
            onClick={() => setAttachOpen(true)}
          />
          <Button
            label={t('prj.list.create')}
            variant="primary"
            size="sm"
            icon={<PlusIcon width={18} height={18} aria-hidden="true" />}
            onClick={() => setCreateOpen(true)}
          />
        </>
      ) : null}
    </>
  );

  return (
    <Page title={t('app.projects')} description={t('app.sub.projects')} actions={actions}>
      <VStack gap={6}>
        <HStack gap={3} align="center" justify="between" wrap="wrap">
          <VStack gap={0.5}>
            <Text type="body" weight="semibold">{t('prj.list.count', { n: projects.length })}</Text>
            <Text size="xsm" color="secondary">{t('prj.list.hint')}</Text>
          </VStack>
          {statuses.length > 1 ? (
            <SegmentedControl value={filter} onChange={setFilter} label={t('prj.list.filter')} size="sm">
              <SegmentedControlItem value="all" label={t('prj.list.filter.all')} />
              {statuses.map((status) => (
                <SegmentedControlItem key={status} value={status} label={t(`status.${status}`, {}, status)} />
              ))}
            </SegmentedControl>
          ) : null}
        </HStack>

        {!canWrite ? <Hint>{t('prj.detail.viewer_hint')}</Hint> : null}

        {projects.length === 0 ? (
          <EmptyState
            title={t('prj.list.empty.title')}
            description={t('prj.list.empty.text')}
            headingLevel={2}
            actions={canWrite ? (
              <Button label={t('prj.list.create')} variant="primary" onClick={() => setCreateOpen(true)} />
            ) : undefined}
          />
        ) : groups.length === 0 ? (
          <EmptyState
            title={t('prj.list.empty.filtered.title')}
            description={t('prj.list.empty.filtered.text')}
            headingLevel={2}
            isCompact
          />
        ) : (
          groups.map((group, index) => (
            <VStack key={group.status} gap={3}>
              {index > 0 ? <Divider /> : null}
              <SectionHead
                level={2}
                title={t(`status.${group.status}`, {}, group.status)}
                hint={group.status === 'degraded' ? t('projects.state.degraded') : undefined}
                action={<Badge variant="neutral" label={t('prj.list.group', { n: group.projects.length })} />}
              />
              <VStack gap={2}>
                {sortProjects(group.projects).map((project) => (
                  <ProjectRow key={project.id} project={project} />
                ))}
              </VStack>
            </VStack>
          ))
        )}
      </VStack>

      {canWrite && projects.length > 0 ? (
        <VStack gap={3}>
          <Divider />
          <SectionHead level={2} title={t('prj.list.cta.title')} hint={t('prj.list.cta.hint')} />
          <Grid columns={{ minWidth: 300, max: 2 }} gap={3}>
            <ClickableCard label={t('prj.list.cta.create')} onClick={() => setCreateOpen(true)} padding={4}>
              <HStack gap={3} align="center">
                <PlusIcon width={24} height={24} aria-hidden="true" />
                <VStack gap={0.5}>
                  <Text type="body" weight="semibold">{t('prj.list.cta.create')}</Text>
                  <Text type="supporting" color="secondary">{t('prj.list.cta.create.text')}</Text>
                </VStack>
              </HStack>
            </ClickableCard>
            <ClickableCard label={t('prj.list.cta.attach')} onClick={() => setAttachOpen(true)} padding={4}>
              <HStack gap={3} align="center">
                <FolderPlusIcon width={24} height={24} aria-hidden="true" />
                <VStack gap={0.5}>
                  <Text type="body" weight="semibold">{t('prj.list.cta.attach')}</Text>
                  <Text type="supporting" color="secondary">{t('prj.list.cta.attach.text')}</Text>
                </VStack>
              </HStack>
            </ClickableCard>
          </Grid>
        </VStack>
      ) : null}

      <CreateProjectDialog
        isOpen={createOpen}
        onOpenChange={setCreateOpen}
        onDone={(projectId) => { afterCreate(projectId); }}
      />
      <AttachWorkspaceDialog
        isOpen={attachOpen}
        onOpenChange={setAttachOpen}
        onDone={(projectId) => { afterCreate(projectId); }}
      />
    </Page>
  );
}
