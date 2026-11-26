#!/usr/bin/env bash
set -euo pipefail
umask 077
# Run as the same ordinary user who owns the configured OpenCode/OMO runtime.
# --install-only updates program files without changing projects or starting work.
if [[ $EUID == 0 ]]; then echo 'Run as your normal OpenCode user, not root.' >&2; exit 2; fi
if [[ $# -gt 1 || ( $# == 1 && $1 != --install-only ) ]]; then echo 'Usage: bash install.sh [--install-only]' >&2; exit 2; fi
MODE=${1:-}
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
 debian:12|debian:13|ubuntu:24.04|ubuntu:26.04) ;;
 *) echo "Unsupported OS: ${ID:-unknown} ${VERSION_ID:-unknown}. Use Debian 12/13 or Ubuntu 24.04/26.04." >&2; exit 2;;
esac
[[ $(ps -p 1 -o comm=) == systemd ]] || { echo 'A systemd VM is required.' >&2; exit 2; }
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"
command -v opencode >/dev/null || { echo 'Install/configure OpenCode V2 and OMO Slim first.' >&2; exit 2; }
TARGET="$HOME/scripts/autocontroller"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
UNIT_DIR="$HOME/.config/systemd/user"
[[ $(realpath -m -- "$TARGET") == "$TARGET" ]] || { echo 'Unsafe symlink in installation path.' >&2; exit 2; }
if systemctl --user is-active --quiet autocontroller.service 2>/dev/null; then
 echo 'Stop the existing AutoController before upgrading: systemctl --user stop autocontroller.service' >&2; exit 2
fi
# APT uses official distro and vendor repositories; no arbitrary curl|bash.
if [[ "$MODE" != --install-only ]]; then
 sudo -v
 sudo apt-get update
 sudo apt-get install -y --no-install-recommends ca-certificates curl git python3 tar gzip extrepo
 if ! command -v gh >/dev/null || ! gh project link --help >/dev/null 2>&1 || ! gh pr checks --help | grep -q -- --json; then
   tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
   curl --fail --silent --show-error --location --proto '=https' --max-time 60 https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$tmp"
   sudo install -d -m 755 /etc/apt/keyrings /etc/apt/sources.list.d
   sudo install -m 644 "$tmp" /etc/apt/keyrings/githubcli-archive-keyring.gpg
   printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
   sudo apt-get update; sudo apt-get install -y gh
 fi
 if ! command -v mise >/dev/null; then
   sudo extrepo enable mise
   sudo apt-get update; sudo apt-get install -y mise
 fi
 sudo loginctl enable-linger "$(id -un)"
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
mkdir -p "$TARGET" "$TARGET/state" "$UNIT_DIR" "$HOME/.local/bin"
chmod 700 "$TARGET" "$TARGET/state"
for file in autonomy-controller.py opencode-project-safe.py release_policy.py release_artifacts.py memory.py bootstrap_policy.py onboarding.py lifecycle.py install.sh; do
 [[ "$SOURCE/$file" == "$TARGET/$file" ]] || install -m 700 "$SOURCE/$file" "$TARGET/$file"
done
for file in README.md config.example.json autocontroller.service; do
 [[ "$SOURCE/$file" == "$TARGET/$file" ]] || install -m 600 "$SOURCE/$file" "$TARGET/$file"
done
# Release archives carry VERSION and MANIFEST.sha256; source checkouts do not. A source
# install must never keep a stale release version from an earlier archive install.
for file in VERSION MANIFEST.sha256 PROGRAM.json; do
 if [[ -f "$SOURCE/$file" ]]; then
  [[ "$SOURCE/$file" == "$TARGET/$file" ]] || install -m 600 "$SOURCE/$file" "$TARGET/$file"
 else
  rm -f "$TARGET/$file"
 fi
done
for dir in skills standards tests; do
 mkdir -p "$TARGET/$dir"
 if [[ "$SOURCE" != "$TARGET" ]]; then cp -R "$SOURCE/$dir/." "$TARGET/$dir/"; fi
done
if [[ ! -e "$TARGET/config.json" ]]; then
 install -m 600 "$SOURCE/config.example.json" "$TARGET/config.json"
else
 echo 'Existing controller configuration and state preserved.'
fi
ln -sfn "$TARGET/autocontroller.service" "$UNIT_DIR/autocontroller.service"
ln -sfn "$TARGET/onboarding.py" "$HOME/.local/bin/autocontroller"
systemctl --user daemon-reload
if [[ "$MODE" == --install-only ]]; then
 echo "Installed (not started). Run $HOME/.local/bin/autocontroller add or restart an existing configured service."
 exit 0
fi
# On clean VMs this is the one entrypoint. Existing configured controllers are
# not automatically re-onboarded; choose add explicitly to enroll another repo.
if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["projects"] else 1)' "$TARGET/config.json"; then
 echo 'Existing configuration found; use autocontroller add to enroll another repo.'
 systemctl --user enable --now autocontroller.service
else
 exec python3 "$TARGET/onboarding.py" add
fi
