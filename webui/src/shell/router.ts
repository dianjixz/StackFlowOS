/**
 * router.ts — hash routing without a dependency.
 *
 * `#/`                 chat (home)
 * `#/projects`         project list
 * `#/projects/p-1`     one project
 * `#/agents`           roster
 * `#/agents/<id>`      one agent's conversation (legacy `#agent/<id>` works)
 * `#/units`, `#/approvals`, `#/events`, `#/autonomy`, `#/settings`
 *
 * A screen may carry a query string (`#/events?type=approval`), parsed into
 * `query` so deep links keep working after the redesign.
 */

import { useSyncExternalStore } from 'react';

export const APP_KEYS = [
  'home',
  'projects',
  'agents',
  'units',
  'approvals',
  'events',
  'autonomy',
  'settings',
] as const;

export type AppKey = (typeof APP_KEYS)[number];

export interface Route {
  app: AppKey;
  id: string;
  query: URLSearchParams;
  raw: string;
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  const onHash = (): void => listener();
  window.addEventListener('hashchange', onHash);
  listeners.add(listener);
  return () => {
    window.removeEventListener('hashchange', onHash);
    listeners.delete(listener);
  };
}

/** Legacy aliases: the old console used `#agent/<id>` and `#project/<id>`. */
const ALIASES: Record<string, AppKey> = {
  agent: 'agents',
  project: 'projects',
  unit: 'units',
  approval: 'approvals',
  event: 'events',
  app: '',
} as unknown as Record<string, AppKey>;

export function parseHash(hash: string): Route {
  const raw = hash || '';
  const trimmed = raw.replace(/^#\/?/, '');
  if (!trimmed) return { app: 'home', id: '', query: new URLSearchParams(), raw };
  const [path, search = ''] = trimmed.split('?');
  const [head, ...rest] = path.split('/').filter(Boolean);
  const alias = ALIASES[head];
  const app = (alias || head) as AppKey;
  const known = APP_KEYS.includes(app);
  return {
    app: known ? app : 'home',
    id: decodeURIComponent(rest.join('/') || ''),
    query: new URLSearchParams(search),
    raw,
  };
}

let cachedRoute: Route = typeof window === 'undefined'
  ? { app: 'home', id: '', query: new URLSearchParams(), raw: '' }
  : parseHash(window.location.hash);

function getRoute(): Route {
  if (typeof window === 'undefined') return cachedRoute;
  const next = parseHash(window.location.hash);
  if (next.raw !== cachedRoute.raw) cachedRoute = next;
  return cachedRoute;
}

export function useRoute(): Route {
  return useSyncExternalStore(subscribe, getRoute, getRoute);
}

/** Navigate without a full reload; `path` may be given with or without `#`. */
export function navigate(path: string): void {
  const next = path.startsWith('#') ? path : `#/${path.replace(/^\/+/, '')}`;
  if (window.location.hash === next) return;
  window.location.hash = next;
}

export function hrefFor(app: AppKey, id = '', query = ''): string {
  const base = app === 'home' ? '#/' : `#/${app}${id ? `/${encodeURIComponent(id)}` : ''}`;
  return query ? `${base}?${query}` : base;
}
