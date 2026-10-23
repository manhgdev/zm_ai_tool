import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../frontend/src/pages/FlowPage.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('FlowPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
function handler(name) {
  let found
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) found = node.getText(ast)
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(found, name)
  return ts.transpile(`const ${found}`)
}
function harness(overrides = {}) {
  const notifications = []
  const context = vm.createContext({
    accountSaving: false, accountDraft: { email: 'flow-test', plan: 'Free' },
    accountLoginMode: 'chrome', accountCookie: '', editingAccount: 'new', accounts: [],
    setAccountSaving(value) { context.accountSaving = value },
    setAccounts(update) { context.accounts = update(context.accounts) },
    setEditingAccount(value) { context.editingAccount = value },
    setAccountDraft(value) { context.accountDraft = value },
    setAccountCookie(value) { context.accountCookie = value },
    setApiError(value) { context.apiError = value },
    setConfirmAction(value) { context.confirmAction = value },
    toast: { success: text => notifications.push(['success', text]), error: text => notifications.push(['error', text]) },
    t: (vi, en) => en,
    ...overrides,
  })
  vm.runInContext(handler('saveAccount') + handler('deleteAccount'), context)
  return { context, notifications, run: code => vm.runInContext(code, context) }
}

test('adding and editing Flow accounts show success and clear the form only after saving', async () => {
  for (const editingAccount of ['new', 'account']) {
    const state = harness({
      editingAccount, accounts: [{ id: 'account', isDefault: true }],
      flowRequest: async (url, init) => {
        assert.equal(init.method, editingAccount === 'new' ? 'POST' : 'PUT')
        assert.equal(url, editingAccount === 'new' ? '/api/flow/accounts' : '/api/flow/accounts/account')
        assert.equal(JSON.parse(init.body).email, 'flow-test@gmail.com')
        return { id: 'account', email: 'flow-test@gmail.com' }
      },
    })
    await state.run('saveAccount()')
    assert.deepEqual(state.notifications, [['success', 'Flow account saved.']])
    assert.equal(state.context.editingAccount, null)
    assert.equal(state.context.accountSaving, false)
    assert.equal(state.context.accounts.length, 1)
  }
})

test('save failure is visible and preserves the draft and cookie for correction', async () => {
  for (const error of ['FLOW_COOKIE_INVALID', 'Network unavailable']) {
    const state = harness({
      accountLoginMode: 'cookie', accountCookie: 'synthetic-test-cookie',
      flowRequest: async () => { throw error },
    })
    await state.run('saveAccount()')
    assert.equal(state.notifications[0][0], 'error')
    assert.match(state.notifications[0][1], /Could not save account: (Invalid cookie|Network unavailable)/)
    assert.equal(state.context.editingAccount, 'new')
    assert.equal(state.context.accountCookie, 'synthetic-test-cookie')
    assert.equal(state.context.accountDraft.email, 'flow-test')
    assert.equal(state.context.accountSaving, false)
    assert.equal(state.context.accounts.length, 0)
  }
})

test('delete reports the result and keeps the account when the API rejects deletion', async () => {
  for (const failure of [false, true]) {
    const state = harness({
      accounts: [{ id: 'account', label: 'flow-test' }],
      flowRequest: async () => { if (failure) throw 'FLOW_PROFILE_BUSY' },
    })
    state.run('deleteAccount(accounts[0])')
    assert.equal(state.context.accounts.length, 1)
    await state.context.confirmAction.run()
    assert.equal(state.context.accounts.length, failure ? 1 : 0)
    assert.equal(state.notifications[0][0], failure ? 'error' : 'success')
  }
})

test('Series does not mount in the account tab until opened, then stays mounted to preserve drafts', () => {
  let panel
  function visit(node) {
    if (ts.isJsxExpression(node) && node.expression && ts.isBinaryExpression(node.expression)
      && node.expression.left.getText(ast) === 'seriesOpened') panel = node.expression.getText(ast)
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(panel)
  const code = ts.transpile(`const panel = (${panel}); panel`, { jsx: ts.JsxEmit.React })
  const context = vm.createContext({
    seriesOpened: false, utilityView: 'accounts', onOpenSrtImage() {},
    sortedAccounts: [], isFlowAccountConnected() {}, FlowSeriesPanel() {},
    React: { createElement: (type, props, ...children) => ({ type, props, children }) },
  })
  assert.equal(vm.runInContext(code, context), false)
  context.seriesOpened = true
  context.utilityView = 'series'
  const opened = vm.runInContext(`(() => { ${code} return panel })()`, context)
  assert.equal(opened.props.style.display, 'contents')
  context.utilityView = 'accounts'
  const hidden = vm.runInContext(`(() => { ${code} return panel })()`, context)
  assert.equal(hidden.props.style.display, 'none')
  assert.equal(hidden.children[0].type, context.FlowSeriesPanel)
})
