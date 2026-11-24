/**
 * i18n/index.ts — tiny, dependency-free internationalisation for the console.
 *
 * Rules:
 *   * zh is the source of truth: a key missing from `en` falls back to the zh
 *     string (and then to the caller-supplied fallback / the key itself).
 *   * Screens keep their keys in `src/i18n/keys/<screen>.ts`; the glob below
 *     merges them, so parallel work never edits one shared dictionary.
 *   * The choice persists in localStorage and is published through a store, so
 *     every component re-renders on a language switch.
 *   * Never build markup from dictionary strings — only text.
 */

import { useCallback, useSyncExternalStore } from 'react';
import dict from './dict.json';
import { en as extraEn, zh as extraZh } from './extra';

export type Lang = 'zh' | 'en';
export type Dict = Record<string, string>;
export type DictPair = { zh: Dict; en: Dict };

export const LANGS: Lang[] = ['zh', 'en'];
export const LANG_STORAGE_KEY = 'sfo-lang';

interface DictModule {
  zh?: Dict;
  en?: Dict;
}

const modules = import.meta.glob<DictModule>('./keys/*.ts', { eager: true });

function merge(base: DictPair, ...layers: DictPair[]): DictPair {
  const zh: Dict = { ...base.zh };
  const en: Dict = { ...base.en };
  for (const layer of layers) {
    Object.assign(zh, layer.zh);
    Object.assign(en, layer.en);
  }
  return { zh, en };
}

const extraPairs: DictPair[] = [{ zh: extraZh, en: extraEn }];
const screenPairs: DictPair[] = Object.values(modules)
  .map((mod) => ({ zh: mod.zh ?? {}, en: mod.en ?? {} }));

export const DICTS: Record<Lang, Dict> = {
  zh: merge({ zh: dict.zh as Dict, en: dict.en as Dict }, ...screenPairs, ...extraPairs).zh,
  en: merge({ zh: dict.zh as Dict, en: dict.en as Dict }, ...screenPairs, ...extraPairs).en,
};

const listeners = new Set<() => void>();

function readStoredLang(): Lang {
  try {
    const stored = window.localStorage.getItem(LANG_STORAGE_KEY);
    return LANGS.includes(stored as Lang) ? (stored as Lang) : 'zh';
  } catch {
    return 'zh';
  }
}

let currentLang: Lang = typeof window === 'undefined' ? 'zh' : readStoredLang();

export function getLang(): Lang {
  return currentLang;
}

export function hasKey(key: string, lang: Lang = currentLang): boolean {
  return Boolean(DICTS[lang] && key in DICTS[lang]);
}

/**
 * Translate `key` with `{param}` interpolation.
 * Lookup order: active language → zh → explicit fallback → key.
 */
export function t(key: string, params: Record<string, string | number> = {}, fallback: string | null = null): string {
  const active = DICTS[currentLang] || DICTS.zh;
  let text = active[key];
  if (text === undefined) text = DICTS.zh[key];
  if (text === undefined) text = fallback !== null && fallback !== undefined ? fallback : key;
  return String(text).replace(/\{(\w+)\}/g, (match, name: string) => (
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
  ));
}

export function setLang(lang: Lang): Lang {
  const next = LANGS.includes(lang) ? lang : 'zh';
  if (next === currentLang) return next;
  currentLang = next;
  try {
    window.localStorage.setItem(LANG_STORAGE_KEY, next);
  } catch {
    /* ignore quota/private-mode failures */
  }
  document.documentElement.lang = next === 'en' ? 'en' : 'zh-CN';
  for (const listener of [...listeners]) listener();
  return next;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Reactive translation helper: re-renders the caller when the language changes. */
export function useT(): (key: string, params?: Record<string, string | number>, fallback?: string | null) => string {
  useLang();
  return useCallback(t, []);
}

export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang, getLang);
}

/** Bootstrap: set the document language once at startup. */
export function initI18n(): Lang {
  document.documentElement.lang = currentLang === 'en' ? 'en' : 'zh-CN';
  return currentLang;
}
