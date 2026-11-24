/**
 * CapabilitiesPane — skills, plugins and MCP servers.
 *
 * Each catalog loads independently and degrades on its own: a missing
 * endpoint shows "not enabled", 403 shows "forbidden", anything else shows an
 * inline retry.  Write controls disappear for a known viewer.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { List, ListItem } from '@astryxdesign/core/List';
import { Selector } from '@astryxdesign/core/Selector';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Text } from '@astryxdesign/core/Text';
import { TextArea } from '@astryxdesign/core/TextArea';
import { api } from '../../api/client';
import { t, useT } from '../../i18n';
import { useCanWrite } from '../../session';
import { useStore, type SfoState } from '../../state/store';
import { toast, toastError } from '../../ui/toast';
import {
  LoadingLine,
  LoadError,
  PaneSection,
  ViewerNote,
  errorKind,
  errorMessage,
} from './common';

const selProjectsList = (s: SfoState): SfoState['projects'] => s.projects;

interface SkillRow extends Record<string, unknown> {
  id: string;
  name: string;
  version: string;
  description: string;
  path: string;
  valid: boolean;
  enabled: boolean;
  scopes: string[];
  error: string;
}

interface PluginRow extends Record<string, unknown> {
  id: string;
  name: string;
  version: string;
  loaded: boolean;
  trusted: boolean;
  description: string;
  error: string;
  contributes: string[];
}

interface McpServerRow extends Record<string, unknown> {
  name: string;
  state: string;
  connected: boolean;
  command: string;
  lastError: string;
  toolCount: number;
}

interface ToolRow extends Record<string, unknown> {
  name: string;
  description: string;
}

function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value);
}

function skillRows(data: unknown): SkillRow[] {
  const payload = data && typeof data === 'object' ? data as Record<string, any> : {};
  const rows = Array.isArray(payload.skills) ? payload.skills
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? data : [];
  return (rows as Record<string, any>[]).map((row, index) => {
    const scopes = Array.isArray(row.enabled_projects) ? row.enabled_projects
      : Array.isArray(row.enabled_in) ? row.enabled_in
        : Array.isArray(row.projects) ? row.projects : [];
    const valid = row.valid !== false && !row.error;
    return {
      id: text(row.id || row.name || `skill-${index}`),
      name: text(row.name || row.id || `skill-${index}`),
      version: text(row.version),
      description: text(row.description),
      path: text(row.path),
      valid,
      enabled: Boolean(row.enabled === true || row.enabled_global === true || scopes.length),
      scopes: scopes.map(text),
      error: text(row.error),
    };
  });
}

function pluginRows(data: unknown): PluginRow[] {
  const payload = data && typeof data === 'object' ? data as Record<string, any> : {};
  const rows = Array.isArray(payload.plugins) ? payload.plugins
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? data : [];
  return (rows as Record<string, any>[]).map((row, index) => {
    const contributes = row.contributes && typeof row.contributes === 'object' ? row.contributes : {};
    const bits = [
      ...(Array.isArray(contributes.tools) ? contributes.tools : []).map((name: unknown) => `tool:${text(name)}`),
      ...(Array.isArray(contributes.skills) ? contributes.skills : []).map((name: unknown) => `skill:${text(name)}`),
    ];
    return {
      id: text(row.id || row.name || `plugin-${index}`),
      name: text(row.name || row.id || `plugin-${index}`),
      version: text(row.version),
      loaded: Boolean(row.loaded === true || row.active === true || row.state === 'loaded' || row.status === 'loaded'),
      trusted: row.trusted !== false && row.allow_untrusted !== true,
      description: text(row.description),
      error: text(row.error),
      contributes: bits,
    };
  });
}

function serverRows(data: unknown): McpServerRow[] {
  const payload = data && typeof data === 'object' ? data as Record<string, any> : {};
  const rows = Array.isArray(payload.servers) ? payload.servers
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? data : [];
  return (rows as Record<string, any>[]).map((row, index) => {
    const state = text(row.state).toLowerCase();
    const connected = row.connected === true || state === 'ready' || state === 'starting';
    return {
      name: text(row.name || row.id || `server-${index}`),
      state,
      connected,
      command: [text(row.command), ...(Array.isArray(row.args) ? row.args.map(text) : [])]
        .filter(Boolean).join(' '),
      lastError: text(row.last_error || row.error),
      toolCount: Number(row.tool_count ?? (Array.isArray(row.tools) ? row.tools.length : 0)) || 0,
    };
  });
}

function toolRows(data: unknown): ToolRow[] {
  const payload = data && typeof data === 'object' ? data as Record<string, any> : {};
  const rows = Array.isArray(payload.tools) ? payload.tools
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? data : [];
  return (rows as Record<string, any>[]).map((row, index) => ({
    name: text(row.name || row.id || `tool-${index}`),
    description: text(row.description),
  }));
}

function connectionVariant(row: McpServerRow): 'success' | 'warning' | 'error' | 'neutral' {
  if (row.state === 'ready') return 'success';
  if (row.state === 'starting') return 'warning';
  if (row.state === 'failed') return 'error';
  return 'neutral';
}

function connectionLabel(row: McpServerRow): string {
  if (row.state === 'ready') return t('settings.cap.mcp.connected');
  if (row.state === 'starting') return t('settings.cap.mcp.connecting');
  if (row.state === 'failed') return t('settings.cap.mcp.failed');
  return t('settings.cap.mcp.disconnected');
}

export function CapabilitiesPane(): ReactNode {
  const tr = useT();
  const canWrite = useCanWrite();
  const projects = useStore(selProjectsList);

  const [scope, setScope] = useState('');
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  const [skillsError, setSkillsError] = useState<unknown>(null);
  const [skillsLoading, setSkillsLoading] = useState(true);

  const [plugins, setPlugins] = useState<PluginRow[] | null>(null);
  const [pluginsError, setPluginsError] = useState<unknown>(null);
  const [pluginsLoading, setPluginsLoading] = useState(true);

  const [servers, setServers] = useState<McpServerRow[] | null>(null);
  const [serversError, setServersError] = useState<unknown>(null);
  const [serversLoading, setServersLoading] = useState(true);

  const [busy, setBusy] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  async function loadSkills(projectId: string): Promise<void> {
    setSkillsLoading(true);
    setSkillsError(null);
    try {
      setSkills(skillRows(await api.getSkills(projectId)));
    } catch (error) {
      setSkills(null);
      setSkillsError(error);
    } finally {
      setSkillsLoading(false);
    }
  }

  async function loadPlugins(): Promise<void> {
    setPluginsLoading(true);
    setPluginsError(null);
    try {
      setPlugins(pluginRows(await api.getPlugins()));
    } catch (error) {
      setPlugins(null);
      setPluginsError(error);
    } finally {
      setPluginsLoading(false);
    }
  }

  async function loadServers(): Promise<void> {
    setServersLoading(true);
    setServersError(null);
    try {
      setServers(serverRows(await api.getMcpServers()));
    } catch (error) {
      setServers(null);
      setServersError(error);
    } finally {
      setServersLoading(false);
    }
  }

  useEffect(() => {
    void loadSkills(scope);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  useEffect(() => {
    void loadPlugins();
    void loadServers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await Promise.all([loadSkills(scope), loadPlugins(), loadServers()]);
    } finally {
      setRefreshing(false);
    }
  };

  const toggleSkill = async (skill: SkillRow, enable: boolean): Promise<void> => {
    setBusy(`skill:${skill.id}`);
    try {
      await api.setSkillEnabled(skill.id, enable, scope);
      toast(tr('settings.cap.skills.toggled', {
        action: tr(enable ? 'settings.cap.skills.enable' : 'settings.cap.skills.disable'),
        id: skill.id,
      }), 'info');
      await loadSkills(scope);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy('');
    }
  };

  const togglePlugin = async (plugin: PluginRow): Promise<void> => {
    setBusy(`plugin:${plugin.id}`);
    try {
      if (plugin.loaded) await api.unloadPlugin(plugin.id);
      else await api.loadPlugin(plugin.id);
      toast(tr('settings.cap.plugins.toggled', {
        action: tr(plugin.loaded ? 'settings.cap.plugins.unload' : 'settings.cap.plugins.load'),
        id: plugin.id,
      }), 'info');
      await loadPlugins();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy('');
    }
  };

  const toggleServer = async (server: McpServerRow): Promise<void> => {
    setBusy(`mcp:${server.name}`);
    try {
      if (server.connected) await api.disconnectMcp(server.name);
      else await api.connectMcp(server.name);
      toast(tr('settings.cap.mcp.toggled', {
        action: tr(server.connected ? 'settings.cap.mcp.disconnect' : 'settings.cap.mcp.connect'),
        name: server.name,
      }), 'info');
      await loadServers();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy('');
    }
  };

  const scopeOptions = [
    { value: '', label: tr('settings.cap.skills.global') },
    ...projects.map((project) => ({ value: project.id, label: project.name || project.id })),
  ];

  return (
    <VStack gap={4}>
      <PaneSection
        title={tr('settings.cap.title')}
        hint={tr('settings.cap.hint')}
        actions={(
          <Button
            label={tr('settings.cap.refresh')}
            size="sm"
            variant="ghost"
            isLoading={refreshing}
            onClick={() => { void refresh(); }}
          />
        )}
      >
        <VStack gap={2}>
          {!canWrite ? <ViewerNote /> : null}
          <HStack gap={2} align="center" wrap="wrap">
            <Badge label={`Skills ${skills ? skills.length : '—'}`} variant="neutral" />
            <Badge label={`Plugins ${plugins ? plugins.length : '—'}`} variant="neutral" />
            <Badge label={`MCP ${servers ? servers.length : '—'}`} variant="neutral" />
          </HStack>
        </VStack>
      </PaneSection>

      <PaneSection
        title={tr('settings.cap.skills.title')}
        hint={tr('settings.cap.skills.hint')}
        actions={(
          <Selector
            label={tr('settings.cap.skills.scope')}
            options={scopeOptions}
            value={scope}
            onChange={(value) => setScope(value || '')}
            size="sm"
            variant="secondary"
          />
        )}
      >
        <VStack gap={3}>
          {skillsLoading && !skills ? <LoadingLine label={tr('settings.cap.loading')} /> : null}
          {skillsError ? (
            <LoadError
              error={skillsError}
              unavailableKey="settings.cap.skills.unavailable"
              forbiddenKey="settings.cap.forbidden"
              errorKey="settings.cap.error"
              onRetry={() => { void loadSkills(scope); }}
            />
          ) : null}
          {!skillsLoading && !skillsError && skills && skills.length === 0 ? (
            <Text size="sm" color="secondary">{tr('settings.cap.skills.empty')}</Text>
          ) : null}
          {skills && skills.length ? (
            <List hasDividers density="compact">
              {skills.map((skill) => (
                <ListItem
                  key={skill.id}
                  align="start"
                  label={(
                    <HStack gap={2} align="center" wrap="wrap">
                      <Text size="sm" weight="semibold">{skill.name}</Text>
                      {skill.version ? <Badge label={`v${skill.version}`} variant="neutral" /> : null}
                      <Badge
                        label={tr(skill.valid ? 'settings.cap.skills.valid' : 'settings.cap.skills.invalid')}
                        variant={skill.valid ? 'success' : 'error'}
                      />
                      <Badge
                        label={tr(skill.enabled ? 'settings.cap.skills.enabled' : 'settings.cap.skills.disabled')}
                        variant={skill.enabled ? 'info' : 'neutral'}
                      />
                    </HStack>
                  )}
                  description={(
                    <VStack gap={0.5}>
                      {skill.description ? <Text size="sm" color="secondary">{skill.description}</Text> : null}
                      {skill.path ? <Text size="xsm" color="secondary">{skill.path}</Text> : null}
                      {!skill.valid && skill.error ? <Text size="xsm" color="secondary">{skill.error}</Text> : null}
                      {skill.scopes.length ? (
                        <Text size="xsm" color="secondary">
                          {tr('settings.cap.skills.enabled_in', { scopes: skill.scopes.join(', ') })}
                        </Text>
                      ) : null}
                    </VStack>
                  )}
                  endContent={canWrite && skill.valid ? (
                    <Button
                      label={tr(skill.enabled ? 'settings.cap.skills.disable' : 'settings.cap.skills.enable')}
                      size="sm"
                      variant={skill.enabled ? 'ghost' : 'secondary'}
                      isLoading={busy === `skill:${skill.id}`}
                      onClick={() => { void toggleSkill(skill, !skill.enabled); }}
                    />
                  ) : undefined}
                />
              ))}
            </List>
          ) : null}
        </VStack>
      </PaneSection>

      <PaneSection title={tr('settings.cap.plugins.title')} hint={tr('settings.cap.plugins.hint')}>
        <VStack gap={3}>
          {pluginsLoading && !plugins ? <LoadingLine label={tr('settings.cap.loading')} /> : null}
          {pluginsError ? (
            <LoadError
              error={pluginsError}
              unavailableKey="settings.cap.plugins.unavailable"
              forbiddenKey="settings.cap.forbidden"
              errorKey="settings.cap.error"
              onRetry={() => { void loadPlugins(); }}
            />
          ) : null}
          {!pluginsLoading && !pluginsError && plugins && plugins.length === 0 ? (
            <Text size="sm" color="secondary">{tr('settings.cap.plugins.empty')}</Text>
          ) : null}
          {plugins && plugins.length ? (
            <List hasDividers density="compact">
              {plugins.map((plugin) => (
                <ListItem
                  key={plugin.id}
                  align="start"
                  label={(
                    <HStack gap={2} align="center" wrap="wrap">
                      <Text size="sm" weight="semibold">{plugin.name}</Text>
                      {plugin.version ? <Badge label={`v${plugin.version}`} variant="neutral" /> : null}
                      <Badge
                        label={tr(plugin.loaded ? 'settings.cap.plugins.loaded' : 'settings.cap.plugins.not_loaded')}
                        variant={plugin.loaded ? 'success' : 'neutral'}
                      />
                      <Badge
                        label={tr(plugin.trusted ? 'settings.cap.plugins.trusted' : 'settings.cap.plugins.untrusted')}
                        variant={plugin.trusted ? 'neutral' : 'warning'}
                      />
                    </HStack>
                  )}
                  description={(
                    <VStack gap={0.5}>
                      {plugin.description ? <Text size="sm" color="secondary">{plugin.description}</Text> : null}
                      {plugin.error ? <Text size="xsm" color="secondary">{plugin.error}</Text> : null}
                      {plugin.contributes.length ? (
                        <HStack gap={1} wrap="wrap">
                          {plugin.contributes.map((item) => (
                            <Badge key={item} label={item} variant="info" />
                          ))}
                        </HStack>
                      ) : null}
                    </VStack>
                  )}
                  endContent={canWrite ? (
                    <Button
                      label={tr(plugin.loaded ? 'settings.cap.plugins.unload' : 'settings.cap.plugins.load')}
                      size="sm"
                      variant={plugin.loaded ? 'ghost' : 'secondary'}
                      isLoading={busy === `plugin:${plugin.id}`}
                      onClick={() => { void togglePlugin(plugin); }}
                    />
                  ) : undefined}
                />
              ))}
            </List>
          ) : null}
        </VStack>
      </PaneSection>

      <PaneSection title={tr('settings.cap.mcp.title')} hint={tr('settings.cap.mcp.hint')}>
        <VStack gap={3}>
          {serversLoading && !servers ? <LoadingLine label={tr('settings.cap.loading')} /> : null}
          {serversError ? (
            <LoadError
              error={serversError}
              unavailableKey="settings.cap.mcp.unavailable"
              forbiddenKey="settings.cap.forbidden"
              errorKey="settings.cap.error"
              onRetry={() => { void loadServers(); }}
            />
          ) : null}
          {!serversLoading && !serversError && servers && servers.length === 0 ? (
            <Text size="sm" color="secondary">{tr('settings.cap.mcp.empty')}</Text>
          ) : null}
          {servers && servers.length ? servers.map((server) => (
            <McpServerCard
              key={server.name}
              server={server}
              canWrite={canWrite}
              isBusy={busy === `mcp:${server.name}`}
              onToggle={() => { void toggleServer(server); }}
            />
          )) : null}
        </VStack>
      </PaneSection>
    </VStack>
  );
}

/* ------------------------------------------------------------------ */
/* one MCP server: connect toggle + lazy tool list + call test         */
/* ------------------------------------------------------------------ */

