#!/usr/bin/env bash
# Fully offline regression suite with an isolated HOME, no credentials and
# blocking shims for every tool that could reach GitHub, OpenCode, systemd,
# privilege escalation, the network or the Engram runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# The product targets the distribution python3 (>= 3.11). Fail before doing anything else.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "offline tests require python3 >= 3.11; found: $(python3 --version 2>&1)" >&2
  exit 2
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

mkdir -p -- "$TMP/home" "$TMP/shims"
chmod 700 -- "$TMP" "$TMP/home"
: > "$TMP/gitconfig"

# Hermetic environment: no inherited identity, credentials, user git config, or
# controller/OpenCode/Engram state. The suite is often run from a shell that is itself
# inside a controller-managed OpenCode session, so the ambient AUTOCONTROLLER_*,
# AUTONOMY_*, OPENCODE_*, OPENCODE and ENGRAM_* variables are removed too. Otherwise
# ProjectWorker.env() (which copies os.environ) would pass them into the fake agent
# (for example AUTOCONTROLLER_LEARNING_INSTRUCTIONS would give a selection session a
# learning grant). The E2E tests also remove these prefixes per test (see E2EBase).
export HOME="$TMP/home"
export GIT_CONFIG_GLOBAL="$TMP/gitconfig"
export GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="offline-test" GIT_AUTHOR_EMAIL="offline-test@example.invalid"
export GIT_COMMITTER_NAME="offline-test" GIT_COMMITTER_EMAIL="offline-test@example.invalid"
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN
while IFS= read -r name; do
  case "$name" in
    *_API_KEY) unset "$name" ;;
    AUTOCONTROLLER_*|AUTONOMY_*|OPENCODE_*|OPENCODE|ENGRAM_*) unset "$name" ;;
  esac
done < <(compgen -e)

# Blocking shims: any accidental call fails loudly with a distinct exit code.
for name in gh opencode systemctl loginctl sudo curl engram; do
  cat > "$TMP/shims/$name" <<EOF
#!/usr/bin/env bash
echo "offline test guard: $name blocked" >&2
exit 97
EOF
  chmod 755 -- "$TMP/shims/$name"
done
export PATH="$TMP/shims:$PATH"

cd -- "$ROOT"
python3 -m unittest discover -s tests -v
