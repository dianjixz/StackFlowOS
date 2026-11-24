/**
 * HealthPanel.tsx — the onboarding self-check card.
 *
 * Loads on demand (the operator clicks 入住自检) so the page does not call an
 * optional endpoint on every visit.  A 404/405/501 renders the "not enabled"
 * note rather than an error; `fail` checks are blockers, `warn` is advisory.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Button } from '@astryxdesign/core/Button';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { List, ListItem } from '@astryxdesign/core/List';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Spinner } from '@astryxdesign/core/Spinner';
import { Text } from '@astryxdesign/core/Text';
import { api, isForbidden, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { toast, toastError } from '../../ui/toast';
import { normalizeHealth } from './data';
import type { HealthReport } from './types';
import { Hint, SectionHead } from './ui';

type LoadError = 'unsupported' | 'forbidden' | 'other' | '';

export function HealthPanel({
  projectId,
  canWrite,
  onMutated,
  runSignal = 0,
}: {
  projectId: string;
  canWrite: boolean;
  onMutated: () => Promise<void> | void;
  /** Bumped by the page header's 入住自检 button to run the check on demand. */
  runSignal?: number;
}): ReactNode {
  const [report, setReport] = useState<HealthReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [onboarding, setOnboarding] = useState(false);
  const [loadError, setLoadError] = useState<LoadError>('');
  const [failure, setFailure] = useState('');

  const check = async (): Promise<void> => {
    setLoading(true);
    setLoadError('');
    setFailure('');
    try {
      const data = await api.getProjectHealth(projectId);
      setReport(normalizeHealth(data));
    } catch (cause) {
      setReport(null);
      if (isUnsupported(cause)) setLoadError('unsupported');
      else if (isForbidden(cause)) setLoadError('forbidden');
      else {
        setLoadError('other');
        setFailure(cause instanceof Error ? cause.message : String(cause));
      }
      toastError(cause);
    } finally {
      setLoading(false);
    }
  };

  const onboard = async (): Promise<void> => {
    setOnboarding(true);
    try {
      await api.onboardProject(projectId);
      toast(t('prj.health.onboard.done'), 'info');
      const fresh = await api.getProjectHealth(projectId);
      setReport(normalizeHealth(fresh));
      await onMutated();
    } catch (cause) {
      toastError(cause);
    } finally {
      setOnboarding(false);
    }
  };

  const lastSignal = useRef(runSignal);
  useEffect(() => {
    if (runSignal > lastSignal.current) {
      lastSignal.current = runSignal;
      void check();
    }
    // `check` is intentionally not a dependency: only a new signal runs it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runSignal]);

  const summary = report?.summary || {};
  const checks = report?.checks || [];

  const body = ((): ReactNode => {
    if (loading) {
      return (
        <HStack gap={2} align="center">
          <Spinner size="sm" />
          <Text size="sm" color="secondary">{t('prj.health.loading')}</Text>
        </HStack>
      );
    }
    if (loadError === 'unsupported') return <Hint>{t('prj.health.unavailable', { id: projectId })}</Hint>;
    if (loadError === 'forbidden') return <Hint isWarning>{t('prj.health.forbidden')}</Hint>;
    if (loadError === 'other') return <Hint isWarning>{t('prj.health.error', { message: failure })}</Hint>;
    if (!report) return <Hint>{t('prj.health.idle')}</Hint>;

    return (
      <VStack gap={3}>
        <HStack gap={3} align="center" wrap="wrap">
          <Badge
            variant={report.ready ? 'success' : 'error'}
            label={report.ready ? t('prj.health.verdict.ready') : t('prj.health.verdict.not_ready')}
          />
          <Text size="xsm" color="secondary" hasTabularNumbers>
            {t('prj.health.summary', {
              ok: Number(summary.ok) || 0,
              warn: Number(summary.warn) || 0,
              fail: Number(summary.fail) || 0,
              unchecked: Number(summary.unchecked) || 0,
            })}
          </Text>
        </HStack>
        <Hint isWarning={!report.ready}>
          {report.ready ? t('prj.health.ready') : t('prj.health.not_ready')}
        </Hint>
        {!report.ready && (report.not_ready_reasons || []).length ? (
          <VStack gap={1}>
            <Text size="sm" weight="semibold">{t('prj.health.blockers')}</Text>
            {(report.not_ready_reasons || []).map((reason) => (
              <Hint key={reason} isWarning>{`✕ ${reason}`}</Hint>
            ))}
          </VStack>
        ) : null}
        <List density="compact" hasDividers>
          {checks.map((entry, index) => {
            const status = entry.status || (entry.checked === false ? 'warn' : 'ok');
            const tone = status === 'ok' ? 'success' : status === 'warn' ? 'warning' : 'error';
            const label = t(`prj.health.check.${status}`, {}, status);
            return (
              <ListItem
                key={`${entry.id || index}`}
                label={(
                  <HStack gap={2} align="center" wrap="wrap">
                    <StatusDot variant={tone} label={label} />
                    <Text type="body" weight="semibold">{entry.label || entry.id || '—'}</Text>
                    <Text size="2xs" color="secondary">{label}</Text>
                  </HStack>
                )}
                description={(
                  <VStack gap={0.5}>
                    <Text size="xsm" color="secondary">{entry.message || ''}</Text>
                    {entry.fix && status !== 'ok' ? (
                      <Text size="2xs" color="secondary">{t('prj.health.fix', { fix: entry.fix })}</Text>
                    ) : null}
                  </VStack>
                )}
              />
            );
          })}
        </List>
      </VStack>
    );
  })();

  return (
    <VStack gap={3}>
      <SectionHead
        level={2}
        title={t('prj.health.title')}
        hint={t('prj.health.hint')}
        action={(
          <>
            {report && !report.ready && canWrite ? (
              <Button
                label={t('prj.health.onboard')}
                variant="secondary"
                size="sm"
                isLoading={onboarding}
                clickAction={onboard}
              />
            ) : null}
            <Button
              label={report ? t('prj.health.recheck') : t('prj.action.healthcheck')}
              variant={report ? 'ghost' : 'secondary'}
              size="sm"
              isLoading={loading}
              clickAction={check}
            />
          </>
        )}
      />
      {body}
    </VStack>
  );
}
