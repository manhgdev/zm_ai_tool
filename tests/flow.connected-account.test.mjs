import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isFlowAccountConnected, selectedFlowAccount } from '../frontend/src/features/flow/flow.helpers.ts';

const offline = { label: 'Offline default', status: 'reconnect', projectId: 'p', isDefault: true, plan: 'Ultra' };
const connecting = { label: 'Connecting', status: 'connecting', projectId: 'p' };
const incomplete = { label: 'No project', status: 'online' };
const online = { label: 'Connected', status: 'online', projectId: 'p', plan: 'Pro' };
test('Only connected accounts appear in options or resolve for selection', () => {
  const accounts = [offline, connecting, incomplete, online];
  assert.deepEqual(accounts.filter(isFlowAccountConnected), [online]);
  assert.equal(selectedFlowAccount(accounts, 'random'), online);
  assert.equal(selectedFlowAccount(accounts, online.label), online);
  for (const account of [offline, connecting, incomplete]) assert.equal(selectedFlowAccount(accounts, account.label), undefined);
  assert.equal(selectedFlowAccount([offline, connecting, incomplete], 'random'), undefined);
});
test('Main selector and Series receive connected accounts; empty state asks to connect in VI/EN', () => {
  const page = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8');
  assert.ok(page.includes('options={["random", ...accounts.filter(isFlowAccountConnected).map((account) => account.label)]}'));
  assert.ok(page.includes('accounts={accounts.filter(isFlowAccountConnected).map((acc)'));
  assert.ok(page.includes('Vui lòng kết nối tài khoản Flow trước khi tạo.'));
  assert.ok(page.includes('Please connect a Flow account before generating.'));
});
