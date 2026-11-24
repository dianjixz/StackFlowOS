/**
 * manifest.ts — the application registry: one entry per screen.
 *
 * The shell renders navigation and routes from this table, so a screen only
 * exists once it is listed here.
 */

import { lazy, type ComponentType, type LazyExoticComponent, type SVGProps } from 'react';
import {
  BoltIcon,
  ChatBubbleLeftRightIcon,
  Cog6ToothIcon,
  CpuChipIcon,
  FolderIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UserGroupIcon,
} from '@heroicons/react/24/outline';
import type { AppKey } from './router';
import type { ScreenProps } from '../screens/types';

export type IconType = ComponentType<SVGProps<SVGSVGElement>>;

export interface ScreenDef {
  key: AppKey;
  /** i18n key of the navigation label. */
  labelKey: string;
  /** i18n key of the one-line description under the page title. */
  descriptionKey: string;
  Icon: IconType;
  Component: LazyExoticComponent<ComponentType<ScreenProps>>;
}

export const SCREENS: Record<AppKey, ScreenDef> = {
  home: {
    key: 'home',
    labelKey: 'nav.home',
    descriptionKey: 'chat.sub.waiting',
    Icon: ChatBubbleLeftRightIcon,
    Component: lazy(() => import('../screens/chat')),
  },
  projects: {
    key: 'projects',
    labelKey: 'nav.projects',
    descriptionKey: 'app.sub.projects',
    Icon: FolderIcon,
    Component: lazy(() => import('../screens/projects')),
  },
  agents: {
    key: 'agents',
    labelKey: 'nav.agents',
    descriptionKey: 'app.sub.agents',
    Icon: UserGroupIcon,
    Component: lazy(() => import('../screens/agents')),
  },
  units: {
    key: 'units',
    labelKey: 'nav.units',
    descriptionKey: 'app.sub.units',
    Icon: CpuChipIcon,
    Component: lazy(() => import('../screens/units')),
  },
  approvals: {
    key: 'approvals',
    labelKey: 'nav.approvals',
    descriptionKey: 'app.sub.approvals',
    Icon: ShieldCheckIcon,
    Component: lazy(() => import('../screens/approvals')),
  },
  events: {
    key: 'events',
    labelKey: 'nav.events',
    descriptionKey: 'app.sub.events',
    Icon: BoltIcon,
    Component: lazy(() => import('../screens/events')),
  },
  autonomy: {
    key: 'autonomy',
    labelKey: 'nav.autonomy',
    descriptionKey: 'app.sub.autonomy',
    Icon: SparklesIcon,
    Component: lazy(() => import('../screens/autonomy')),
  },
  settings: {
    key: 'settings',
    labelKey: 'nav.settings',
    descriptionKey: 'app.sub.settings',
    Icon: Cog6ToothIcon,
    Component: lazy(() => import('../screens/settings')),
  },
};

export const WORKSPACE_APPS: AppKey[] = ['home', 'projects', 'agents'];
export const PLATFORM_APPS: AppKey[] = ['units', 'approvals', 'events', 'autonomy'];
export const SYSTEM_APPS: AppKey[] = ['settings'];
