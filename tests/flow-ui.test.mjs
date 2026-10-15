import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { isFlowAccountConnected, selectedFlowAccount, formatFlowJobSettingsMeta } from '../frontend/src/features/flow/flow.helpers.ts'
import { explainFlowError, explainFlowEvent, technicalFlowDetail, formatFlowExplain, flowStageLabel } from '../frontend/src/features/flow/flow.explain.ts'

test('prompt height defaults to flex and only saves a native manual resize', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../frontend/src/pages/FlowPage.css', import.meta.url), 'utf8');
  assert.match(css, /flex: 1 0 212px/);
  const start = source.indexOf('const textarea = event.currentTarget;');
  const body = source.slice(start, source.indexOf('\n                }}', start));
  let finish;
  const saved = [];
  const run = new Function('event', 'window', 'setPromptHeight', 'localStorage', body);
  const textarea = { style: { height: '' } };
  const storage = { setItem: (key, value) => saved.push([key, value]) };
  const win = { addEventListener: (_event, fn) => { finish = fn; } };
  run({ currentTarget: textarea }, win, () => {}, storage);
  finish();
  assert.equal(saved.length, 0);
  run({ currentTarget: textarea }, win, () => {}, storage);
  textarea.style.height = '540px';
  finish();
  assert.deepEqual(saved, [['zm-ai-tool:flow:prompt-height', '540']]);
});

test('retry locks duplicate confirmation until every request settles', async () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf('  const confirmRetryJob ='), source.indexOf('  const deleteWebFlowOutputs ='));
  const code = ts.transpileModule(body + '\nreturn confirmRetryJob;', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const jobs = [{ id: 'a', status: 'failed' }, { id: 'b', status: 'failed' }];
  const target = { jobs, concurrency: '1', groups: [{ jobs, accountId: 'random' }], accountId: 'random', model: 'Nano Banana 2', duration: '', fresh: false, headless: true, removeLogo: false };
  const lock = { current: false };
  const busy = [];
  const pending = [];
  let posts = 0;
  const request = (url, options) => {
    if (!options) return Promise.resolve({ jobs: [] });
    posts++;
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  };
  const available = (account) => isFlowAccountConnected(account);
  const confirm = new Function(
    'retryTarget', 'retryBusyRef', 'setRetryBusy', 'setRetryTarget', 'accounts',
    'isFlowAccountConnected', 'isFlowAccountAvailable', 'probeAccountsBeforeJobs',
    'toast', 't', 'flowRequest', 'updateJob', 'normalizeFlowJobs', 'setJobs', 'setApiError', 'setSettings', 'SETTINGS_KEY',
    code,
  )(
    target, lock, (value) => busy.push(value), () => {}, [],
    isFlowAccountConnected, available, async () => true,
    { error() {}, success() {} }, (vi) => vi, request, () => {}, () => [], () => {}, () => {}, () => {}, 'settings',
  );
  const first = confirm();
  const second = confirm();
  // Probe is awaited before retry POSTs; wait one tick for prepare to finish.
  await new Promise(setImmediate);
  assert.equal(posts, 2);
  assert.equal(lock.current, true);
  pending[0].reject(new Error('test failure'));
  await new Promise(setImmediate);
  assert.equal(lock.current, true);
  pending[1].resolve({});
  await Promise.allSettled([first, second]);
  // finally{} that clears retryBusyRef runs after allSettled of the POSTs.
  await new Promise(setImmediate);
  assert.equal(lock.current, false);
  assert.deepEqual(busy, [true, false]);
  for (const marker of ['onClick={retryAllJobs}', 'onClick={() => retryFolderJobs', 'onClick={() => retryJob(job.id)}']) {
    const pos = source.indexOf(marker);
    const button = source.slice(source.lastIndexOf('<button', pos), source.indexOf('</button>', pos));
    assert.match(button, /disabled=\{[^}]*retryBusy/);
  }
});

