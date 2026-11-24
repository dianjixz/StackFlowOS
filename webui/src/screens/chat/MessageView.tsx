/**
 * MessageView.tsx — one conversation turn, rendered with the Astryx chat
 * primitives (bubble, metadata, tool-call cards, markdown body).
 */

import type { ReactNode } from 'react';
import {
  ChatMessage,
  ChatMessageBubble,
  ChatMessageMetadata,
  ChatToolCalls,
} from '@astryxdesign/core/Chat';
import { Avatar } from '@astryxdesign/core/Avatar';
import { Markdown } from '@astryxdesign/core/Markdown';
import { Text } from '@astryxdesign/core/Text';
import { Timestamp } from '@astryxdesign/core/Timestamp';
import { t } from '../../i18n';
import type { ChatMessage as ChatMessageData, ToolCall } from '../../state/store';

function toolStatus(value: unknown): 'complete' | 'running' | 'error' | undefined {
  const state = String(value || '').toLowerCase();
  if (['done', 'complete', 'ok', 'success'].includes(state)) return 'complete';
  if (['error', 'failed', 'fail'].includes(state)) return 'error';
  if (['running', 'pending', 'started'].includes(state)) return 'running';
  return undefined;
}

function ToolCards({ calls }: { calls: ToolCall[] }): ReactNode {
  return (
    <ChatToolCalls
      calls={calls.map((call) => ({
        name: String(call.tool || call.name || 'tool'),
        target: call.args ? JSON.stringify(call.args) : undefined,
        status: toolStatus(call.state),
        duration: typeof call.duration_ms === 'number' ? `${call.duration_ms}ms` : undefined,
      }))}
    />
  );
}

export function MessageView({ message }: { message: ChatMessageData }): ReactNode {
  const isUser = message.role === 'user';
  const tools = Array.isArray(message.meta?.tools) ? (message.meta.tools as ToolCall[]) : [];
  return (
    <ChatMessage
      sender={isUser ? 'user' : 'assistant'}
      avatar={(
        <Avatar
          name={isUser ? t('chat.me') : t('chat.assistant')}
          size="sm"
        />
      )}
    >
      {tools.length > 0 ? <ToolCards calls={tools} /> : null}
      <ChatMessageBubble
        variant={isUser ? 'filled' : 'ghost'}
        metadata={(
          <ChatMessageMetadata
            timestamp={<Timestamp value={Math.floor(Number(message.ts) / 1000)} format="time" />}
          />
        )}
      >
        <Markdown density="compact">{String(message.text || '')}</Markdown>
      </ChatMessageBubble>
    </ChatMessage>
  );
}

/** The ephemeral assistant draft while tokens are still arriving. */
export function StreamView({ text }: { text: string }): ReactNode {
  return (
    <ChatMessage
      sender="assistant"
      avatar={<Avatar name={t('chat.assistant')} size="sm" />}
    >
      <ChatMessageBubble
        variant="ghost"
        metadata={<ChatMessageMetadata footer={t('chat.stream.live')} />}
      >
        {text
          ? <Markdown density="compact">{text}</Markdown>
          : <Text size="sm" color="secondary">{t('chat.typing')}…</Text>}
      </ChatMessageBubble>
    </ChatMessage>
  );
}

/** The "thinking" placeholder before the first token arrives. */
export function TypingView(): ReactNode {
  return (
    <ChatMessage
      sender="assistant"
      avatar={<Avatar name={t('chat.assistant')} size="sm" />}
    >
      <ChatMessageBubble variant="ghost">
        <Text size="sm" color="secondary">{t('chat.typing')}…</Text>
      </ChatMessageBubble>
    </ChatMessage>
  );
}
