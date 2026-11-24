/**
 * RuleDialog.tsx — create one auto-approval / deny rule.
 *
 * The backend accepts a partial rule: `expires_at` and `uses_left` are null
 * when omitted ("long-lived" / "unlimited"), and `principal` defaults to `*`.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Dialog, DialogHeader } from '@astryxdesign/core/Dialog';
import { VStack, HStack } from '@astryxdesign/core/Layout';
import { Grid } from '@astryxdesign/core/Grid';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { DateTimeInput } from '@astryxdesign/core/DateTimeInput';
import type { ISODateTimeString } from '@astryxdesign/core/DateTimeInput';
import { Selector } from '@astryxdesign/core/Selector';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Text } from '@astryxdesign/core/Text';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { toast, toastError } from '../../ui/toast';

interface RuleDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}

export function RuleDialog({ isOpen, onClose, onCreated }: RuleDialogProps): ReactNode {
  const [effect, setEffect] = useState('allow');
  const [capability, setCapability] = useState('');
  const [principal, setPrincipal] = useState('*');
  const [riskMax, setRiskMax] = useState('high');
  const [expires, setExpires] = useState<ISODateTimeString | undefined>(undefined);
  const [usesLeft, setUsesLeft] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!isOpen) return;
    setEffect('allow');
    setCapability('');
    setPrincipal('*');
    setRiskMax('high');
    setExpires(undefined);
    setUsesLeft('');
    setNote('');
    setBusy(false);
    setError('');
  }, [isOpen]);

  const submit = async (): Promise<void> => {
    const cap = capability.trim();
    if (!cap) {
      setError(t('approvals.rule.capability.invalid'));
      return;
    }
    const usesRaw = usesLeft.trim();
    let uses: number | null = null;
    if (usesRaw) {
      const parsed = Number(usesRaw);
      if (!Number.isInteger(parsed) || parsed < 0) {
        setError(t('approvals.rule.uses_invalid'));
        return;
      }
      uses = parsed;
    }
    let expiresAt: number | null = null;
    if (expires) {
      const parsed = Date.parse(expires);
      if (!Number.isFinite(parsed)) {
        setError(t('approvals.rule.expires_invalid'));
        return;
      }
      expiresAt = parsed;
    }
    setBusy(true);
    setError('');
    try {
      await api.createRule({
        effect,
        capability: cap,
        principal: principal.trim() || '*',
        risk_max: riskMax,
        expires_at: expiresAt,
        uses_left: uses,
        note: note.trim(),
      });
      toast(t('approvals.rule.created'), 'info');
      onClose();
      onCreated();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      toastError(caught);
      setBusy(false);
    }
  };

  return (
    <Dialog
      isOpen={isOpen}
      onOpenChange={(open) => { if (!open) onClose(); }}
      width={640}
      maxHeight="86dvh"
      purpose="form"
    >
      <VStack gap={3}>
        <DialogHeader
          title={t('approvals.rule.create.title')}
          subtitle={t('approvals.rule.create.text')}
          onOpenChange={(open) => { if (!open) onClose(); }}
        />

        {error ? <Banner status="error" title={t('approvals.decide.error')} description={error} container="section" /> : null}

        <Grid columns={{ minWidth: 240, max: 2 }} gap={3}>
          <Selector
            label={t('approvals.rule.field.effect')}
            options={[
              { value: 'allow', label: t('approvals.effect.allow') },
              { value: 'deny', label: t('approvals.effect.deny') },
            ]}
            value={effect}
            onChange={(value) => setEffect(value || 'allow')}
            isDisabled={busy}
          />
          <TextInput
            label={t('approvals.rule.field.capability')}
            placeholder="fs.write / shell.run / *"
            value={capability}
            onChange={setCapability}
            description={t('approvals.rule.field.capability.hint')}
            isDisabled={busy}
            isRequired
          />
          <TextInput
            label={t('approvals.rule.field.principal')}
            placeholder={t('approvals.rule.field.principal.placeholder')}
            value={principal}
            onChange={setPrincipal}
            description={t('approvals.rule.field.principal.hint')}
            isDisabled={busy}
          />
          <Selector
            label={t('approvals.rule.field.risk_max')}
            options={[
              { value: 'high', label: t('approvals.risk.high') },
              { value: 'medium', label: t('approvals.risk.medium') },
              { value: 'low', label: t('approvals.risk.low') },
            ]}
            value={riskMax}
            onChange={(value) => setRiskMax(value || 'high')}
            description={t('approvals.rule.field.risk_max.hint')}
            isDisabled={busy}
          />
          <DateTimeInput
            label={t('approvals.rule.field.expires')}
            value={expires}
            onChange={setExpires}
            description={t('approvals.rule.field.expires.hint')}
            isOptional
            hasClear
            isDisabled={busy}
          />
          <TextInput
            label={t('approvals.rule.field.uses_left')}
            placeholder={t('approvals.rule.field.uses_left.placeholder')}
            value={usesLeft}
            onChange={setUsesLeft}
            description={t('approvals.rule.field.uses_left.hint')}
            inputMode="numeric"
            isDisabled={busy}
            isOptional
          />
        </Grid>

        <TextInput
          label={t('approvals.rule.field.note')}
          placeholder={t('approvals.rule.field.note.placeholder')}
          value={note}
          onChange={setNote}
          isDisabled={busy}
          isOptional
        />

        <HStack gap={2} justify="end" align="center" wrap="wrap">
          <Text size="xsm" color="secondary">{t('approvals.rule.create.footnote')}</Text>
          <Button
            label={t('approvals.rules.add')}
            variant="primary"
            size="sm"
            isLoading={busy}
            onClick={() => { void submit(); }}
          />
        </HStack>
      </VStack>
    </Dialog>
  );
}
