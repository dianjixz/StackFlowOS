/**
 * PolicyForm.tsx — the autonomy policy editor.
 *
 * It only PATCHes fields that differ from the server policy, so a stale form
 * can never write back (or clear) a field the operator did not touch.  Shell
 * and network delegation are gated by the same guards the backend enforces, so
 * the operator sees the hint before a 400.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { VStack, HStack } from '@astryxdesign/core/Layout';
import { Grid } from '@astryxdesign/core/Grid';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { CheckboxList, CheckboxListItem } from '@astryxdesign/core/CheckboxList';
import { Divider } from '@astryxdesign/core/Divider';
import { NumberInput } from '@astryxdesign/core/NumberInput';
import { Selector } from '@astryxdesign/core/Selector';
import { Switch } from '@astryxdesign/core/Switch';
import { TextArea } from '@astryxdesign/core/TextArea';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Heading, Text } from '@astryxdesign/core/Text';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { toast, toastError } from '../../ui/toast';
import {
  capabilityLabel,
  hasPatch,
  isDirty,
  modeLabel,
  policyDraft,
  policyGuard,
  policyPatch,
  riskMaxLabel,
} from './common';
import {
  APPROVE_MODES,
  CAPABILITIES,
  DISPATCH_MODES,
  RISK_LEVELS,
  type AutonomyStatus,
  type PolicyDraft,
} from './types';

interface PolicyFormProps {
  status: AutonomyStatus;
  canWrite: boolean;
  /** Incremented when the policy is changed by a control outside this form. */
  resetSignal: number;
  onSaved: (data: unknown) => void;
}

function unionOptions(base: readonly string[], current: unknown): string[] {
  const value = String(current || '');
  const list = [...base];
  if (value && !list.includes(value)) list.push(value);
  return list;
}

function splitLines(text: string): string[] {
  return text.split('\n').map((line) => line.trim()).filter(Boolean);
}

function splitCommas(text: string): string[] {
  return text.split(',').map((item) => item.trim()).filter(Boolean);
}

