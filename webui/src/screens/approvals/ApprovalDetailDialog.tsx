/**
 * ApprovalDetailDialog.tsx — one approval request, in full.
 *
 * The list endpoint only carries the truncated preview; opening the dialog
 * fetches `GET /api/approvals/{id}` for the bounded full preview.  When that
 * endpoint is missing (older backend / fixture), the dialog falls back to the
 * list preview instead of failing — an unavailable detail must not look like
 * a broken console.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Dialog, DialogHeader } from '@astryxdesign/core/Dialog';
import { VStack, HStack } from '@astryxdesign/core/Layout';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Divider } from '@astryxdesign/core/Divider';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { TextArea } from '@astryxdesign/core/TextArea';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Text } from '@astryxdesign/core/Text';
import { api, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { fmtCountdown, fmtDateTime, statusLabel, statusTone } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import { badgeVariant, modeLabel, previewText, RiskBadge, scopeText, useNow } from './common';
import type { ApprovalRow, DecisionMode } from './types';

interface ApprovalDetailDialogProps {
  approval: ApprovalRow | null;
  projects: { id: string; name?: string }[];
  canWrite: boolean;
  onClose: () => void;
  onDecided: () => void;
}

type Busy = DecisionMode | 'deny' | null;

export function ApprovalDetailDialog({
  approval,
  projects,
  canWrite,
  onClose,
  onDecided,
}: ApprovalDetailDialogProps): ReactNode {
  const now = useNow(1000);
  const [detail, setDetail] = useState<ApprovalRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<Busy>(null);
  const [actionError, setActionError] = useState('');

  const id = approval?.id ?? '';

  useEffect(() => {
    setDetail(null);
    setDetailError('');
    setNote('');
    setActionError('');
    setBusy(null);
    if (!id) return undefined;
    let alive = true;
    setLoading(true);
    api.getApproval(id)
      .then((data) => {
        if (alive) setDetail(data as ApprovalRow);
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setDetailError(isUnsupported(error) ? '' : (error instanceof Error ? error.message : String(error)));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, [id]);

  const pending = approval?.status === 'pending';
  const expires = fmtCountdown(approval?.expires_at, now);
  const project = projects.find((row) => row.id === approval?.project_id);
  const preview = previewText(
    detail?.preview_full || detail?.preview || approval?.preview,
    t('approvals.detail.no_preview'),
  );
  const fullPreview = Boolean(detail?.preview_full) && !detailError;
  const matchedRule = detail?.policy?.matched_rule;
  const wouldAutoApprove = detail?.policy?.would_auto_approve;

  const decide = async (decision: 'allow' | 'deny', mode: DecisionMode): Promise<void> => {
    if (!approval || busy) return;
    setBusy(decision === 'deny' ? 'deny' : mode);
    setActionError('');
    try {
      await api.decideApproval(approval.id, decision, mode, note.trim());
      toast(
        decision === 'allow'
          ? t('approvals.toast.allowed', { capability: approval.capability })
          : t('approvals.toast.denied', { capability: approval.capability }),
        'info',
      );
      onClose();
      onDecided();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
      toastError(error);
      setBusy(null);
    }
  };

  return (
    <Dialog
      isOpen={Boolean(approval)}
      onOpenChange={(open) => { if (!open) onClose(); }}
      width={760}
      maxHeight="86dvh"
      purpose="info"
    >
      {!approval ? null : (
        <VStack gap={3}>
          <DialogHeader
            title={`${approval.capability} · ${approval.id}`}
            subtitle={approval.reason || undefined}
            onOpenChange={(open) => { if (!open) onClose(); }}
          />

          <HStack gap={2} align="center" wrap="wrap">
            <RiskBadge risk={approval.risk} />
            <Badge
              label={statusLabel(approval.status)}
              variant={badgeVariant(statusTone(approval.status))}
            />
            <Badge
              label={expires.expired
                ? t('approvals.detail.expired')
                : t('approvals.detail.countdown', { time: expires.text })}
              variant={expires.expired ? 'error' : expires.urgent ? 'warning' : 'neutral'}
            />
            {approval.mode ? <Badge label={modeLabel(approval.mode)} variant="neutral" /> : null}
            {wouldAutoApprove ? (
              <Badge label={t('approvals.detail.would_auto_approve')} variant="info" />
            ) : null}
          </HStack>

          <MetadataList columns="multi">
            <MetadataListItem label={t('approvals.col.principal')}>
              <Text type="code" size="sm">{approval.principal || '—'}</Text>
            </MetadataListItem>
            <MetadataListItem label={t('approvals.col.project')}>
              <Text size="sm">{project?.name || approval.project_id || '—'}</Text>
            </MetadataListItem>
            <MetadataListItem label={t('approvals.detail.task')}>
              <Text type="code" size="sm">{approval.task_id || '—'}</Text>
            </MetadataListItem>
            <MetadataListItem label={t('approvals.detail.requested_at')}>
              <Text size="sm">{fmtDateTime(approval.created_at)}</Text>
            </MetadataListItem>
            <MetadataListItem label={t('approvals.col.expires')}>
              <Text size="sm">{fmtDateTime(approval.expires_at)}</Text>
            </MetadataListItem>
            {matchedRule ? (
              <MetadataListItem label={t('approvals.detail.matched_rule')}>
                <Text type="code" size="sm">{matchedRule}</Text>
              </MetadataListItem>
            ) : null}
            {approval.decided_at ? (
              <MetadataListItem label={t('approvals.detail.decided_at')}>
                <Text size="sm">{fmtDateTime(approval.decided_at)}</Text>
              </MetadataListItem>
            ) : null}
            {approval.decided_by ? (
              <MetadataListItem label={t('approvals.detail.decided_by')}>
                <Text type="code" size="sm">{approval.decided_by}</Text>
              </MetadataListItem>
            ) : null}
            {approval.note ? (
              <MetadataListItem label={t('approvals.detail.note')}>
                <Text size="sm">{approval.note}</Text>
              </MetadataListItem>
            ) : null}
          </MetadataList>

          <VStack gap={1}>
            <Text type="label" weight="semibold">{t('approvals.detail.reason')}</Text>
            <Text as="p" size="sm">{approval.reason || t('approvals.detail.no_reason')}</Text>
          </VStack>

          <VStack gap={1}>
            <HStack gap={2} align="center" wrap="wrap">
              <Text type="label" weight="semibold">{t('approvals.detail.scope')}</Text>
              <Text size="xsm" color="secondary">{t('approvals.detail.scope_hint')}</Text>
            </HStack>
            <CodeBlock
              code={scopeText(approval.scope)}
              language="json"
              hasLineNumbers
              maxHeight={200}
              width="100%"
              size="sm"
            />
          </VStack>

          <VStack gap={1}>
            <HStack gap={2} align="center" wrap="wrap">
              <Text type="label" weight="semibold">{t('approvals.detail.preview')}</Text>
              {detail?.preview_truncated ? (
                <Badge
                  label={t('approvals.detail.preview_truncated', { bytes: detail.preview_bytes || 0 })}
                  variant="warning"
                />
              ) : fullPreview ? (
                <Badge label={t('approvals.detail.preview_full')} variant="neutral" />
              ) : null}
              {detail?.preview_sha256 ? (
                <Text size="xsm" color="secondary">
                  {t('approvals.detail.sha256', { digest: detail.preview_sha256.slice(0, 16) })}
                </Text>
              ) : null}
              {loading ? <Text size="xsm" color="secondary">{t('approvals.detail.preview_loading')}</Text> : null}
            </HStack>
            <CodeBlock
              code={preview}
              language="plaintext"
              hasLineNumbers
              isWrapped
              maxHeight={280}
              width="100%"
              size="sm"
            />
            {detailError ? (
              <Banner
                status="warning"
                title={t('approvals.detail.preview_fallback')}
                description={detailError}
                container="section"
              />
            ) : null}
          </VStack>

          <Divider />

          {pending ? (
            canWrite ? (
              <VStack gap={2}>
                {actionError ? (
                  <Banner status="error" title={t('approvals.decide.error')} description={actionError} container="section" />
                ) : null}
                <TextArea
                  label={t('approvals.field.note')}
                  placeholder={t('approvals.field.note.placeholder')}
                  value={note}
                  onChange={setNote}
                  rows={2}
                  isDisabled={busy !== null}
                />
                <HStack gap={2} justify="between" align="center" wrap="wrap">
                  <HStack gap={2} align="center" wrap="wrap">
                    <Button
                      label={t('approvals.allow_once')}
                      variant="primary"
                      size="sm"
                      tooltip={t('approvals.allow_once.tip')}
                      isLoading={busy === 'once'}
                      isDisabled={busy !== null}
                      onClick={() => { void decide('allow', 'once'); }}
                    />
                    <Button
                      label={t('approvals.allow_session')}
                      variant="secondary"
                      size="sm"
                      tooltip={t('approvals.allow_session.tip')}
                      isLoading={busy === 'session'}
                      isDisabled={busy !== null}
                      onClick={() => { void decide('allow', 'session'); }}
                    />
                    <Button
                      label={t('approvals.allow_always')}
                      variant="secondary"
                      size="sm"
                      tooltip={t('approvals.allow_always.tip')}
                      isLoading={busy === 'always'}
                      isDisabled={busy !== null}
                      onClick={() => { void decide('allow', 'always'); }}
                    />
                  </HStack>
                  <Button
                    label={t('approvals.deny')}
                    variant="destructive"
                    size="sm"
                    isLoading={busy === 'deny'}
                    isDisabled={busy !== null}
                    onClick={() => { void decide('deny', 'once'); }}
                  />
                </HStack>
              </VStack>
            ) : (
              <Text size="sm" color="secondary">{t('approvals.viewer_hint')}</Text>
            )
          ) : (
            <VStack gap={2}>
              <Text type="label" weight="semibold">{t('approvals.detail.decision')}</Text>
              <MetadataList columns="multi">
                <MetadataListItem label={t('approvals.detail.decision')}>
                  <Badge
                    label={statusLabel(approval.status)}
                    variant={badgeVariant(statusTone(approval.status))}
                  />
                </MetadataListItem>
                <MetadataListItem label={t('approvals.detail.mode')}>
                  <Text size="sm">{modeLabel(approval.mode)}</Text>
                </MetadataListItem>
                <MetadataListItem label={t('approvals.detail.decided_by')}>
                  <Text type="code" size="sm">{approval.decided_by || '—'}</Text>
                </MetadataListItem>
                <MetadataListItem label={t('approvals.detail.decided_at')}>
                  <Text size="sm">{fmtDateTime(approval.decided_at)}</Text>
                </MetadataListItem>
              </MetadataList>
              {approval.note ? <Text as="p" size="sm">{approval.note}</Text> : null}
            </VStack>
          )}
        </VStack>
      )}
    </Dialog>
  );
}
