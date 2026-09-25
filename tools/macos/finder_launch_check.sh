#!/usr/bin/env bash
# Launch the built app the way Finder does and prove it finds agent CLIs.
#
# `open` starts the app through LaunchServices, so it gets the minimal
# environment a Finder, Dock or login item launch gets, not the PATH of this
# shell. Three fake CLIs sit in ~/.local/bin. The app must find them.
#
# Usage: tools/macos/finder_launch_check.sh dist/AgnView.app
set -euo pipefail

app="${1:?path to AgnView.app}"
bin_dir="$HOME/.local/bin"
names=(claude codex agy)
existed=()

cleanup() {
    for name in "${names[@]}"; do
        rm -f "$bin_dir/$name"
    done
    pkill -f "$app/Contents/MacOS/AgnView" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$bin_dir"
for name in "${names[@]}"; do
    if [ -e "$bin_dir/$name" ]; then
        existed+=("$name")
    fi
    printf '#!/bin/sh\necho "%s 0.0.0-ci"\n' "$name" > "$bin_dir/$name"
    chmod +x "$bin_dir/$name"
done
if [ "${#existed[@]}" -gt 0 ]; then
    echo "Replacing existing files in ~/.local/bin: ${existed[*]}"
fi

# Force the PATH Finder gives, so a runner that has ~/.local/bin in its own
# environment cannot make the check pass by accident.
open -n --env PATH=/usr/bin:/bin:/usr/sbin:/sbin "$app"

info="$HOME/.agnview/desktop-instance.json"
port=""
for attempt in $(seq 1 60); do
    if [ -f "$info" ]; then
        port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$info" 2>/dev/null || true)"
    fi
    if [ -n "$port" ]; then
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || true)"
        if [ "$code" != "000" ]; then
            echo "The hub answered on port $port after $((attempt * 2)) seconds"
            break
        fi
    fi
    port=""
    sleep 2
done
if [ -z "$port" ]; then
    echo "The app launched through LaunchServices did not start its hub" >&2
    tail -40 "$HOME/.agnview/logs/desktop.log" 2>/dev/null || true
    exit 1
fi

# A loopback request marked same-origin is the dashboard's own access, so no
# token is needed.
curl -sf -H "Sec-Fetch-Site: same-origin" "http://127.0.0.1:$port/api/diagnostics/path" > "$RUNNER_TEMP/path-diagnostics.json"

python3 - "$RUNNER_TEMP/path-diagnostics.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
print("PATH the app saw (home shown as ~):")
print("  started with:", data["repair"].get("before"))
print("  after repair:", ":".join(data["path"]))
print("  repair:", {k: v for k, v in data["repair"].items() if k not in ("path", "before")})
for name, hit in data["lookup"].items():
    print(f"  {name}: {hit}")

problems = []
if "~/.local/bin" in (data["repair"].get("before") or "").split(":"):
    problems.append("the app already had ~/.local/bin at start, so this check proves nothing")
for name in ("claude", "codex", "agy"):
    hit = data["lookup"].get(name, {})
    if not hit.get("found") or not str(hit.get("path", "")).startswith("~/.local/bin/"):
        problems.append(f"{name} did not resolve to ~/.local/bin: {hit}")
if problems:
    print("FAILED:", *problems, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("claude, codex and agy resolve to ~/.local/bin in a Finder-style launch")
PY

# A real dispatch. The fake codex runs, so the console must not say it is
# not installed.
session="ci-finder-launch"
curl -sf -X POST -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" \
    -d "{\"agent\":\"codex\",\"prompt\":\"hello\",\"session_id\":\"$session\",\"working_directory\":\"$HOME\"}" \
    "http://127.0.0.1:$port/api/console/dispatch" > /dev/null

for attempt in $(seq 1 30); do
    curl -sf -H "Sec-Fetch-Site: same-origin" \
        "http://127.0.0.1:$port/api/console/logs?agent=all&session_id=$session" > "$RUNNER_TEMP/console.json" || true
    if python3 - "$RUNNER_TEMP/console.json" <<'PY'
import json
import sys

try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if any(r.get("source") != "user_input" for r in rows) else 1)
PY
    then
        break
    fi
    sleep 1
done

python3 - "$RUNNER_TEMP/console.json" <<'PY'
import json
import sys

rows = json.load(open(sys.argv[1]))
text = " ".join(str(r.get("content", "")) for r in rows)
print("Console after the dispatch:", text[:400].replace(str(__import__("os").path.expanduser("~")), "~"))
if not any(r.get("source") != "user_input" for r in rows):
    print("FAILED: the agent gave no output", file=sys.stderr)
    sys.exit(1)
if "not installed" in text or "not on PATH" in text:
    print("FAILED: the chat reported the CLI as missing", file=sys.stderr)
    sys.exit(1)
print("The chat ran the CLI found through the repaired PATH")
PY