export function PolicyForm({ status, canWrite, resetSignal, onSaved }: PolicyFormProps): ReactNode {
  const [draft, setDraft] = useState<PolicyDraft>(() => policyDraft(status.policy));
  const [patternsText, setPatternsText] = useState(() => policyDraft(status.policy).approve_shell_patterns.join('\n'));
  const [domainsText, setDomainsText] = useState(() => policyDraft(status.policy).approve_domains.join(', '));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const effectiveDraft = useMemo<PolicyDraft>(() => ({
    ...draft,
    // `enabled` is owned by the hero switch; never resend a stale value from
    // a form that was rendered before the switch flipped.
    enabled: Boolean(status.policy.enabled),
    approve_shell_patterns: splitLines(patternsText),
    approve_domains: splitCommas(domainsText),
  }), [draft, domainsText, patternsText, status.policy]);

  const dirty = isDirty(effectiveDraft, status.policy);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // A control outside this form (enable switch, emergency stop) changed the
  // policy: adopt it, even when the form had unsaved edits.
  useEffect(() => {
    if (!resetSignal) return;
    const next = policyDraft(status.policy);
    setDraft(next);
    setPatternsText(next.approve_shell_patterns.join('\n'));
    setDomainsText(next.approve_domains.join(', '));
    setError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetSignal]);

  // A background refresh (autonomy event) may replace the policy.  Keep the
  // operator's unsaved edits; only re-sync when the form is clean.
  useEffect(() => {
    if (dirtyRef.current) return;
    const next = policyDraft(status.policy);
    setDraft(next);
    setPatternsText(next.approve_shell_patterns.join('\n'));
    setDomainsText(next.approve_domains.join(', '));
  }, [status.policy]);

  const set = <K extends keyof PolicyDraft>(key: K, value: PolicyDraft[K]): void => {
    setDraft((previous) => ({ ...previous, [key]: value }));
  };

  const save = async (): Promise<void> => {
    if (!canWrite || saving) return;
    if (!Number.isInteger(effectiveDraft.interval_s)
      || effectiveDraft.interval_s < 10 || effectiveDraft.interval_s > 86400) {
      setError(t('autonomy.policy.interval_invalid'));
      return;
    }
    const patch = policyPatch(effectiveDraft, status.policy);
    if (!hasPatch(patch)) {
      toast(t('autonomy.policy.nothing'), 'info');
      return;
    }
    const refusal = policyGuard(effectiveDraft, patch);
    if (refusal) {
      setError(refusal);
      return;
    }
    setSaving(true);
    setError('');
    try {
      const data = await api.patchAutonomyPolicy(patch);
      toast(t('autonomy.policy.saved'), 'info');
      onSaved(data);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      toastError(caught);
    } finally {
      setSaving(false);
    }
  };

  const disabled = !canWrite || saving;
  const dispatchOptions = unionOptions(DISPATCH_MODES, draft.dispatch_mode);
  const approveOptions = unionOptions(APPROVE_MODES, draft.approve_mode);
  const riskOptions = unionOptions(RISK_LEVELS, draft.approve_risk_max);

  return (
    <VStack gap={3}>
      <HStack gap={3} align="start" justify="between" wrap="wrap">
        <VStack gap={0.5}>
          <Heading level={3}>{t('autonomy.policy.title')}</Heading>
          <Text size="sm" color="secondary">{t('autonomy.policy.hint')}</Text>
        </VStack>
        <Button
          label={t('autonomy.policy.save')}
          variant="primary"
          size="sm"
          isLoading={saving}
          isDisabled={!canWrite}
          onClick={() => { void save(); }}
        />
      </HStack>

      {error ? (
        <Banner status="error" title={t('autonomy.policy.error')} description={error} container="section" />
      ) : null}

      <Grid columns={{ minWidth: 220, max: 3 }} gap={3}>
        <Selector
          label={t('autonomy.policy.dispatch_mode')}
          options={dispatchOptions.map((value) => ({ value, label: modeLabel(value) }))}
          value={draft.dispatch_mode}
          onChange={(value) => set('dispatch_mode', value || 'off')}
          isDisabled={disabled}
        />
        <Selector
          label={t('autonomy.policy.approve_mode')}
          options={approveOptions.map((value) => ({ value, label: modeLabel(value) }))}
          value={draft.approve_mode}
          onChange={(value) => set('approve_mode', value || 'off')}
          isDisabled={disabled}
        />
        <Selector
          label={t('autonomy.policy.risk_max')}
          options={riskOptions.map((value) => ({ value, label: riskMaxLabel(value) }))}
          value={draft.approve_risk_max}
          onChange={(value) => set('approve_risk_max', value || 'medium')}
          description={t('autonomy.policy.risk_max.hint')}
          isDisabled={disabled}
        />
        <NumberInput
          label={t('autonomy.policy.interval')}
          value={draft.interval_s}
          onChange={(value) => set('interval_s', value)}
          min={10}
          max={86400}
          step={10}
          isIntegerOnly
          units="s"
          description={t('autonomy.policy.interval.hint')}
          isDisabled={disabled}
        />
      </Grid>

      <Divider />

      <CheckboxList
        label={t('autonomy.policy.capabilities')}
        description={t('autonomy.policy.capabilities.hint')}
        value={draft.approve_capabilities}
        onChange={(values) => set('approve_capabilities', values)}
        density="compact"
        isDisabled={disabled}
        width="100%"
      >
        {CAPABILITIES.map((capability) => (
          <CheckboxListItem
            key={capability}
            label={capabilityLabel(capability)}
            value={capability}
            align="center"
          />
        ))}
      </CheckboxList>

      <VStack gap={1}>
        <Text size="xsm" color="secondary">{t('autonomy.policy.shell_hint')}</Text>
        <Text size="xsm" color="secondary">{t('autonomy.policy.net_hint')}</Text>
      </VStack>

      <Grid columns={{ minWidth: 260, max: 2 }} gap={3}>
        <Switch
          label={t('autonomy.policy.shell')}
          value={draft.approve_shell}
          onChange={(checked) => set('approve_shell', checked)}
          isDisabled={disabled}
        />
        <TextInput
          label={t('autonomy.policy.domains')}
          value={domainsText}
          onChange={setDomainsText}
          placeholder="example.com, api.example.com"
          description={t('autonomy.policy.domains.hint')}
          isDisabled={disabled}
        />
      </Grid>

      <TextArea
        label={t('autonomy.policy.shell_patterns')}
        value={patternsText}
        onChange={setPatternsText}
        placeholder={'^pytest\\b'}
        rows={2}
        description={t('autonomy.policy.shell_patterns.hint')}
        isDisabled={disabled}
      />

      <Divider />

      <VStack gap={1}>
        <Heading level={4}>{t('autonomy.policy.quota')}</Heading>
        <Text size="sm" color="secondary">{t('autonomy.policy.quota.hint')}</Text>
      </VStack>
      <Grid columns={{ minWidth: 220, max: 4 }} gap={3}>
        <NumberInput
          label={t('autonomy.policy.quota.agents')}
          value={draft.dispatch_max_agents}
          onChange={(value) => set('dispatch_max_agents', value)}
          min={0}
          step={1}
          isIntegerOnly
          isDisabled={disabled}
        />
        <NumberInput
          label={t('autonomy.policy.quota.tasks')}
          value={draft.dispatch_max_tasks_per_hour}
          onChange={(value) => set('dispatch_max_tasks_per_hour', value)}
          min={0}
          step={1}
          isIntegerOnly
          isDisabled={disabled}
        />
        <NumberInput
          label={t('autonomy.policy.quota.spawns')}
          value={draft.dispatch_max_spawns_per_hour}
          onChange={(value) => set('dispatch_max_spawns_per_hour', value)}
          min={0}
          step={1}
          isIntegerOnly
          isDisabled={disabled}
        />
        <NumberInput
          label={t('autonomy.policy.quota.approvals')}
          value={draft.approve_max_per_hour}
          onChange={(value) => set('approve_max_per_hour', value)}
          min={0}
          step={1}
          isIntegerOnly
          isDisabled={disabled}
        />
      </Grid>

      <HStack gap={3} align="center" justify="between" wrap="wrap">
        <Text size="xsm" color="secondary">
          {canWrite ? t('autonomy.policy.save_hint') : t('autonomy.policy.viewer')}
        </Text>
        <HStack gap={2} align="center">
          {dirty ? <Text size="xsm" color="accent">{t('autonomy.policy.dirty')}</Text> : null}
          <Button
            label={t('autonomy.policy.save')}
            variant="primary"
            size="sm"
            isLoading={saving}
            isDisabled={!canWrite}
            onClick={() => { void save(); }}
          />
        </HStack>
      </HStack>
    </VStack>
  );
}
