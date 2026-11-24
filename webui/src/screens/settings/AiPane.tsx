/**
 * AiPane — LLM usage/quota, model catalogue, route/fallback JSON editing and
 * the live connection test.  Every write goes through PATCH /api/config and
 * only changed fields are submitted.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Grid } from '@astryxdesign/core/Grid';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { NumberInput } from '@astryxdesign/core/NumberInput';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { Selector } from '@astryxdesign/core/Selector';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Table } from '@astryxdesign/core/Table';
import { Text, Heading } from '@astryxdesign/core/Text';
import { TextArea } from '@astryxdesign/core/TextArea';
import { TextInput } from '@astryxdesign/core/TextInput';
import { api, type Json } from '../../api/client';
import { t, useT } from '../../i18n';
import { useCanWrite } from '../../session';
import { useStore } from '../../state/store';
import { toast, toastError } from '../../ui/toast';
import {
  ARRAY_EXPECTED,
  LoadingLine,
  LoadError,
  PaneSection,
  ViewerNote,
  errorKind,
  errorMessage,
  fmtCost,
  fmtInt,
  jsonText,
  parseJsonArray,
  restartFieldLabel,
  sameJson,
  selConfig,
  submitConfigPatch,
} from './common';

/* ------------------------------------------------------------------ */
/* tolerant payload readers (the API contract allows a few spellings)  */
/* ------------------------------------------------------------------ */

type Dict = Record<string, any>;

function asDict(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {};
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item): item is Dict => Boolean(item) && typeof item === 'object') : [];
}

function pick(source: Dict, keys: string[], fallback: unknown = undefined): unknown {
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key];
  }
  return fallback;
}

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

const IN_KEYS = ['prompt', 'tokens_in', 'input_tokens', 'prompt_tokens', 'tokens_input'];
const OUT_KEYS = ['completion', 'tokens_out', 'output_tokens', 'completion_tokens', 'tokens_output'];

interface CircuitInfo {
  state: string;
  failures: number;
  reason: string;
}

function circuitOf(payload: Dict): CircuitInfo {
  const raw = asDict(pick(payload, ['circuit_breaker', 'breaker', 'circuit'], {}));
  const state = String(
    (typeof pick(payload, ['circuit_breaker', 'breaker', 'circuit']) === 'string'
      ? pick(payload, ['circuit_breaker', 'breaker', 'circuit'])
      : pick(raw, ['state', 'status'], pick(payload, ['circuit_state', 'breaker_state'], ''))) || '',
  ).toLowerCase();
  return {
    state,
    failures: num(pick(raw, ['failures'], 0)),
    reason: String(pick(raw, ['reason'], '') || ''),
  };
}

const CIRCUIT_TONES: Record<string, 'success' | 'error' | 'warning' | 'neutral'> = {
  closed: 'success',
  open: 'error',
  half_open: 'warning',
  'half-open': 'warning',
};

function circuitLabel(state: string): string {
  const key = state === 'half-open' ? 'half_open' : state;
  return t(`settings.ai.circuit.${key}`, {}, t('settings.ai.circuit.unknown'));
}

function modelHealth(entry: Dict): { ok: boolean | null; label: string; variant: 'success' | 'error' | 'neutral' | 'warning' } {
  const raw = pick(entry, ['ok', 'healthy', 'available', 'up', 'status', 'health'], undefined);
  if (raw === true || String(raw).toLowerCase() === 'ok' || String(raw).toLowerCase() === 'ready') {
    return { ok: true, label: t('status.ready'), variant: 'success' };
  }
  if (raw === false || ['down', 'failed', 'error', 'unavailable', 'open'].includes(String(raw).toLowerCase())) {
    return { ok: false, label: t('status.failed'), variant: 'error' };
  }
  if (String(raw).toLowerCase() === 'degraded') {
    return { ok: false, label: t('status.degraded'), variant: 'warning' };
  }
  if (raw === undefined || raw === null || raw === '') {
    return { ok: null, label: t('status.unknown'), variant: 'neutral' };
  }
  return { ok: null, label: String(raw), variant: 'neutral' };
}

/* ------------------------------------------------------------------ */
/* rows for tables                                                     */
/* ------------------------------------------------------------------ */

interface BreakdownRow extends Record<string, unknown> {
  id: string;
  kind: string;
  name: string;
  tokens: number;
  cost: number;
}