function McpServerCard({ server, canWrite, isBusy, onToggle }: {
  server: McpServerRow;
  canWrite: boolean;
  isBusy: boolean;
  onToggle: () => void;
}): ReactNode {
  const tr = useT();
  const [expanded, setExpanded] = useState(false);
  return (
    <Card padding={4}>
      <VStack gap={3}>
        <HStack justify="between" align="start" gap={3} wrap="wrap">
          <VStack gap={1}>
            <HStack gap={2} align="center" wrap="wrap">
              <StatusDot variant={connectionVariant(server)} label={connectionLabel(server)} />
              <Text size="sm" weight="semibold">{server.name}</Text>
              <Badge label={connectionLabel(server)} variant={connectionVariant(server)} />
              <Badge
                label={tr('settings.cap.mcp.tool_count', { count: server.toolCount })}
                variant="neutral"
              />
            </HStack>
            {server.command ? <Text size="xsm" color="secondary">{server.command}</Text> : null}
            {server.lastError ? <Text size="xsm" color="secondary">{server.lastError}</Text> : null}
          </VStack>
          <HStack gap={2} align="center" wrap="wrap">
            <Button
              label={expanded ? tr('common.close') : tr('settings.cap.mcp.tools.load')}
              size="sm"
              variant="ghost"
              onClick={() => setExpanded((prev) => !prev)}
            />
            {canWrite ? (
              <Button
                label={tr(server.connected ? 'settings.cap.mcp.disconnect' : 'settings.cap.mcp.connect')}
                size="sm"
                variant={server.connected ? 'ghost' : 'secondary'}
                isLoading={isBusy}
                onClick={onToggle}
              />
            ) : null}
          </HStack>
        </HStack>
        {expanded ? <McpToolsPanel server={server.name} canWrite={canWrite} /> : null}
      </VStack>
    </Card>
  );
}

