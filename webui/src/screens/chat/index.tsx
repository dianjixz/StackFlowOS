/**
 * Chat screen — the home surface (system assistant + work panel).
 */

import type { ReactNode } from 'react';
import { t } from '../../i18n';
import { Placeholder } from '../../ui/Placeholder';
import type { ScreenProps } from '../types';

export default function ChatScreen(_props: ScreenProps): ReactNode {
  return (
    <Placeholder
      title={t('nav.home')}
      description={t('chat.sub.waiting')}
      detail={t('chat.empty.title')}
    />
  );
}
