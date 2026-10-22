import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

test('release commits all changes, increments patch beyond 9 and pushes an annotated tag', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'zm-release-'))
  const repo = path.join(root, 'repo')
  const remote = path.join(root, 'remote.git')
  mkdirSync(path.join(repo, 'scripts'), { recursive: true })
  const run = (command, args) => execFileSync(command, args, { cwd: repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  run('git', ['init', '--bare', remote])
  run('git', ['init', '-b', 'main'])
  run('git', ['config', 'user.email', 'release@example.com'])
  run('git', ['config', 'user.name', 'Release test'])
  run('git', ['remote', 'add', 'origin', remote])
  for (const file of ['release.sh', 'backup-before-edit.sh']) {
    copyFileSync(new URL(`../scripts/${file}`, import.meta.url), path.join(repo, 'scripts', file))
  }
  const scripts = JSON.parse(readFileSync(new URL('../package.json', import.meta.url))).scripts
  writeFileSync(path.join(repo, 'package.json'), JSON.stringify({ name: 'release-test', version: '1.2.9', scripts: { release: scripts.release } }))
  writeFileSync(path.join(repo, '.gitignore'), '*.bk*\npackage-lock.json\n')
  writeFileSync(path.join(repo, 'modified.txt'), 'before')
  writeFileSync(path.join(repo, 'deleted.txt'), 'before')
  run('git', ['add', '-A'])
  run('git', ['commit', '-m', 'initial'])
  run('git', ['push', 'origin', 'main'])
  writeFileSync(path.join(repo, 'modified.txt'), 'after')
  writeFileSync(path.join(repo, 'new.txt'), 'new')
  run('git', ['rm', 'deleted.txt'])
  run('npm', ['run', 'release'])
  assert.equal(JSON.parse(readFileSync(path.join(repo, 'package.json'))).version, '1.2.10')
  assert.equal(run('git', ['status', '--porcelain']), '')
  assert.equal(run('git', ['cat-file', '-t', 'v1.2.10']), 'tag')
  const commit = run('git', ['rev-parse', 'HEAD'])
  assert.equal(run('git', ['rev-parse', 'v1.2.10^{}']), commit)
  assert.equal(run('git', ['show', 'HEAD:modified.txt']), 'after')
  assert.equal(run('git', ['show', 'HEAD:new.txt']), 'new')
  assert.equal(run('git', ['ls-tree', '--name-only', 'HEAD', 'deleted.txt']), '')
  for (const ref of ['refs/heads/main', 'refs/tags/v1.2.10^{}']) {
    assert.equal(run('git', ['ls-remote', 'origin', ref]).split(/\s/)[0], commit)
  }
  // Existing local/remote tags must fail before changing the package or committing.
  run('git', ['tag', '-a', 'v1.2.11', '-m', 'reserved'])
  run('git', ['push', 'origin', 'v1.2.11'])
  run('git', ['tag', '-d', 'v1.2.11'])
  assert.notEqual(spawnSync('npm', ['run', 'release'], { cwd: repo }).status, 0)
  assert.equal(run('git', ['rev-parse', 'HEAD']), commit)
  assert.equal(JSON.parse(readFileSync(path.join(repo, 'package.json'))).version, '1.2.10')
  assert.notEqual(spawnSync('npm', ['run', 'release', '--', '1.2.8'], { cwd: repo }).status, 0)
  assert.equal(run('git', ['status', '--porcelain']), '')
})