function McpToolsPanel({ server, canWrite }: { server: string; canWrite: boolean }): ReactNode {
  const tr = useT();
  const [tools, setTools] = useState<ToolRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tool, setTool] = useState('');
  const [args, setArgs] = useState('{}');
  const [argsError, setArgsError] = useState('');
  const [calling, setCalling] = useState(false);
  const [result, setResult] = useState('');
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api.getMcpTools(server)
      .then((data) => { if (alive) setTools(toolRows(data)); })
      .catch((err) => { if (alive) { setTools(null); setError(err); } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [server]);

  const call = async (): Promise<void> => {
    if (!tool) return;
    setArgsError('');
    setPending(false);
    let parsed: unknown;
    try {
      parsed = JSON.parse(args || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error(ARGS_OBJECT_EXPECTED);
      }
    } catch (err) {
      const message = err instanceof Error && err.message === ARGS_OBJECT_EXPECTED
        ? tr('settings.cap.mcp.args.invalid', { message: tr('settings.cap.mcp.args.object') })
        : tr('settings.cap.mcp.args.invalid', { message: err instanceof Error ? err.message : String(err) });
      setArgsError(message);
      return;
    }
    setCalling(true);
    try {
      const data = await api.callMcp({ server, tool, args: parsed });
      const textBody = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
      setResult(textBody);
      const dataObj = data && typeof data === 'object' ? data as Record<string, unknown> : {};
      if (dataObj.status === 'pending') {
        setPending(true);
        toast(tr('settings.cap.mcp.call.need_approval'), 'info');
      } else {
        toast(tr('settings.cap.mcp.call.ok'), 'info');
      }
    } catch (err) {
      const kind = errorKind(err);
      const message = kind === 'forbidden'
        ? tr('settings.cap.mcp.call.need_approval')
        : tr('settings.cap.mcp.call.failed', { message: errorMessage(err) });
      setResult(message);
      if (kind === 'forbidden') {
        setPending(true);
        toast(tr('settings.cap.mcp.call.need_approval'), 'info');
      } else {
        toastError(err);
      }
    } finally {
      setCalling(false);
    }
  };

  if (loading) return <LoadingLine label={tr('settings.cap.mcp.tools.loading')} />;
  if (error) {
    return (
      <LoadError
        error={error}
        unavailableKey="settings.cap.mcp.tools.unavailable"
        forbiddenKey="settings.cap.forbidden"
        errorKey="settings.cap.error"
      />
    );
  }
  if (!tools || tools.length === 0) {
    return <Text size="sm" color="secondary">{tr('settings.cap.mcp.tools.empty')}</Text>;
  }

  const options = tools.map((row) => ({ value: row.name, label: row.name }));
  const selected = tools.find((row) => row.name === tool);

  return (
    <VStack gap={3}>
      <List hasDividers density="compact">
        {tools.map((row) => (
          <ListItem
            key={row.name}
            label={<Text size="sm" weight="semibold">{row.name}</Text>}
            description={row.description ? <Text size="xsm" color="secondary">{row.description}</Text> : undefined}
          />
        ))}
      </List>
      {canWrite ? (
        <VStack gap={2}>
          <Selector
            label={tr('settings.cap.mcp.tool')}
            options={options}
            value={tool}
            onChange={(value) => setTool(value || '')}
            placeholder={tr('settings.cap.mcp.select_tool')}
            size="sm"
          />
          {selected?.description ? (
            <Text size="xsm" color="secondary">{selected.description}</Text>
          ) : null}
          <TextArea
            label={tr('settings.cap.mcp.args')}
            value={args}
            onChange={(value) => { setArgs(value); if (argsError) setArgsError(''); }}
            rows={4}
            hasSpellCheck={false}
            status={argsError ? { type: 'error', message: argsError } : undefined}
          />
          <HStack gap={2} align="center">
            <Button
              label={calling ? tr('settings.cap.mcp.calling') : tr('settings.cap.mcp.call')}
              size="sm"
              variant="secondary"
              isLoading={calling}
              isDisabled={!tool}
              onClick={() => { void call(); }}
            />
          </HStack>
          {pending ? <Banner status="warning" title={tr('settings.cap.mcp.call.need_approval')} /> : null}
          {result ? (
            <CodeBlock
              code={result}
              title={tr('settings.cap.mcp.result')}
              language="json"
              size="sm"
              isWrapped
              width="100%"
              maxHeight={240}
            />
          ) : null}
        </VStack>
      ) : null}
    </VStack>
  );
}

const ARGS_OBJECT_EXPECTED = 'SFO_MCP_ARGS_OBJECT';
