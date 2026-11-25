#!/usr/bin/env bash
# Run the test suite and summarise passed / failed / errors / skipped counts,
# including *why* each test was skipped.
#
#   scripts/test-report.sh                          # whole suite
#   scripts/test-report.sh tests/test_address.py    # one file while developing
#   scripts/test-report.sh tests/ -k sandbox        # extra args go to pytest
#   scripts/test-report.sh --basetemp=/dev/shm/pytest-w6
#
# This always runs real pytest; skipped tests are reported as skipped, never as
# passed.  The exit status is pytest's own exit status.
#
# Interpreter order (the suite needs pynng, so use a venv that has the project
# dependencies installed; the machine default is /tmp/sfo-venv):
#   1. $SFO_PYTHON
#   2. /tmp/sfo-venv/bin/python (if present, as documented in docs/team-plan.md)
#   3. python3 from PATH
#
# Reports are written under .stackflowos/test-report/ (gitignored):
#   junit.xml   machine-readable results parsed for the summary
#   pytest.log  full pytest output
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${SFO_PYTHON:-}" ]]; then
  PY="$SFO_PYTHON"
elif [[ -x /tmp/sfo-venv/bin/python ]]; then
  PY=/tmp/sfo-venv/bin/python
else
  PY="$(command -v python3 || true)"
fi

if [[ -z "${PY:-}" || ! -x "$PY" ]]; then
  echo "test-report: no python interpreter found (set SFO_PYTHON=/path/to/python)" >&2
  exit 2
fi

if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "test-report: pytest is not importable by $PY" >&2
  echo "test-report: install the dev extra, e.g. $PY -m pip install -e '.[dev]'" >&2
  exit 2
fi

REPORT_DIR="${SFO_TEST_REPORT_DIR:-$ROOT/.stackflowos/test-report}"
if ! mkdir -p "$REPORT_DIR"; then
  echo "test-report: cannot create report dir: $REPORT_DIR" >&2
  exit 2
fi
JUNIT="$REPORT_DIR/junit.xml"
LOG="$REPORT_DIR/pytest.log"
rm -f "$JUNIT"

echo "test-report: $PY -m pytest -q -rs $*"
"$PY" -m pytest -q -rs --junitxml="$JUNIT" "$@" 2>&1 | tee "$LOG"
STATUS=${PIPESTATUS[0]}

"$PY" - "$JUNIT" "$LOG" <<'PY'
"""Summarise a pytest JUnit XML report using only the standard library."""
from __future__ import annotations

import collections
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

junit = Path(sys.argv[1])
log = Path(sys.argv[2])

print()
print("== test summary ==")
if not junit.is_file():
    print(f"no JUnit report at {junit} (pytest probably failed before running tests)")
    print(f"full log: {log}")
    raise SystemExit(0)

try:
    root = ET.parse(junit).getroot()
except ET.ParseError as exc:  # a killed pytest can leave partial XML
    print(f"cannot parse JUnit report {junit}: {exc}")
    print(f"full log: {log}")
    raise SystemExit(0)

counts: collections.Counter[str] = collections.Counter()
skip_reasons: collections.Counter[str] = collections.Counter()
skip_examples: dict[str, str] = {}
problems: list[tuple[str, str, str]] = []

for case in root.iter("testcase"):
    name = "::".join(part for part in (case.get("classname"), case.get("name"))
                     if part) or "(unnamed test)"
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    if failure is not None:
        counts["failed"] += 1
        message = (failure.get("message") or failure.text or "").strip()
        problems.append(("failed", name, message.splitlines()[0] if message else "(no message)"))
    elif error is not None:
        counts["errors"] += 1
        message = (error.get("message") or error.text or "").strip()
        problems.append(("error", name, message.splitlines()[0] if message else "(no message)"))
    elif skipped is not None:
        counts["skipped"] += 1
        reason = (skipped.get("message") or "").strip() or "(no reason recorded)"
        skip_reasons[reason] += 1
        skip_examples.setdefault(reason, name)
    else:
        counts["passed"] += 1

total = sum(counts.values())
if total == 0:
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is not None:
        print(f"pytest reported {suite.get('tests', '?')} tests, "
              f"{suite.get('failures', '?')} failures, "
              f"{suite.get('errors', '?')} errors, "
              f"{suite.get('skipped', '?')} skipped")
    print(f"full log: {log}")
    raise SystemExit(0)

print(f"passed {counts['passed']}  failed {counts['failed']}  "
      f"errors {counts['errors']}  skipped {counts['skipped']}  "
      f"(total {total})")

if skip_reasons:
    print()
    print(f"skipped {counts['skipped']}, grouped by reason:")
    for reason, count in sorted(skip_reasons.items(),
                                key=lambda item: (-item[1], item[0])):
        print(f"  {count:>3}x  {reason}")
        print(f"        e.g. {skip_examples[reason]}")

if problems:
    print()
    print("failed / errored (first line of the message):")
    for kind, name, message in problems:
        print(f"  [{kind}] {name}: {message}")

print()
print(f"junit: {junit}")
print(f"log:   {log}")
PY

exit "$STATUS"
