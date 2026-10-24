#!/usr/bin/env bash
# Tests for bridge-launcher, built with the test allowlist: run on a Mac as
#   ./build.sh test-allow.json && ./test.sh
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)/.build/release/bridge-launcher"
fail=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: want $3, got $2"; fail=1; fi; }

"$B" /bin/sh -c 'exit 7'; check "exit status passes through" "$?" 7
"$B"; check "no program is a usage error" "$?" 64
"$B" /nonexistent/program 2>/dev/null; check "unstartable program" "$?" 127
out=$("$B" /bin/echo hello); check "stdout passes through" "$out" hello

# The confused-deputy guards: only exact allowlisted command lines, root-owned targets.
"$B" /bin/sh -c 'echo pwned' 2>/dev/null; check "an allowlisted program with other args is refused" "$?" 126
"$B" /bin/ls 2>/dev/null; check "a program not on the list is refused" "$?" 126
"$B" /bin/echo hello extra 2>/dev/null; check "extra arguments are refused" "$?" 126
mkdir -p /tmp/bridge-launcher-test && printf '#!/bin/sh\necho ran\n' >/tmp/bridge-launcher-test/user-owned.sh && chmod 0755 /tmp/bridge-launcher-test/user-owned.sh
"$B" /tmp/bridge-launcher-test/user-owned.sh 2>/dev/null; check "a user-owned target is refused even when listed" "$?" 126
printf 'echo ran\n' >/tmp/bridge-launcher-test/user-script.sh
"$B" /bin/sh /tmp/bridge-launcher-test/user-script.sh 2>/dev/null; check "a user-owned FILE argument is refused (root-owned interpreter)" "$?" 126
rm -rf /tmp/bridge-launcher-test

# Environment scrub. dyld already strips DYLD_* for Apple (SIP) binaries, so the check needs a
# non-Apple program: a tiny printenv, installed root-owned (the launcher refuses anything else).
# DYLD_INSERT_LIBRARIES itself is not set here: it would target the launcher, which only the
# hardened-runtime signature (codesign --options runtime) protects.
T=/usr/local/libexec/bridge-launcher-test
w=$(mktemp -d); printf '#include <stdio.h>\nextern char **environ;\nint main(void){for(char **e=environ;*e;e++)puts(*e);return 0;}\n' >"$w/p.c"
cc -o "$w/printenv" "$w/p.c" && sudo mkdir -p "$T" && sudo install -o root -g wheel -m 0755 "$w/printenv" "$T/printenv"; rm -rf "$w"
env_out=$(DYLD_FALLBACK_LIBRARY_PATH=/x LD_PRELOAD=/x PYTHONPATH=/x PYTHONSTARTUP=/x KEEP_ME=yes MACOS_BRIDGE_TEST=1 "$B" "$T/printenv")
check "DYLD_/LD_/PYTHON* are scrubbed" "$(printf '%s\n' "$env_out" | grep -cE '^(DYLD_|LD_|PYTHON)')" 0
check "a variable not on the env allowlist is dropped" "$(printf '%s\n' "$env_out" | grep -c '^KEEP_ME=')" 0
check "a variable on the env allowlist passes" "$(printf '%s\n' "$env_out" | grep -c '^MACOS_BRIDGE_TEST=1$')" 1
check "HOME (base set) passes" "$(printf '%s\n' "$env_out" | grep -c '^HOME=')" 1
sudo rm -rf "$T"

check "the child starts in /, not the caller's directory" "$(cd /tmp && "$B" /bin/pwd)" /
D="$(cd "$(dirname "$0")" && pwd)"
"$D/build.sh" --check "$D/test-allow.json" >/dev/null 2>&1
check "build.sh --check accepts a valid allowlist (control)" "$?" 0
bad=$("$D/build.sh" --check "$D/test-bad-python.json" 2>&1); rc=$?
check "build.sh refuses a Python command line without -I" "$rc:$(printf '%s' "$bad" | grep -c 'must run isolated (-I)')" "1:1"

"$B" /bin/sleep 30 & lpid=$!
sleep 1; kill -TERM "$lpid"; wait "$lpid"; rc=$?
check "SIGTERM is forwarded and reported as 128+15" "$rc" 143
sleep 0.5
check "the child is gone after SIGTERM" "$(pgrep -f '^/bin/sleep 30$' | wc -l | tr -d ' ')" 0

exit $fail
