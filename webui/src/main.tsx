/**
 * main.tsx — boot the Astryx console.
 *
 * Order matters: Astryx base styles first, then the theme's production CSS
 * (imported by the generated `astryx-themes.ts`), then the small app stylesheet.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import './app.css';
import { initI18n } from './i18n';
import { AppTheme } from './shell/theme';
import App from './App';

initI18n();

const root = document.getElementById('root');
if (!root) throw new Error('missing #root');
createRoot(root).render(
  <StrictMode>
    <AppTheme>
      <App />
    </AppTheme>
  </StrictMode>,
);
