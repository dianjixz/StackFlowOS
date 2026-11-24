/**
 * Placeholder.tsx — the "this screen exists but is not built yet" state.
 *
 * It is deliberately obvious in review: every screen landing wave replaces its
 * placeholder with real content, so a leftover one is a visible bug.
 */

import type { ReactNode } from 'react';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Page } from './Page';

export interface PlaceholderProps {
  title: string;
  description: string;
  detail: string;
}

export function Placeholder({ title, description, detail }: PlaceholderProps): ReactNode {
  return (
    <Page title={title} description={description}>
      <EmptyState title={detail} description={description} isCompact />
    </Page>
  );
}
