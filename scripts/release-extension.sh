#!/usr/bin/env bash
# Release the LangStage VS Code extension.
#
#   VSCE_PAT="op://kzest/VSCE_PAT/credential" OVSX_PAT="op://kzest/OVSX_PAT/credential" \
#     op run -- bash scripts/release-extension.sh [--dry-run]
#
# Steps (each is skipped when that version is already there, so a partly failed run
# can simply be re-run):
#   1. check the tree is clean, on main, and level with origin/main
#   2. read the version from extension/package.json
#   3. build the VSIX: npm ci && npm run package (in extension/)
#   4. publish to the VS Code Marketplace   (npx @vscode/vsce publish, token: VSCE_PAT)
#   5. publish to Open VSX                  (npx ovsx publish, token: OVSX_PAT)
#   6. create the GitHub release extension-v<version> with the VSIX attached (gh)
#
# --dry-run builds the VSIX and reports what each publish step would do, then stops
# before publishing anything. It needs no tokens, and a dirty tree or another branch
# is a warning instead of an error, so it can be exercised from a feature branch.
#
# The tokens are read from the environment by vsce and ovsx; they are never echoed.

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (use --dry-run or --help)" >&2; exit 64 ;;
  esac
done

EXT_ID="dkedar7.langstage-vscode"
OVSX_NAMESPACE="dkedar7"
OVSX_NAME="langstage-vscode"
REPO="dkedar7/langstage-vscode"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log()  { printf '==> %s\n' "$*"; }
skip() { printf '    skip: %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
# A check that is fatal for a real release but only a warning for a dry run.
guard() {
  if [ "$DRY_RUN" = 1 ]; then printf '    warning (dry run): %s\n' "$*"; else die "$*"; fi
}

for tool in git node npm npx gh curl; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is required but not on PATH"
done

# 1. Clean tree on main -----------------------------------------------------------------
log "Checking the working tree"
if [ -n "$(git status --porcelain)" ]; then
  guard "the working tree is not clean (commit or stash first)"
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  guard "on branch '$BRANCH', not main"
fi
git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  guard "HEAD is not origin/main (pull or push first)"
fi

# 2. Version ------------------------------------------------------------------------------
VERSION="$(node -p "require('./extension/package.json').version")"
TAG="extension-v${VERSION}"
VSIX="extension/langstage-vscode-${VERSION}.vsix"
log "Releasing ${EXT_ID} ${VERSION} (tag ${TAG})"

if [ "$DRY_RUN" = 0 ]; then
  [ -n "${VSCE_PAT:-}" ] || die "VSCE_PAT is not set (run through: op run -- ...)"
  [ -n "${OVSX_PAT:-}" ] || die "OVSX_PAT is not set (run through: op run -- ...)"
fi

# 3. Build --------------------------------------------------------------------------------
log "Building the VSIX (npm ci && npm run package)"
(cd extension && npm ci && npm run package)
[ -f "$VSIX" ] || die "expected $VSIX after packaging"
log "Built $VSIX ($(wc -c <"$VSIX" | tr -d ' ') bytes)"

# What is already published? --------------------------------------------------------------
marketplace_has_version() {
  # vsce show lists every published version; no token needed.
  (cd extension && npx --no-install vsce show "$EXT_ID" --json 2>/dev/null) \
    | node -e '
      let s = ""; process.stdin.on("data", d => s += d).on("end", () => {
        try {
          const v = JSON.parse(s).versions || [];
          process.exit(v.some(x => x.version === process.argv[1]) ? 0 : 1);
        } catch { process.exit(1); }
      });' "$VERSION"
}
openvsx_has_version() {
  curl -fsS -o /dev/null "https://open-vsx.org/api/${OVSX_NAMESPACE}/${OVSX_NAME}/${VERSION}" 2>/dev/null
}
github_has_release() {
  gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1
}
github_release_has_vsix() {
  gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[].name' 2>/dev/null \
    | grep -qx "$(basename "$VSIX")"
}

# Release notes: the CHANGELOG section for this extension version, when there is one.
release_notes() {
  awk -v ver="$VERSION" '
    $0 ~ "^## \\[Extension " ver "\\]" { on = 1; next }
    on && /^## / { exit }
    on { print }' CHANGELOG.md
}

# 4. VS Code Marketplace -------------------------------------------------------------------
log "VS Code Marketplace"
if marketplace_has_version; then
  skip "${EXT_ID} ${VERSION} is already on the Marketplace"
elif [ "$DRY_RUN" = 1 ]; then
  echo "    would run: npx @vscode/vsce publish --packagePath $VSIX"
else
  (cd extension && npx --no-install vsce publish --packagePath "$(basename "$VSIX")")
fi

# 5. Open VSX -----------------------------------------------------------------------------
log "Open VSX"
if openvsx_has_version; then
  skip "${OVSX_NAMESPACE}/${OVSX_NAME} ${VERSION} is already on Open VSX"
elif [ "$DRY_RUN" = 1 ]; then
  echo "    would run: npx ovsx publish $VSIX"
else
  npx --yes ovsx publish "$VSIX"
fi

# 6. GitHub release -----------------------------------------------------------------------
log "GitHub release ${TAG}"
if github_has_release; then
  if github_release_has_vsix; then
    skip "${TAG} already exists with $(basename "$VSIX") attached"
  elif [ "$DRY_RUN" = 1 ]; then
    echo "    would run: gh release upload $TAG $VSIX --repo $REPO"
  else
    gh release upload "$TAG" "$VSIX" --repo "$REPO"
  fi
elif [ "$DRY_RUN" = 1 ]; then
  echo "    would run: gh release create $TAG $VSIX --repo $REPO --target main --title 'Extension ${VERSION}'"
else
  NOTES="$(release_notes)"
  [ -n "$NOTES" ] || NOTES="LangStage VS Code extension ${VERSION}."
  gh release create "$TAG" "$VSIX" --repo "$REPO" --target main \
    --title "Extension ${VERSION}" --notes "$NOTES"
fi

if [ "$DRY_RUN" = 1 ]; then
  log "Dry run complete: nothing was published"
else
  log "Released ${EXT_ID} ${VERSION}"
fi
