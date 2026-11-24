/**
 * theme.tsx — light / dark / system mode for the Astryx theme.
 *
 * The choice persists in localStorage and is exposed through a tiny store so
 * the top bar toggle and the `<Theme>` provider stay in sync.
 */

import { useSyncExternalStore, type ReactNode } from 'react';
import { Theme } from '@astryxdesign/core/theme';
import { InternationalizationProvider } from '@astryxdesign/core/i18n';
import { LayerProvider } from '@astryxdesign/core/Layer';
import zhCN from '@astryxdesign/core/locales/zh-CN.generated.js';
import { useLang } from '../i18n';
import { themes, defaultThemeSlug } from '../astryx-themes';

export type ThemeMode = 'light' | 'dark' | 'system';
export type ResolvedMode = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'sfo-theme';
const MODES: ThemeMode[] = ['light', 'dark', 'system'];

function readStoredMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return MODES.includes(stored as ThemeMode) ? (stored as ThemeMode) : 'system';
  } catch {
    return 'system';
  }
}

let mode: ThemeMode = typeof window === 'undefined' ? 'system' : readStoredMode();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getThemeMode(): ThemeMode {
  return mode;
}

export function setThemeMode(next: ThemeMode): ThemeMode {
  const value = MODES.includes(next) ? next : 'system';
  if (value === mode) return mode;
  mode = value;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch {
    /* ignore quota/private-mode failures */
  }
  for (const listener of [...listeners]) listener();
  return mode;
}

export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, getThemeMode, getThemeMode);
}

function systemMode(): ResolvedMode {
  if (typeof window === 'undefined' || !window.matchMedia) return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Theme provider: wires the Astryx theme, the selected color mode and the
 * component locale (so built-in strings such as "Show more" or a tool-call
 * status follow the console language instead of staying English).
 */
export function AppTheme({ children }: { children: ReactNode }): ReactNode {
  const selected = useThemeMode();
  const lang = useLang();
  const resolved = selected === 'system' ? systemMode() : selected;
  return (
    <Theme theme={themes[defaultThemeSlug]} mode={resolved}>
      <InternationalizationProvider
        locale={lang === 'en' ? 'en' : 'zh-CN'}
        messages={{ 'zh-CN': zhCN }}
      >
        <LayerProvider>{children}</LayerProvider>
      </InternationalizationProvider>
    </Theme>
  );
}