test('create account probe uses selectedAccountLabel not legacy settings.account', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const start = source.indexOf('const probeTargets = selectedAccountLabel === "random"');
  assert.ok(start > 0, 'create path must probe from selectedAccountLabel');
  const snippet = source.slice(start, start + 420);
  assert.match(snippet, /isFlowAccountAvailable/);
  assert.doesNotMatch(
    source.slice(source.indexOf('if (effectiveSettings !== settings)'), source.indexOf('const cookiesReady = await probeAccountsBeforeJobs')),
    /settings\.account === "random"/,
  );
  // Retry: specific accounts must be available/connected before prepare.
  const retryProbe = source.slice(source.indexOf('const probeTargets = Array.from(new Map('), source.indexOf('if (!(await probeAccountsBeforeJobs(probeTargets))'));
  assert.match(retryProbe, /row && isFlowAccountAvailable\(row\)/);
});

test('video resolution selection follows the matching download quality', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const matches = source.match(/resolution\.toLowerCase\(\) === "360p" \|\| resolution\.toLowerCase\(\) === "720p" \? \{ quality: resolution \} : \{\}/g) || [];
  assert.equal(matches.length, 2);
});

test('retry restores only connected accounts and preserves random jobs', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const groupsBody = source.slice(source.indexOf('  const buildRetryGroups ='), source.indexOf('  const openRetrySettings ='));
  const body = source.slice(source.indexOf('  const openRetrySettings ='), source.indexOf('  const retryJob ='));
  const code = ts.transpileModule(groupsBody + body + '\nreturn openRetrySettings;', {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const account = { id: 'online', label: 'Online', status: 'online', projectId: 'p' };
  const offline = { id: 'offline', label: 'Offline', status: 'reconnect', projectId: 'p' };
  const job = { id: 'job', kind: 'image', mode: 'text', status: 'failed', accountId: 'offline', account: 'Offline', settings: { model: 'Nano Banana 2', ratio: '1:1' } };
  for (const [accounts, randomAccount, expected] of [
    [[offline, account], false, 'online'],
    [[offline], false, 'random'],
    [[offline, account], true, 'random'],
  ]) {
    let state;
    const open = new Function('accounts', 'isFlowAccountConnected', 'setRetryTarget', 'settings', 'isFreshCreateJob', 'retryBusyRef', code)(
      accounts, isFlowAccountConnected, (value) => { state = value; }, { concurrency: '1', headless: true }, () => false, { current: false },
    );
    open([{ ...job, randomAccount }]);
    assert.equal(state.accountId, expected);
    assert.equal(state.groups[0].accountId, expected);
  }
});

test('retry dialog keeps previous Open Chrome setting from the job', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const groupsBody = source.slice(source.indexOf('  const buildRetryGroups ='), source.indexOf('  const openRetrySettings ='));
  const body = source.slice(source.indexOf('  const openRetrySettings ='), source.indexOf('  const retryJob ='));
  const code = ts.transpileModule(groupsBody + body + '\nreturn openRetrySettings;', {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const account = { id: 'online', label: 'Online', status: 'online', projectId: 'p' };
  const base = { id: 'job', kind: 'image', mode: 'text', status: 'failed', accountId: 'online', account: 'Online', settings: { model: 'Nano Banana 2', ratio: '1:1' } };
  for (const [jobHeadless, settingsHeadless, expected] of [
    [false, true, false],
    [true, false, true],
    [undefined, false, false],
    [undefined, true, true],
  ]) {
    let state;
    const open = new Function('accounts', 'isFlowAccountConnected', 'setRetryTarget', 'settings', 'isFreshCreateJob', 'retryBusyRef', code)(
      [account], isFlowAccountConnected, (value) => { state = value; }, { concurrency: '1', headless: settingsHeadless }, () => false, { current: false },
    );
    const settings = { ...base.settings };
    if (jobHeadless !== undefined) settings.headless = jobHeadless;
    open([{ ...base, settings }]);
    assert.equal(state.headless, expected, `job=${jobHeadless} settings=${settingsHeadless}`);
  }
});

test('global retry account choice updates every group and new groups inherit it', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const select = source.slice(source.indexOf('label={t("Tài khoản", "Account")}'));
  const handler = select.match(/onChange=\{([^\n]+)\}/)?.[1];
  assert.ok(handler, 'retry account FlowSelect onChange handler');
  let state = { accountId: 'offline', groups: [{ accountId: 'offline', kind: 'image' }, { accountId: 'other', kind: 'video' }], touched: [] };
  const change = new Function('setRetryTarget', 'return ' + handler)((update) => { state = update(state); });
  for (const accountId of ['random', 'online']) {
    change(accountId);
    assert.equal(state.accountId, accountId);
    assert.deepEqual(state.groups.map((group) => group.accountId), [accountId, accountId]);
    assert.deepEqual(state.groups.map((group) => group.kind), ['image', 'video']);
  }
  const includeDone = source.slice(source.indexOf('checked={retryTarget.includeDone}'), source.indexOf('checked={!retryTarget.headless}'));
  assert.match(includeDone, /accountId: current\.accountId/);
  assert.doesNotMatch(includeDone, /accountId: first\.accountId/);
  assert.match(select, /onChange/);
});

{
const offline = { label: 'Offline default', status: 'reconnect', projectId: 'p', isDefault: true, plan: 'Ultra' };

const connecting = { label: 'Connecting', status: 'connecting', projectId: 'p' };

const incomplete = { label: 'No project', status: 'online' };

const imageOnly = { label: 'Image gallery', status: 'online', imageProjectId: 'img' };

const online = { label: 'Connected', status: 'online', projectId: 'p', plan: 'Pro' };

test('Only connected accounts appear in options or resolve for selection', () => {
  const accounts = [offline, connecting, incomplete, imageOnly, online];
  assert.deepEqual(accounts.filter(isFlowAccountConnected), [imageOnly, online]);
  assert.equal(selectedFlowAccount(accounts, 'random'), online);
  assert.equal(selectedFlowAccount(accounts, online.label), online);
  assert.equal(selectedFlowAccount(accounts, imageOnly.label), imageOnly);
  for (const account of [offline, connecting, incomplete]) assert.equal(selectedFlowAccount(accounts, account.label), undefined);
  assert.equal(selectedFlowAccount([offline, connecting, incomplete], 'random'), undefined);
});


}

{
test("video meta includes download quality", () => {
  assert.equal(
    formatFlowJobSettingsMeta("video", {
      model: "Omni 1.1 Flash",
      ratio: "16:9",
      duration: "4",
      resolution: "",
      quality: "720p",
    }),
    "Omni 1.1 Flash · 16:9 · 4s · 720p",
  );
});


test("video meta shows gen resolution and labeled download quality", () => {
  assert.equal(
    formatFlowJobSettingsMeta(
      "video",
      {
        model: "Omni 1.1 Flash",
        ratio: "16:9",
        duration: "4",
        resolution: "360p",
        quality: "720p",
      },
      { downloadLabel: "tải" },
    ),
    "Omni 1.1 Flash · 16:9 · 4s · 360p · tải 720p",
  );
});

test("video meta shows logo removal", () => {
  assert.equal(
    formatFlowJobSettingsMeta("video", { model: "Veo", ratio: "16:9", removeLogo: true }, { logoLabel: "Del Logo" }),
    "Veo · 16:9 · Del Logo",
  );
});


test("image meta includes resolution", () => {
  assert.equal(
    formatFlowJobSettingsMeta("image", {
      model: "Nano Banana 2",
      ratio: "16:9",
      duration: "",
      resolution: "2K",
      quality: "",
    }),
    "Nano Banana 2 · 16:9 · 2K",
  );
});


test("image meta hides leftover video resolution", () => {
  assert.equal(
    formatFlowJobSettingsMeta("image", {
      model: "Nano Banana 2",
      ratio: "16:9",
      duration: "",
      resolution: "360p",
      quality: "720p",
    }),
    "Nano Banana 2 · 16:9",
  );
});
}

{
test("technical account diagnostics remove nested error-code and UI-status duplicates", () => {
  const detail = technicalFlowDetail(
    "FLOW_AUTOMATION_BLOCKED: FLOW_AUTOMATION_BLOCKED: warning Không thành công We noticed some unusual activity. Please visit the Help Center.",
  );
  assert.equal(detail, "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity. Please visit the Help Center.");
  assert.equal((detail.match(/FLOW_AUTOMATION_BLOCKED/g) || []).length, 1);
});


test("explains FLOW_RESULT_NOT_FOUND — raw detail as title, no summary, keeps action", () => {
  const explained = explainFlowError(
    "FLOW_RESULT_NOT_FOUND: Flow has no pending or completed result for this submission",
  );
  assert.equal(explained.code, "FLOW_RESULT_NOT_FOUND");
  // Raw technical detail surfaces as title, not a friendly label
  assert.match(explained.titleVi, /Flow has no pending/i);
  // No explanation paragraph
  assert.equal(explained.summaryVi, "");
  // Action is kept
  assert.match(explained.actionVi, /tự gửi/i);
});


test("explains empty batchGenerateImages timeout — raw detail as title, action kept", () => {
  const explained = explainFlowError(
    "Timed out (120s) waiting for batchGenerateImages. Captured so far: []",
  );
  assert.equal(explained.code, "FLOW_IMAGE_RPC_TIMEOUT");
  // Raw message surfaces as title
  assert.match(explained.titleVi, /batchGenerateImages/i);
  // No summary
  assert.equal(explained.summaryVi, "");
  // Action still present
  assert.match(explained.actionVi, /Chạy lại/i);
});


test("maps job_failed event title", () => {
  const event = explainFlowEvent("job_failed");
  assert.equal(event.titleEn, "Job failed");
});


test("distinguishes Flow unusual-activity blocks from content rejection — raw detail as title", () => {
  const explained = explainFlowError(
    "FLOW_GENERATION_REJECTED: Chúng tôi nhận thấy có hoạt động bất thường nào đó. Vui lòng chờ vài giây rồi thử lại.",
  );
  assert.equal(explained.code, "FLOW_GENERATION_REJECTED");
  // Raw detail in title (not friendly label)
  assert.match(explained.titleVi, /hoạt động bất thường/i);
  // No summary
  assert.equal(explained.summaryVi, "");
  // Action for unusual-activity path
  assert.match(explained.actionVi, /Chờ vài giây/i);
});


test("technical detail preserves distinct nested codes and provider diagnostics", () => {
  const raw = "FLOW_WORKER_FAILED: FLOW_PLAN_SYNC_FAILED: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://flow.google.com/project/example";
  assert.equal(technicalFlowDetail(raw), raw);
  assert.equal(technicalFlowDetail("FLOW_QUOTA_EXHAUSTED: quota exceeded"), "FLOW_QUOTA_EXHAUSTED: quota exceeded");
  assert.equal(technicalFlowDetail(""), "");
  const repeated = "FLOW_AUTOMATION_BLOCKED: FLOW_AUTOMATION_BLOCKED: warning Không thành công We noticed some unusual activity.";
  assert.equal(technicalFlowDetail(technicalFlowDetail(repeated)), technicalFlowDetail(repeated));
});


test("download failures explain existing media without automatic generation retry", () => {
  const result = explainFlowError("FLOW_DOWNLOAD_FAILED: Locator.wait_for timeout");
  assert.equal(result.titleVi, "Đã tạo media, tải file thất bại");
  assert.equal(result.titleEn, "Media generated, download failed");
  assert.match(result.actionVi, /Kết quả trên Flow được giữ lại/);
  assert.match(result.actionEn, /result is preserved on Flow/);
});

test("network/Chrome load errors explain wait-and-resume not manual hammer retry", () => {
  const result = explainFlowError(
    "FLOW_WORKER_FAILED: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://flow.google.com/project/example",
  );
  assert.equal(result.code, "FLOW_NETWORK_DISCONNECTED");
  assert.match(result.actionVi, /chờ mạng|tự chạy tiếp|Huỷ/i);
  assert.match(result.actionEn, /waits for the network|resumes automatically|Cancel/i);
  assert.doesNotMatch(result.actionVi, /không tiếp tục retry liên tục/);
  assert.doesNotMatch(result.actionEn, /do not keep retrying continuously/);
});

test("CDN TLS disconnect explains as download failure not generic retry", () => {
  const result = explainFlowError(
    "Client network socket disconnected before secure TLS connection was established (ECONNRESET) Call log: - → GET https://flow-content.google/image/abc",
  );
  assert.equal(result.code, "FLOW_DOWNLOAD_FAILED");
  assert.match(result.actionVi, /thử tải file lần nữa/);
  assert.match(result.actionEn, /download the file again/);
});

test("generation timeout explains recover-first without unusual activity", () => {
  const result = explainFlowError(
    "FLOW_GENERATION_TIMEOUT: no completed image media appeared after submit",
  );
  assert.equal(result.code, "FLOW_GENERATION_TIMEOUT");
  assert.match(result.actionVi, /khôi phục kết quả đã gửi trước/);
  assert.match(result.actionEn, /recovers the existing submission first/i);
  assert.doesNotMatch(result.actionVi, /bất thường/i);
  assert.doesNotMatch(result.actionEn, /unusual activity/i);
});

test("logo-removal failure badge is short and only gated on FLOW_LOGO_REMOVE_FAILED", () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf('const jobStatusText ='), source.indexOf('const jobErrorText ='));
  assert.match(body, /FLOW_LOGO_REMOVE_FAILED/);
  assert.match(body, /Lỗi xoá logo/);
  assert.match(body, /Logo failed/);
  // Success path must stay generic "Hoàn thành" / "Completed".
  assert.doesNotMatch(body, /Hoàn thành.*logo|logo.*Hoàn thành/i);
});

