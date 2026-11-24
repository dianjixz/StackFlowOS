/**
 * WorkPanel.tsx — the live "现场" column beside the conversation.
 *
 * Three glanceable blocks: projects (progress + task counts), agents on duty,
 * and the event ticker.  Everything links into its full screen.
 */

import type { ReactNode } from 'react';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Button } from '@astryxdesign/core/Button';
import { Badge } from '@astryxdesign/core/Badge';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { ClickableCard } from '@astryxdesign/core/ClickableCard';
import { List, ListItem } from '@astryxdesign/core/List';
import { Avatar } from '@astryxdesign/core/Avatar';
import { Divider } from '@astryxdesign/core/Divider';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { t } from '../../i18n';
import { useStore, type Agent, type Project, type SfoEvent, type SfoState } from '../../state/store';
import { navigate } from '../../shell/router';
import {
  agoShort,
  eventSummary,
  fmtTime,
  roleLabel,
  statusLabel,
  statusTone,
} from '../../ui/format';

const ACTIVE_AGENT_STATUSES = new Set(['working', 'waiting', 'idle', 'spawning']);
const EPHEMERAL_EVENTS = new Set(['chat.delta', 'chat.typing', 'agent.chat.delta', 'agent.chat.typing']);
const STATUS_RANK: Record<string, number> = { active: 0, review: 1, degraded: 2, planning: 3, done: 4, archived: 5 };

const selProjects = (s: SfoState): Project[] => s.projects;
const selAgents = (s: SfoState): Agent[] => s.agents;
const selEvents = (s: SfoState): SfoEvent[] => s.events;

function badgeVariant(tone: ReturnType<typeof statusTone>): 'neutral' | 'info' | 'success' | 'warning' | 'error' {
  switch (tone) {
    case 'success': return 'success';
    case 'warning': return 'warning';
    case 'danger': return 'error';
    case 'info': return 'info';
    default: return 'neutral';
  }
}

function SectionHeader({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }): ReactNode {
  return (
    <HStack justify="between" align="center" gap={2}>
      <HStack gap={1.5} align="center">
        <Text weight="semibold">{title}</Text>
        {hint ? <Text size="xsm" color="secondary">{hint}</Text> : null}
      </HStack>
      {action}
    </HStack>
  );
}

function ProjectCard({ project }: { project: Project }): ReactNode {
  const status = project.degraded ? 'degraded' : project.status;
  const counts = project.counts || {};
  const progress = Math.max(0, Math.min(100, Math.round(Number(project.progress) || 0)));
  const pending = Number(project.pending_approvals) || 0;
  return (
    <ClickableCard
      label={t('chat.panel.open_project', { name: project.name })}
      padding={3}
      onClick={() => navigate(`projects/${project.id}`)}
    >
      <VStack gap={1.5}>
        <HStack justify="between" align="center" gap={2}>
          <Text weight="semibold" maxLines={1}>{project.name || '—'}</Text>
          <Badge label={statusLabel(status)} variant={badgeVariant(statusTone(status))} />
        </HStack>
        <ProgressBar
          value={progress}
          label={project.name || project.id}
          isLabelHidden
          hasValueLabel
        />
        <HStack justify="between" align="center" gap={2}>
          <Text size="xsm" color="secondary">
            {t('status.doing')} {Number(counts.doing) || 0} · {t('status.review')} {Number(counts.review) || 0}
            {' · '}{t('status.done')} {Number(counts.done) || 0}
          </Text>
          <HStack gap={1.5} align="center">
            {pending > 0 ? <Badge label={t('chat.panel.pending', { n: pending })} variant="warning" /> : null}
            <Text size="xsm" color="secondary">{agoShort(project.updated_at)}</Text>
          </HStack>
        </HStack>
      </VStack>
    </ClickableCard>
  );
}

function AgentRow({ agent }: { agent: Agent }): ReactNode {
  const name = agent.display_name || agent.id;
  return (
    <ListItem
      key={agent.id}
      label={name}
      description={`${roleLabel(agent.role)} · ${statusLabel(agent.status)}`}
      startContent={<Avatar name={name} size="sm" />}
      endContent={<Badge label={statusLabel(agent.status)} variant={badgeVariant(statusTone(agent.status))} />}
      onClick={() => navigate(`agents/${agent.id}`)}
    />
  );
}

