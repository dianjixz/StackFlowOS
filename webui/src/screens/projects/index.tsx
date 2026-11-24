/**
 * Projects screen — placeholder until this wave lands.
 *
 * Contract: default export a component that takes `ScreenProps` ({id, query})
 * and renders inside the AppShell content area.  Keep every string in
 * `src/i18n/keys/projects.ts` (zh + en) and every write action behind
 * `useCanWrite()`.
 */

import type { ReactNode } from 'react';
import { t } from '../../i18n';
import { Placeholder } from '../../ui/Placeholder';
import type { ScreenProps } from '../types';

export default function ProjectsScreen({ id }: ScreenProps): ReactNode {
  return (
    <Placeholder
      title={t('app.projects')}
      description={t('app.sub.projects')}
      detail={t('app.projects') + ' · ' + (id || '')}
    />
  );
}