test("queue exposes Xoá logo after Ghép and Xoá tất cả logo after collapse", () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const tools = source.slice(
    source.indexOf('className="flow-queue-tools"'),
    source.indexOf('className="flow-queue-kind-tabs"'),
  );
  assert.match(tools, /Thu gọn tất cả[\s\S]*Xoá tất cả logo/);
  const folder = source.slice(
    source.indexOf('className="flow-queue-folder-actions"'),
    source.indexOf('className="flow-queue-folder-summary"'),
  );
  assert.match(folder, /Ghép[\s\S]*Xoá logo[\s\S]*Tạo mới[\s\S]*Hủy[\s\S]*Xóa/);
  assert.match(source, /logoOnly:\s*true/);
  const canRemove = source.slice(source.indexOf('const canRemoveLogo ='), source.indexOf('const removeLogoOnly ='));
  assert.match(canRemove, /settings\.removeLogo/);
  assert.match(canRemove, /isLogoRemoveFailed/);
  assert.match(canRemove, /logoDone/);
});
}

{
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
}

test('Flow queue bulk folding is bilingual, covers both kinds and persists', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8')
  const body = source.match(/const toggleAllFoldersCollapsed = \(\) => \{([\s\S]*?)\n  \};/)[1]
  const groups = [{ kind: 'video', outputDir: 'same' }, { kind: 'image', outputDir: 'same' }]
  let state = { 'video-same': true, unrelated: true }
  let saved
  const toggle = new Function('queueGroups', 'setCollapsedFolders', 'sessionStorage', 'COLLAPSED_FOLDERS_KEY', body)
  const run = () => toggle(groups, update => { state = update(state) }, { setItem: (_key, value) => { saved = JSON.parse(value) } }, 'test')
  run()
  assert.deepEqual(state, { 'video-same': true, 'image-same': true, unrelated: true })
  assert.deepEqual(saved, state)
  run()
  assert.deepEqual(state, { 'video-same': false, 'image-same': false, unrelated: true })
  assert.deepEqual(saved, state)
})

