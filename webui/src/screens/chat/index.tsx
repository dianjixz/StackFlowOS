/**
 * chat — the home surface: the conversation with the system operator, plus the
 * live work panel (projects · agents · events) when there is room for it.
 *
 * The chat itself is Astryx `ChatLayout` / `ChatMessage*` / `ChatComposer`; the
 * panel is a set of cards and lists that link into the full screens.
 */

import { useState, type ReactNode } from 'react';
import {
  HStack,
  Layout,
  LayoutHeader,
  LayoutPanel,
  StackItem,
  VStack,
} from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Button } from '@astryxdesign/core/Button';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import {
  ChatComposer,
  ChatComposerInput,
  ChatLayout,
  ChatMessageList,
} from '@astryxdesign/core/Chat';
import { useMediaQuery } from '@astryxdesign/core/hooks';
import { api } from '../../api/client';
import { t } from '../../i18n';
import {
  addLocalChat,
  removeChatMessage,
  useStore,
  type ChatMessage,
  type SfoState,
} from '../../state/store';
import { toastError } from '../../ui/toast';
import type { ScreenProps } from '../types';
import { MessageView, StreamView, TypingView } from './MessageView';
import { WorkPanel } from './WorkPanel';

const SUGGESTION_KEYS = ['snake', 'progress', 'approvals', 'about'] as const;

const selMessages = (s: SfoState): ChatMessage[] => s.chat;
const selStream = (s: SfoState): SfoState['stream'] => s.stream;
const selTyping = (s: SfoState): boolean => s.typing;
const selConfig = (s: SfoState): SfoState['config'] => s.config;
const selSse = (s: SfoState): SfoState['sse'] => s.sse;

export default function ChatScreen(_props: ScreenProps): ReactNode {
  const messages = useStore(selMessages);
  const stream = useStore(selStream);
  const typing = useStore(selTyping);
  const config = useStore(selConfig);
  const sse = useStore(selSse);
  const isWide = useMediaQuery('(min-width: 1180px)');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);

  const provider = String(config?.llm?.provider || '');
  const model = String(config?.llm?.model || 'OpenAI');
  const subtitle = sending
    ? t('chat.sub.sending')
    : sse === 'reconnecting' ? t('chat.sub.reconnecting')
      : provider === 'openai' ? t('chat.sub.openai', { model })
        : provider === 'mock' ? t('chat.sub.mock')
          : t('chat.sub.waiting');

  async function send(text: string): Promise<void> {
    const value = String(text || '').trim();
    if (!value || sending) return;
    setSending(true);
    setDraft('');
    const tempId = `tmp-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`;
    addLocalChat({ id: tempId, role: 'user', text: value, ts: Date.now() });
    try {
      const data = await api.sendChat(value) as { message?: ChatMessage; message_id?: string };
      removeChatMessage(tempId);
      if (data && data.message) addLocalChat(data.message);
      else if (data && data.message_id) {
        addLocalChat({ id: data.message_id, role: 'user', text: value, ts: Date.now() });
      }
    } catch (error) {
      removeChatMessage(tempId);
      toastError(error);
    } finally {
      setSending(false);
    }
  }

  const sseVariant = sse === 'live' ? 'success' : sse === 'offline' ? 'neutral' : 'warning';

  const composer = (
    <ChatComposer
      value={draft}
      onChange={setDraft}
      onSubmit={(value) => { void send(value); }}
      placeholder={t('chat.input.placeholder')}
      input={<ChatComposerInput />}
      isDisabled={sending}
      footerActions={<Text size="xsm" color="secondary">{t('chat.hint')}</Text>}
    />
  );

  const emptyState = (
    <VStack gap={3} align="center" padding={8}>
      <Heading level={3}>{t('chat.empty.title')}</Heading>
      <Text size="sm" color="secondary" justify="center">{t('chat.empty.text')}</Text>
      <HStack gap={2} wrap="wrap" justify="center">
        {SUGGESTION_KEYS.map((key) => (
          <Button
            key={key}
            label={t(`chat.suggestion.${key}.label`)}
            variant="secondary"
            size="sm"
            onClick={() => { void send(t(`chat.suggestion.${key}.text`)); }}
          />
        ))}
      </HStack>
    </VStack>
  );

  return (
    <Layout
      height="fill"
      content={(
        <VStack height="100%" gap={0}>
          <LayoutHeader hasDivider padding={3} paddingBlockEnd={3} label={t('chat.heading')}>
            <HStack justify="between" align="center" gap={3} wrap="wrap">
              <VStack gap={0.5}>
                <Heading level={2}>{t('chat.heading')}</Heading>
                <Text size="sm" color="secondary">{subtitle}</Text>
              </VStack>
              <HStack gap={1.5} align="center">
                <StatusDot
                  variant={sseVariant}
                  label={t('shell.connection')}
                  isPulsing={sse === 'live'}
                />
                <Text size="xsm" color="secondary">{t(`sse.${sse}`)}</Text>
              </HStack>
            </HStack>
          </LayoutHeader>
          <StackItem size="fill">
            <ChatLayout
              className="sfo-chat-fill"
              density="balanced"
              composer={composer}
              emptyState={emptyState}
            >
              <ChatMessageList>
                {messages.map((message) => <MessageView key={message.id} message={message} />)}
                {stream.active ? <StreamView text={stream.text} /> : null}
                {typing && !stream.active ? <TypingView /> : null}
              </ChatMessageList>
            </ChatLayout>
          </StackItem>
        </VStack>
      )}
      end={isWide ? (
        <LayoutPanel
          width={400}
          hasDivider
          padding={0}
          label={t('chat.panel.title')}
        >
          <WorkPanel />
        </LayoutPanel>
      ) : undefined}
    />
  );
}
