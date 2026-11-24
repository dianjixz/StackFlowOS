/**
 * Projects screen — the project list (`#/projects`) and one project's board
 * (`#/projects/:id`), rewritten with the Astryx design system.
 *
 * This folder owns every component, helper and string the screen needs; the
 * shell only mounts the default export and hands it the route id.
 */

import type { ReactNode } from 'react';
import { useT } from '../../i18n';
import type { ScreenProps } from '../types';
import { ProjectDetail } from './ProjectDetail';
import { ProjectList } from './ProjectList';
import './projects.css';

export default function ProjectsScreen({ id }: ScreenProps): ReactNode {
  useT(); // re-render the screen when the language switches
  return id ? <ProjectDetail id={id} /> : <ProjectList />;
}
