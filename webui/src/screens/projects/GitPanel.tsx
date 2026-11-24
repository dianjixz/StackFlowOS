/**
 * GitPanel.tsx — the workspace git card on the project detail page.
 *
 * The endpoint is an optional extension: a 404/405/501 renders the calm
 * "not enabled" note instead of an error, and a read-only workspace renders
 * the reason the backend reports.
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { HStack, StackItem, VStack } from '@astryxdesign/core/Layout';
import { List, ListItem } from '@astryxdesign/core/List';
import { Spinner } from '@astryxdesign/core/Spinner';
import { Text } from '@astryxdesign/core/Text';
import { TextInput } from '@astryxdesign/core/TextInput';
import { useImperativeAlertDialog } from '@astryxdesign/core/AlertDialog';
import { ArrowPathIcon } from '@heroicons/react/24/outline';
import { api, isForbidden, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { clip, fmtDateTime } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import { gitCommitSha, gitCommitSubject, normalizeGit } from './data';
import type { GitState } from './types';
import { Hint, SectionHead } from './ui';

type LoadError = 'unsupported' | 'forbidden' | 'other';

function defaultRef(git: GitState): string {
  const second = git.commits[1];
  const sha = second ? gitCommitSha(second) : '';
  return sha || 'HEAD~1';
}

function commitTime(commit: GitState['commits'][number]): string {
  const value = commit.ts ?? commit.time;
  if (value !== undefined && value !== null && value !== '') {
    if (typeof value === 'number' || /^\d+$/.test(String(value))) {
      const asNumber = Number(value);
      return fmtDateTime(asNumber < 1e12 ? asNumber * 1000 : asNumber);
    }
    return String(value);
  }
  return commit.date ? String(commit.date) : '';
}

export function GitPanel({
  projectId,
  projectName,
  canWrite,
  onMutated,
}: {
  projectId: string;
  projectName: string;
  canWrite: boolean;
  onMutated: () => Promise<void> | void;
}): ReactNode {
  const [git, setGit] = useState<GitState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [failure, setFailure] = useState('');
  const [ref, setRef] = useState('');
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const alert = useImperativeAlertDialog();

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    setFailure('');
    try {
      const data = await api.getProjectGit(projectId);
      const next = normalizeGit(data);
      setGit(next);
      setRef((current) => current || defaultRef(next));
    } catch (cause) {
      setGit(null);
      if (isUnsupported(cause)) setLoadError('unsupported');
      else if (isForbidden(cause)) setLoadError('forbidden');
      else {
        setLoadError('other');
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const rollback = async (): Promise<void> => {
    const target = ref.trim();
    if (!target) {
      toast(t('prj.git.rollback.need_ref'), 'error');
      return;
    }
    setRollbackBusy(true);
    try {
      await api.rollbackProjectGit(projectId, target);
      toast(t('prj.git.rollback.done', { ref: target }), 'info');
      await load();
      await onMutated();
    } catch (cause) {
      toast(t('prj.git.rollback.failed', { message: cause instanceof Error ? cause.message : String(cause) }), 'error');
      toastError(cause);
    } finally {
      setRollbackBusy(false);
    }
  };

  const body = ((): ReactNode => {
    if (loading) {
      return (
        <HStack gap={2} align="center">
          <Spinner size="sm" />
          <Text size="sm" color="secondary">{t('prj.git.loading')}</Text>
        </HStack>
      );
    }
    if (loadError === 'unsupported') return <Hint>{t('prj.git.unavailable', { id: projectId })}</Hint>;
    if (loadError === 'forbidden') return <Hint isWarning>{t('prj.git.forbidden')}</Hint>;
    if (loadError === 'other') return <Hint isWarning>{t('prj.git.error', { message: failure })}</Hint>;
    if (!git) return <Hint>{t('prj.git.loading')}</Hint>;
    if (!git.enabled) return <Hint>{t('prj.git.unavailable', { id: projectId })}</Hint>;
    if (!git.repo) {
      return (
        <Hint>
          {git.reason ? t('prj.git.not_repo_reason', { reason: git.reason }) : t('prj.git.not_repo')}
        </Hint>
      );
    }

    return (
      <VStack gap={4}>
        <Card padding={3}>
          <HStack gap={3} align="center" wrap="wrap">
            <Text size="xsm" color="secondary">{t('prj.git.branch')}</Text>
            <Text type="code" size="sm">{git.branch}</Text>
            <Badge variant={git.dirty ? 'warning' : 'success'} label={git.dirty
              ? t('prj.git.dirty', { n: git.changes.length })
              : t('prj.git.clean')} />
            {git.head ? (
              <Text size="xsm" color="secondary" hasTabularNumbers>
                {`${t('prj.git.head')} ${clip(git.head, 12)}`}
              </Text>
            ) : null}
            {git.workspace ? (
              <Text size="xsm" color="secondary" maxLines={1}>
                {clip(git.workspace, 80)}
              </Text>
            ) : null}
          </HStack>
        </Card>

        {git.changes.length ? (
          <VStack gap={1.5}>
            <Text size="sm" weight="semibold">{t('prj.git.diff')}</Text>
            <List density="compact" hasDividers>
              {git.changes.map((change, index) => (
                <ListItem
                  key={`${change.path || change.line || index}`}
                  label={(
                    <HStack gap={2} align="center" wrap="wrap">
                      <Badge variant="neutral" label={change.status || 'M'} />
                      <Text type="code" size="sm">{change.path || change.line || '—'}</Text>
                    </HStack>
                  )}
                  endContent={(
                    <HStack gap={2} align="center">
                      {change.additions ? <Text size="2xs" color="secondary">{`+${change.additions}`}</Text> : null}
                      {change.deletions ? <Text size="2xs" color="secondary">{`-${change.deletions}`}</Text> : null}
                    </HStack>
                  )}
                />
              ))}
            </List>
          </VStack>
        ) : (
          <Hint>{t('prj.git.diff.empty')}</Hint>
        )}

        {git.diff.trim() ? (
          <CodeBlock
            code={git.diff}
            language="plaintext"
            hasLanguageLabel={false}
            hasLineNumbers={false}
            hasCopyButton={false}
            isWrapped
            maxHeight={280}
            width="100%"
            size="sm"
          />
        ) : null}

        <VStack gap={1.5}>
          <Text size="sm" weight="semibold">{t('prj.git.commits')}</Text>
          {git.commits.length === 0 ? <Hint>{t('prj.git.commits.empty')}</Hint> : (
            <List density="compact" hasDividers>
              {git.commits.slice(0, 10).map((commit, index) => {
                const sha = gitCommitSha(commit);
                return (
                  <ListItem
                    key={`${sha || index}`}
                    startContent={<Text type="code" size="2xs">{clip(sha, 10)}</Text>}
                    label={gitCommitSubject(commit)}
                    endContent={commitTime(commit) ? (
                      <Text size="2xs" color="secondary" hasTabularNumbers>{commitTime(commit)}</Text>
                    ) : undefined}
                  />
                );
              })}
            </List>
          )}
        </VStack>

        {git.rollback ? (
          canWrite ? (
            <HStack gap={2} align="end" wrap="wrap">
              <StackItem size="fill">
                <TextInput
                  label={t('prj.git.rollback.ref')}
                  placeholder={t('prj.git.rollback.placeholder')}
                  value={ref}
                  onChange={setRef}
                  width="100%"
                />
              </StackItem>
              <Button
                label={t('prj.git.rollback')}
                variant="destructive"
                isLoading={rollbackBusy}
                onClick={() => {
                  const target = ref.trim();
                  if (!target) {
                    toast(t('prj.git.rollback.need_ref'), 'error');
                    return;
                  }
                  alert.show({
                    title: t('prj.git.rollback'),
                    description: t('prj.git.rollback.confirm', { name: projectName, ref: target }),
                    actionLabel: t('prj.git.rollback'),
                    cancelLabel: t('common.cancel'),
                    onAction: () => { alert.hide(); void rollback(); },
                  });
                }}
              />
            </HStack>
          ) : (
            <Hint>{t('prj.git.viewer_hint')}</Hint>
          )
        ) : null}
        {alert.element}
      </VStack>
    );
  })();

  return (
    <VStack gap={3}>
      <SectionHead
        level={2}
        title={t('git.title')}
        hint={t('prj.git.hint')}
        action={(
          <Button
            label={t('prj.git.reload')}
            variant="ghost"
            size="sm"
            icon={<ArrowPathIcon width={18} height={18} aria-hidden="true" />}
            isLoading={loading}
            clickAction={load}
          />
        )}
      />
      {body}
    </VStack>
  );
}
