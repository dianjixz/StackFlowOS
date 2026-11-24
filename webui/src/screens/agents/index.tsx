/**
 * Agents screen — roster (`#/agents`) and one agent's conversation
 * (`#/agents/:id`; the legacy `#agent/<id>` link is normalised by the router).
 *
 * Contract: default export a component that takes `ScreenProps` ({id, query})
 * and renders inside the AppShell content area.  Every string lives in
 * `src/i18n/keys/agents.ts`; every write action is gated by `useCanWrite()`.
 */

import type { ReactNode } from 'react';
import type { ScreenProps } from '../types';
import { AgentChatScreen } from './ChatView';
import { AgentRosterScreen } from './Roster';

export default function AgentsScreen({ id }: ScreenProps): ReactNode {
  return id ? <AgentChatScreen agentId={id} /> : <AgentRosterScreen />;
}