function EventRow({ event }: { event: SfoEvent }): ReactNode {
  const summary = eventSummary(event);
  // A bare event has no payload, so the summary is just its type — don't print
  // the same string twice in one row.
  const description = summary === event.type
    ? fmtTime(event.ts)
    : `${fmtTime(event.ts)} · ${event.type}`;
  return (
    <ListItem
      key={event.id || event.seq}
      label={summary}
      description={description}
      onClick={() => navigate('events')}
    />
  );
}

export function WorkPanel(): ReactNode {
  const projects = useStore(selProjects);
  const agents = useStore(selAgents);
  const events = useStore(selEvents);

  const sortedProjects = [...projects].sort((a, b) => {
    const rankA = STATUS_RANK[a.degraded ? 'degraded' : a.status] ?? 9;
    const rankB = STATUS_RANK[b.degraded ? 'degraded' : b.status] ?? 9;
    if (rankA !== rankB) return rankA - rankB;
    return (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0);
  });
  const activeAgents = agents.filter((agent) => ACTIVE_AGENT_STATUSES.has(agent.status));
  const recentEvents = [...events]
    .filter((event) => !EPHEMERAL_EVENTS.has(event.type))
    .sort((a, b) => (Number(b.seq) || 0) - (Number(a.seq) || 0))
    .slice(0, 12);

  return (
    <VStack gap={4} padding={4} height="100%" isScrollable>
      <HStack justify="between" align="center" gap={2}>
        <VStack gap={0.5}>
          <Heading level={2}>{t('chat.panel.title')}</Heading>
          <Text size="xsm" color="secondary">
            {t('chat.panel.project_count', { n: sortedProjects.length })}
            {' · '}
            {t('chat.panel.agent_count', { n: activeAgents.length })}
          </Text>
        </VStack>
        <Button
          label={t('chat.panel.all')}
          variant="ghost"
          size="sm"
          onClick={() => navigate('projects')}
        />
      </HStack>

      <VStack gap={2}>
        <SectionHeader
          title={t('projects.label')}
          action={(
            <Button
              label={t('chat.panel.all')}
              variant="ghost"
              size="sm"
              onClick={() => navigate('projects')}
            />
          )}
        />
        {sortedProjects.length === 0 ? (
          <EmptyState
            title={t('chat.panel.no_projects')}
            description={t('chat.panel.no_projects_hint')}
            isCompact
            headingLevel={3}
          />
        ) : (
          <VStack gap={2}>
            {sortedProjects.slice(0, 4).map((project) => (
              <ProjectCard key={project.id} project={project} />
            ))}
          </VStack>
        )}
      </VStack>

      <Divider />

      <VStack gap={2}>
        <SectionHeader
          title={t('agents.label')}
          hint={t('chat.panel.agent_count', { n: activeAgents.length })}
          action={(
            <Button
              label={t('chat.panel.all')}
              variant="ghost"
              size="sm"
              onClick={() => navigate('agents')}
            />
          )}
        />
        {activeAgents.length === 0 ? (
          <Text size="sm" color="secondary">{t('agents.empty')}</Text>
        ) : (
          <List density="compact">
            {activeAgents.slice(0, 6).map((agent) => <AgentRow key={agent.id} agent={agent} />)}
          </List>
        )}
      </VStack>

      <Divider />

      <VStack gap={2}>
        <SectionHeader
          title={t('events.label')}
          hint={t('chat.panel.event_count', { n: recentEvents.length })}
          action={(
            <Button
              label={t('chat.panel.all')}
              variant="ghost"
              size="sm"
              onClick={() => navigate('events')}
            />
          )}
        />
        {recentEvents.length === 0 ? (
          <Text size="sm" color="secondary">{t('events.empty')}</Text>
        ) : (
          <List density="compact">
            {recentEvents.map((event) => <EventRow key={event.id || event.seq} event={event} />)}
          </List>
        )}
      </VStack>
    </VStack>
  );
}
