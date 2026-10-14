import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

for (const platform of ['macos', 'windows']) {
  test(`${platform} publishing handles existing releases and concurrent creation`, () => {
    const source = readFileSync(new URL(`../.github/workflows/release-${platform}.yml`, import.meta.url), 'utf8');
    const block = source.match(/          # Create metadata separately:[\s\S]*?          gh release upload[^\n]+/)[0];
    for (const scenario of ['existing', 'new', 'race', 'failure']) {
      const script = `set -euo pipefail
TAG=v1; BODY=notes; PKG=app.pkg; INSTALLER=app.exe
views=0
 gh() {
  case "$2" in
    view) views=$((views + 1)); [[ "$SCENARIO" == existing || ( "$SCENARIO" == race && "$views" -gt 1 ) ]] ;;
    create) [[ "$*" != *app.pkg* && "$*" != *app.exe* ]] || exit 90
      [[ "$SCENARIO" == new ]] ;;
    upload) [[ "$*" == *--clobber* ]] || exit 91; echo uploaded ;;
  esac
 }
${block}`;
      const result = spawnSync('bash', ['-c', script], { env: { ...process.env, SCENARIO: scenario }, encoding: 'utf8' });
      assert.equal(result.status, scenario === 'failure' ? 1 : 0, result.stderr);
      assert.equal(result.stdout.includes('uploaded'), scenario !== 'failure');
    }
    assert.match(source, /group: \$\{\{ github.workflow \}\}-\$\{\{ github.ref \}\}/);
    assert.match(source, /cancel-in-progress: false/);
  });
}
