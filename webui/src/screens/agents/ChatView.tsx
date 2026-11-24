/**
 * ChatView.tsx — `#/agents/:id`, one agent's durable conversation.
 *
 * Layout: a page header (identity + thread actions), the Astryx chat surface
 * (message log + composer dock) and a context panel (project, task, approvals,
 * activity and agent metadata).  Streaming lives in `useAgentChat`.
 */

import { useMemo, useState, type ReactNode } from 'react';
import {
  Layout,
  LayoutContent,
  LayoutHeader,
  LayoutPanel,
  HStack,
  VStack,
} from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Button } from '@astryxdesign/core/Button';
import { IconButton } from '@astryxdesign/core/IconButton';
import { Badge } from '@astryxdesign/core/Badge';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Avatar } from '@astryxdesign/core/Avatar';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { Banner } from '@astryxdesign/core/Banner';
import { Card } from '@astryxdesign/core/Card';
import { Divider } from '@astryxdesign/core/Divider';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Spinner } from '@astryxdesign/core/Spinner';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { Markdown } from '@astryxdesign/core/Markdown';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { List, ListItem } from '@astryxdesign/core/List';
import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
  ChatMessageMetadata,
  ChatSystemMessage,
  ChatToolCalls,
} from '@astryxdesign/core/Chat';
import {
  ArrowLeftIcon,
  ArrowPathIcon,
  ChatBubbleLeftRightIcon,
  FolderIcon,
  ShieldCheckIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { t } from '../../i18n';
import { useStore, type Agent, type Project, type SfoState } from '../../state/store';
import { useCanWrite } from '../../session';
import { navigate } from '../../shell/router';
import {
  ago,
  clip,
  eventSummary,
  fmtDateTime,
  fmtTime,
  roleLabel,
  statusLabel,
  toneForType,
  type Tone,
} from '../../ui/format';
import type { AgentChatMessage, DraftState } from './types';
import {
  AgentAvatar,
  StatusBadge,
  agentName,
  isTerminal,
  statusBadgeVariant,
  statusDotVariant,
  toolCallItems,
} from './common';
import { useAgentChat } from './useAgentChat';
import type { AgentTask } from './types';

const ACTIVE_STATUSES: ReadonlySet<string> = new Set(['spawning', 'idle', 'working', 'waiting']);

const selSse = (s: SfoState): SfoState['sse'] => s.sse;

function dotVariantForTone(tone: Tone): 'success' | 'warning' | 'error' | 'accent' | 'neutral' {
  switch (tone) {
    case 'success':
      return 'success';
    case 'warning':
      return 'warning';
    case 'danger':
      return 'error';
    case 'info':
      return 'accent';
    default:
      return 'neutral';
  }
}

function roleHint(role: string | undefined): string {
  if (!role) return t('agents.role_hint.fallback');
  return t(`agents.role_hint.${role}`, {}, t('agents.role_hint.fallback'));
}

/* ------------------------------------------------------------------ */
/* message log                                                        */
/* ------------------------------------------------------------------ */

function MessageRow({
  message,
  agent,
  model,
}: {
  message: AgentChatMessage;
  agent: Agent | null;
  model: string;
}): ReactNode {
  if (message.role === 'system') {
    return (
      <ChatSystemMessage key={message.id}>
        {message.text || t('agents.chat.stopped')}
      </ChatSystemMessage>
    );
  }
  const isUser = message.role === 'user';
  const name = isUser ? t('agents.chat.you') : agentName(agent);
  const tools = message.meta?.tools || [];
  const metadata = (
    <ChatMessageMetadata
      timestamp={fmtTime(message.ts)}
      footer={!isUser && model ? <Text size="xsm" color="secondary">{clip(model, 28)}</Text> : undefined}
      status={message.pending ? 'sending' : undefined}
    />
  );
  return (
    <ChatMessage
      key={message.id}
      sender={isUser ? 'user' : 'assistant'}
      name={isUser ? undefined : name}
      avatar={isUser ? <Avatar name={name} size="md" /> : <AgentAvatar agent={agent} size="md" />}
    >
      {!isUser && tools.length ? (
        <ChatToolCalls
          calls={toolCallItems(tools)}
          label={t('agents.chat.tool.group', { n: tools.length })}
          defaultIsExpanded
        />
      ) : null}
      <ChatMessageBubble metadata={metadata}>
        <Markdown density="compact">{message.text}</Markdown>
      </ChatMessageBubble>
    </ChatMessage>
  );
}

function TypingRow({ agent }: { agent: Agent | null }): ReactNode {
  return (
    <ChatMessage sender="assistant" name={agentName(agent)} avatar={<AgentAvatar agent={agent} size="md" />}>
      <ChatMessageBubble>
        <HStack gap={2} align="center">
          <Spinner size="sm" aria-label={t('agents.chat.thinking')} />
          <Text color="secondary">{t('agents.chat.thinking')}</Text>
        </HStack>
      </ChatMessageBubble>
    </ChatMessage>
  );
}

function DraftRow({
  agent,
  draft,
}: {
  agent: Agent | null;
  draft: DraftState;
}): ReactNode {
  return (
    <ChatMessage sender="assistant" name={agentName(agent)} avatar={<AgentAvatar agent={agent} size="md" />}>
      {draft.tools.length ? (
        <ChatToolCalls
          calls={toolCallItems(draft.tools)}
          label={t('agents.chat.tool.group', { n: draft.tools.length })}
          defaultIsExpanded
        />
      ) : null}
      <ChatMessageBubble
        metadata={(
          <ChatMessageMetadata
            footer={<Text size="xsm" color="secondary">{t('agents.chat.streaming')}</Text>}
          />
        )}
      >
        {draft.text ? (
          <Markdown density="compact" isStreaming>{draft.text}</Markdown>
        ) : (
          <HStack gap={2} align="center">
            <Spinner size="sm" aria-label={t('agents.chat.thinking')} />
            <Text color="secondary">{t('agents.chat.thinking')}</Text>
          </HStack>
        )}
      </ChatMessageBubble>
    </ChatMessage>
  );
}

function ConversationEmpty({
  agent,
  project,
}: {
  agent: Agent | null;
  project: Project | null;
}): ReactNode {
  const status = String(agent?.status || '');
  const notes: string[] = [];
  if (isTerminal(status)) notes.push(t('agents.chat.empty.terminal'));
  else if (ACTIVE_STATUSES.has(status) && status !== 'idle') notes.push(t('agents.chat.empty.active'));
  if (!project) notes.push(t('agents.chat.empty.no_project'));
  const description = [roleHint(agent?.role), ...notes].join(' ');
  return (
    <EmptyState
      icon={agent ? <AgentAvatar agent={agent} size="xl" /> : <ChatBubbleLeftRightIcon width={40} height={40} />}
      title={t('agents.chat.empty.title', { name: agentName(agent) })}
      description={description}
      headingLevel={3}
    />
  );
}

/* ------------------------------------------------------------------ */
/* context sidebar                                                    */
/* ------------------------------------------------------------------ */

function ProjectCard({ project }: { project: Project | null }): ReactNode {
  if (!project) {
    return (
      <Card padding={3}>
        <VStack gap={1.5}>
          <Text type="label" weight="semibold">{t('agents.aside.project')}</Text>
          <Text size="sm" color="secondary">{t('agents.aside.project.none')}</Text>
        </VStack>
      </Card>
    );
  }
  const progress = Math.max(0, Math.min(100, Math.round(Number(project.progress) || 0)));
  const counts = project.counts || {};
  return (
    <Card padding={3}>
      <VStack gap={2}>
        <HStack justify="between" align="center" gap={2}>
          <Text type="label" weight="semibold">{t('agents.aside.project')}</Text>
          <StatusBadge status={project.status} />
        </HStack>
        <VStack gap={0.5}>
          <Text weight="semibold" maxLines={1}>{project.name || project.id}</Text>
          <Text type="code" size="xsm" color="secondary">{project.id}</Text>
        </VStack>
        <ProgressBar
          label={t('agents.aside.project.progress', { n: progress })}
          value={progress}
          max={100}
          isLabelHidden
          hasValueLabel
          variant={project.status === 'done' ? 'success' : 'accent'}
        />
        {project.goal ? <Text size="sm" color="secondary" maxLines={3}>{project.goal}</Text> : null}
        <HStack gap={3} wrap="wrap">
          <Text size="xsm" color="secondary">{statusLabel('todo')} <Text as="span" weight="semibold" color="primary">{Number(counts.todo) || 0}</Text></Text>
          <Text size="xsm" color="secondary">{statusLabel('doing')} <Text as="span" weight="semibold" color="primary">{Number(counts.doing) || 0}</Text></Text>
          <Text size="xsm" color="secondary">{statusLabel('review')} <Text as="span" weight="semibold" color="primary">{Number(counts.review) || 0}</Text></Text>
          <Text size="xsm" color="secondary">{statusLabel('done')} <Text as="span" weight="semibold" color="primary">{Number(counts.done) || 0}</Text></Text>
        </HStack>
        <Button
          label={t('agents.aside.project.open')}
          variant="ghost"
          size="sm"
          icon={<FolderIcon width={16} height={16} />}
          onClick={() => navigate(`projects/${project.id}`)}
        />
      </VStack>
    </Card>
  );
}

function TaskCard({
  agent,
  tasks,
}: {
  agent: Agent | null;
  tasks: AgentTask[];
}): ReactNode {
  const current = agent?.task_id ? tasks.find((task) => task.id === agent.task_id) || null : null;
  return (
    <Card padding={3}>
      <VStack gap={2}>
        <Text type="label" weight="semibold">{t('agents.aside.task')}</Text>
        {current ? (
          <VStack gap={1}>
            <HStack gap={2} align="center">
              <Badge label={statusLabel(current.status)} variant={statusBadgeVariant(current.status)} />
              <Text size="sm" weight="semibold">{current.title || current.id}</Text>
            </HStack>
            <Text type="code" size="xsm" color="secondary">{current.id}</Text>
          </VStack>
        ) : (
          <Text size="sm" color="secondary">
            {agent?.task_id
              ? t('agents.aside.task.missing', { id: agent.task_id })
              : t('agents.aside.task.none')}
          </Text>
        )}
        {tasks.length ? (
          <VStack gap={1}>
            <Divider />
            <Text size="xsm" color="secondary">{t('agents.aside.task.list')}</Text>
            <List density="compact" hasDividers>
              {tasks.slice(0, 6).map((task) => (
                <ListItem
                  key={task.id}
                  label={clip(task.title || task.id, 48)}
                  startContent={<StatusDot variant={statusDotVariant(task.status)} label={statusLabel(task.status)} />}
                  endContent={<Text size="xsm" color="secondary">{statusLabel(task.status)}</Text>}
                />
              ))}
            </List>
          </VStack>
        ) : null}
      </VStack>
    </Card>
  );
}

function ApprovalsCard({
  pending,
}: {
  pending: number;
}): ReactNode {
  return (
    <Card padding={3}>
      <VStack gap={2}>
        <HStack justify="between" align="center">
          <Text type="label" weight="semibold">{t('agents.aside.approvals')}</Text>
          <Badge label={String(pending)} variant={pending ? 'warning' : 'neutral'} />
        </HStack>
        <Text size="sm" color="secondary">
          {pending ? t('agents.aside.approvals.pending', { n: pending }) : t('agents.aside.approvals.none')}
        </Text>
        <Button
          label={t('agents.aside.approvals.open')}
          variant="ghost"
          size="sm"
          icon={<ShieldCheckIcon width={16} height={16} />}
          onClick={() => navigate('approvals')}
        />
      </VStack>
    </Card>
  );
}

function ActivityCard({ activity }: { activity: SfoState['events'] }): ReactNode {
  return (
    <Card padding={3}>
      <VStack gap={2}>
        <Text type="label" weight="semibold">{t('agents.aside.activity')}</Text>
        {activity.length ? (
          <List density="compact" hasDividers>
            {activity.map((event) => (
              <ListItem
                key={`${event.id || event.seq}`}
                label={clip(eventSummary(event), 72)}
                description={`${fmtTime(event.ts)} · ${event.type}`}
                startContent={<StatusDot variant={dotVariantForTone(toneForType(event.type))} label={event.type} />}
              />
            ))}
          </List>
        ) : (
          <Text size="sm" color="secondary">{t('agents.aside.activity.none')}</Text>
        )}
      </VStack>
    </Card>
  );
}

function MetaCard({
  agent,
  provider,
  model,
  workspace,
}: {
  agent: Agent | null;
  provider: string;
  model: string;
  workspace: string;
}): ReactNode {
  const stats = agent?.stats || {};
  const statsText = Object.entries(stats)
    .map(([key, value]) => `${key}: ${value}`)
    .join(' · ');
  return (
    <Card padding={3}>
      <VStack gap={2}>
        <Text type="label" weight="semibold">{t('agents.aside.agent')}</Text>
        <MetadataList label={{ position: 'start', width: 82 }}>
        <MetadataListItem label={t('agents.aside.field.id')}>
          <Text type="code" size="xsm">{agent?.id || '—'}</Text>
        </MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.role')}>{roleLabel(agent?.role)}</MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.status')}>{statusLabel(agent?.status)}</MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.model')}>{clip(model || agent?.model || '—', 28)}</MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.provider')}>{provider || '—'}</MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.unit')}>{agent?.unit_id || '—'}</MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.heartbeat')}>
          {agent?.last_seen ? ago(agent.last_seen) : '—'}
        </MetadataListItem>
        <MetadataListItem label={t('agents.aside.field.created')}>
          {agent?.created_at ? fmtDateTime(agent.created_at) : '—'}
        </MetadataListItem>
        {workspace ? (
          <MetadataListItem label={t('agents.aside.field.workspace')}>
            <Text type="code" size="xsm" maxLines={2}>{clip(workspace, 90)}</Text>
          </MetadataListItem>
        ) : null}
        <MetadataListItem label={t('agents.aside.field.stats')}>
          {statsText || t('agents.aside.stats.none')}
        </MetadataListItem>
        </MetadataList>
        {agent?.error ? (
          <Banner status="error" title={t('agents.aside.field.error')} description={agent.error} />
        ) : null}
      </VStack>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* screen                                                             */
