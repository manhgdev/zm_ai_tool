import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import path from 'node:path'

const source = readFileSync(new URL('../scripts/setup.mjs', import.meta.url), 'utf8')
  .split("if (process.argv.includes('--check-python'))")[0]
  .replace(/^import .*\n/gm, '')
  .replace('fileURLToPath(import.meta.url)', "'/repo/scripts/setup.mjs'")

function setup(platform, versions, uvPath, options = {}) {
  const calls = []
  const removed = []
  let installed = false
  const managed = '/managed/python 3.12.10/python'
  const context = {
    path: platform === 'win32' ? path.win32 : path.posix,
    homedir: () => '/user', tmpdir: () => '/tmp', mkdtempSync: () => '/tmp/uv-bootstrap',
    rmSync: dir => removed.push(dir),
    process: { platform, env: {}, exit: code => { throw new Error(`exit ${code}`) } },
    console: { log() {}, error() {} },
    spawnSync(command, args) {
      calls.push([command, args])
      if (args.includes('-c')) {
        const version = command === managed && (installed || options.cached)
          ? '3.12' : versions[`${command} ${args[0]}`] || versions[command]
        return { status: version ? 0 : 1, stdout: version || '' }
      }
      if (command === uvPath) {
        if (args[0] === '--version') return { status: options.bootstrap ? 1 : 0 }
        if (args[0] === 'python' && args[1] === 'install') installed = true
        if (args[0] === 'python' && args[1] === 'find') {
          return { status: installed || options.cached ? 0 : 1, stdout: managed }
        }
        return { status: 0 }
      }
      if (args.includes('venv') || args.includes('pip')) return { status: 0 }
      if (['curl', 'sh', 'powershell.exe'].includes(command)) return { status: 0, stdout: '# installer' }
      return { status: 1, stdout: '' }
    },
  }
  return { find: install => runInNewContext(`${source}\nfindPython(${install})`, context), calls, removed }
}

test('Windows finds installed Python outside PATH without winget', () => {
  const python = path.win32.join('/user', 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe')
  const s = setup('win32', { [python]: '3.12' })
  assert.equal(s.find(true).command, python)
  assert.ok(!s.calls.some(([, args]) => args.includes('install')))
})

test('missing compatible Python downloads 3.12.10 and uses its absolute path', () => {
  const s = setup('linux', { python3: '3.14' }, 'uv')
  assert.equal(s.find(true).command, '/managed/python 3.12.10/python')
  assert.ok(s.calls.some(([command, args]) => command === 'uv' && args.join(' ') === 'python install 3.12.10 --no-bin --no-registry'))
})

test('Python 3.14 alone downloads compatible Python without winget or pip', () => {
  const s = setup('win32', { python: '3.14' }, path.win32.join('/tmp/uv-bootstrap', 'uv.exe'), { bootstrap: true })
  assert.equal(s.find(true).command, '/managed/python 3.12.10/python')
  assert.ok(s.calls.some(([command]) => command === 'powershell.exe'))
  assert.ok(!s.calls.some(([, args]) => args.includes('pip') || args.includes('venv')))
  assert.deepEqual(s.removed, ['/tmp/uv-bootstrap'])
})

test('--check-python uses cached managed Python without installing anything', () => {
  const s = setup('linux', {}, 'uv', { cached: true })
  assert.equal(s.find(false).command, '/managed/python 3.12.10/python')
  assert.ok(!s.calls.some(([, args]) => args.includes('install')))
})

for (const platform of ['win32', 'linux']) {
  test(`${platform} without any Python bootstraps standalone uv`, () => {
    const paths = platform === 'win32' ? path.win32 : path.posix
    const uv = paths.join('/tmp/uv-bootstrap', platform === 'win32' ? 'uv.exe' : 'uv')
    const s = setup(platform, {}, uv, { bootstrap: true })
    assert.equal(s.find(true).command, '/managed/python 3.12.10/python')
    assert.ok(s.calls.some(([command]) => command === (platform === 'win32' ? 'powershell.exe' : 'sh')))
    assert.deepEqual(s.removed, ['/tmp/uv-bootstrap'])
  })
}

test('missing Python in check mode fails without downloading or clearing environments', () => {
  const s = setup('linux', {}, 'uv')
  assert.throws(() => s.find(false), /exit 1/)
  assert.ok(!s.calls.some(([, args]) => args.includes('install') || args.includes('--clear')))
})