test('image capability normalization preserves persisted video download quality', () => {
  const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf('function applyAccountCapabilities('), source.indexOf('function flowRouteQueryPanel('));
  const code = ts.transpileModule(body + '\nreturn applyAccountCapabilities;', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const apply = new Function('flowVideoDownloadQualities', 'settingsWithSelectedModel', 'clampFlowImageResolution', 'isFlowVideoUiResolution', 'isFlowImageUiResolution', 'FLOW_IMAGE_MODELS', code)(
    () => ['360p', '720p'], (settings, kind, model) => ({ ...settings, model }), () => '1K', () => false, () => true, ['Nano Banana 2.1'],
  );
  for (const quality of ['720p', '1080p', '4K']) {
    const current = { model: 'Nano Banana 2.1', imageModel: 'Nano Banana 2.1', imageRatio: '16:9', resolution: '1K', quality };
    const model = { name: current.model, ratios: ['16:9'], resolutions: ['1K'], durations: [] };
    for (const account of [undefined, { capabilityStatus: 'verified', capabilityCatalog: { image: { models: [model] } } }]) {
      const next = apply(account, current, 'image', current.model);
      const persisted = JSON.parse(JSON.stringify(next));
      assert.equal(persisted.quality, quality);
    }
  }
  assert.match(source, /: videoDownloadQualityOptions\[0\]\)\s*: effectiveSettings.quality/);
});