/* ------------------------------------------------------------------ */

export function AgentChatScreen({ agentId }: { agentId: string }): ReactNode {
  const canWrite = useCanWrite();
  const sse = useStore(selSse);
  const chat = useAgentChat(agentId);
  const [composerValue, setComposerValue] = useState('');
  const [sending, setSending] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [clearBusy, setClearBusy] = useState(false);
  const [killBusy, setKillBusy] = useState(false);

  const agent = chat.liveAgent;
  const project = chat.liveProject;
  const name = agentName(agent);
  const terminal = isTerminal(agent?.status);

  const hint = useMemo(() => {
    if (sse === 'reconnecting') return t('agents.chat.reconnecting');
    return t('agents.chat.hint');
  }, [sse]);

  const submit = async (raw: string): Promise<void> => {
    const text = raw.trim();
    if (!text || chat.busy || sending) return;
    setSending(true);
    const ok = await chat.send(text);
    setSending(false);
    if (ok) setComposerValue('');
    else setComposerValue(text);
  };

  const composer: ReactNode = canWrite ? (
    <ChatComposer
      value={composerValue}
      onChange={setComposerValue}
      onSubmit={(value) => { void submit(value); }}
      onStop={() => { void chat.cancel(); }}
      isStopShown={chat.busy}
      placeholder={chat.busy ? t('agents.chat.placeholder_busy') : t('agents.chat.placeholder')}
      isDisabled={sending}
      status={sse === 'reconnecting' ? { type: 'warning', message: t('agents.chat.reconnecting') } : undefined}
      footerActions={<Text size="xsm" color="secondary">{hint}</Text>}
    />
  ) : (
    <Banner
      status="info"
      container="section"
      title={t('agents.chat.viewer.title')}
      description={t('agents.chat.viewer.text')}
    />
  );

  const log = chat.messages.length ? (
    <ChatMessageList gap={3} isStreaming={chat.busy}>
      {chat.messages.map((message) => (
        <MessageRow key={message.id} message={message} agent={agent} model={chat.model} />
      ))}
      {chat.draft ? <DraftRow key="draft" agent={agent} draft={chat.draft} /> : null}
      {chat.busy && !chat.draft ? <TypingRow key="typing" agent={agent} /> : null}
    </ChatMessageList>
  ) : (
    <ChatMessageList
      gap={3}
      isStreaming={chat.busy}
      emptyState={chat.loading
        ? (
          <VStack gap={2} align="center" padding={6}>
            <Spinner size="md" />
            <Text color="secondary">{t('agents.chat.loading')}</Text>
          </VStack>
        )
        : chat.error && !agent
          ? (
            <EmptyState
              title={t('agents.chat.load_error')}
              description={chat.error}
              headingLevel={3}
              actions={(
                <Button
                  label={t('agents.chat.retry')}
                  variant="primary"
                  icon={<ArrowPathIcon width={16} height={16} />}
                  onClick={() => { void chat.reload(); }}
                />
              )}
            />
          )
          : <ConversationEmpty agent={agent} project={project} />}
    >
      {null}
    </ChatMessageList>
  );

  const header = (
    <LayoutHeader hasDivider padding={3} label={name}>
      <HStack justify="between" align="center" gap={3} wrap="wrap">
        <HStack gap={2} align="center">
          <IconButton
            label={t('agents.chat.back')}
            variant="ghost"
            size="sm"
            icon={<ArrowLeftIcon width={18} height={18} />}
            onClick={() => navigate('agents')}
          />
          <AgentAvatar agent={agent} size="lg" />
          <VStack gap={0.5}>
            <HStack gap={2} align="center" wrap="wrap">
              <Heading level={2}>{name}</Heading>
              {agent ? <StatusBadge status={agent.status} /> : null}
            </HStack>
            <HStack gap={2} align="center" wrap="wrap">
              <Text size="xsm" color="secondary">{roleLabel(agent?.role)} · {agentId}</Text>
              {project ? <Text size="xsm" color="secondary">· {project.name || project.id}</Text> : null}
              {chat.model ? <Badge label={clip(chat.model, 24)} variant="neutral" /> : null}
            </HStack>
          </VStack>
        </HStack>
        <HStack gap={2} align="center">
          <IconButton
            label={t('agents.chat.refresh')}
            variant="ghost"
            size="sm"
            isLoading={chat.refreshing}
            icon={<ArrowPathIcon width={18} height={18} />}
            onClick={() => { void chat.reload(); }}
          />
          {canWrite ? (
            <Button
              label={t('agents.chat.clear')}
              variant="ghost"
              size="sm"
              icon={<TrashIcon width={16} height={16} />}
              onClick={() => setClearOpen(true)}
            />
          ) : null}
          {canWrite && !terminal ? (
            <Button
              label={t('agents.chat.kill')}
              variant="destructive"
              size="sm"
              icon={<XMarkIcon width={16} height={16} />}
              onClick={() => setKillOpen(true)}
            />
          ) : null}
        </HStack>
      </HStack>
    </LayoutHeader>
  );

  const content = (
    <VStack height="100%" gap={0}>
      {chat.pendingApprovals.length ? (
        <Banner
          status="warning"
          container="section"
          title={t('agents.chat.approvals.banner', {
            n: chat.pendingApprovals.length,
            caps: chat.pendingApprovals
              .slice(0, 2)
              .map((item) => item.capability)
              .join('、'),
          })}
          endContent={(
            <Button label={t('agents.chat.approvals.go')} variant="ghost" size="sm" onClick={() => navigate('approvals')} />
          )}
        />
      ) : null}
      <ChatLayout density="balanced" composer={composer}>
        {log}
      </ChatLayout>
    </VStack>
  );

  const aside = (
    <VStack gap={3}>
      <ProjectCard project={project} />
      <TaskCard agent={agent} tasks={chat.tasks} />
      <ApprovalsCard pending={chat.pendingApprovals.length} />
      <ActivityCard activity={chat.activity} />
      <MetaCard agent={agent} provider={chat.provider} model={chat.model || agent?.model || ''} workspace={project?.workspace || ''} />
    </VStack>
  );

  return (
    <>
      <Layout
        height="fill"
        header={header}
        content={<LayoutContent padding={0} isScrollable={false}>{content}</LayoutContent>}
        end={(
          <LayoutPanel width={340} hasDivider isScrollable padding={3} label={t('agents.aside.label')}>
            {aside}
          </LayoutPanel>
        )}
      />
      <AlertDialog
        isOpen={clearOpen}
        onOpenChange={setClearOpen}
        title={t('agents.chat.clear.title')}
        description={t('agents.chat.clear.text')}
        actionLabel={t('agents.chat.clear.confirm')}
        cancelLabel={t('agents.kill.cancel')}
        actionVariant="destructive"
        isActionLoading={clearBusy}
        onAction={() => {
          setClearBusy(true);
          void chat.clear().then((ok) => {
            setClearBusy(false);
            if (ok) setClearOpen(false);
          });
        }}
      />
      <AlertDialog
        isOpen={killOpen}
        onOpenChange={setKillOpen}
        title={t('agents.kill.title', { name })}
        description={t('agents.kill.text')}
        actionLabel={t('agents.kill.confirm')}
        cancelLabel={t('agents.kill.cancel')}
        actionVariant="destructive"
        isActionLoading={killBusy}
        onAction={() => {
          setKillBusy(true);
          void chat.kill().then((ok) => {
            setKillBusy(false);
            if (ok) setKillOpen(false);
          });
        }}
      />
    </>
  );
}
