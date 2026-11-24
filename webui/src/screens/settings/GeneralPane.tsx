/**
 * GeneralPane — system information, interface language, theme value and the
 * current (read-only) identity.
 */

import type { ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Text } from '@astryxdesign/core/Text';
import { setLang, t, useLang, useT, type Lang } from '../../i18n';
import { useAuth, useCanWrite } from '../../session';
import { useThemeMode } from '../../shell/theme';
import { useStore } from '../../state/store';
import { fmtDateTime, fmtUptime, roleLabel } from '../../ui/format';
import { toast } from '../../ui/toast';
import { PaneSection, selConfig, selStats, selSys } from './common';

function languageName(lang: Lang): string {
  return lang === 'zh' ? t('settings.general.lang.zh') : t('settings.general.lang.en');
}

export function GeneralPane(): ReactNode {
  const tr = useT();
  const lang = useLang();
  const theme = useThemeMode();
  const sys = useStore(selSys);
  const stats = useStore(selStats);
  const config = useStore(selConfig);
  const auth = useAuth();
  const canWrite = useCanWrite();

  const transport = String(sys?.transport || config?.transport?.mode || '—').toUpperCase();
  const webPort = sys?.web_port ?? config?.web?.port ?? '—';
  const uptime = stats?.uptime_s ?? sys?.stats?.uptime_s;
  const themeLabel = theme === 'light'
    ? tr('shell.theme_light')
    : theme === 'dark' ? tr('shell.theme_dark') : tr('shell.theme_system');

  return (
    <VStack gap={4}>
      <PaneSection
        title={tr('settings.general.system.title')}
        hint={tr('settings.general.system.hint')}
      >
        <MetadataList columns={2} label={{ position: 'top' }}>
          <MetadataListItem label={tr('settings.general.version')}>
            <Text size="sm" hasTabularNumbers>{String(sys?.version || '—')}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.host')}>
            <Text size="sm">{String(sys?.host || '—')}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.transport')}>
            <HStack gap={2} align="center">
              <Badge label={transport} variant="neutral" />
              <Text size="xsm" color="secondary">{String(config?.transport?.mode || '')}</Text>
            </HStack>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.web_port')}>
            <Text size="sm" hasTabularNumbers>{String(webPort)}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.uptime')}>
            <Text size="sm">{fmtUptime(uptime)}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.started')}>
            <Text size="sm">{fmtDateTime(sys?.started_at)}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.pid')}>
            <Text size="sm" hasTabularNumbers>{String(sys?.pid ?? '—')}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.last_seq')}>
            <Text size="sm" hasTabularNumbers>{String(sys?.last_seq ?? '—')}</Text>
          </MetadataListItem>
          <MetadataListItem label={tr('settings.general.projects_agents')}>
            <Text size="sm" hasTabularNumbers>
              {`${Number(stats?.projects ?? 0)} / ${Number(stats?.agents?.total ?? 0)}`}
            </Text>
          </MetadataListItem>
        </MetadataList>
      </PaneSection>

      <PaneSection title={tr('settings.general.lang.title')} hint={tr('settings.general.lang.hint')}>
        <SegmentedControl
          label={tr('settings.general.lang.title')}
          value={lang}
          onChange={(value) => {
            const next: Lang = value === 'en' ? 'en' : 'zh';
            if (next === lang) return;
            setLang(next);
            toast(t('settings.general.lang.changed', { lang: languageName(next) }), 'info');
          }}
        >
          <SegmentedControlItem value="zh" label={tr('settings.general.lang.zh')} />
          <SegmentedControlItem value="en" label={tr('settings.general.lang.en')} />
        </SegmentedControl>
      </PaneSection>

      <PaneSection title={tr('settings.general.theme.title')} hint={tr('settings.general.theme.hint')}>
        <HStack gap={2} align="center">
          <Text size="sm" color="secondary">{tr('settings.general.theme.current')}</Text>
          <Badge label={themeLabel} variant="neutral" />
        </HStack>
      </PaneSection>

      <PaneSection
        title={tr('settings.general.identity.title')}
        hint={tr('settings.general.identity.hint')}
      >
        <VStack gap={3}>
          {auth.known ? (
            <MetadataList columns={2} label={{ position: 'top' }}>
              <MetadataListItem label={tr('settings.general.identity.user')}>
                <Text size="sm">{auth.user || tr('settings.unknown')}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.general.identity.role')}>
                <Badge
                  label={roleLabel(auth.role) || tr('settings.unknown')}
                  variant={auth.role === 'viewer' ? 'warning' : 'success'}
                />
              </MetadataListItem>
              <MetadataListItem label={tr('settings.general.identity.session')}>
                {auth.currentId
                  ? <Badge label={tr('settings.security.sessions.current')} variant="success" />
                  : <Text size="sm">{tr('settings.none')}</Text>}
              </MetadataListItem>
              <MetadataListItem label={tr('settings.general.identity.status')}>
                <Text size="sm">{tr('settings.general.identity.known')}</Text>
              </MetadataListItem>
            </MetadataList>
          ) : (
            <Banner status="info" title={tr('settings.general.identity.unknown')} />
          )}
          {!canWrite ? <Banner status="warning" title={tr('settings.general.identity.viewer')} /> : null}
        </VStack>
      </PaneSection>
    </VStack>
  );
}
