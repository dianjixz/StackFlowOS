/**
 * SecurityPane — browser sessions (list / revoke / rotate / sign out) and the
 * read-only web + security configuration.  Secrets are never rendered: the
 * backend only exposes public metadata, and the cookie lives in HttpOnly.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Dialog, DialogHeader } from '@astryxdesign/core/Dialog';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Table } from '@astryxdesign/core/Table';
import { Text } from '@astryxdesign/core/Text';
import { api } from '../../api/client';
import { t, useT } from '../../i18n';
import { loadSessions, signOut, useAuth, useCanWrite, type SessionRow } from '../../session';
import { useStore } from '../../state/store';
import { ago, fmtDateTime, roleLabel } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import {
  LoadingLine,
  LoadError,
  PaneSection,
  ViewerNote,
  selConfig,
} from './common';

type ConfirmAction =
  | { kind: 'revoke'; id: string }
  | { kind: 'rotate' }
  | { kind: 'logout' }
  | null;

interface SessionTableRow extends Record<string, unknown> {
  id: string;
  user: string;
  role: string;
  created: number;
  last_seen: number;
  expires: number;
  isCurrent: boolean;
}

function sessionId(row: SessionRow): string {
  return String(row.id || row.session_id || '');
}

function isCurrent(row: SessionRow, currentId: string): boolean {
  return Boolean(row.current || row.current_session
    || row.current_id === currentId
    || (currentId && sessionId(row) === currentId));
}

export function SecurityPane(): ReactNode {
  const tr = useT();
  const auth = useAuth();
  const canWrite = useCanWrite();
  const config = useStore(selConfig);
  const [confirm, setConfirm] = useState<ConfirmAction>(null);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    void loadSessions({ quiet: false, force: true });
  }, []);

  const reload = async (): Promise<void> => {
    await loadSessions({ quiet: false, force: true });
  };

  const refresh = async (): Promise<void> => {
    setBusy('refresh');
    try {
      await reload();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy('');
    }
  };

  const run = async (action: ConfirmAction): Promise<void> => {
    if (!action) return;
    setConfirm(null);
    setBusy(action.kind === 'revoke' ? `revoke:${action.id}` : action.kind);
    try {
      if (action.kind === 'revoke') {
        await api.revokeSession(action.id);
        toast(t('settings.security.sessions.revoked'), 'info');
        await reload();
      } else if (action.kind === 'rotate') {
        await api.rotateSessions();
        toast(t('settings.security.sessions.rotated'), 'info');
        await reload();
      } else {
        await signOut();
        toast(t('settings.security.sessions.logged_out'), 'info');
      }
    } catch (error) {
      toastError(error);
    } finally {
      setBusy('');
    }
  };

  const sessions: SessionRow[] = auth.sessions || [];
  const rows: SessionTableRow[] = sessions.map((row) => ({
    id: sessionId(row),
    user: String(row.user || row.username || '—'),
    role: String(row.role || ''),
    created: Number(row.created ?? row.created_at ?? 0),
    last_seen: Number(row.last_seen ?? 0),
    expires: Number(row.expires ?? 0),
    isCurrent: isCurrent(row, auth.currentId),
  }));

  const showActions = canWrite && !auth.unavailable && !auth.forbidden;

  const columns = [
    {
      key: 'user',
      header: tr('settings.security.sessions.user'),
      renderCell: (row: SessionTableRow) => (
        <HStack gap={1.5} align="center" wrap="wrap">
          <Text size="sm" weight="semibold">{row.user}</Text>
          {row.isCurrent ? <Badge label={tr('settings.security.sessions.current')} variant="success" /> : null}
        </HStack>
      ),
    },
    {
      key: 'role',
      header: tr('settings.security.sessions.role'),
      renderCell: (row: SessionTableRow) => (
        <Badge
          label={roleLabel(row.role) || tr('settings.unknown')}
          variant={row.role === 'viewer' ? 'warning' : row.role === 'operator' ? 'success' : 'neutral'}
        />
      ),
    },
    {
      key: 'created',
      header: tr('settings.security.sessions.created'),
      renderCell: (row: SessionTableRow) => (
        <VStack gap={0.5}>
          <Text size="sm">{ago(row.created)}</Text>
          <Text size="xsm" color="secondary">{fmtDateTime(row.created)}</Text>
        </VStack>
      ),
    },
    {
      key: 'last_seen',
      header: tr('settings.security.sessions.last_seen'),
      renderCell: (row: SessionTableRow) => (
        <VStack gap={0.5}>
          <Text size="sm">{ago(row.last_seen)}</Text>
          <Text size="xsm" color="secondary">{fmtDateTime(row.last_seen)}</Text>
        </VStack>
      ),
    },
    ...(showActions ? [{
      key: 'actions',
      header: '',
      renderCell: (row: SessionTableRow) => (
        <Button
          label={tr('settings.security.sessions.revoke')}
          size="sm"
          variant="ghost"
          isDisabled={row.isCurrent || !row.id}
          isLoading={busy === `revoke:${row.id}`}
          tooltip={row.isCurrent ? tr('settings.security.sessions.current') : undefined}
          onClick={() => setConfirm({ kind: 'revoke', id: row.id })}
        />
      ),
    }] : []),
  ];

  const security = config?.security || {};
  const web = config?.web || {};
  const ask = tr('settings.security.policy.ask');
  const deny = tr('settings.security.policy.deny');
  const yes = tr('settings.security.policy.yes');
  const no = tr('settings.security.policy.no');

  return (
    <VStack gap={4}>
      <PaneSection
        title={tr('settings.security.sessions.title')}
        hint={tr('settings.security.sessions.hint')}
        actions={(
          <HStack gap={2} align="center" wrap="wrap">
            {showActions ? (
              <>
                <Button
                  label={tr('settings.security.sessions.rotate')}
                  size="sm"
                  variant="secondary"
                  isLoading={busy === 'rotate'}
                  onClick={() => setConfirm({ kind: 'rotate' })}
                />
                <Button
                  label={tr('settings.security.sessions.logout')}
                  size="sm"
                  variant="destructive"
                  isLoading={busy === 'logout'}
                  onClick={() => setConfirm({ kind: 'logout' })}
                />
              </>
            ) : null}
            <Button
              label={tr('settings.security.sessions.refresh')}
              size="sm"
              variant="ghost"
              isLoading={busy === 'refresh'}
              isDisabled={auth.loading}
              onClick={() => { void refresh(); }}
            />
          </HStack>
        )}
      >
        <VStack gap={3}>
          {!showActions ? <ViewerNote /> : null}
          {auth.known ? (
            <HStack gap={2} align="center" wrap="wrap">
              <Text size="sm" color="secondary">{tr('settings.general.identity.user')}</Text>
              <Text size="sm" weight="semibold">{auth.user || tr('settings.unknown')}</Text>
              <Badge label={roleLabel(auth.role) || tr('settings.unknown')} variant={auth.role === 'viewer' ? 'warning' : 'success'} />
            </HStack>
          ) : null}
          {auth.loading && !auth.sessions ? <LoadingLine label={tr('settings.security.sessions.loading')} /> : null}
          {auth.unavailable ? (
            <Banner status="info" title={tr('settings.security.sessions.unavailable')} />
          ) : null}
          {auth.forbidden ? (
            <Banner status="warning" title={tr('settings.security.sessions.forbidden')} />
          ) : null}
          {!auth.loading && !auth.unavailable && !auth.forbidden && auth.error && !auth.sessions ? (
            <LoadError
              error={new Error(auth.error)}
              unavailableKey="settings.security.sessions.unavailable"
              forbiddenKey="settings.security.sessions.forbidden"
              errorKey="settings.security.sessions.error"
              onRetry={() => { void refresh(); }}
            />
          ) : null}
          {!auth.loading && !auth.unavailable && !auth.forbidden && auth.sessions && rows.length === 0 ? (
            <Banner status="info" title={tr('settings.security.sessions.empty')} />
          ) : null}
          {rows.length > 0 ? (
            <Table
              data={rows}
              columns={columns}
              idKey="id"
              density="compact"
              hasHover
              emptyState={tr('settings.security.sessions.empty')}
            />
          ) : null}
        </VStack>
      </PaneSection>

      <PaneSection title={tr('settings.security.web.title')} hint={tr('settings.security.web.hint')}>
        {config ? (
          <MetadataList columns={2} label={{ position: 'top' }}>
            <MetadataListItem label={tr('settings.security.web.host')}>
              <Text size="sm">{String(web.host || '—')}</Text>
            </MetadataListItem>
            <MetadataListItem label={tr('settings.security.web.port')}>
              <Text size="sm" hasTabularNumbers>{String(web.port ?? '—')}</Text>
            </MetadataListItem>
            <MetadataListItem label={tr('settings.security.web.open_browser')}>
              <Text size="sm">{web.open_browser ? yes : no}</Text>
            </MetadataListItem>
            <MetadataListItem label={tr('settings.security.web.sse_heartbeat')}>
              <Text size="sm" hasTabularNumbers>{String(web.sse_heartbeat_s ?? '—')}</Text>
            </MetadataListItem>
          </MetadataList>
        ) : (
          <Banner status="info" title={tr('settings.security.config_missing')} />
        )}
      </PaneSection>

      <PaneSection
        title={tr('settings.security.policy.title')}
        hint={tr('settings.security.policy.hint')}
      >
        {config ? (
          <VStack gap={3}>
            <MetadataList columns={2} label={{ position: 'top' }}>
              <MetadataListItem label={tr('settings.security.policy.auto_allow_low')}>
                <Text size="sm">{security.auto_allow_low ? yes : no}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.medium_default')}>
                <Text size="sm">{String(security.medium_default) === 'deny' ? deny : ask}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.high_default')}>
                <Text size="sm">{String(security.high_default) === 'deny' ? deny : ask}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.approval_ttl')}>
                <Text size="sm" hasTabularNumbers>{String(security.approval_ttl_s ?? '—')}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.grant_ttl')}>
                <Text size="sm" hasTabularNumbers>{String(security.grant_ttl_s ?? '—')}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.shell_timeout')}>
                <Text size="sm" hasTabularNumbers>{String(security.shell_timeout_s ?? '—')}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.allow_shell')}>
                <Text size="sm">{security.allow_shell ? yes : no}</Text>
              </MetadataListItem>
              <MetadataListItem label={tr('settings.security.policy.max_preview')}>
                <Text size="sm" hasTabularNumbers>{String(security.max_preview_chars ?? '—')}</Text>
              </MetadataListItem>
            </MetadataList>
            <Text size="xsm" color="secondary">{tr('settings.security.policy.note')}</Text>
          </VStack>
        ) : (
          <Banner status="info" title={tr('settings.security.config_missing')} />
        )}
      </PaneSection>

      <Dialog
        isOpen={confirm !== null}
        onOpenChange={(open) => { if (!open) setConfirm(null); }}
        width={480}
        padding={4}
        purpose="required"
      >
        <VStack gap={4}>
          <DialogHeader
            title={confirmTitle(tr, confirm)}
            subtitle={confirmText(tr, confirm)}
            onOpenChange={(open) => { if (!open) setConfirm(null); }}
          />
          <HStack gap={2} justify="end" align="center">
            <Button
              label={tr('common.cancel')}
              variant="ghost"
              onClick={() => setConfirm(null)}
            />
            <Button
              label={tr('settings.confirm')}
              variant={confirm?.kind === 'revoke' || confirm?.kind === 'logout' ? 'destructive' : 'primary'}
              onClick={() => { void run(confirm); }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </VStack>
  );
}

function confirmTitle(
  tr: (key: string, params?: Record<string, string | number>, fallback?: string | null) => string,
  action: ConfirmAction,
): string {
  if (!action) return '';
  if (action.kind === 'revoke') return tr('settings.security.sessions.revoke_title');
  if (action.kind === 'rotate') return tr('settings.security.sessions.rotate_title');
  return tr('settings.security.sessions.logout_title');
}

function confirmText(
  tr: (key: string, params?: Record<string, string | number>, fallback?: string | null) => string,
  action: ConfirmAction,
): string {
  if (!action) return '';
  if (action.kind === 'revoke') {
    return tr('settings.security.sessions.revoke_text', { id: action.id });
  }
  if (action.kind === 'rotate') return tr('settings.security.sessions.rotate_text');
  return tr('settings.security.sessions.logout_text');
}
