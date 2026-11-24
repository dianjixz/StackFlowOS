#!/usr/bin/env node
/**
 * shot.mjs — screenshot the console for visual review.
 *
 * Usage:
 *   node scripts/shot.mjs [url] [out] [width] [height] [waitMs] [theme=light|dark] [lang=zh|en]
 *
 * The dev server proxies /api to `python3 scripts/ui_mock.py` (port 17877), so
 * this works without booting the real hub.  Console errors are printed: an
 * empty list is part of "it looks right".
 */

import { chromium } from 'playwright-core';

const args = process.argv.slice(2);
const options = new Map();
const positional = [];
for (const arg of args) {
  if (arg.includes('=') && /^(theme|lang|full)=/.test(arg)) {
    const [key, value] = arg.split('=');
    options.set(key, value);
  } else {
    positional.push(arg);
  }
}

const [url = 'http://127.0.0.1:5180/', out = '/tmp/sfo-shot.png', w = '1440', h = '900', wait = '1800'] = positional;
const theme = options.get('theme');
const lang = options.get('lang');

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--font-render-hinting=none'],
});
const page = await browser.newPage({ viewport: { width: Number(w), height: Number(h) }, deviceScaleFactor: 1 });
if (theme) {
  await page.addInitScript((value) => window.localStorage.setItem('sfo-theme', value), theme);
}
if (lang) {
  await page.addInitScript((value) => window.localStorage.setItem('sfo-lang', value), lang);
}
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForTimeout(Number(wait));
await page.screenshot({ path: out, fullPage: options.get('full') === '1' });
console.log(`shot -> ${out}`);
console.log(errors.length ? `console errors:\n${errors.slice(0, 8).join('\n')}` : 'console errors: none');
await browser.close();
