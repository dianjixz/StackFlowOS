# webui — the StackFlowOS operator console (Astryx)

The console is a React 19 single-page app built with the
[Astryx](https://github.com/meta/astryx) design system. It replaces the old
hand-written `stackflowos/webassets/{css,js}` console.

**Runtime:** the Python hub serves the *built* assets from
`stackflowos/webassets` — no Node process is needed at runtime. Node is only
needed to work on the console or to rebuild it.

## Commands

```bash
npm install                 # once
npm run dev                 # vite dev server on :5180, /api proxied to the fixture
npm run build               # typecheck-free production build into ./dist
npm run typecheck           # tsc --noEmit
node scripts/shot.mjs URL OUT.png [w] [h] [waitMs]   # screenshot for review
npm run build:assets        # build into ../stackflowos/webassets (release step)
```

The fixture backend is `python3 ../scripts/ui_mock.py` (port 17877). It serves
realistic in-memory data and an SSE ticker, so the console can be developed and
screenshotted without booting a hub:

```bash
# terminal 1
python3 scripts/ui_mock.py            # from the repo root
# terminal 2
cd webui && npm run dev               # http://127.0.0.1:5180/
# share review state
node scripts/shot.mjs http://127.0.0.1:5180/ /tmp/console.png
```

## Architecture

```
src/
  main.tsx            bootstrap: Astryx CSS, i18n, theme, <App/>
  App.tsx             AppShell + TopNav/SideNav, routing, 401 gate, toasts
  api/client.ts       the only HTTP boundary (`api.*`, ApiError, `request`)
  state/store.ts      /api/state snapshot + SSE reducer (+ useStore selectors)
  session/index.ts    identity, session list, viewer gate, 401 gate state
  i18n/               dict.json (legacy shell strings) + extra.ts (shell)
                      + keys/<screen>.ts — one dictionary file per screen
  shell/              router (hash), manifest (nav registry), theme, brand
  ui/                 Page frame, Placeholder, format helpers, toast bridge
  screens/<app>/      one folder per destination; default export a screen
```

Rules that keep the tree reviewable:

* A screen only edits its own `src/screens/<app>/` folder plus
  `src/i18n/keys/<app>.ts`. Shared files are owned by the shell.
* i18n: zh is the source of truth; add the same key to `en`. Keys live in the
  screen's own dictionary file, which `i18n/index.ts` merges automatically.
* Write actions are hidden behind `useCanWrite()` (viewer role) and every
  failing action goes through `toastError()`.
* Use Astryx components and layout primitives (`Layout`, `HStack`, `VStack`,
  `Card`, `Section`, `List`, `Table`, `Dialog`, …). No raw `<div>` for layout,
  no hard-coded hex colors or px radii — use theme tokens (`var(--color-*)`,
  `var(--spacing-*)`, `var(--radius-*)`).
* Prefer tokens/classes over inline styles. The hub's CSP allows inline
  *styles* (`style-src 'self' 'unsafe-inline'`) because the Astryx runtime
  injects one stylesheet and sets CSS variables for size/position props;
  scripts stay same-origin only (`script-src 'self'`).