interface ModelTableRow extends Record<string, unknown> {
  id: string;
  provider: string;
  model: string;
  role: string;
  health: string;
  healthVariant: 'success' | 'error' | 'neutral' | 'warning';
  latency: number | null;
}

interface BudgetItem {
  id: string;
  used: number;
  budget: number;
}

interface FieldErrors {
  provider?: string;
  model?: string;
  temperature?: string;
  max_tokens?: string;
  timeout_s?: string;
}

interface RouteErrors {
  routes?: string;
  fallback?: string;
}

interface TestResult {
  ok: boolean;
  provider: string;
  model: string;
  latency: number | null;
  error: string;
}

/* ------------------------------------------------------------------ */
/* pane                                                                */
/* ------------------------------------------------------------------ */

export function AiPane(): ReactNode {
  const tr = useT();
  const canWrite = useCanWrite();
  const config = useStore(selConfig);

  const [usage, setUsage] = useState<Dict | null>(null);
  const [usageError, setUsageError] = useState<unknown>(null);
  const [usageLoading, setUsageLoading] = useState(true);
  const [models, setModels] = useState<Dict | null>(null);
  const [modelsError, setModelsError] = useState<unknown>(null);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const seeded = useRef(false);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [temperature, setTemperature] = useState<number | null>(null);
  const [maxTokens, setMaxTokens] = useState<number | null>(null);
  const [timeoutS, setTimeoutS] = useState<number | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});

  const [routesText, setRoutesText] = useState('');
  const [fallbackText, setFallbackText] = useState('');
  const [routeErrors, setRouteErrors] = useState<RouteErrors>({});
  const [saving, setSaving] = useState(false);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [restartFields, setRestartFields] = useState<string[]>([]);

  const llmCfg = asDict(config?.llm);

  useEffect(() => {
    if (seeded.current || !config) return;
    seeded.current = true;
    setProvider(String(llmCfg.provider ?? ''));
    setModel(String(llmCfg.model ?? ''));
    setBaseUrl(String(llmCfg.base_url ?? ''));
    setTemperature(llmCfg.temperature === undefined || llmCfg.temperature === null ? null : Number(llmCfg.temperature));
    setMaxTokens(llmCfg.max_tokens === undefined || llmCfg.max_tokens === null ? null : Number(llmCfg.max_tokens));
    setTimeoutS(llmCfg.timeout_s === undefined || llmCfg.timeout_s === null ? null : Number(llmCfg.timeout_s));
    setRoutesText(jsonText(llmCfg.routes));
    setFallbackText(jsonText(llmCfg.fallback));
  }, [config, llmCfg]);

  async function loadUsage(): Promise<void> {
    setUsageLoading(true);
    setUsageError(null);
    try {
      setUsage(asDict(await api.getLlmUsage()));
    } catch (error) {
      setUsage(null);
      setUsageError(error);
    } finally {
      setUsageLoading(false);
    }
  }

  async function loadModels(): Promise<void> {
    setModelsLoading(true);
    setModelsError(null);
    try {
      setModels(asDict(await api.getLlmModels()));
    } catch (error) {
      setModels(null);
      setModelsError(error);
    } finally {
      setModelsLoading(false);
    }
  }

  useEffect(() => {
    void loadUsage();
    void loadModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await Promise.all([loadUsage(), loadModels()]);
    } finally {
      setRefreshing(false);
    }
  };

  /* ---- usage numbers ---- */
  const totals = asDict(pick(usage || {}, ['totals', 'usage', 'summary'], {}));
  const limits = asDict((usage || {}).limits);
  const circuit = circuitOf(usage || {});
  const budget = asDict((usage || {}).budget);
  const budgetTotal = num(budget.project_token_budget);
  const budgetUsed = num(pick(budget, ['used'], totals.total));
  const budgetRemaining = budget.remaining === null || budget.remaining === undefined
    ? null : num(budget.remaining);
  const projects = asDict((usage || {}).projects);
  const usageRows: BreakdownRow[] = [];
  const addBuckets = (source: unknown, kind: string): void => {
    for (const [name, bucket] of Object.entries(asDict(source))) {
      const item = asDict(bucket);
      usageRows.push({
        id: `${kind}:${name}`,
        kind,
        name,
        tokens: num(pick(item, ['total', 'tokens', 'tokens_used', 'used'], 0)),
        cost: num(pick(item, ['cost', 'cost_usd', 'total_cost'], 0)),
      });
    }
  };
  addBuckets((usage || {}).providers, tr('settings.ai.models.provider'));
  addBuckets((usage || {}).models, tr('settings.ai.models.model'));
  addBuckets((usage || {}).roles, tr('settings.ai.models.role'));
  const budgetItems: BudgetItem[] = budgetTotal > 0
    ? Object.entries(projects)
      .map(([id, bucket]) => ({
        id,
        used: num(pick(asDict(bucket), ['total', 'tokens_used', 'used'], 0)),
        budget: budgetTotal,
      }))
      .sort((a, b) => b.used - a.used)
    : [];
  const hasUsage = Boolean(totals.prompt || totals.completion || totals.total || totals.requests
    || usageRows.length || budgetTotal || circuit.state);

  /* ---- models ---- */
  const providerEntries = asList((models || {}).providers);
  const primary = asDict((models || {}).primary);
  const modelRows: ModelTableRow[] = (providerEntries.length ? providerEntries : Object.keys(primary).length ? [primary] : [])
    .map((entry, index) => {
      const health = modelHealth(entry);
      return {
        id: `${String(pick(entry, ['provider', 'name'], `model-${index}`))}:${String(pick(entry, ['model', 'model_id'], ''))}:${String(pick(entry, ['role'], ''))}`,
        provider: String(pick(entry, ['provider', 'name'], '—')),
        model: String(pick(entry, ['model', 'model_id'], '—')),
        role: String(pick(entry, ['role'], '—')),
        health: health.label,
        healthVariant: health.variant,
        latency: Number.isFinite(Number(pick(entry, ['last_latency_ms'], NaN))) ? num(pick(entry, ['last_latency_ms'], 0)) : null,
      };
    });
  const routes = asList((models || {}).routes);
  const fallbacks = asList((models || {}).fallback);

  /* ---- save ---- */
  const buildPatch = (): Json | null => {
    const errors: FieldErrors = {};
    if (temperature === null || !Number.isFinite(temperature)) errors.temperature = tr('settings.ai.config.empty_number', { label: tr('settings.ai.config.temperature') });
    if (maxTokens === null || !Number.isFinite(maxTokens)) errors.max_tokens = tr('settings.ai.config.empty_number', { label: tr('settings.ai.config.max_tokens') });
    if (timeoutS === null || !Number.isFinite(timeoutS)) errors.timeout_s = tr('settings.ai.config.empty_number', { label: tr('settings.ai.config.timeout') });
    setFieldErrors(errors);
    if (Object.keys(errors).length) return null;

    const patch: Json = {};
    if (provider !== String(llmCfg.provider ?? '')) patch.provider = provider;
    if (model !== String(llmCfg.model ?? '')) patch.model = model;
    if (baseUrl !== String(llmCfg.base_url ?? '')) patch.base_url = baseUrl;
    if (apiKey.trim() !== '') patch.api_key = apiKey.trim();
    if (Number(temperature) !== Number(llmCfg.temperature ?? NaN)) patch.temperature = Number(temperature);
    if (Number(maxTokens) !== Number(llmCfg.max_tokens ?? NaN)) patch.max_tokens = Number(maxTokens);
    if (Number(timeoutS) !== Number(llmCfg.timeout_s ?? NaN)) patch.timeout_s = Number(timeoutS);

    let parsedRoutes: unknown[];
    let parsedFallback: unknown[];
    const routeErrs: RouteErrors = {};
    try {
      parsedRoutes = parseJsonArray(routesText);
    } catch (error) {
      parsedRoutes = [];
      routeErrs.routes = jsonErrorLabel(tr, 'settings.ai.routes.label', error);
    }
    try {
      parsedFallback = parseJsonArray(fallbackText);
    } catch (error) {
      parsedFallback = [];
      routeErrs.fallback = jsonErrorLabel(tr, 'settings.ai.routes.fallback_label', error);
    }
    setRouteErrors(routeErrs);
    if (Object.keys(routeErrs).length) return null;

    if (!sameJson(parsedRoutes, llmCfg.routes === undefined ? [] : llmCfg.routes)) patch.routes = parsedRoutes;
    if (!sameJson(parsedFallback, llmCfg.fallback === undefined ? [] : llmCfg.fallback)) patch.fallback = parsedFallback;

    if (!Object.keys(patch).length) {
      toast(tr('settings.nothing_to_save'), 'info');
      return null;
    }
    return { llm: patch };
  };

  const save = async (): Promise<void> => {
    if (!canWrite) return;
    const payload = buildPatch();
    if (!payload) return;
    setSaving(true);
    try {
      const result = await submitConfigPatch(payload);
      setApiKey('');
      setRestartFields(result.restartRequired);
      if (result.restartRequired.length) {
        toast(tr('settings.saved_restart', {
          fields: result.restartRequired.map(restartFieldLabel).join(' · '),
        }), 'info');
      } else {
        toast(tr('settings.saved'), 'info');
      }
    } catch (error) {
      toastError(error);
    } finally {
      setSaving(false);
    }
  };

  const test = async (): Promise<void> => {
    setTesting(true);
    try {
      const raw = asDict(await api.testLlm());
      const result: TestResult = {
        ok: Boolean(raw.ok),
        provider: String(raw.provider ?? '—'),
        model: String(raw.model ?? '—'),
        latency: raw.latency_ms === undefined || raw.latency_ms === null ? null : num(raw.latency_ms),
        error: String(raw.error ?? ''),
      };
      setTestResult(result);
      if (result.ok) toast(tr('settings.ai.test.ok', { provider: result.provider, model: result.model }), 'info');
      else toast(tr('settings.ai.test.failed', { message: result.error || tr('settings.unknown') }), 'error');
    } catch (error) {
      const kind = errorKind(error);
      setTestResult({
        ok: false,
        provider: '—',
        model: '—',
        latency: null,
        error: kind === 'unavailable' ? tr('settings.ai.test.unavailable') : errorMessage(error),
      });
      if (kind !== 'unavailable') toastError(error);
    } finally {
      setTesting(false);
    }
  };

  const providerOptions = [
    { value: 'mock', label: 'mock' },
    { value: 'openai', label: 'openai' },
  ];
  if (provider && !providerOptions.some((option) => option.value === provider)) {
    providerOptions.push({ value: provider, label: provider });
  }
  const apiKeyConfigured = String(llmCfg.api_key ?? '') === '***' || Boolean(llmCfg.api_key);

  const circuitVariant = CIRCUIT_TONES[circuit.state] || 'neutral';

  const modelColumns = [
    {
      key: 'provider',
      header: tr('settings.ai.models.provider'),
      renderCell: (row: ModelTableRow) => <Text size="sm">{row.provider}</Text>,
    },
    {
      key: 'model',
      header: tr('settings.ai.models.model'),
      renderCell: (row: ModelTableRow) => <Text size="sm">{row.model}</Text>,
    },
    {
      key: 'role',
      header: tr('settings.ai.models.role'),
      renderCell: (row: ModelTableRow) => <Text size="sm">{row.role}</Text>,
    },
    {
      key: 'health',
      header: tr('settings.ai.models.health'),
      renderCell: (row: ModelTableRow) => <Badge label={row.health} variant={row.healthVariant} />,
    },
    {
      key: 'latency',
      header: tr('settings.ai.models.latency'),
      renderCell: (row: ModelTableRow) => (
        <Text size="sm" hasTabularNumbers>{row.latency === null ? '—' : `${row.latency}ms`}</Text>
      ),
    },
  ];

  return (
    <VStack gap={4}>
      <PaneSection
        title={tr('settings.ai.usage.title')}
        hint={tr('settings.ai.usage.hint')}
        actions={(
          <Button
            label={tr('settings.refresh')}
            size="sm"
            variant="ghost"
            isLoading={refreshing}
            onClick={() => { void refresh(); }}
          />
        )}
      >
        <VStack gap={3}>
          {usageLoading && !usage ? <LoadingLine label={tr('settings.ai.usage.loading')} /> : null}
          {usageError ? (
            <LoadError
              error={usageError}
              unavailableKey="settings.ai.usage.unavailable"
              forbiddenKey="settings.ai.usage.forbidden"
              errorKey="settings.ai.usage.error"
              onRetry={() => { void loadUsage(); }}
            />
          ) : null}
          {!usageLoading && !usageError && usage && !hasUsage ? (
            <EmptyState
              title={tr('settings.ai.usage.empty.title')}
              description={tr('settings.ai.usage.empty.text')}
              isCompact
            />
          ) : null}
          {usage && hasUsage ? (
            <VStack gap={3}>
              <Grid columns={{ minWidth: 170, max: 4 }} gap={3}>
                <MetricTile label={tr('settings.ai.tokens_in')} value={fmtInt(pick(totals, IN_KEYS))} />
                <MetricTile label={tr('settings.ai.tokens_out')} value={fmtInt(pick(totals, OUT_KEYS))} />
                <MetricTile label={tr('settings.ai.cost')} value={fmtCost(pick(totals, ['cost_usd', 'cost', 'total_cost', 'cost_total']))} />
                <MetricTile label={tr('settings.ai.requests')} value={fmtInt(pick(totals, ['requests', 'calls', 'count']))} />
                <MetricTile
                  label={tr('settings.ai.rpm')}
                  value={rateValue(limits, 'rpm_used', 'rpm_limit', totals, ['rpm', 'requests_per_minute'])}
                />
                <MetricTile
                  label={tr('settings.ai.tpm')}
                  value={rateValue(limits, 'tpm_used', 'tpm_limit', totals, ['tpm', 'tokens_per_minute'])}
                />
              </Grid>

              <HStack gap={3} align="center" wrap="wrap">
                <Text size="sm" color="secondary">{tr('settings.ai.circuit')}</Text>
                <HStack gap={1.5} align="center">
                  <StatusDot variant={circuitVariant} label={circuitLabel(circuit.state)} />
                  <Text size="sm">{circuitLabel(circuit.state)}</Text>
                </HStack>
                {circuit.failures > 0 ? (
                  <Text size="xsm" color="secondary">
                    {tr('settings.ai.circuit.failures', { count: circuit.failures })}
                  </Text>
                ) : null}
                {circuit.reason ? <Text size="xsm" color="secondary">{circuit.reason}</Text> : null}
              </HStack>

              <VStack gap={2}>
                <Heading level={4}>{tr('settings.ai.budget.title')}</Heading>
                {budgetTotal > 0 ? (
                  <VStack gap={3}>
                    <BudgetBar
                      label={tr('settings.ai.budget.title')}
                      used={budgetUsed}
                      budget={budgetTotal}
                      labelText={budgetRemaining === null ? undefined : tr('settings.ai.budget.remaining', { remaining: fmtInt(budgetRemaining) })}
                    />
                    {budgetItems.map((item) => (
                      <BudgetBar
                        key={item.id}
                        label={item.id}
                        used={item.used}
                        budget={item.budget}
                      />
                    ))}
                  </VStack>
                ) : (
                  <Text size="sm" color="secondary">{tr('settings.ai.budget.none')}</Text>
                )}
                {budget.exceeded ? <Banner status="error" title={tr('settings.ai.budget.exceeded')} /> : null}
              </VStack>

              {usageRows.length ? (
                <Table
                  data={usageRows}
                  columns={[
                    { key: 'kind', header: tr('settings.ai.breakdown.kind'), renderCell: (row: BreakdownRow) => <Badge label={row.kind} variant="neutral" /> },
                    { key: 'name', header: tr('settings.ai.models.model'), renderCell: (row: BreakdownRow) => <Text size="sm">{row.name}</Text> },
                    { key: 'tokens', header: tr('settings.ai.tokens'), renderCell: (row: BreakdownRow) => <Text size="sm" hasTabularNumbers>{fmtInt(row.tokens)}</Text> },
                    { key: 'cost', header: tr('settings.ai.cost'), renderCell: (row: BreakdownRow) => <Text size="sm" hasTabularNumbers>{fmtCost(row.cost)}</Text> },
                  ]}
                  idKey="id"
                  density="compact"
                  emptyState={false}
                />
              ) : null}
            </VStack>
          ) : null}
        </VStack>
      </PaneSection>

      <PaneSection
        title={tr('settings.ai.models.title')}
        hint={tr('settings.ai.models.hint')}
        actions={(
          <Button
            label={tr('settings.refresh')}
            size="sm"
            variant="ghost"
            isLoading={modelsLoading}
            onClick={() => { void loadModels(); }}
          />
        )}
      >
        <VStack gap={3}>
          {modelsLoading && !models ? <LoadingLine label={tr('settings.ai.models.loading')} /> : null}
          {modelsError ? (
            <LoadError
              error={modelsError}
              unavailableKey="settings.ai.models.unavailable"
              forbiddenKey="settings.ai.usage.forbidden"
              errorKey="settings.ai.models.error"
              onRetry={() => { void loadModels(); }}
            />
          ) : null}
          {models && !modelsError ? (
            modelRows.length || routes.length || fallbacks.length ? (
              <VStack gap={3}>
                {modelRows.length ? (
                  <Table
                    data={modelRows}
                    columns={modelColumns}
                    idKey="id"
                    density="compact"
                    emptyState={false}
                  />
                ) : null}
                {routes.length ? (
                  <VStack gap={1}>
                    <Heading level={4}>{tr('settings.ai.models.routes_title')}</Heading>
                    <MetadataList columns="multi" label={{ position: 'start' }}>
                    {routes.map((route, index) => (
                      <MetadataListItem key={`route-${index}`} label={String(pick(route, ['role'], '—'))}>
                        <Text size="sm">{`${String(pick(route, ['provider'], '?'))}/${String(pick(route, ['model'], '?'))}`}</Text>
                      </MetadataListItem>
                    ))}
                    </MetadataList>
                  </VStack>
                ) : null}
                <HStack gap={2} align="center" wrap="wrap">
                  <Text size="sm" color="secondary">{tr('settings.ai.models.fallback')}</Text>
                  <Text size="sm">
                    {fallbacks.length
                      ? fallbacks.map((entry) => `${String(pick(entry, ['provider'], '?'))}/${String(pick(entry, ['model'], '?'))}`).join(' → ')
                      : tr('settings.ai.models.none')}
                  </Text>
                </HStack>
              </VStack>
            ) : (
              <Banner status="info" title={tr('settings.ai.models.empty')} />
            )
          ) : null}
        </VStack>
      </PaneSection>

      <PaneSection
        title={tr('settings.ai.config.title')}
        hint={tr('settings.ai.config.hint')}
        actions={canWrite ? (
          <Button
            label={tr('common.save')}
            size="sm"
            variant="primary"
            isLoading={saving}
            onClick={() => { void save(); }}
          />
        ) : undefined}
      >
        {!canWrite ? <ViewerNote /> : null}
        <VStack gap={3}>
          <Grid columns={{ minWidth: 240, max: 2 }} gap={3}>
            <Selector
              label={tr('settings.ai.config.provider')}
              options={providerOptions}
              value={provider}
              onChange={(value) => setProvider(value || '')}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
            />
            <TextInput
              label={tr('settings.ai.config.model')}
              value={model}
              onChange={setModel}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              placeholder="mock-small"
            />
            <TextInput
              label={tr('settings.ai.config.base_url')}
              value={baseUrl}
              onChange={setBaseUrl}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              placeholder="https://api.example.com/v1"
            />
            <TextInput
              label={tr('settings.ai.config.api_key')}
              type="password"
              value={apiKey}
              onChange={setApiKey}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              description={tr('settings.ai.config.api_key_keep')}
              placeholder={apiKeyConfigured ? tr('settings.ai.config.api_key_set') : tr('settings.ai.config.api_key_empty')}
            />
            <NumberInput
              label={tr('settings.ai.config.temperature')}
              value={temperature}
              onChange={setTemperature}
              step={0.1}
              min={0}
              max={2}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              status={fieldErrors.temperature ? { type: 'error', message: fieldErrors.temperature } : undefined}
            />
            <NumberInput
              label={tr('settings.ai.config.max_tokens')}
              value={maxTokens}
              onChange={setMaxTokens}
              step={1}
              isIntegerOnly
              min={1}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              status={fieldErrors.max_tokens ? { type: 'error', message: fieldErrors.max_tokens } : undefined}
            />
            <NumberInput
              label={tr('settings.ai.config.timeout')}
              value={timeoutS}
              onChange={setTimeoutS}
              step={1}
              min={1}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              status={fieldErrors.timeout_s ? { type: 'error', message: fieldErrors.timeout_s } : undefined}
            />
          </Grid>
        </VStack>
      </PaneSection>

      <PaneSection
        title={tr('settings.ai.routes.title')}
        hint={tr('settings.ai.routes.hint')}
        actions={canWrite ? (
          <Button
            label={tr('common.save')}
            size="sm"
            variant="primary"
            isLoading={saving}
            onClick={() => { void save(); }}
          />
        ) : undefined}
      >
        <VStack gap={3}>
          {restartFields.length ? (
            <Banner
              status="warning"
              title={tr('settings.saved_restart', { fields: restartFields.map(restartFieldLabel).join(' · ') })}
              isDismissable
              onDismiss={() => setRestartFields([])}
            />
          ) : null}
          <Grid columns={{ minWidth: 280, max: 2 }} gap={3}>
            <TextArea
              label={tr('settings.ai.routes.label')}
              value={routesText}
              onChange={(value) => {
                setRoutesText(value);
                if (routeErrors.routes) setRouteErrors((prev) => ({ ...prev, routes: undefined }));
              }}
              rows={7}
              hasSpellCheck={false}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              status={routeErrors.routes ? { type: 'error', message: routeErrors.routes } : undefined}
            />
            <TextArea
              label={tr('settings.ai.routes.fallback_label')}
              value={fallbackText}
              onChange={(value) => {
                setFallbackText(value);
                if (routeErrors.fallback) setRouteErrors((prev) => ({ ...prev, fallback: undefined }));
              }}
              rows={7}
              hasSpellCheck={false}
              isDisabled={!canWrite}
              isReadOnly={!canWrite}
              status={routeErrors.fallback ? { type: 'error', message: routeErrors.fallback } : undefined}
            />
          </Grid>
          {canWrite ? <Text size="xsm" color="secondary">{tr('settings.unsaved_hint')}</Text> : null}
        </VStack>
      </PaneSection>

      <PaneSection title={tr('settings.ai.test.title')} hint={tr('settings.ai.test.hint')}>
        <VStack gap={3}>
          <HStack gap={2} align="center" wrap="wrap">
            <Button
              label={testing ? tr('settings.ai.test.testing') : tr('settings.ai.test.button')}
              size="sm"
              variant="secondary"
              isLoading={testing}
              isDisabled={!canWrite}
              onClick={() => { void test(); }}
            />
            {testResult ? null : <Text size="sm" color="secondary">{tr('settings.ai.test.never')}</Text>}
          </HStack>
          {testResult ? (
            <Banner
              status={testResult.ok ? 'success' : 'error'}
              title={testResult.ok
                ? tr('settings.ai.test.ok', { provider: testResult.provider, model: testResult.model })
                  + (testResult.latency === null ? '' : tr('settings.ai.test.latency', { latency: testResult.latency }))
                : tr('settings.ai.test.failed', { message: testResult.error || tr('settings.unknown') })}
            />
          ) : null}
        </VStack>
      </PaneSection>
    </VStack>
  );
}

