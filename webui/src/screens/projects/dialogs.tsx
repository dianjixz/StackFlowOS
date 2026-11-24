/**
 * dialogs.tsx — the write forms for the Projects screen.
 *
 * Every dialog owns its own fields and its own API call, then hands the result
 * back through `onDone` so the caller can refresh the snapshot and navigate.
 * Errors go through the shared `toastError()` bridge.
 */

import { useState, type ReactNode } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { Dialog, DialogHeader } from '@astryxdesign/core/Dialog';
import { Layout, LayoutContent, LayoutFooter, HStack, VStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { TextArea } from '@astryxdesign/core/TextArea';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Selector } from '@astryxdesign/core/Selector';
import { api, isUnsupported } from '../../api/client';
import { t } from '../../i18n';
import { toast, toastError } from '../../ui/toast';
import { roleLabel } from '../../ui/format';
import type { ProjectCard } from './types';

const ROLES = ['lead', 'architect', 'coder', 'tester', 'reviewer', 'researcher', 'aggregator', 'assistant'];

interface DialogShellProps {
  isOpen: boolean;
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer: ReactNode;
  onOpenChange: (isOpen: boolean) => void;
  width?: number;
}

function DialogShell({ isOpen, title, subtitle, children, footer, onOpenChange, width = 560 }: DialogShellProps): ReactNode {
  return (
    <Dialog isOpen={isOpen} onOpenChange={onOpenChange} width={width} purpose="form">
      <Layout
        height="fill"
        padding={4}
        header={<DialogHeader title={title} subtitle={subtitle} onOpenChange={onOpenChange} />}
        content={<LayoutContent>{children}</LayoutContent>}
        footer={<LayoutFooter><HStack gap={2} justify="end" wrap="wrap">{footer}</HStack></LayoutFooter>}
      />
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/* create project                                                      */
/* ------------------------------------------------------------------ */

export function CreateProjectDialog({
  isOpen,
  onOpenChange,
  onDone,
}: {
  isOpen: boolean;
  onOpenChange: (isOpen: boolean) => void;
  onDone: (projectId: string) => void | Promise<void>;
}): ReactNode {
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const close = (): void => {
    setBusy(false);
    setError('');
    setName('');
    setGoal('');
    onOpenChange(false);
  };

  const submit = async (): Promise<void> => {
    const projectName = name.trim();
    const projectGoal = goal.trim();
    if (!projectName && !projectGoal) {
      setError(t('prj.create.need_input'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const card = (await api.createProject(
        projectName || projectGoal.slice(0, 24),
        projectGoal || projectName,
      )) as ProjectCard;
      toast(t('prj.create.done', { name: card.name || card.id }), 'info');
      close();
      await onDone(card.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      toastError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      isOpen={isOpen}
      title={t('prj.create.title')}
      subtitle={t('prj.create.text')}
      onOpenChange={(open) => { if (!open) close(); }}
      footer={(
        <>
          <Button label={t('common.cancel')} variant="ghost" onClick={close} />
          <Button label={t('prj.list.create')} variant="primary" isLoading={busy} clickAction={submit} />
        </>
      )}
    >
      <VStack gap={4}>
        <TextInput
          label={t('prj.create.name')}
          placeholder={t('prj.create.name.placeholder')}
          description={t('prj.create.name.hint')}
          value={name}
          onChange={setName}
          status={error ? { type: 'error', message: error } : undefined}
          width="100%"
        />
        <TextArea
          label={t('prj.create.goal')}
          placeholder={t('prj.create.goal.placeholder')}
          value={goal}
          onChange={setGoal}
          rows={4}
          width="100%"
        />
      </VStack>
    </DialogShell>
  );
}

/* ------------------------------------------------------------------ */
/* attach workspace                                                    */
/* ------------------------------------------------------------------ */

interface AttachResult {
  id?: string;
  name?: string;
  existing?: boolean;
}

export function AttachWorkspaceDialog({
  isOpen,
  onOpenChange,
  onDone,
}: {
  isOpen: boolean;
  onOpenChange: (isOpen: boolean) => void;
  onDone: (projectId: string) => void | Promise<void>;
}): ReactNode {
  const [path, setPath] = useState('');
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [allowWrites, setAllowWrites] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const close = (): void => {
    setBusy(false);
    setError('');
    setPath('');
    setName('');
    setGoal('');
    setAllowWrites(false);
    onOpenChange(false);
  };

  const submit = async (): Promise<void> => {
    const dir = path.trim();
    if (!dir) {
      setError(t('prj.attach.path.required'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const card = (await api.attachProject({
        path: dir,
        name: name.trim(),
        goal: goal.trim(),
        allow_writes: allowWrites,
        confirm: dir,
      })) as AttachResult;
      const label = card.name || dir;
      toast(t(card.existing ? 'prj.attach.existing' : 'prj.attach.done', { name: label }), 'info');
      close();
      if (card.id) await onDone(card.id);
    } catch (cause) {
      setError(isUnsupported(cause)
        ? t('prj.attach.unsupported')
        : cause instanceof Error ? cause.message : String(cause));
      toastError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      isOpen={isOpen}
      title={t('prj.attach.title')}
      subtitle={t('prj.attach.text')}
      onOpenChange={(open) => { if (!open) close(); }}
      footer={(
        <>
          <Button label={t('common.cancel')} variant="ghost" onClick={close} />
          <Button label={t('prj.list.attach')} variant="primary" isLoading={busy} clickAction={submit} />
        </>
      )}
    >
      <VStack gap={4}>
        <TextInput
          label={t('prj.attach.path')}
          placeholder={t('prj.attach.path.placeholder')}
          description={t('prj.attach.path.hint')}
          value={path}
          onChange={setPath}
          status={error ? { type: 'error', message: error } : undefined}
          width="100%"
        />
        <TextInput
          label={t('prj.attach.name')}
          placeholder={t('prj.attach.name.placeholder')}
          value={name}
          onChange={setName}
          width="100%"
        />
        <TextInput
          label={t('prj.attach.goal')}
          placeholder={t('prj.attach.goal.placeholder')}
          value={goal}
          onChange={setGoal}
          width="100%"
        />
        <CheckboxInput
          label={t('prj.attach.allow')}
          value={allowWrites}
          onChange={setAllowWrites}
        />
      </VStack>
    </DialogShell>
  );
}

/* ------------------------------------------------------------------ */
/* create task                                                         */
/* ------------------------------------------------------------------ */

export function CreateTaskDialog({
  isOpen,
  projectId,
  onOpenChange,
  onDone,
}: {
  isOpen: boolean;
  projectId: string;
  onOpenChange: (isOpen: boolean) => void;
  onDone: () => void | Promise<void>;
}): ReactNode {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [role, setRole] = useState('none');
  const [priority, setPriority] = useState('0');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const close = (): void => {
    setBusy(false);
    setError('');
    setTitle('');
    setDescription('');
    setRole('none');
    setPriority('0');
    onOpenChange(false);
  };

  const submit = async (): Promise<void> => {
    const taskTitle = title.trim();
    if (!taskTitle) {
      setError(t('prj.task.create.heading.required'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api.createTask(projectId, {
        title: taskTitle,
        description: description.trim(),
        role: role === 'none' ? null : role,
        priority: Number(priority) || 0,
      });
      toast(t('prj.task.create.done'), 'info');
      close();
      await onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      toastError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      isOpen={isOpen}
      title={t('prj.task.create.title')}
      subtitle={t('prj.task.create.text')}
      onOpenChange={(open) => { if (!open) close(); }}
      footer={(
        <>
          <Button label={t('common.cancel')} variant="ghost" onClick={close} />
          <Button label={t('prj.board.add')} variant="primary" isLoading={busy} clickAction={submit} />
        </>
      )}
    >
      <VStack gap={4}>
        <TextInput
          label={t('prj.task.create.heading')}
          placeholder={t('prj.task.create.heading.placeholder')}
          value={title}
          onChange={setTitle}
          status={error ? { type: 'error', message: error } : undefined}
          width="100%"
        />
        <TextArea
          label={t('prj.task.create.description')}
          placeholder={t('prj.task.create.description.placeholder')}
          value={description}
          onChange={setDescription}
          rows={3}
          width="100%"
        />
        <HStack gap={3} align="start" wrap="wrap">
          <Selector
            label={t('prj.task.create.role')}
            value={role}
            onChange={(next) => setRole(next ?? 'none')}
            options={[
              { value: 'none', label: t('prj.task.create.role.none') },
              ...ROLES.map((item) => ({ value: item, label: roleLabel(item) })),
            ]}
          />
          <Selector
            label={t('prj.task.create.priority')}
            value={priority}
            onChange={(next) => setPriority(next ?? '0')}
            options={[0, 1, 2, 3].map((value) => ({
              value: String(value),
              label: t(`prj.priority.${value}`),
            }))}
          />
        </HStack>
      </VStack>
    </DialogShell>
  );
}

/* ------------------------------------------------------------------ */
/* delete project                                                      */
/* ------------------------------------------------------------------ */

interface DeleteResult {
  agents_stopped?: number;
  workspace_purged?: boolean;
}

export function DeleteProjectDialog({
  project,
  isOpen,
  onOpenChange,
  onDone,
}: {
  project: ProjectCard;
  isOpen: boolean;
  onOpenChange: (isOpen: boolean) => void;
  onDone: () => void | Promise<void>;
}): ReactNode {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const name = project.name || project.id;

  const close = (): void => {
    setBusy(false);
    setError('');
    setTyped('');
    onOpenChange(false);
  };

  const submit = async (): Promise<void> => {
    if (typed.trim() !== name) {
      setError(t('prj.delete.mismatch'));
      toast(t('prj.delete.mismatch'), 'error');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const result = (await api.deleteProject(project.id, {
        purgeWorkspace: true,
        confirm: typed.trim(),
      })) as DeleteResult;
      const stopped = Number(result.agents_stopped) || 0;
      toast(
        t('prj.delete.done', { n: stopped }) + (result.workspace_purged ? t('prj.delete.purged') : ''),
        'info',
      );
      close();
      await onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      toastError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogShell
      isOpen={isOpen}
      title={t('prj.delete.title')}
      onOpenChange={(open) => { if (!open) close(); }}
      footer={(
        <>
          <Button label={t('common.cancel')} variant="ghost" onClick={close} />
          <Button
            label={t('prj.delete.action')}
            variant="destructive"
            isLoading={busy}
            isDisabled={typed.trim().length === 0}
            clickAction={submit}
          />
        </>
      )}
    >
      <VStack gap={2}>
        <TextInput
          label={t('prj.delete.input')}
          placeholder={t('prj.delete.placeholder', { name })}
          value={typed}
          onChange={setTyped}
          status={error ? { type: 'error', message: error } : undefined}
          width="100%"
        />
        <Text size="xsm" color="secondary">{t('prj.delete.text', { name })}</Text>
      </VStack>
    </DialogShell>
  );
}
