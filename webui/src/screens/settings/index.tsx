/**
 * Settings screen — general / security / AI / capabilities.
 *
 * The active section lives in the hash query (`#/settings?tab=security`), so
 * every pane is deep-linkable and screenshottable without extra state.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { VStack } from '@astryxdesign/core/Layout';
import { Tab, TabList } from '@astryxdesign/core/TabList';
import { api } from '../../api/client';
import { useT } from '../../i18n';
import { navigate } from '../../shell/router';
import { applyConfig, useStore } from '../../state/store';
import { Page } from '../../ui/Page';
import { toast, toastError } from '../../ui/toast';
import type { ScreenProps } from '../types';
import { AiPane } from './AiPane';
import { CapabilitiesPane } from './CapabilitiesPane';
import { GeneralPane } from './GeneralPane';
import { SecurityPane } from './SecurityPane';
import { LoadingLine, selConfig } from './common';

const TABS = ['general', 'security', 'ai', 'capabilities'] as const;
type TabKey = (typeof TABS)[number];

function parseTab(query: URLSearchParams): TabKey {
  const value = query.get('tab');
  return (TABS as readonly string[]).includes(value || '') ? value as TabKey : 'general';
}

export default function SettingsScreen({ query }: ScreenProps): ReactNode {
  const tr = useT();
  const tab = parseTab(query);
  const config = useStore(selConfig);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadConfig = async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      applyConfig((await api.getConfig()) as Parameters<typeof applyConfig>[0]);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!config) void loadConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      applyConfig((await api.getConfig()) as Parameters<typeof applyConfig>[0]);
      toast(tr('settings.reloaded'), 'info');
    } catch (err) {
      toastError(err);
    } finally {
      setRefreshing(false);
    }
  };

  const selectTab = (value: string): void => {
    const next = (TABS as readonly string[]).includes(value) ? value as TabKey : 'general';
    navigate(next === 'general' ? 'settings' : `settings?tab=${next}`);
  };

  let pane: ReactNode;
  if (tab === 'security') pane = <SecurityPane />;
  else if (tab === 'ai') pane = <AiPane />;
  else if (tab === 'capabilities') pane = <CapabilitiesPane />;
  else pane = <GeneralPane />;

  return (
    <Page
      title={tr('settings.title')}
      description={tr('settings.sub')}
      actions={(
        <Button
          label={tr('settings.refresh')}
          size="sm"
          variant="ghost"
          isLoading={refreshing}
          onClick={() => { void refresh(); }}
        />
      )}
    >
      <VStack gap={4}>
        <TabList value={tab} onChange={selectTab} hasDivider aria-label={tr('settings.tabs.label')}>
          <Tab value="general" label={tr('settings.tab.general')} />
          <Tab value="security" label={tr('settings.tab.security')} />
          <Tab value="ai" label={tr('settings.tab.ai')} />
          <Tab value="capabilities" label={tr('settings.tab.capabilities')} />
        </TabList>

        {loading && !config ? <LoadingLine label={tr('settings.loading')} /> : null}
        {error ? (
          <Banner
            status="error"
            title={tr('settings.load_failed', {
              message: error instanceof Error ? error.message : String(error),
            })}
            endContent={(
              <Button
                label={tr('settings.retry')}
                size="sm"
                variant="secondary"
                onClick={() => { void loadConfig(); }}
              />
            )}
          />
        ) : null}
        {!config && !loading && !error ? (
          <Banner status="info" title={tr('settings.loading')} />
        ) : null}

        {config ? (
          <VStack gap={4} role="tabpanel" aria-label={tr(`settings.tab.${tab}`)}>
            {pane}
          </VStack>
        ) : null}
      </VStack>
    </Page>
  );
}