/* ------------------------------------------------------------------ */
/* small components                                                    */
/* ------------------------------------------------------------------ */

function MetricTile({ label, value }: { label: string; value: string }): ReactNode {
  return (
    <Card variant="muted" padding={3}>
      <VStack gap={1}>
        <Text size="xsm" color="secondary">{label}</Text>
        <Text size="xl" weight="semibold" hasTabularNumbers>{value}</Text>
      </VStack>
    </Card>
  );
}

function BudgetBar({ label, used, budget, labelText }: {
  label: string;
  used: number;
  budget: number;
  labelText?: string;
}): ReactNode {
  const ratio = budget > 0 ? used / budget : 0;
  return (
    <VStack gap={1}>
      <HStack justify="between" gap={2} align="center" wrap="wrap">
        <Text size="sm">{label}</Text>
        <Text size="xsm" color="secondary" hasTabularNumbers>
          {`${fmtInt(used)} / ${fmtInt(budget)}`}
        </Text>
      </HStack>
      <ProgressBar
        label={label}
        value={Math.min(used, budget)}
        max={budget}
        isLabelHidden
        hasValueLabel
        variant={ratio >= 1 ? 'error' : ratio >= 0.9 ? 'warning' : 'accent'}
        formatValueLabel={() => (labelText || `${Math.round(ratio * 100)}%`)}
      />
    </VStack>
  );
}

function rateValue(limits: Dict, usedKey: string, limitKey: string, totals: Dict, fallback: string[]): string {
  const limit = num(pick(limits, [limitKey, limitKey.replace('_limit', '')], 0));
  const used = num(pick(limits, [usedKey], pick(totals, fallback)));
  if (limit > 0) return `${fmtInt(used)} / ${fmtInt(limit)}`;
  return fmtInt(used);
}

function jsonErrorLabel(
  tr: (key: string, params?: Record<string, string | number>, fallback?: string | null) => string,
  labelKey: string,
  error: unknown,
): string {
  const label = tr(labelKey);
  if (error instanceof Error && error.message === ARRAY_EXPECTED) {
    return tr('settings.ai.routes.array', { label });
  }
  const message = error instanceof Error ? error.message : String(error);
  return tr('settings.ai.routes.invalid', { label, message });
}
