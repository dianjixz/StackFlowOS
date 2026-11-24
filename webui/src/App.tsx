/**
 * App.tsx — the application shell: navigation, routing, connection state and
 * the two blocking screens (backend offline, session expired).
 *
 * Layout: Astryx `AppShell` owns the window; `SideNav` carries the product
 * brand and every destination; `TopNav` is the per-page bar (title, transport,
 * counters, approvals, identity, theme and language).  Screens render inside
 * the shell's content area and own their own `Layout` frame.
 */

import { Suspense, useEffect, useState, type ReactNode } from 'react';
import { AppShell } from '@astryxdesign/core/AppShell';
import { SideNav, SideNavItem, SideNavSection } from '@astryxdesign/core/SideNav';
import { TopNav } from '@astryxdesign/core/TopNav';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Button } from '@astryxdesign/core/Button';
import { IconButton } from '@astryxdesign/core/IconButton';
import { Badge } from '@astryxdesign/core/Badge';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Card } from '@astryxdesign/core/Card';
import { Spinner } from '@astryxdesign/core/Spinner';
import { DropdownMenu, DropdownMenuItem } from '@astryxdesign/core/DropdownMenu';
import {
  ComputerDesktopIcon,
  MoonIcon,
  ShieldCheckIcon,
  SunIcon,
} from '@heroicons/react/24/outline';
import { t, useLang, useT, setLang } from './i18n';
import { loadState, useStore, type SfoState } from './state/store';
import { bootStore } from './state/store';
import { canWrite, initAuthGate, loadSessions, signOut, useAuth, useBlocked } from './session';
import { fmtClock } from './ui/format';
import { ToastHost } from './ui/toast';
import { BrandMark } from './shell/BrandMark';
import { SCREENS, PLATFORM_APPS, SYSTEM_APPS, WORKSPACE_APPS } from './shell/manifest';
import { navigate, useRoute, type AppKey, type Route } from './shell/router';
import { setThemeMode, useThemeMode, type ThemeMode } from './shell/theme';

/* stable selectors (never recreate them per render) */
const selLoaded = (s: SfoState): boolean => s.loaded;
const selLoading = (s: SfoState): boolean => s.loading;
const selError = (s: SfoState): unknown => s.error;
const selSse = (s: SfoState): SfoState['sse'] => s.sse;
const selSys = (s: SfoState): SfoState['sys'] => s.sys;
const selStats = (s: SfoState): SfoState['stats'] => s.stats;
const selPending = (s: SfoState): number => s.approvals.filter((a) => a.status === 'pending').length;

function FullPageSpinner(): ReactNode {
  return (
    <HStack width="100%" height="100%" align="center" justify="center">
      <Spinner size="md" />
    </HStack>
  );
}

/* ------------------------------------------------------------------ */
/* top bar                                                            */
/* ------------------------------------------------------------------ */

function ConnectionChip(): ReactNode {
  const sse = useStore(selSse);
  const variant = sse === 'live' ? 'success' : sse === 'offline' ? 'neutral' : 'warning';
  return (
    <HStack gap={1.5} align="center">
      <StatusDot
        variant={variant}
        label={t('shell.connection')}
        isPulsing={sse === 'live'}
        tooltip={`${t('shell.connection')} · ${t(`sse.${sse}`)}`}
      />
      <Text size="xsm" color="secondary">{t(`sse.${sse}`)}</Text>
    </HStack>
  );
}

function Clock(): ReactNode {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <Text size="xsm" color="secondary" hasTabularNumbers>
      {fmtClock(now)}
    </Text>
  );
}

function ThemeToggle(): ReactNode {
  const mode = useThemeMode();
  const next = (): ThemeMode => (mode === 'light' ? 'dark' : mode === 'dark' ? 'system' : 'light');
  const label = mode === 'light'
    ? t('shell.theme_light')
    : mode === 'dark' ? t('shell.theme_dark') : t('shell.theme_system');
  return (
    <IconButton
      label={`${t('shell.theme')}: ${label}`}
      variant="ghost"
      size="sm"
      icon={mode === 'light' ? <SunIcon /> : mode === 'dark' ? <MoonIcon /> : <ComputerDesktopIcon />}
      onClick={() => setThemeMode(next())}
    />
  );
}

function LangToggle(): ReactNode {
  const lang = useLang();
  return (
    <Button
      label={lang === 'zh' ? 'EN' : '中'}
      variant="ghost"
      size="sm"
      tooltip={t('shell.lang')}
      onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
    />
  );
}

function SessionMenu(): ReactNode {
  const auth = useAuth();
  const label = auth.known
    ? `${auth.user || t('shell.session')}${auth.role ? ` · ${auth.role}` : ''}`
    : t('shell.session');
  return (
    <DropdownMenu
      button={{
        label,
        variant: 'ghost',
        size: 'sm',
        endContent: auth.known ? <Badge label={auth.role} variant={auth.role === 'viewer' ? 'warning' : 'neutral'} /> : undefined,
      }}
    >
      <DropdownMenuItem label={t('nav.settings')} onClick={() => navigate('settings')} />
      <DropdownMenuItem label={t('shell.logout')} onClick={() => { void signOut(); }} />
    </DropdownMenu>
  );
}

