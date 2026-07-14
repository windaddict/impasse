#!/usr/bin/env python3
"""Validate the Impasse schemas and every example fixture.

Dev/CI tool (NOT a shipped runtime helper): requires `jsonschema`. The shipped
skill helpers under scripts/ are stdlib-only; schema validation runs in CI where a
dev dependency is fine.

Checks, all with format assertion enabled:
  1. both schemas are valid Draft 2020-12 meta-schemas;
  2. schemas/examples/*.reviewer-response.json validate against the reviewer schema;
  3. schemas/examples/*.reconciliation-result.json validate against the reconciliation schema;
  4. finding ids are unique within each reviewer example (an invariant JSON Schema can't express).

Exit code is non-zero if anything fails.
"""
from __future__ import annotations
import json
import pathlib
import sys

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover
    sys.exit("jsonschema is required for validation: pip install jsonschema")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = SCHEMAS / "examples"

REVIEWER = SCHEMAS / "reviewer-response.v1.json"
RECONCILE = SCHEMAS / "reconciliation-result.v1.json"


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def main() -> int:
    failures: list[str] = []

    reviewer_schema = load(REVIEWER)
    reconcile_schema = load(RECONCILE)

    for name, schema in [("reviewer-response", reviewer_schema), ("reconciliation-result", reconcile_schema)]:
        try:
            Draft202012Validator.check_schema(schema)
            print(f"  meta-valid: {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name} is not a valid Draft 2020-12 schema: {exc}")

    reviewer = Draft202012Validator(reviewer_schema, format_checker=FormatChecker())
    reconcile = Draft202012Validator(reconcile_schema, format_checker=FormatChecker())

    checked = 0
    for path in sorted(EXAMPLES.glob("*.json")) if EXAMPLES.is_dir() else []:
        if path.name.endswith(".reviewer-response.json"):
            validator, label = reviewer, "reviewer"
        elif path.name.endswith(".reconciliation-result.json"):
            validator, label = reconcile, "reconciliation"
        else:
            failures.append(f"{path.name}: unrecognized example suffix")
            continue

        doc = load(path)
        errs = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        if errs:
            for e in errs:
                failures.append(f"{path.name}: {'/'.join(map(str, e.path)) or '<root>'}: {e.message}")
            continue

        # Invariant JSON Schema can't express: unique finding ids in a reviewer example.
        if label == "reviewer":
            ids = [f["id"] for f in doc.get("findings", [])]
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                failures.append(f"{path.name}: duplicate finding ids {sorted(dupes)}")
                continue

        checked += 1
        print(f"  valid ({label}): {path.name}")

    if not EXAMPLES.is_dir() or checked == 0:
        failures.append("no example fixtures found under schemas/examples/")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll schemas + {checked} examples valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
