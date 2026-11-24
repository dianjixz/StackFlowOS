/**
 * toast.ts — one global way to show transient feedback.
 *
 * Screens call `toast()` / `toastError()` from anywhere (event handlers,
 * async loaders).  `ToastHost` installs the Astryx `useToast` implementation
 * once at the app root, so no screen has to thread a hook through its props.
 */

import { useToast } from '@astryxdesign/core/Toast';
import { useEffect } from 'react';
import { ApiError, isForbidden, isUnsupported } from '../api/client';
import { t } from '../i18n';

export type ToastKind = 'info' | 'error';

type ShowToast = (options: { body: string; type: ToastKind; autoHideDuration?: number }) => void;

let impl: ShowToast | null = null;

/** Mounted once by `ToastHost`; screens never need this. */
export function bindToast(show: ShowToast | null): void {
  impl = show;
}

export function toast(message: string, kind: ToastKind = 'info', duration = 4600): void {
  if (impl) impl({ body: message, type: kind === 'error' ? 'error' : 'info', autoHideDuration: duration });
  else if (kind === 'error') console.error('[toast]', message);
  else console.info('[toast]', message);
}

/** Turn any thrown value into a user-facing toast, without leaking internals. */
export function toastError(error: unknown, fallback = t('shell.error_title')): void {
  if (isUnsupported(error)) {
    toast(t('shell.unsupported'), 'info');
    return;
  }
  if (error instanceof ApiError) {
    toast(error.message || fallback, 'error');
    return;
  }
  if (isForbidden(error)) {
    toast(t('shell.forbidden'), 'error');
    return;
  }
  toast(error instanceof Error ? error.message : String(error || fallback), 'error');
}

/** Astryx toast bridge: renders once inside the theme, binds the module-level fn. */
export function ToastHost(): null {
  const showToast = useToast();
  useEffect(() => {
    bindToast(showToast as unknown as ShowToast);
    return () => bindToast(null);
  }, [showToast]);
  return null;
}
