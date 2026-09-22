#!/usr/bin/env bash
# scripts/release.sh — đồng bộ version và push release
# Dùng: ./scripts/release.sh 4.1.5
# Hoặc: ./scripts/release.sh patch|minor|major  (tự tăng)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_JSON="$REPO_ROOT/package.json"

# ── 1. Tính version mới ───────────────────────────────────────────
current="$(node -e "process.stdout.write(require('$PKG_JSON').version)")"
arg="${1:-patch}"

if [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  NEW="$arg"
else
  IFS='.' read -r maj min pat <<< "$current"
  case "$arg" in
    major) NEW="$((maj+1)).0.0" ;;
    minor) NEW="$maj.$((min+1)).0" ;;
    # Patch 0–9 only; 8.0.9 → 8.1.0 (never 8.0.10)
    patch)
      if [ "$pat" -ge 9 ]; then
        NEW="$maj.$((min+1)).0"
      else
        NEW="$maj.$min.$((pat+1))"
      fi
      ;;
    *) echo "Dùng: $0 <x.y.z|patch|minor|major>"; exit 1 ;;
  esac
fi

echo "▸ Release: $current → $NEW"

# ── 2. Kiểm tra working tree sạch ────────────────────────────────
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- ':!package.json')" ]; then
  echo "✖ Working tree còn thay đổi chưa commit (ngoài package.json)."
  echo "  Commit hoặc stash trước."
  exit 1
fi

# ── 3. Cập nhật package.json rồi tạo git tag ────────
node -e "
  const fs = require('fs');
  const p = JSON.parse(fs.readFileSync('$PKG_JSON','utf8'));
  p.version = '$NEW';
  fs.writeFileSync('$PKG_JSON', JSON.stringify(p, null, 2) + '\n');
"

git -C "$REPO_ROOT" add "$PKG_JSON"
git -C "$REPO_ROOT" commit -m "chore: release v$NEW"
git -C "$REPO_ROOT" tag "v$NEW"
git -C "$REPO_ROOT" push origin HEAD "v$NEW"

echo ""
echo "✔ Đã push commit + tag v$NEW"
echo "  CI build: ZM_AI_TOOL_v${NEW}-macos-arm64.pkg + windows-x64.zip"
echo "  Theo dõi: https://github.com/manhgdev/zm_ai_tool/actions"