function TopBar(): ReactNode {
  const sys = useStore(selSys);
  const stats = useStore(selStats);
  const pending = useStore(selPending);
  const auth = useAuth();
  const transport = String(sys?.transport || '—').toUpperCase();
  const online = Number(stats?.units?.online ?? 0);
  const leases = Number(stats?.leases ?? 0);

  return (
    <TopNav
      label={t('shell.main_nav')}
      heading={(
        <HStack gap={2} align="center">
          <BrandMark />
          <Heading level={1} accessibilityLevel={2}>StackFlow OS</Heading>
          {sys?.host ? <Text size="xsm" color="secondary">{sys.host}</Text> : null}
        </HStack>
      )}
      endContent={(
        <HStack gap={3} align="center" wrap="wrap">
          <Badge label={transport} variant="neutral" />
          <ConnectionChip />
          <Text size="xsm" color="secondary">
            {t('shell.units')} <Text as="span" weight="semibold" color="primary">{online}</Text>
          </Text>
          <Text size="xsm" color="secondary">
            {t('shell.leases')} <Text as="span" weight="semibold" color="primary">{leases}</Text>
          </Text>
          <Button
            label={String(pending)}
            icon={<ShieldCheckIcon />}
            variant={pending > 0 ? 'secondary' : 'ghost'}
            size="sm"
            tooltip={t('shell.approvals')}
            onClick={() => navigate('approvals')}
          />
          {auth.known && !canWrite() ? <Badge label={t('shell.viewer_hint')} variant="warning" /> : null}
          <LangToggle />
          <ThemeToggle />
          <SessionMenu />
          <Clock />
        </HStack>
      )}
    />
  );
}

/* ------------------------------------------------------------------ */
/* side bar                                                           */
/* ------------------------------------------------------------------ */

function navItem(app: AppKey, route: Route, endContent?: ReactNode): ReactNode {
  const def = SCREENS[app];
  const Icon = def.Icon;
  return (
    <SideNavItem
      key={app}
      label={t(def.labelKey)}
      icon={<Icon width={20} height={20} aria-hidden="true" />}
      isSelected={route.app === app}
      endContent={endContent}
      onClick={() => navigate(app === 'home' ? '' : app)}
    />
  );
}

function SideNavBar({ route }: { route: Route }): ReactNode {
  const pending = useStore(selPending);
  return (
    <SideNav collapsible topContent={null}>
      <SideNavSection title={t('nav.workspace')}>
        {WORKSPACE_APPS.map((app) => navItem(app, route))}
      </SideNavSection>
      <SideNavSection title={t('nav.platform')}>
        {PLATFORM_APPS.map((app) => navItem(
          app,
          route,
          app === 'approvals' && pending > 0
            ? <Badge label={String(pending)} variant="warning" />
            : undefined,
        ))}
      </SideNavSection>
      <SideNavSection title={t('nav.system')}>
        {SYSTEM_APPS.map((app) => navItem(app, route))}
      </SideNavSection>
    </SideNav>
  );
}

/* ------------------------------------------------------------------ */
/* blocking screens                                                   */
/* ------------------------------------------------------------------ */

function OfflineScreen({ error }: { error: unknown }): ReactNode {
  const [busy, setBusy] = useState(false);
  const message = error instanceof Error ? error.message : String(error || '');
  return (
    <VStack width="100%" height="100%" align="center" justify="center" padding={6}>
      <Card padding={6} maxWidth={560}>
        <EmptyState
          title={t('shell.offline_title')}
          description={`${t('shell.offline_text')}${message ? `\n${message}` : ''}`}
          headingLevel={2}
          actions={(
            <Button
              label={t('shell.retry')}
              variant="primary"
              isLoading={busy}
              onClick={() => {
                setBusy(true);
                loadState().finally(() => setBusy(false));
              }}
            />
          )}
        />
      </Card>
    </VStack>
  );
}

function AuthGate(): ReactNode {
  return (
    <div className="sfo-gate" role="dialog" aria-modal="true" aria-label={t('login.title')}>
      <Card padding={6} maxWidth={560}>
        <EmptyState
          title={t('login.title')}
          description={t('login.text')}
          headingLevel={2}
          actions={(
            <Button label={t('login.retry')} variant="primary" onClick={() => window.location.reload()} />
          )}
        />
        <Text size="xsm" color="secondary">{t('login.hint')}</Text>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* shell                                                              */
/* ------------------------------------------------------------------ */

export default function App(): ReactNode {
  const route = useRoute();
  const loaded = useStore(selLoaded);
  const loading = useStore(selLoading);
  const error = useStore(selError);
  const blocked = useBlocked();
  useT(); // re-render every string on a language switch

  useEffect(() => {
    void bootStore();
    void loadSessions();
    return initAuthGate();
  }, []);

  useEffect(() => {
    const base = 'StackFlow OS';
    const app = route.app === 'home' ? SCREENS.home : SCREENS[route.app];
    document.title = `${t(app.labelKey)} · ${base}`;
  }, [route.app]);

  const Screen = (SCREENS[route.app] ?? SCREENS.home).Component;

  let content: ReactNode;
  if (!loaded && error && !loading) {
    content = <OfflineScreen error={error} />;
  } else if (!loaded) {
    content = <FullPageSpinner />;
  } else {
    content = (
      <Suspense fallback={<FullPageSpinner />}>
        <Screen id={route.id} query={route.query} />
      </Suspense>
    );
  }

  return (
    <>
      <AppShell
        variant="wash"
        contentPadding={0}
        topNav={<TopBar />}
        sideNav={<SideNavBar route={route} />}
      >
        {content}
      </AppShell>
      <ToastHost />
      {blocked ? <AuthGate /> : null}
    </>
  );
}
