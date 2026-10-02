import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { explainFlowError, explainFlowEvent, formatFlowExplain, flowStageLabel } from '../frontend/src/features/flow/flow.explain.ts';

// Exercise the actual log JSX and copy formatter, without mounting the page's API effects.
const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
const begin = source.indexOf('{logs.map((entry) =>');
assert.ok(begin >= 0);
const end = source.indexOf('\n                })}', begin);
assert.ok(end > begin);
const row = source.slice(begin + 1, end + '\n                })'.length);
const copyBegin = source.indexOf('  const formatLogEntry = ');
const copyEnd = source.indexOf('  const copyLogs = ', copyBegin);
const code = ts.transpileModule(
  `${source.slice(copyBegin, copyEnd)}\nreturn { rows: ${row}, copied: logs.map(formatLogEntry) };`,
  { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } },
).outputText;
const render = new Function('React', 'logs', 'accounts', 'locale', 't', 'logEventText',
  'explainFlowError', 'formatFlowExplain', 'flowStageLabel', 'copyOneLog', 'explainFlowEvent', code);
const cases = [
  ['job_failed', 'error', 'FLOW_WORKER_FAILED: FLOW_PLAN_SYNC_FAILED: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://flow.google.com/project/example\nCall log:\n - navigating to project', { stage: 'profile' }],
  ['account_suspended', 'warning', 'Tài khoản acc_pro bị tạm cách ly / Account acc_pro quarantined', { reason: 'Quota exceeded', suspendedUntil: 1791019025, duration: 50400 }],
  ['account_unsuspended', 'info', 'Đã gỡ tạm cách ly cho tài khoản pro_user / Suspension cleared for account pro_user', {}],
  ['account_fallback', 'warning', 'Tự động chuyển sang tài khoản savesafekey: Page.goto: net::ERR_INTERNET_DISCONNECTED / Auto-fallback to account savesafekey: Page.goto: net::ERR_INTERNET_DISCONNECTED', {}],
  ['future_event', 'info', 'An unrecognized event with a diagnostic message', {}],
];
for (const locale of ['vi', 'en']) {
  for (const [event, level, message, details] of cases) {
    test(`${locale}: ${event} renders and copies the full diagnostic once`, () => {
      const t = (vi, en) => locale === 'vi' ? vi : en;
      const entry = { id: 'log-1', jobId: 'job-1', accountId: 'account-1', createdAt: 1791019025, event, level, message, details };
      const { rows, copied } = render(React, [entry], [{ id: 'account-1', label: 'Account one' }], locale, t,
        (event) => { const label = explainFlowEvent(event); return t(label.titleVi, label.titleEn); },
        explainFlowError, formatFlowExplain, flowStageLabel, () => {}, explainFlowEvent);
      const html = renderToStaticMarkup(React.createElement(React.Fragment, null, ...rows));
      const diagnostic = renderToStaticMarkup(React.createElement('pre', null, message));
      assert.equal(html.split(diagnostic).length - 1, 1);
      const title = html.match(/<strong>(.*?)<\/strong>/s)?.[1];
      assert.ok(title);
      assert.ok(!title.includes('Page.goto') && !title.includes(message));
      assert.equal(html.match(/<pre>/g)?.length, Object.keys(details).some(key => key !== 'stage') ? 2 : 1);
      assert.equal(copied[0].split(message).length - 1, 1);
      assert.ok(copied[0].includes('job=job-1') && copied[0].includes(`event=${event}`));
      if (level === 'info') assert.ok(!html.includes('flow-log-action'));
    });
  }
}

for (const locale of ['vi', 'en']) {
  for (const scenario of [
    { name: 'live account', accounts: [{ id: 'account-1', label: 'Pro', email: 'live@example.com' }], details: {}, email: 'live@example.com' },
    { name: 'deleted account snapshot', accounts: [], details: { accountEmail: 'saved@example.com' }, email: 'saved@example.com' },
    { name: 'snapshot before account update', accounts: [{ id: 'account-1', label: 'Pro', email: 'new@example.com' }], details: { accountEmail: 'old@example.com' }, email: 'old@example.com' },
  ]) {
    test(`${locale}: ${scenario.name} identifies the account in UI and clipboard`, () => {
      const t = (vi, en) => locale === 'vi' ? vi : en;
      const entry = { id: 'log-1', jobId: 'job-1', accountId: 'account-1', createdAt: 1791019025, event: 'account_suspended', level: 'warning', message: 'Quota exceeded', details: scenario.details };
      const { rows, copied } = render(React, [entry], scenario.accounts, locale, t,
        (event) => { const label = explainFlowEvent(event); return t(label.titleVi, label.titleEn); },
        explainFlowError, formatFlowExplain, flowStageLabel, () => {}, explainFlowEvent);
      const html = renderToStaticMarkup(React.createElement(React.Fragment, null, ...rows));
      assert.equal(html.split(scenario.email).length - 1, 1);
      assert.equal(copied[0].split(scenario.email).length - 1, 1);
      assert.ok(!html.includes('accountEmail'));
    });
  }
}
