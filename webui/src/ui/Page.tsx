/**
 * Page.tsx — the standard full-page frame for a screen.
 *
 * One header strip (title + description + actions) and one scrollable content
 * area, so every screen in the console has the same rhythm.
 */

import type { ReactNode } from 'react';
import { HStack, Layout, LayoutContent, LayoutHeader, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';

export interface PageProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  /** Set false when the screen draws its own inner layout (e.g. a split view). */
  isPadded?: boolean;
}

export function Page({ title, description, actions, children, isPadded = true }: PageProps): ReactNode {
  return (
    <Layout
      height="fill"
      header={(
        <LayoutHeader hasDivider padding={4} label={title}>
          <HStack justify="between" align="center" gap={3} wrap="wrap">
            <VStack gap={0.5}>
              <Heading level={2}>{title}</Heading>
              {description ? <Text size="sm" color="secondary">{description}</Text> : null}
            </VStack>
            {actions ? <HStack gap={2} align="center" wrap="wrap">{actions}</HStack> : null}
          </HStack>
        </LayoutHeader>
      )}
      content={<LayoutContent padding={isPadded ? 4 : 0}>{children}</LayoutContent>}
    />
  );
}
