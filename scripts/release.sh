#!/usr/bin/env bash
# scripts/release.sh — đồng bộ version và push release
# Dùng: ./scripts/release.sh 4.1.5
# Hoặc: ./scripts/release.sh patch|minor|major  (tự tăng)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── 1. Tính version mới ───────────────────────────────────────────
current="$(node -p 'require("./package.json").version')"
arg="${1:-patch}"

if [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  NEW="$arg"
else
  IFS='.' read -r maj min pat <<< "$current"
  case "$arg" in
    major) NEW="$((maj+1)).0.0" ;;
    minor) NEW="$maj.$((min+1)).0" ;;
    patch) NEW="$maj.$min.$((pat+1))" ;;
    *) echo "Dùng: $0 <x.y.z|patch|minor|major>"; exit 1 ;;
  esac
fi

echo "▸ Release: $current → $NEW"

# ── 2. Kiểm tra trước khi thay đổi version ───────────────────────
if [ "$(git branch --show-current)" != main ]; then
  echo "Cần ở branch main. / Switch to branch main first." >&2
  exit 1
fi
if ! node -e 'const a=process.argv[1].split(".").map(Number), b=process.argv[2].split(".").map(Number); process.exit(a.some((n,i)=>n>b[i] && a.slice(0,i).every((v,j)=>v===b[j])) ? 0 : 1)' "$NEW" "$current"; then
  echo "Version mới phải lớn hơn version hiện tại. / New version must exceed the current version." >&2
  exit 1
fi
remote_tag="$(git ls-remote --tags origin "refs/tags/v$NEW")"
if git show-ref --verify --quiet "refs/tags/v$NEW" || [ -n "$remote_tag" ]; then
  echo "Tag đã tồn tại. / Tag already exists: v$NEW" >&2
  exit 1
fi
git diff --check
git diff --cached --check

# ── 3. Cập nhật package.json rồi tạo git tag ────────
bash scripts/backup-before-edit.sh package.json
bash scripts/backup-before-edit.sh package-lock.json
npm version "$NEW" --no-git-tag-version

git add -A
git commit -m "chore: bump version to $NEW"
git push origin main
git tag -a "v$NEW" -m "Release v$NEW"
git push origin "v$NEW"

release_commit="$(git rev-parse HEAD)"
remote_refs="$(git ls-remote origin refs/heads/main "refs/tags/v$NEW^{}")"
for ref in refs/heads/main "refs/tags/v$NEW^{}"; do
  if ! echo "$remote_refs" | awk -v sha="$release_commit" -v ref="$ref" '$1 == sha && $2 == ref { found=1 } END { exit !found }'; then
    echo "Không xác minh được remote. / Remote verification failed: $ref" >&2
    exit 1
  fi
done
if [ -n "$(git status --porcelain)" ]; then
  echo "Còn thay đổi sau release. / Working tree changed during release." >&2
  exit 1
fi

echo ""
echo "✔ Đã push commit + tag v$NEW"
echo "  CI build: ZM_AI_TOOL_v${NEW}-macos-arm64.pkg + windows-x64-Setup.exe"
echo "  Theo dõi: https://github.com/manhgdev/zm_ai_tool/actions"
