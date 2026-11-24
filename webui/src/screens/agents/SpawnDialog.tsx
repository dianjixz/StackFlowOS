/**
 * SpawnDialog.tsx — "派工": pick a role, a project and (optionally) one task,
 * then `POST /api/agents`.  Viewer roles never reach this dialog.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Dialog, DialogHeader } from '@astryxdesign/core/Dialog';
import { Selector } from '@astryxdesign/core/Selector';
import { Button } from '@astryxdesign/core/Button';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { api } from '../../api/client';
import { t } from '../../i18n';
import { loadState, type Project } from '../../state/store';
import { roleLabel, statusLabel } from '../../ui/format';
import { toast, toastError } from '../../ui/toast';
import { ROLE_ORDER } from './common';
import { normalizeTasks, type AgentTask } from './types';

export interface SpawnDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  projects: Project[];
}

export function SpawnDialog({ isOpen, onOpenChange, projects }: SpawnDialogProps): ReactNode {
  const [role, setRole] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [errors, setErrors] = useState<{ role?: string; project?: string }>({});
  const [busy, setBusy] = useState(false);

  /* Reset the form each time the dialog opens. */
  useEffect(() => {
    if (!isOpen) return;
    setRole(null);
    setProjectId(null);
    setTaskId(null);
    setTasks([]);
    setErrors({});
    setBusy(false);
  }, [isOpen]);

  /* The task list follows the selected project. */
  useEffect(() => {
    if (!isOpen || !projectId) {
      setTasks([]);
      return undefined;
    }
    let alive = true;
    setTasksLoading(true);
    api.getTasks(projectId)
      .then((data) => {
        if (alive) setTasks(normalizeTasks(data));
      })
      .catch((error) => {
        console.warn('[agents] could not load tasks for spawn dialog', error);
        if (alive) setTasks([]);
      })
      .finally(() => {
        if (alive) setTasksLoading(false);
      });
    return () => { alive = false; };
  }, [isOpen, projectId]);

  const submit = async (): Promise<void> => {
    const nextErrors: { role?: string; project?: string } = {};
    if (!role) nextErrors.role = t('agents.spawn.role_required');
    if (!projectId) nextErrors.project = t('agents.spawn.project_required');
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length || !role || !projectId) return;
    setBusy(true);
    try {
      const result = await api.spawnAgent(role, projectId, taskId) as { id?: string; display_name?: string } | null;
      const name = result?.display_name || roleLabel(role);
      toast(t('agents.spawn.success', { name }), 'info');
      await loadState({ quiet: true }).catch(() => null);
      onOpenChange(false);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      purpose="form"
      width={480}
      padding={5}
    >
      <VStack gap={4}>
        <DialogHeader
          title={t('agents.spawn.title')}
          subtitle={t('agents.spawn.subtitle')}
          onOpenChange={onOpenChange}
        />
        <VStack gap={3}>
          <Selector
            label={t('agents.spawn.role_label')}
            placeholder={t('agents.spawn.role')}
            options={ROLE_ORDER.map((value) => ({ value, label: roleLabel(value) }))}
            value={role}
            onChange={(value) => {
              setRole(value);
              setErrors((prev) => ({ ...prev, role: undefined }));
            }}
            hasClear
            status={errors.role ? { type: 'error', message: errors.role } : undefined}
          />
          <Selector
            label={t('agents.spawn.project_label')}
            placeholder={t('agents.spawn.project')}
            options={projects.map((project) => ({ value: project.id, label: project.name || project.id }))}
            value={projectId}
            onChange={(value) => {
              setProjectId(value);
              setTaskId(null);
              setErrors((prev) => ({ ...prev, project: undefined }));
            }}
            hasClear
            status={errors.project ? { type: 'error', message: errors.project } : undefined}
          />
          <Selector
            label={t('agents.spawn.task')}
            placeholder={t('agents.spawn.task_none')}
            options={tasks.map((task) => ({
              value: task.id,
              label: task.title || task.id,
              description: statusLabel(task.status),
            }))}
            value={taskId}
            onChange={setTaskId}
            hasClear
            isDisabled={!projectId || tasksLoading}
            description={tasksLoading ? t('agents.spawn.tasks_loading') : undefined}
          />
        </VStack>
        <HStack justify="end" gap={2}>
          <Button
            label={t('agents.spawn.cancel')}
            variant="ghost"
            onClick={() => onOpenChange(false)}
            isDisabled={busy}
          />
          <Button
            label={t('agents.spawn.submit')}
            variant="primary"
            onClick={() => { void submit(); }}
            isLoading={busy}
          />
        </HStack>
      </VStack>
    </Dialog>
  );
}
