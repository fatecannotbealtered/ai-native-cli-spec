#!/usr/bin/env python3
"""Check the ai-native-cli-spec 2.0 machine contract: JSON Schemas, example corpus and semantic rules.

Requirements: Python 3.10 or later with the jsonschema (4.18 or later) and referencing packages:

    python -m pip install "jsonschema>=4.18" referencing

Usage:

    python scripts/check-contract.py
        Check contract/schemas and contract/examples (spec-repo CI). Exit 0 when everything passes,
        1 with a list of failures otherwise, 2 when the environment or the command line is wrong.

    python scripts/check-contract.py --schema envelope out.json [more.json ...]
        Validate documents, for example real CLI output (VER-002), with the schema and the semantic
        checks. "-" reads stdin; --lines treats every non-empty line as one document (NDJSON).
        A schema can also be named with a $defs fragment: envelope#/$defs/stream_line.

What the corpus check covers:

 1. Strict JSON everywhere: UTF-8 without BOM, no duplicate keys, no NaN or Infinity.
 2. Every schema validates against the draft 2020-12 metaschema, has $id urn:ai-native-cli-spec:<name>:2.0
    matching its file name, and every $ref resolves offline.
 3. The schemas agree with contract.json: enums, key sets, id and token formats, the core error table,
    and the order, codes and defaults of receipt_rules.
 4. examples/index.json lists every example exactly once, each citing a rule of the catalog (the rule
    headings of ../.agent/*-SPEC.md, or the built-in RULE_IDS when the specs are not next to the
    contract). A valid example passes its schema and every semantic check. An invalid example fails, and
    every failure matches the violation its index entry states, so it fails for the stated reason and no
    other.
 5. Semantic checks a schema cannot express: contract.json receipt_rules (ok and code of envelopes that
    carry receipts), batch summaries and batch.ok_rule (including batch lists shortened to fit a size
    budget: omitted, failures first), page continuation and total, preview and token consistency
    (including shortened batch previews), operation records, scope times and paths, reference tree,
    built-ins, error table, risk tier and examples, size budgets, snake_case keys, string ids, UTC times
    and meta.untrusted paths.

Size budgets (CLI-031, CLI-032, CLI-042) on whole envelopes: an error is at most 2000 characters; a success
is at most 8000, except the full reference (data with exactly contract.json
self_description.reference.required_keys), which is exempt (contract.json size_budgets.exempt);
reference --index data is held to the index budget (8000) and reference --command data to the command
budget (6000). Raw-format output is not an envelope and is not checked. NDJSON stream lines are not
budgeted.

The checks are static: they show that documents are well formed and consistent, not that a tool produced
them or that its domain behaviour is right.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

if sys.version_info < (3, 10):
    sys.exit("check-contract.py needs Python 3.10 or later")

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT202012
except ImportError:  # pragma: no cover - environment problem
    sys.stderr.write('check-contract.py needs jsonschema>=4.18 and referencing: python -m pip install "jsonschema>=4.18" referencing\n')
    sys.exit(2)

METASCHEMA = "https://json-schema.org/draft/2020-12/schema"
URN_PREFIX = "urn:ai-native-cli-spec:"
URN_SUFFIX = ":2.0"
SCHEMA_NAMES = ["common", "envelope", "error-details", "preview", "receipt", "operation", "scope", "page", "batch", "reference",
                "reference-index", "reference-command", "context", "doctor", "changelog", "update", "notice"]

# Rule IDs of the 2.0 catalog (BLUEPRINT section 3). Index entries must cite a rule of the catalog: the rule
# headings of ../.agent/{CLI,SEC,SKILL,VERIFY}-SPEC.md when they are next to the contract (the spec repo), else
# this list.
RULE_IDS = (
    {f"CLI-{n:03d}" for n in [*range(1, 8), *range(10, 19), *range(20, 30), *range(30, 39), *range(40, 50), *range(50, 66),
                              70, 71, 80, 81, 90, 100, 101, 102, 110]}
    | {f"SEC-{n:03d}" for n in [1, 2, 10, 11, 12, 13, 20, 21, 22, 30, 40, 50, 60]}
    | {f"SKL-{n:03d}" for n in range(1, 12)}
    | {f"VER-{n:03d}" for n in range(1, 9)}
)
SPEC_FILES = ["CLI-SPEC.md", "SEC-SPEC.md", "SKILL-SPEC.md", "VERIFY-SPEC.md"]
RULE_HEADING = re.compile(r"^#{2,4} ((?:CLI|SEC|SKL|VER)-[0-9]{3})\b", re.MULTILINE)


def rule_catalog(contract_dir: Path) -> tuple[set, str]:
    """The rule IDs index entries may cite, and where they came from."""
    agent = contract_dir.resolve().parent / ".agent"
    files = [agent / name for name in SPEC_FILES]
    if not all(f.is_file() for f in files):
        return set(RULE_IDS), "built-in list"
    ids = set()
    for f in files:
        ids |= set(RULE_HEADING.findall(f.read_text(encoding="utf-8")))
    return ids, "spec headings"

RESERVED_ROOTS = {"reference", "context", "doctor", "changelog", "update", "operation", "help", "version"}  # CLI-021
LIBRARY_BUILTINS = ["reference", "context", "doctor", "changelog", "operation show", "operation list", "operation resolve"]
SELF_UPDATE = "update"  # CLI-070: outside the confirm gate
TIERS = {"T0": 0, "T1": 1, "T2": 2}
MAP_KEYS = {"error_codes", "exit_codes"}  # objects keyed by codes, not by field names
RESERVED_RULE_CODES = {"E_OUTCOME_UNKNOWN", "E_PARTIAL", "E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"}

SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
TIMESTAMP = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,9})?Z")


# ---------------------------------------------------------------------------------------------------- failures


@dataclass(frozen=True)
class Failure:
    kind: str  # parse | schema | semantic
    check: str  # parse problem, JSON Schema keyword, or semantic check id
    path: str  # JSON pointer into the document; "" is the root
    message: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.check} at {self.path or '/'}: {self.message}"


class Report:
    """Collects semantic failures."""

    def __init__(self) -> None:
        self.items: list[Failure] = []

    def add(self, check: str, path: str, message: str) -> None:
        self.items.append(Failure("semantic", check, path, message))


def child(base: str, key) -> str:
    return f"{base}/{str(key).replace('~', '~0').replace('/', '~1')}"


def pointer(parts) -> str:
    out = ""
    for part in parts:
        out = child(out, part)
    return out


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------------- strict JSON


class StrictJSONError(ValueError):
    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


def parse_strict(raw: bytes):
    """Parse one JSON document strictly. Returns (value, failures); duplicate keys are reported and the last value
    kept so the rest of the document can still be checked. Raises StrictJSONError when nothing usable remains."""
    failures: list[Failure] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        failures.append(Failure("parse", "bom", "", "a UTF-8 byte order mark is not allowed (CLI-001)"))
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJSONError("encoding", f"not UTF-8: {exc}") from exc
    duplicates: list[str] = []

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                duplicates.append(key)
            obj[key] = value
        return obj

    def constant(name):
        raise StrictJSONError("non_finite_number", f"{name} is not JSON")

    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise StrictJSONError("syntax", str(exc)) from exc
    for key in duplicates:
        failures.append(Failure("parse", "duplicate_key", "", f"duplicate key {key!r}; strict parsing rejects the document (SEC-011)"))
    return value, failures


def load_strict(path: Path):
    value, failures = parse_strict(path.read_bytes())
    if failures:
        raise StrictJSONError(failures[0].check, f"{path.name}: {failures[0].message}")
    return value


# ---------------------------------------------------------------------------------------------------- versions and times


def semver_key(version: str):
    core, _, pre = version.split("+", 1)[0].partition("-")
    numbers = tuple(int(part) for part in core.split("."))
    if not pre:
        return numbers, (1,)
    return numbers, (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))


def parse_time(value: str) -> datetime:
    """A schema-valid UTC timestamp; fractions up to nanoseconds (datetime.fromisoformat before 3.11 takes 6 digits at most)."""
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise ValueError(f"not an ISO 8601 UTC timestamp with Z: {value!r}")
    date, _, rest = value.rstrip("Z").partition("T")
    clock, _, fraction = rest.partition(".")
    year, month, day = map(int, date.split("-"))
    hour, minute, second = map(int, clock.split(":"))
    return datetime(year, month, day, hour, minute, second, int((fraction or "0")[:6].ljust(6, "0")), tzinfo=timezone.utc)


def is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


# ---------------------------------------------------------------------------------------------------- contract


class Contract:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.core = data["error_codes"]["core"]
        self.budgets = data["size_budgets"]
        self.max_ttl = data["token"]["max_ttl_seconds"]
        self.min_tier = data["effects"]["minimum_risk_tier"]
        self.control_flags = [flag.lstrip("-") for flag in data["control_flags"]["global"]]
        self.known_extensions = data["error_codes"]["extension"]["known_extensions"]
        self.spec_version = data["spec_version"]
        self.reference_keys = set(data["self_description"]["reference"]["required_keys"])


# ---------------------------------------------------------------------------------------------------- receipt rules


def lasting(outcome: dict) -> bool:
    return outcome.get("change") == "applied" or outcome.get("delivery") == "delivered" or outcome.get("persistence") == "saved"


class Codes:
    """The error codes a receipt rule allows: exact codes and, for "the cause code of the failed step", any code
    the other rules do not reserve."""

    def __init__(self, exact=(), cause: bool = False) -> None:
        self.exact = frozenset(exact)
        self.cause = cause

    def allows(self, code: str) -> bool:
        return code in self.exact or (self.cause and code not in RESERVED_RULE_CODES)

    def only(self, code: str) -> bool:
        return self.exact == {code} and not self.cause

    def describe(self) -> str:
        parts = sorted(self.exact)
        if self.cause:
            parts.append("the cause code of the failed step (not " + ", ".join(sorted(RESERVED_RULE_CODES - self.exact)) + ")")
        return " or ".join(parts) or "ok"


def receipt_rule(receipt: dict):
    """contract.json receipt_rules, base profile, first match wins. Returns (rule number, ok, Codes)."""
    o = receipt["outcome"]
    if "unknown" in (o["execution"], o["change"], o["delivery"], o["persistence"]) or o["execution"] == "running":
        return 1, False, Codes({"E_OUTCOME_UNKNOWN"})
    if "partial" in (o["change"], o["delivery"], o["persistence"]):
        return 2, False, Codes({"E_PARTIAL"})
    stopped = o["execution"] in ("failed", "interrupted", "not_started")
    if stopped and lasting(o):
        return 3, False, Codes({"E_PARTIAL"})
    if stopped:
        return 4, False, Codes(cause=True)  # default E_SERVER
    if o["change"] == "rolled_back":
        # The cause that made the tool roll back: a failed or incomplete read-back (default E_VERIFY_FAILED) or
        # the code of a later step. Never unknown or partial: those outcomes are rules 1 and 2.
        return 5, False, Codes({"E_VERIFY_FAILED", "E_VERIFY_INCOMPLETE"}, cause=True)
    if o["delivery"] == "rejected":
        return 6, False, Codes(cause=True)  # default E_FORBIDDEN
    if o["verification"] == "failed":
        return 7, False, Codes({"E_VERIFY_FAILED"})
    if o["verification"] == "incomplete":
        return 8, False, Codes({"E_VERIFY_INCOMPLETE"})
    return 9, True, Codes()


# The shape of contract.json receipt_rules that receipt_rule() implements: (ok, code or "cause", the default cause
# code, a phrase of the rule's "when"), in order.
EXPECTED_RECEIPT_RULES = [
    (False, "E_OUTCOME_UNKNOWN", None, "unknown"),
    (False, "E_PARTIAL", None, "partial"),
    (False, "E_PARTIAL", None, "applied"),
    (False, "cause", "E_SERVER", "failed, interrupted or not_started"),
    (False, "cause", "E_VERIFY_FAILED", "rolled_back"),
    (False, "cause", "E_FORBIDDEN", "rejected"),
    (False, "E_VERIFY_FAILED", None, "verification is failed"),
    (False, "E_VERIFY_INCOMPLETE", None, "verification is incomplete"),
    (True, None, None, "otherwise"),
]


def receipt_rules_match(rules: list) -> bool:
    if len(rules) != len(EXPECTED_RECEIPT_RULES):
        return False
    for rule, (ok, code, default, phrase) in zip(rules, EXPECTED_RECEIPT_RULES, strict=True):
        if rule["ok"] != ok or phrase not in rule["when"]:
            return False
        if code == "cause":
            if "cause code" not in str(rule["code"]) or f"default {default}" not in str(rule["code"]):
                return False
        elif rule["code"] != code:
            return False
    return True


# ---------------------------------------------------------------------------------------------------- semantic checks


def check_receipt(receipt: dict, base: str, r: Report) -> None:
    observed = receipt.get("observed", {})
    for i, field in enumerate(receipt.get("mismatched", [])):
        if field.split(".", 1)[0] not in observed:
            r.add("receipt_consistency", child(child(base, "mismatched"), i),
                  f"mismatched field {field!r} is not in observed; report the value read back (CLI-059)")


def classify_item(item: dict) -> str:
    if item.get("ok"):
        return "succeeded"
    error, receipt = item.get("error"), item.get("receipt")
    if error and error.get("code") == "E_OUTCOME_UNKNOWN":
        return "unknown"
    if receipt and receipt_rule(receipt)[0] == 1:
        return "unknown"
    if not error and (not receipt or (receipt["outcome"]["execution"] == "not_started" and not lasting(receipt["outcome"]))):
        return "skipped"
    return "failed"


def item_applied(item: dict) -> bool:
    return bool(item.get("ok")) or bool(item.get("receipt") and lasting(item["receipt"]["outcome"]))


BATCH_COUNTS = ("succeeded", "failed", "unknown", "skipped")


def batch_expected_code(batch: dict):
    """contract.json batch.ok_rule: None when every item succeeded, else the code the batch must carry ("" when the
    listed items do not say). A shortened list (omitted) is judged by its summary, which counts every item."""
    items, summary = batch["items"], batch["summary"]
    kinds = [classify_item(item) for item in items]
    unknown, applied = "unknown" in kinds, any(item_applied(item) for item in items)
    if "omitted" in batch:
        if summary["succeeded"] == summary["total"] and all(kind == "succeeded" for kind in kinds):
            return None
        unknown, applied = unknown or summary["unknown"] > 0, applied or summary["succeeded"] > 0
    elif all(kind == "succeeded" for kind in kinds):
        return None
    if unknown:
        return "E_OUTCOME_UNKNOWN"
    if applied:
        return "E_PARTIAL"
    for item in items:
        if item.get("error"):
            return item["error"]["code"]
    return ""


def check_batch(batch: dict, base: str, r: Report) -> None:
    items, summary = batch["items"], batch["summary"]
    keys = [item["target"] for item in items]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        r.add("batch_targets", child(base, "items"), f"targets are de-duplicated before execution (CLI-063); repeated: {', '.join(repeated)}")
    operations = [item["operation_id"] for item in items]
    if len(set(operations)) != len(operations) or batch.get("operation_id") in operations:
        r.add("batch_targets", child(base, "items"), "every item runs under its own operation id, distinct from the batch id (CLI-063)")
    for i, item in enumerate(items):
        at = child(child(base, "items"), i)
        receipt = item.get("receipt")
        if receipt:
            check_receipt(receipt, child(at, "receipt"), r)
            if receipt["operation_id"] != item["operation_id"]:
                r.add("batch_rule", child(at, "receipt"), "the item and its receipt name the same operation")
            number, ok, codes = receipt_rule(receipt)
            if ok != item["ok"]:
                r.add("batch_rule", child(at, "ok"), f"an item's ok follows its receipt (receipt rule {number} gives ok {str(ok).lower()})")
            elif not ok and "error" in item and not codes.allows(item["error"]["code"]):
                r.add("batch_rule", child(child(at, "error"), "code"), f"receipt rule {number} gives {codes.describe()}")
        if not item["ok"] and "error" not in item and classify_item(item) != "skipped":
            r.add("batch_rule", at, "an attempted item that did not succeed carries its error code")
    tally = Counter(classify_item(item) for item in items)
    omitted = batch.get("omitted")
    if omitted is None:
        expected = {"total": len(items), **{key: tally[key] for key in BATCH_COUNTS}}
        for key, value in expected.items():
            if summary.get(key) != value:
                r.add("batch_summary", child(child(base, "summary"), key),
                      f"summary.{key} is {summary.get(key)} but the items give {value} (CLI-063: counts equal the item tally)")
        return
    # A list shortened to fit the size budget: summary counts every item, the list keeps failures first.
    if summary["total"] != len(items) + omitted:
        r.add("batch_summary", child(base, "omitted"),
              f"omitted is {omitted}, but summary.total {summary['total']} less the {len(items)} listed items is "
              f"{summary['total'] - len(items)} (CLI-063)")
    if sum(summary[key] for key in BATCH_COUNTS) != summary["total"]:
        r.add("batch_summary", child(base, "summary"), "summary counts every item exactly once: succeeded + failed + unknown + skipped = total (CLI-063)")
    for key in BATCH_COUNTS:
        if summary[key] < tally[key]:
            r.add("batch_summary", child(child(base, "summary"), key),
                  f"summary.{key} is {summary[key]}, but {tally[key]} of the listed items are {key} (CLI-063)")
    not_ok, listed_not_ok = sum(summary[key] for key in BATCH_COUNTS[1:]), sum(tally[key] for key in BATCH_COUNTS[1:])
    succeeded = [classify_item(item) == "succeeded" for item in items]
    if succeeded != sorted(succeeded):
        r.add("batch_shortening", child(base, "items"), "a shortened list puts the items with ok false first, before any that succeeded (CLI-063)")
    elif tally["succeeded"] and listed_not_ok < not_ok:
        r.add("batch_shortening", child(base, "items"),
              f"a shortened list keeps every item with ok false before any that succeeded; it leaves out {not_ok - listed_not_ok} of the "
              f"{not_ok} items with ok false but lists {tally['succeeded']} that succeeded (CLI-063)")


def check_page(page: dict, base: str, r: Report) -> None:
    if page["count"] != len(page["items"]):
        r.add("page_count", child(base, "count"), f"count is {page['count']} but there are {len(page['items'])} items (CLI-041)")
    if "next_offset" in page and "offset" in page and page["next_offset"] < page["offset"] + max(page["count"], 1):
        r.add("page_offset", child(base, "next_offset"), "next_offset advances past offset + count, so the next page reads new items (CLI-041)")
    if "total" in page:
        end = page.get("offset", 0) + page["count"]
        if page["total"] < end:
            r.add("page_total", child(base, "total"), f"total {page['total']} is less than the {end} items read up to the end of this page (CLI-041)")
        elif not page["has_more"] and "offset" in page and page["total"] != end:
            r.add("page_total", child(base, "total"),
                  f"has_more is false, so this page ends the list at offset + count = {end} items; total says {page['total']} (CLI-041)")


def check_preview(preview: dict, base: str, r: Report) -> None:
    if "items" not in preview:
        return
    targets, items = preview["targets"], preview["items"]
    omitted = preview.get("omitted", 0)
    if len(targets) != len(items):
        r.add("preview_batch", child(base, "items"), f"items follow the {len(targets)} listed targets one to one (CLI-063); there are {len(items)}")
    elif [t["key"] for t in targets] != [item["target"]["key"] for item in items]:
        r.add("preview_batch", child(base, "items"), "items follow targets one to one, in input order (CLI-063)")
    if preview["total"] != len(targets) + omitted:
        if omitted:
            r.add("preview_batch", child(base, "omitted"),
                  f"omitted is {omitted}, but total {preview['total']} less the {len(targets)} listed targets is {preview['total'] - len(targets)} (CLI-063)")
        else:
            r.add("preview_batch", child(base, "total"),
                  f"total {preview['total']} counts the {len(targets)} targets one to one; a preview that leaves targets out says how many in omitted (CLI-063)")
    keys = [t["key"] for t in targets]
    if len(set(keys)) != len(keys):
        r.add("preview_batch", child(base, "targets"), "batch targets are de-duplicated (CLI-063)")
    operations = [item["operation_id"] for item in items]
    if len(set(operations)) != len(operations) or preview["operation_id"] in operations:
        r.add("preview_batch", child(base, "items"), "every batch item has its own operation id, distinct from the batch id (CLI-063)")
    if preview["can_apply"] and not all(item["can_apply"] for item in items):
        r.add("preview_batch", child(base, "can_apply"), "a batch that can be applied has no blocked item")


def check_token(holder: dict, base: str, r: Report) -> None:
    """dry-run data or confirmation-required details: one operation id for preview, token and holder."""
    operation_id = holder["operation_id"]
    if holder["preview"]["operation_id"] != operation_id:
        r.add("token_binding", child(base, "operation_id"), "the operation id is the preview's (CLI-051)")
    token = holder.get("confirm_token")
    if isinstance(token, str) and token[4:30] != operation_id.split("_", 1)[1]:
        r.add("token_binding", child(base, "confirm_token"), "the token carries the id part of its operation id (CLI-055)")


def operation_batch_status(statuses: list):
    if any(s in ("unknown", "dispatching") for s in statuses):
        return "unknown"
    if all(s == "applied" for s in statuses):
        return "applied"
    if any(s in ("applied", "partial") for s in statuses):
        return "partial"
    if all(s == "not_applied" for s in statuses):
        return "not_applied"
    if all(s in ("not_applied", "failed") for s in statuses):
        return "failed"
    return None


def check_operation(record: dict, base: str, r: Report, contract: Contract) -> None:
    operation_id, status = record["operation_id"], record["status"]
    preview, receipt, target = record["preview"], record["receipt"], record["target"]
    if preview:
        check_preview(preview, child(base, "preview"), r)
        if preview["operation_id"] != operation_id:
            r.add("operation_ids", child(child(base, "preview"), "operation_id"), "the recorded preview belongs to this operation")
        if target and "target" in preview and preview["target"]["key"] != target["key"]:
            r.add("operation_ids", child(child(base, "preview"), "target"), "the record and its preview name the same target")
    if receipt:
        check_receipt(receipt, child(base, "receipt"), r)
        if receipt["operation_id"] != operation_id:
            r.add("operation_ids", child(child(base, "receipt"), "operation_id"), "the receipt belongs to this operation")
        if target and receipt["target"]["key"] != target["key"]:
            r.add("operation_ids", child(child(base, "receipt"), "target"), "the record and its receipt name the same target")
        number, _, codes = receipt_rule(receipt)
        known_failure = number not in (1, 2) and not lasting(receipt["outcome"])
        if status == "applied" and (number in (1, 2) or not lasting(receipt["outcome"])):
            r.add("operation_status", child(base, "status"), "applied needs a receipt with a lasting effect and no unknown or partial dimension")
        elif status == "partial" and not codes.only("E_PARTIAL"):
            r.add("operation_status", child(base, "status"), "partial needs a receipt that receipt_rules make E_PARTIAL")
        elif status == "unknown" and number != 1:
            r.add("operation_status", child(base, "status"), "unknown cannot hold a receipt with a known outcome (CLI-060)")
        elif status in ("not_applied", "failed") and not known_failure:
            r.add("operation_status", child(base, "status"), f"{status} holds a receipt with no lasting effect and no unknown dimension")
    items = record["items"]
    if items is not None:
        # An item whose own record cannot be read (target null, status unknown) says nothing
        # about the batch: the status rules and the preview comparison use the readable items.
        statuses = [item["status"] for item in items if item["target"] is not None]
        omitted = record.get("items_omitted", 0)
        expected = operation_batch_status(statuses) if statuses else None
        if status == "planned" and any(s != "planned" for s in statuses):
            r.add("operation_status", child(base, "status"), "a planned batch has only planned items")
        elif omitted and status != "unknown" and any(s in ("unknown", "dispatching") for s in statuses):
            # A shortened list shows only some items: it can still prove the batch unknown.
            r.add("operation_status", child(base, "status"), "a batch with an unknown or dispatching item is unknown")
        elif not omitted and status in ("applied", "partial", "unknown", "failed", "not_applied") and expected and status != expected:
            r.add("operation_status", child(base, "status"), f"a batch whose items are {', '.join(statuses)} is {expected}")
        operations = [item["operation_id"] for item in items]
        if len(set(operations)) != len(operations):
            r.add("operation_ids", child(base, "items"), "every batch item has its own operation id")
        if omitted:
            done = contract.data["operation"]["done_status"]
            seen_done = False
            for item in items:
                if item["status"] in done:
                    seen_done = True
                elif seen_done:
                    r.add("operation_shortening", child(base, "items"),
                          "a shortened batch lists the items that need attention before the done ones (operation.shortening)")
                    break
        if preview and "items" in preview:
            # The record's preview may be shortened (omitted); the record lists every item, or
            # items_omitted counts those it leaves out (attention first, so not in input order).
            planned = [(item["target"]["key"], item["operation_id"]) for item in preview["items"]]
            recorded = [((item["target"] or {}).get("key"), item["operation_id"]) for item in items]
            if preview["total"] != len(items) + omitted:
                listed = f"{len(items)} + {omitted} omitted" if omitted else str(len(items))
                r.add("operation_ids", child(base, "items"), f"a batch record lists every item: its preview counts {preview['total']}, the record has {listed}")
            elif omitted:
                keys = dict((op, key) for key, op in planned)
                if any(op in keys and key is not None and keys[op] != key for key, op in recorded):
                    r.add("operation_ids", child(base, "items"), "a batch record lists the items of its preview")
            elif any(p_op != r_op or (r_key is not None and p_key != r_key)
                     for (p_key, p_op), (r_key, r_op) in zip(planned, recorded)):
                r.add("operation_ids", child(base, "items"), "a batch record lists the items of its preview, in order")
        for i, item in enumerate(items):
            if "receipt" in item and item["receipt"]["operation_id"] != item["operation_id"]:
                r.add("operation_ids", child(child(child(base, "items"), i), "receipt"), "the item and its receipt name the same operation")
    created = parse_time(record["created_at"])
    times = [(key, parse_time(record[key])) for key in ("dispatched_at", "finished_at") if record[key]]
    previous_key, previous = "created_at", created
    for key, value in times:
        if value < previous:
            r.add("operation_times", child(base, key), f"{key} is not before {previous_key}")
        previous_key, previous = key, value
    if record["expires_at"]:
        lifetime = (parse_time(record["expires_at"]) - created).total_seconds()
        if not 0 < lifetime <= contract.max_ttl:
            r.add("token_ttl", child(base, "expires_at"),
                  f"the token lives {lifetime:.0f} s; it expires within {contract.max_ttl} s of the dry-run (CLI-055)")
    if record["resolution"] and parse_time(record["resolution"]["resolved_at"]) < created:
        r.add("operation_times", child(child(base, "resolution"), "resolved_at"), "an operation is resolved after it was created")


def check_scope(scope: dict, base: str, r: Report) -> None:
    if scope["expires_at"] and parse_time(scope["expires_at"]) <= parse_time(scope["granted_at"]):
        r.add("scope_times", child(base, "expires_at"), "a scope expires after it was granted (SEC-011)")
    for key in ("outputs", "roots"):
        for i, path in enumerate(scope.get(key, [])):
            if not is_absolute(path):
                r.add("scope_paths", child(child(base, key), i), "outputs and roots are absolute directories, checked after symlink resolution (SEC-012)")


def check_notice(notice: dict, base: str, r: Report) -> None:
    if notice["type"] != "update_available":
        return
    current, latest = semver_key(notice["current_version"]), semver_key(notice["latest_version"])
    if latest <= current:
        r.add("notice_versions", child(base, "latest_version"), "an update notice names a version newer than the current one (CLI-071)")
    elif latest[0][0] > current[0][0] and notice["severity"] != "warning":
        r.add("notice_versions", child(base, "severity"), "an update that crosses a major version is a warning (CLI-071)")


def check_update_failure(details: dict, base: str, r: Report) -> None:
    if details.get("binary_replaced") and "target_version" in details and details["current_version"] != details["target_version"]:
        r.add("update_versions", child(base, "current_version"), "once the binary is replaced the running version is the target (CLI-070)")


def check_update(update: dict, base: str, r: Report) -> None:
    status = update["status"]
    if status in ("updated", "up_to_date") and update["current_version"] != update["target_version"]:
        r.add("update_versions", child(base, "current_version"), f"{status} reports the final state: current_version equals target_version (CLI-070)")
    # --check with --target-version <older> --allow-downgrade reports an available downgrade.
    if status == "available" and semver_key(update["target_version"]) == semver_key(update["current_version"]):
        r.add("update_versions", child(base, "target_version"), "an available target differs from the current version (CLI-070)")
    if status == "updated" and update.get("previous_version") == update["current_version"]:
        r.add("update_versions", child(base, "previous_version"), "an update changes the installed version")


def check_untrusted(envelope: dict, r: Report) -> None:
    for i, path in enumerate(envelope["meta"].get("untrusted", [])):
        nodes = [envelope]
        segments = path.split(".")
        for depth, segment in enumerate(segments):
            name = segment.rstrip("[]")
            arrays = (len(segment) - len(name)) // 2
            following = depth + 1 < len(segments)
            next_nodes = []
            for node in nodes:
                if not isinstance(node, dict) or name not in node:
                    continue
                values = [node[name]]
                for _ in range(arrays):
                    expanded = []
                    for value in values:
                        if isinstance(value, list):
                            expanded.extend(value)
                        else:
                            r.add("untrusted_paths", child(child("/meta", "untrusted"), i), f"{path}: {name} is not an array, so [] does not apply (CLI-046)")
                    values = expanded
                for value in values:
                    if following and isinstance(value, list):
                        r.add("untrusted_paths", child(child("/meta", "untrusted"), i), f"{path}: {name} is an array; step into it with [] (CLI-046)")
                    else:
                        next_nodes.append(value)
            nodes = next_nodes


def data_kind(data) -> str:
    """Mirror of envelope.schema.json $defs/data: which reserved object data is."""
    if not isinstance(data, dict):
        return "other"
    if all(key in data for key in ("operation_id", "status", "created_at")):
        return "operation"
    if "confirm_token" in data or "preview" in data:
        return "dry_run"
    if "receipt" in data:
        return "write"
    if "items" in data and "summary" in data:
        return "batch"
    if "items" in data and any(key in data for key in ("count", "has_more", "next_cursor", "next_offset", "continuation_reason", "truncated")):
        return "page"
    return "other"


# The data of reference --index and reference --command (reference-index.schema.json, reference-command.schema.json).
INDEX_KEY_SETS = ({"tool", "version", "commands"}, {"tool", "version", "groups"})
COMMAND_KEYS = {"tool", "version", "command", "schemas", "error_codes"}


def reference_kind(data, contract: Contract) -> str:
    """Which reference output data is, by its exact key set: "reference" (full), "index", "command" or ""."""
    if not isinstance(data, dict):
        return ""
    keys = set(data)
    if keys == contract.reference_keys:
        return "reference"
    if keys in INDEX_KEY_SETS:
        return "index"
    if keys == COMMAND_KEYS:
        return "command"
    return ""


def check_error(error: dict, base: str, r: Report, contract: Contract) -> None:
    code, details = error["code"], error["details"]
    at = child(base, "details")
    receipt = details.get("receipt")
    if receipt:
        check_receipt(receipt, child(at, "receipt"), r)
        number, ok, codes = receipt_rule(receipt)
        if ok:
            r.add("receipt_rule", child(at, "receipt"), "receipt_rules make this receipt ok; a fully successful write is not an error (CLI-059)")
        elif not codes.allows(code):
            r.add("receipt_rule", child(base, "code"), f"receipt rule {number} gives {codes.describe()}, not {code} (CLI-059)")
        if "operation_id" in details and details["operation_id"] != receipt["operation_id"]:
            r.add("receipt_consistency", child(at, "operation_id"), "details.operation_id names the receipt's operation")
    if "items" in details and "summary" in details:
        check_batch(details, at, r)
        expected = batch_expected_code(details)
        if expected is None:
            r.add("batch_rule", child(at, "items"), "every item succeeded, so the batch is ok true (CLI-063)")
        elif expected and code != expected:
            r.add("batch_rule", child(base, "code"), f"contract.json batch.ok_rule gives {expected} for these items, not {code} (CLI-063)")
    if "preview" in details:
        check_preview(details["preview"], child(at, "preview"), r)
        if code == "E_CONFIRMATION_REQUIRED" and "operation_id" in details:
            check_token(details, at, r)
    scope = details.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("grant_example"), dict):
        check_scope(scope["grant_example"], child(child(at, "scope"), "grant_example"), r)
    if code == "E_OUTCOME_UNKNOWN" and details["operation_id"] not in details["lookup"]:
        r.add("outcome_lookup", child(at, "lookup"), "lookup is the command that looks up details.operation_id (CLI-015)")
    if "stage" in details and "binary_replaced" in details:
        check_update_failure(details, at, r)


def check_envelope(envelope: dict, r: Report, contract: Contract, budgets: bool = True) -> None:
    size = len(compact(envelope))
    if budgets and not envelope["ok"] and size > contract.budgets["error_chars"]:
        r.add("error_budget", "", f"the error envelope is {size} characters; the budget is {contract.budgets['error_chars']} (CLI-042)")
    if budgets and envelope["ok"]:
        kind = reference_kind(envelope["data"], contract)
        if kind == "index" and size > contract.budgets["reference_index_chars"]:
            r.add("index_budget", "", f"the reference --index response is {size} characters; above {contract.budgets['reference_index_chars']} it returns groups (CLI-031)")
        elif kind == "command" and size > contract.budgets["reference_command_chars"]:
            r.add("command_budget", "", f"the reference --command response is {size} characters; the budget is {contract.budgets['reference_command_chars']} (CLI-032)")
        elif not kind and size > contract.budgets["default_success_chars"]:
            # The full reference (CLI-033) is exempt (contract.json size_budgets.exempt).
            r.add("success_budget", "", f"the response is {size} characters; the default budget is {contract.budgets['default_success_chars']} (CLI-042)")
    check_untrusted(envelope, r)
    for i, notice in enumerate(envelope["meta"].get("notices", [])):
        check_notice(notice, child(child("/meta", "notices"), i), r)
    if not envelope["ok"]:
        check_error(envelope["error"], "/error", r, contract)
        return
    data = envelope["data"]
    kind = data_kind(data)
    if kind == "operation":
        check_operation(data, "/data", r, contract)
    elif kind == "dry_run":
        check_preview(data["preview"], "/data/preview", r)
        check_token(data, "/data", r)
    elif kind == "write":
        receipt = data["receipt"]
        check_receipt(receipt, "/data/receipt", r)
        number, ok, codes = receipt_rule(receipt)
        if not ok:
            r.add("receipt_rule", "/data/receipt", f"receipt rule {number} makes this write ok false with {codes.describe()} (CLI-059)")
        if data["operation_id"] != receipt["operation_id"]:
            r.add("receipt_consistency", "/data/operation_id", "data.operation_id names the receipt's operation")
    elif kind == "batch":
        check_batch(data, "/data", r)
        expected = batch_expected_code(data)
        if expected is not None:
            r.add("batch_rule", "/data", f"a batch with a failed, unknown or skipped item is ok false with {expected or 'its first failed code'} (CLI-063)")
    elif kind == "page":
        check_page(data, "/data", r)


# ---------------------------------------------------------------- reference


def tree_failures(paths: list, aliases: list, base: str, check: str, r: Report) -> None:
    repeated = sorted({p for p in paths if paths.count(p) > 1})
    if repeated:
        r.add(check, base, f"command paths are unique (CLI-021); repeated: {', '.join(repeated)}")
    for path in sorted(set(paths)):
        if any(other.startswith(path + " ") for other in paths):
            r.add(check, base, f"'{path}' is both a command and a group; a path is one or the other (CLI-021)")
    seen = set(paths)
    for alias in aliases:
        if alias in seen:
            r.add(check, base, f"alias '{alias}' collides with a command path or another alias (CLI-021)")
        seen.add(alias)


def value_matches(type_: str, value) -> bool:
    if type_ in ("string", "path"):
        return isinstance(value, str)
    if type_ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "boolean":
        return isinstance(value, bool)
    return True


def param_failures(param: dict, at: str, r: Report) -> None:
    type_ = param["type"]
    if type_ != "enum" and not all(value_matches(type_, v) for v in param.get("enum", [])):
        r.add("reference_links", at, f"--{param['name']}: enum values match type {type_} (CLI-024)")
    if "default" in param:
        values = param["default"] if param.get("multiple") and isinstance(param["default"], list) else [param["default"]]
        if type_ == "enum":
            good = all(v in param["enum"] for v in values)
        else:
            good = all(value_matches(type_, v) for v in values)
        if not good:
            r.add("reference_links", child(at, "default"), f"--{param['name']}: the default matches the parameter's type and values (CLI-024)")


def schema_entry_failures(entry: dict, at: str, r: Report) -> None:
    fields = set(entry["fields"])
    extra = [f for f in entry["default_fields"] if f not in fields]
    if extra:
        r.add("reference_links", child(at, "default_fields"), f"default fields are fields of the schema; unknown: {', '.join(extra)}")
    for i, path in enumerate(entry["untrusted_fields"]):
        if path.split(".", 1)[0].rstrip("[]") not in fields:
            r.add("reference_links", child(child(at, "untrusted_fields"), i), f"untrusted field {path!r} is not a field of the schema (CLI-046)")


def command_failures(tool: str, command: dict, at: str, schemas, table, r: Report) -> None:
    """Checks of one command entry. schemas and table are None when the caller checks those links itself."""
    path = command["path"]
    if schemas is not None and command["output"] not in schemas:
        r.add("reference_links", child(at, "output"), f"{path}: output names an entry of schemas (CLI-020)")
    missing = [code for code in command["error_codes"] if table is not None and code not in table]
    if missing:
        r.add("reference_links", child(at, "error_codes"), f"{path}: error codes are declared in error_codes: {', '.join(missing)} (CLI-026)")
    names = [param["name"] for param in command["params"]]
    if len(set(names)) != len(names):
        r.add("reference_links", child(at, "params"), f"{path}: parameter names are unique")
    for i, param in enumerate(command["params"]):
        param_failures(param, child(child(at, "params"), i), r)
    prefixes = [f"{tool} {p}" for p in [path, *command["aliases"]]]
    for i, example in enumerate(command["examples"]):
        if not any(example == prefix or example.startswith(prefix + " ") for prefix in prefixes):
            r.add("reference_examples", child(child(at, "examples"), i), f"{path}: an example is a runnable call that starts with '{tool} {path}'")
    examples = command["examples"]
    if path == SELF_UPDATE:
        if any("--confirm" in example for example in examples):
            r.add("reference_examples", child(at, "examples"), "update is outside the confirm gate and never takes --confirm (CLI-070)")
    elif command["type"] == "write":
        if not any("--dry-run" in e for e in examples) or not any("--confirm" in e for e in examples):
            r.add("reference_examples", child(at, "examples"), f"{path}: a write command's examples show the --dry-run and --confirm pair (CLI-030)")
    if command["dangerous"] and not all("--dangerous" in e for e in examples):
        r.add("reference_examples", child(at, "examples"), f"{path}: a dangerous command's examples carry --dangerous (CLI-056)")


def error_table_failures(table: dict, at: str, r: Report, contract: Contract, require_core: bool) -> None:
    for code, entry in contract.core.items():
        if code not in table:
            if require_core:
                r.add("reference_error_table", at, f"the error table lists every core code; missing {code} (CLI-010)")
            continue
        got = table[code]
        if (got["exit"], got["retryable"]) != (entry["exit"], entry["retryable"]):
            r.add("reference_error_table", child(at, code), f"{code} keeps exit {entry['exit']} and retryable {str(entry['retryable']).lower()} (CLI-010)")
    for code, got in table.items():
        if code in contract.core:
            continue
        known = contract.known_extensions.get(code)
        if known and (got["exit"], got["retryable"]) != (known["exit"], known["retryable"]):
            r.add("reference_error_table", child(at, code), f"{code} keeps its registered exit {known['exit']} and retryable {known['retryable']}")
        if got["exit"] == 9 and not code.endswith("_REQUIRED"):
            r.add("reference_error_table", child(at, code), f"{code}: exit 9 is reserved for human-action codes (CLI-018)")


def check_reference(ref: dict, base: str, r: Report, contract: Contract) -> None:
    commands = ref["commands"]
    by_path = {c["path"]: c for c in commands}
    tree_failures([c["path"] for c in commands], [a for c in commands for a in c["aliases"]], child(base, "commands"), "reference_tree", r)
    for i, command in enumerate(commands):
        root = command["path"].split(" ", 1)[0]
        if root in RESERVED_ROOTS and command["path"] not in LIBRARY_BUILTINS + [SELF_UPDATE]:
            r.add("reference_tree", child(child(base, "commands"), i), f"'{root}' is a reserved built-in name (CLI-021)")
    for name in LIBRARY_BUILTINS:
        command = by_path.get(name)
        if command is None:
            r.add("reference_builtins", child(base, "commands"), f"the built-in '{name}' is part of the command tree (CLI-021, CLI-031, CLI-061)")
        elif command["type"] != "read" or command["effects"] != ["observe"] or command["dangerous"]:
            r.add("reference_builtins", child(base, "commands"), f"the built-in '{name}' is a read that only observes (the journal is not an effect)")
    table = ref["error_codes"]
    error_table_failures(table, child(base, "error_codes"), r, contract, require_core=True)
    # JSON Schema compares numbers by value, so an exit written 1.0 passed the schema as 1.
    exits = {"0"} | {str(int(entry["exit"])) for entry in table.values()}
    missing = sorted(exits - set(ref["exit_codes"]), key=int)
    if missing:
        r.add("reference_error_table", child(base, "exit_codes"), f"exit_codes explains every exit the error table uses; missing {', '.join(missing)}")
    need = max((TIERS[contract.min_tier[e]] for c in commands if c["path"] != SELF_UPDATE for e in c["effects"]), default=0)
    if need > TIERS[ref["risk_tier"]]:
        r.add("reference_tier", child(base, "risk_tier"), f"the commands' effects need at least T{need}; the tool declares {ref['risk_tier']} (SEC-001)")
    for i, command in enumerate(commands):
        command_failures(ref["tool"], command, child(child(base, "commands"), i), ref["schemas"], table, r)
    for name, entry in ref["schemas"].items():
        schema_entry_failures(entry, child(child(base, "schemas"), name), r)
    names = [flag["name"] for flag in ref["global_flags"]]
    missing_flags = [flag for flag in contract.control_flags if flag not in names]
    if missing_flags or len(set(names)) != len(names):
        r.add("global_flags", child(base, "global_flags"), f"global_flags lists each control flag once; missing {', '.join(missing_flags) or 'none'}")


def check_reference_index(index: dict, base: str, r: Report, contract: Contract) -> None:
    size, budget = len(compact(index)), contract.budgets["reference_index_chars"]
    if size > budget:
        r.add("index_budget", base, f"the index is {size} characters; above {budget} return groups instead (CLI-031)")
    if "commands" in index:
        tree_failures([c["path"] for c in index["commands"]], [], child(base, "commands"), "index_paths", r)
    else:
        paths = [g["path"] for g in index["groups"]]
        if len(set(paths)) != len(paths):
            r.add("index_paths", child(base, "groups"), "group paths are unique (CLI-031)")


def check_reference_command(entry: dict, base: str, r: Report, contract: Contract) -> None:
    size, budget = len(compact(entry)), contract.budgets["reference_command_chars"]
    if size > budget:
        r.add("command_budget", base, f"the command entry is {size} characters; the budget is {budget} (CLI-032)")
    command = entry["command"]
    if set(entry["schemas"]) != {command["output"]}:
        r.add("command_entry_links", child(base, "schemas"), "schemas holds exactly the entry the command's output names (CLI-032)")
    if set(entry["error_codes"]) != set(command["error_codes"]):
        r.add("command_entry_links", child(base, "error_codes"), "error_codes holds exactly the codes the command lists (CLI-026, CLI-032)")
    error_table_failures(entry["error_codes"], child(base, "error_codes"), r, contract, require_core=False)
    command_failures(entry["tool"], command, child(base, "command"), None, None, r)
    for name, schema_entry in entry["schemas"].items():
        schema_entry_failures(schema_entry, child(child(base, "schemas"), name), r)


def check_context(context: dict, base: str, r: Report) -> None:
    directory, path = context["state"]["dir"], context["authorization"]["path"]
    if directory is not None and not is_absolute(directory):
        r.add("context_paths", child(child(base, "state"), "dir"), "the state directory is reported as an absolute path (SEC-030)")
    if path is not None and not is_absolute(path):
        r.add("context_paths", child(child(base, "authorization"), "path"), "the scope path is absolute (SEC-011)")


def check_doctor(doctor: dict, base: str, r: Report) -> None:
    names = [c["check"] for c in doctor["checks"]]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        r.add("doctor_checks", child(base, "checks"), f"check names are unique; repeated: {', '.join(repeated)} (CLI-036)")


def check_changelog(log: dict, base: str, r: Report) -> None:
    keys = [semver_key(e["version"]) for e in log["entries"]]
    if keys != sorted(keys, reverse=True) or len(set(keys)) != len(keys):
        r.add("changelog_order", child(base, "entries"), "entries are unique and newest first (CLI-037)")
    current = semver_key(log["current_version"])
    if any(k > current for k in keys):
        r.add("changelog_order", child(base, "entries"), "no entry is newer than current_version")
    if "since" in log and any(k <= semver_key(log["since"]) for k in keys):
        r.add("changelog_order", child(base, "since"), "--since returns only versions strictly newer than since (CLI-037)")
    if "count" in log and log["count"] != len(log["entries"]):
        r.add("changelog_order", child(base, "count"), "count equals the number of entries")


# ---------------------------------------------------------------- generic checks (CLI-007)


def generic_failures(value, base: str = "", map_object: bool = False) -> list:
    failures = []
    if isinstance(value, dict):
        for key, item in value.items():
            at = child(base, key)
            if not map_object and not SNAKE.fullmatch(key):
                failures.append(Failure("semantic", "snake_case", at, f"key {key!r} is not lowercase snake_case (CLI-007)"))
            if not map_object:
                if (key in ("id", "iid") or key.endswith("_id")) and not (item is None or isinstance(item, str)):
                    failures.append(Failure("semantic", "id_strings", at, f"{key} is an id and ids are strings (CLI-007)"))
                if (key == "ids" or key.endswith("_ids")) and not (isinstance(item, list) and all(isinstance(x, str) for x in item)):
                    failures.append(Failure("semantic", "id_strings", at, f"{key} holds ids and ids are strings (CLI-007)"))
                if key.endswith("_at") and not (item is None or (isinstance(item, str) and TIMESTAMP.fullmatch(item))):
                    failures.append(Failure("semantic", "utc_times", at, f"{key} is a time: ISO 8601 UTC with Z (CLI-007)"))
            failures.extend(generic_failures(item, at, map_object=(key in MAP_KEYS and isinstance(item, dict))))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            failures.extend(generic_failures(item, child(base, i)))
    return failures


# ---------------------------------------------------------------- dispatch


def semantic_failures(kind: str, value, contract: Contract) -> list:
    r = Report()
    checks = {
        "envelope": lambda: check_envelope(value, r, contract),
        "envelope#/$defs/stream_line": lambda: check_envelope(value, r, contract, budgets=False),
        "error-details": lambda: check_error(value, "", r, contract),
        "preview": lambda: check_preview(value, "", r),
        "receipt": lambda: check_receipt(value, "", r),
        "operation": lambda: check_operation(value, "", r, contract),
        "scope": lambda: check_scope(value, "", r),
        "page": lambda: check_page(value, "", r),
        "batch": lambda: check_batch(value, "", r),
        "reference": lambda: check_reference(value, "", r, contract),
        "reference-index": lambda: check_reference_index(value, "", r, contract),
        "reference-command": lambda: check_reference_command(value, "", r, contract),
        "context": lambda: check_context(value, "", r),
        "doctor": lambda: check_doctor(value, "", r),
        "changelog": lambda: check_changelog(value, "", r),
        "update": lambda: check_update(value, "", r),
        "notice": lambda: check_notice(value, "", r),
    }
    check = checks.get(kind)
    if check:
        try:
            check()
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            r.add("internal", "", f"semantic check crashed on an unexpected shape: {type(exc).__name__}: {exc}")
    return r.items + generic_failures(value)


# ---------------------------------------------------------------------------------------------------- schemas


class SchemaSet:
    def __init__(self, contract_dir: Path) -> None:
        self.dir = contract_dir / "schemas"
        self.schemas: dict[str, dict] = {}
        self.failures: list[str] = []
        self.ref_count = 0
        self._validators: dict[str, Draft202012Validator] = {}
        self.registry = Registry()

    def load(self) -> None:
        found = sorted(p.name for p in self.dir.glob("*.schema.json"))
        expected = [f"{name}.schema.json" for name in SCHEMA_NAMES]
        for name in sorted(set(expected) - set(found)):
            self.failures.append(f"schemas: missing {name}")
        for name in sorted(set(found) - set(expected)):
            self.failures.append(f"schemas: unexpected {name}; add it to SCHEMA_NAMES with its examples")
        resources = []
        for file in found:
            name = file.removesuffix(".schema.json")
            try:
                schema = load_strict(self.dir / file)
            except (StrictJSONError, OSError) as exc:
                self.failures.append(f"schemas/{file}: {exc}")
                continue
            if schema.get("$schema") != METASCHEMA:
                self.failures.append(f"schemas/{file}: $schema is {METASCHEMA}")
            if schema.get("$id") != URN_PREFIX + name + URN_SUFFIX:
                self.failures.append(f"schemas/{file}: $id is {URN_PREFIX}{name}{URN_SUFFIX}")
            if schema.get("title") != name:
                self.failures.append(f"schemas/{file}: title is {name}")
            errors = list(Draft202012Validator(Draft202012Validator.META_SCHEMA).iter_errors(schema))
            for error in errors:
                self.failures.append(f"schemas/{file}: metaschema: {pointer(error.absolute_path) or '/'}: {error.message}")
            self.schemas[name] = schema
            resources.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
        self.registry = Registry().with_resources(resources).crawl()
        for name, schema in self.schemas.items():
            for at, ref in self._refs(schema):
                self.ref_count += 1
                try:
                    self.registry.resolver(schema["$id"]).lookup(ref)
                except Unresolvable:
                    self.failures.append(f"schemas/{name}.schema.json: {at or '/'}: $ref {ref} does not resolve")

    def _refs(self, node, at=""):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                yield at, node["$ref"]
            for key, value in node.items():
                if key not in ("enum", "const", "examples", "default"):
                    yield from self._refs(value, child(at, key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from self._refs(value, child(at, i))

    def validator(self, kind: str) -> Draft202012Validator:
        if kind not in self._validators:
            name, _, fragment = kind.partition("#")
            if name not in self.schemas or name == "common":
                raise KeyError(f"unknown schema {kind!r}; use one of: {', '.join(n for n in SCHEMA_NAMES if n != 'common')}")
            ref = URN_PREFIX + name + URN_SUFFIX + (f"#{fragment}" if fragment else "")
            self._validators[kind] = Draft202012Validator({"$schema": METASCHEMA, "$ref": ref}, registry=self.registry)
        return self._validators[kind]

    def node(self, ref: str):
        name, _, fragment = ref.partition("#")
        value = self.schemas[name]
        for part in [p for p in fragment.split("/") if p]:
            value = value[part.replace("~1", "/").replace("~0", "~")]
        return value


def schema_failures(validator: Draft202012Validator, value) -> list:
    failures = []
    for error in sorted(validator.iter_errors(value), key=lambda e: (list(map(str, e.absolute_path)), str(e.validator))):
        failures.append(Failure("schema", error.validator if error.validator is not None else "false", pointer(error.absolute_path), error.message))
    return failures


def validate_document(schemas: SchemaSet, contract: Contract, kind: str, raw: bytes) -> list:
    """All failures of one document: strict parsing, then the schema, then (when the schema passes) the semantic checks."""
    try:
        value, failures = parse_strict(raw)
    except StrictJSONError as exc:
        return [Failure("parse", exc.check, "", str(exc))]
    failures = list(failures) + schema_failures(schemas.validator(kind), value)
    if not any(f.kind == "schema" for f in failures):
        failures.extend(semantic_failures(kind, value, contract))
    return failures


# ---------------------------------------------------------------------------------------------------- contract agreement


def agreement_failures(schemas: SchemaSet, contract: Contract) -> tuple[list, int]:
    """The schemas encode contract.json: compare enums and key sets."""
    c = contract.data
    failures: list[str] = []
    count = 0

    def same(label, expected, actual, ordered=False):
        nonlocal count
        count += 1
        if (list(expected) != list(actual)) if ordered else (set(expected) != set(actual)):
            # key=str: enums may mix null and strings (state.protection).
            shown = (lambda values: list(values)) if ordered else (lambda values: sorted(values, key=str))
            failures.append(f"contract agreement: {label}: contract {shown(expected)} != schema {shown(actual)}")

    def subset(label, expected, actual):
        nonlocal count
        count += 1
        missing = set(expected) - set(actual)
        if missing:
            failures.append(f"contract agreement: {label}: schema lacks {sorted(missing)}")

    n = schemas.node
    try:
        same("effects", c["effects"]["values"], n("common#/$defs/effect")["enum"])
        same("write effects", c["effects"]["write_if_any_of"], n("common#/$defs/write_effect")["enum"])
        same("dangerous effects", c["effects"]["dangerous_by_default"],
             n("common#/$defs/dangerous_rule/if/properties/effects/contains")["enum"])
        same("risk tiers", c["risk_tiers"]["values"], n("common#/$defs/risk_tier")["enum"])
        same("maturity", c["maturity"]["values"], n("common#/$defs/maturity")["enum"])
        same("dispatch_state", c["error_details"]["dispatch_state"], n("common#/$defs/dispatch_state")["enum"])
        same("problem", c["error_details"]["problem"], n("common#/$defs/problem")["enum"])
        same("conflict", c["error_details"]["conflict"], n("common#/$defs/conflict")["enum"])
        same("conflict meanings (contract)", c["error_details"]["conflict"], c["error_details"]["conflict_meaning"])
        same("gate conflicts", c["error_details"]["gate_conflict"], n("common#/$defs/gate_conflict")["enum"])
        subset("gate conflicts are contract conflicts", c["error_details"]["gate_conflict"], c["error_details"]["conflict"])
        same("human_action", c["error_details"]["human_action"], n("common#/$defs/human_action")["enum"])
        same("core codes", c["error_codes"]["core"], n("common#/$defs/core_error_code")["enum"])
        same("retryable core codes", [k for k, v in c["error_codes"]["core"].items() if v["retryable"]], n("common#/$defs/retryable_core_code")["enum"])
        same("non-retryable core codes", [k for k, v in c["error_codes"]["core"].items() if not v["retryable"]],
             n("common#/$defs/non_retryable_core_code")["enum"])
        same("error exits", [int(k) for k in c["exit_codes"] if k != "0"], n("common#/$defs/error_exit")["enum"])
        same("exits used by core codes", {v["exit"] for v in c["error_codes"]["core"].values()} - set(n("common#/$defs/error_exit")["enum"]), [])
        same("token format", [c["token"]["format"]], [n("common#/$defs/confirm_token")["pattern"]])
        same("token-or-null format", [c["token"]["format"]], [n("common#/$defs/confirm_token_or_null")["pattern"]])
        same("operation id format", [c["operation_id"]["format"]], [n("common#/$defs/operation_id")["pattern"]])
        same("batch id format", [c["operation_id"]["batch_format"]], [n("common#/$defs/batch_id")["pattern"]])
        same("schema_version", [c["envelope"]["schema_version_value"]], [n("common#/$defs/schema_version")["const"]])
        untrusted = re.compile(n("common#/$defs/untrusted_path")["pattern"])
        for example in c["envelope"]["untrusted_paths"]["examples"]:
            same(f"untrusted path example {example}", [True], [bool(untrusted.search(example))])
        same("success keys", c["envelope"]["success_keys"], n("envelope#/$defs/success")["required"])
        same("success properties", c["envelope"]["success_keys"], n("envelope#/$defs/success")["properties"])
        same("failure keys", c["envelope"]["error_keys"], n("envelope#/$defs/failure")["required"])
        same("failure properties", c["envelope"]["error_keys"], n("envelope#/$defs/failure")["properties"])
        same("error object keys", c["envelope"]["error_object_keys"], n("error-details")["required"])
        same("error object properties", c["envelope"]["error_object_keys"], n("error-details")["properties"])
        same("meta required", c["envelope"]["meta_required_keys"], n("envelope#/$defs/meta")["required"])
        same("meta keys", c["envelope"]["meta_required_keys"] + c["envelope"]["meta_optional_keys"], n("envelope#/$defs/meta")["properties"])
        same("details always required", c["error_details"]["always_required"], n("error-details#/$defs/details")["required"])
        same("dispatch_state ref", ["urn:ai-native-cli-spec:common:2.0#/$defs/dispatch_state"],
             [n("error-details#/$defs/details")["properties"]["dispatch_state"]["$ref"]])
        details_props = n("error-details#/$defs/details")["properties"]
        for code, rules in c["error_details"]["by_code"].items():
            definition = schemas.schemas["error-details"]["$defs"].get(code)
            same(f"{code} has a details definition", [True], [definition is not None])
            if definition is None:
                continue
            subset(f"{code} required details", rules.get("required", []), definition.get("required", []))
            text = json.dumps(definition)
            for key, keys in rules.items():
                if key.startswith("required_when") or key == "required_one_of":
                    for detail in keys:
                        same(f"{code} {key} {detail} is encoded", [True], [f'"{detail}"' in text])
                if key in ("optional", "required", "required_one_of") or key.startswith("required_when"):
                    subset(f"{code} {key} keys are typed in details", keys, details_props)
        same("size budget", [c["error_details"]["size_budget_chars"]], [c["size_budgets"]["error_chars"]])
        same("excluded from binding / no shadowing", c["control_flags"]["excluded_from_binding"],
             n("reference#/$defs/param/properties/name/not")["enum"])
        p = c["preview"]
        same("preview required", p["required_keys"], n("preview")["required"])
        same("preview keys", p["required_keys"] + p["single_target_keys"] + p["batch_keys"] + p["optional_keys"], n("preview")["properties"])
        same("preview single keys", p["single_target_keys"], n("preview")["allOf"][0]["else"]["required"])
        same("preview batch keys", p["batch_keys"], n("preview")["allOf"][0]["then"]["required"])
        same("target keys", p["target_keys"], n("common#/$defs/target")["properties"])
        same("change required", p["change_keys"]["required"], n("common#/$defs/change")["required"])
        same("change keys", p["change_keys"]["required"] + p["change_keys"]["optional"], n("common#/$defs/change")["properties"])
        same("blocked_by keys", p["blocked_by_keys"], n("common#/$defs/block")["properties"])
        same("dry-run data keys", p["dry_run_data_keys"], n("envelope#/$defs/dry_run_data")["required"])
        rc = c["receipt"]
        same("receipt required", rc["required_keys"], n("receipt")["required"])
        same("receipt keys", rc["required_keys"] + rc["optional_keys"], n("receipt")["properties"])
        for dim, values in rc["outcome"].items():
            same(f"outcome.{dim}", values, n("receipt#/$defs/outcome")["properties"][dim]["enum"])
        same("outcome dimensions", rc["outcome"], n("receipt#/$defs/outcome")["required"])
        same("failed_stage", rc["failed_stage"], n("receipt#/$defs/failed_stage")["enum"])
        same("write data keys", rc["data_keys_on_success"], n("envelope#/$defs/write_data")["properties"])
        count += 1
        if not receipt_rules_match(c["receipt_rules"]["rules"]):
            failures.append("contract agreement: receipt_rules changed; update receipt_rule() and EXPECTED_RECEIPT_RULES in check-contract.py to match")
        b = c["batch"]
        same("batch result required", b["result_keys"]["required"], n("batch")["required"])
        same("batch result keys", b["result_keys"]["required"] + b["result_keys"]["optional"], n("batch")["properties"])
        same("batch item required", b["item_keys"]["required"], n("batch#/$defs/item")["required"])
        same("batch item keys", b["item_keys"]["required"] + b["item_keys"]["optional"], n("batch#/$defs/item")["properties"])
        same("batch item error keys", b["item_error_keys"], n("batch#/$defs/item_error")["properties"])
        same("batch item error required", b["item_error_keys"], n("batch#/$defs/item_error")["required"])
        same("batch summary keys", b["summary_keys"], n("batch#/$defs/summary")["required"])
        o = c["operation"]
        same("operation record keys", o["record_keys"], n("operation")["required"])
        same("operation record properties", o["record_keys"] + o["optional_keys"], n("operation")["properties"])
        same("operation status", o["status"], n("operation#/$defs/status")["enum"])
        subset("operation done status", o["done_status"], o["status"])
        subset("unresolved status", o["unresolved_status"], o["status"])
        same("resolution outcomes", o["resolution_outcomes"], n("operation#/$defs/resolution")["properties"]["outcome"]["enum"])
        same("resolution keys", o["resolution_keys"], n("operation#/$defs/resolution")["required"])
        s = c["scope"]
        same("scope required", s["required_keys"], n("scope")["required"])
        same("scope keys", s["required_keys"] + s["optional_keys"], n("scope")["properties"])
        same("selector required", s["target_selector_keys"]["required"], n("scope#/$defs/selector")["required"])
        same("selector keys", s["target_selector_keys"]["required"] + s["target_selector_keys"]["optional"], n("scope#/$defs/selector")["properties"])
        same("target match", s["target_match"], n("scope#/$defs/selector")["properties"]["match"]["enum"])
        same("limits keys", s["limits_keys"], n("scope#/$defs/limits")["properties"])
        same("refusal required", s["refusal_details_keys"]["required"], n("scope#/$defs/refusal")["required"])
        same("refusal keys", s["refusal_details_keys"]["required"] + s["refusal_details_keys"]["optional"], n("scope#/$defs/refusal")["properties"])
        same("refusal status", s["refusal_status"], n("scope#/$defs/refusal")["properties"]["status"]["enum"])
        pg = c["pagination"]
        same("page required", pg["required_keys"], n("page")["required"])
        same("page keys", pg["required_keys"] + pg["continuation_keys"] + pg["optional_keys"], n("page")["properties"])
        same("page continuation", pg["continuation_keys"], [list(alt["required"])[0] for alt in n("page")["allOf"][0]["then"]["oneOf"]])
        nt = c["notices"]
        same("notice required", nt["required_keys"], n("notice")["required"])
        same("notice severity", nt["severity"], n("notice")["properties"]["severity"]["enum"])
        same("update notice keys", nt["update_available_keys"], n("notice")["then"]["required"])
        sd = c["self_description"]
        same("reference required", sd["reference"]["required_keys"], n("reference")["required"])
        same("reference keys", sd["reference"]["required_keys"], n("reference")["properties"])
        same("command keys", sd["reference"]["command_required_keys"], n("reference#/$defs/command")["required"])
        same("command properties", sd["reference"]["command_required_keys"], n("reference#/$defs/command")["properties"])
        same("param required", sd["reference"]["param_keys"]["required"], n("reference#/$defs/param_fields")["required"])
        same("param keys", sd["reference"]["param_keys"]["required"] + sd["reference"]["param_keys"]["optional"],
             n("reference#/$defs/param_fields")["properties"])
        same("param types", sd["reference"]["param_types"], n("reference#/$defs/param_fields")["properties"]["type"]["enum"])
        same("schema entry keys", sd["reference"]["schema_entry_keys"], n("reference#/$defs/schema_entry")["required"])
        same("schema shapes", sd["reference"]["schema_shapes"], n("reference#/$defs/schema_entry")["properties"]["shape"]["enum"])
        same("index command keys", sd["reference"]["index_command_keys"], n("reference-index#/$defs/command")["required"])
        same("index group keys", sd["reference"]["index_group_keys"], n("reference-index#/$defs/group")["required"])
        same("context required", sd["context"]["required_keys"], n("context")["required"])
        same("credentials keys", sd["context"]["credentials_keys"], n("context")["properties"]["credentials"]["required"])
        same("state keys", sd["context"]["state_keys"], n("context")["properties"]["state"]["required"])
        same("authorization keys", sd["context"]["authorization_keys"], n("context#/$defs/authorization")["required"])
        same("authorization status", sd["context"]["authorization_status"], n("context#/$defs/authorization")["properties"]["status"]["enum"])
        same("context optional keys", sd["context"]["optional_keys"], [k for k in n("context")["properties"] if k not in sd["context"]["required_keys"]])
        same("profile keys", sd["context"]["profile_keys"], n("context#/$defs/profile")["required"])
        same("profile source", sd["context"]["profile_source"], n("context#/$defs/profile")["properties"]["source"]["enum"])
        subset("update error codes are contract codes", sd["update"]["error_codes"], list(c["error_codes"]["core"]))
        same("state protection", c["state_store"]["protection"] + [None], n("context")["properties"]["state"]["properties"]["protection"]["enum"])
        same("state protection meanings (contract)", c["state_store"]["protection"], c["state_store"]["protection_meaning"])
        same("doctor keys", sd["doctor"]["data_keys"], n("doctor")["properties"])
        same("doctor check keys", sd["doctor"]["check_keys"]["required"], n("doctor#/$defs/check")["required"])
        same("doctor status", sd["doctor"]["status"], n("doctor#/$defs/check")["properties"]["status"]["enum"])
        same("doctor required checks", sd["doctor"]["required_checks"],
             [rule["contains"]["properties"]["check"]["const"] for rule in n("doctor")["properties"]["checks"]["allOf"]])
        same("changelog required", sd["changelog"]["required_data_keys"], n("changelog")["required"])
        same("changelog keys", sd["changelog"]["required_data_keys"] + sd["changelog"]["optional_data_keys"], n("changelog")["properties"])
        same("changelog entry keys", sd["changelog"]["entry_keys"], n("changelog#/$defs/entry")["required"])
        same("changelog categories", sd["changelog"]["change_categories"], n("changelog#/$defs/changes")["properties"])
        u = sd["update"]
        same("update required", u["required_keys"], n("update")["required"])
        same("update keys", u["required_keys"] + u["optional_keys"], n("update")["properties"])
        same("update status", u["status"], n("update#/$defs/status")["enum"])
        same("signature_status", u["signature_status"], n("update#/$defs/signature_status")["enum"])
        same("skill_sync_status", u["skill_sync_status"], n("update#/$defs/skill_sync_status")["enum"])
        same("install_method", u["install_method"], n("update#/$defs/install_method")["enum"])
        same("update stages", u["stages"], n("update#/$defs/stage")["enum"])
        same("update failure keys", u["failure_detail_keys"], n("update#/$defs/failure_details")["required"])
        same("built-ins", sd["builtin_commands"], LIBRARY_BUILTINS + [SELF_UPDATE])
    except (KeyError, IndexError, TypeError) as exc:
        failures.append(f"contract agreement: a schema or contract.json lacks an expected node: {type(exc).__name__}: {exc}")
    return failures, count


# ---------------------------------------------------------------------------------------------------- corpus

INDEX_SCHEMA = {
    "$schema": METASCHEMA,
    "type": "object",
    "properties": {
        "$comment": {"type": "string"},
        "spec_version": {"type": "string"},
        "examples": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "pattern": "^(?:valid|invalid)/[a-z0-9]+(?:-[a-z0-9]+)*\\.json$"},
                    "schema": {"type": "string", "minLength": 1},
                    "expect": {"enum": ["valid", "invalid"]},
                    "rule": {"type": "string", "pattern": "^(?:CLI|SEC|SKL|VER)-[0-9]{3}$"},
                    "reason": {"type": "string", "minLength": 1},
                    "violation": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": ["parse", "schema", "semantic"]},
                            "check": {
                                "description": "The keyword or check id; a list when several keywords encode one constraint (maxLength and pattern of an id).",
                                "anyOf": [
                                    {"type": "string", "minLength": 1},
                                    {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                                ],
                            },
                            "path": {"type": "string", "pattern": "^(?:/.*)?$"},
                            "detail": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "check"],
                        "additionalProperties": False,
                    },
                },
                "required": ["file", "schema", "expect", "rule", "reason"],
                "additionalProperties": False,
                "if": {"properties": {"expect": {"const": "invalid"}}},
                "then": {"required": ["violation"]},
                "else": {"not": {"required": ["violation"]}},
            },
        },
    },
    "required": ["spec_version", "examples"],
    "additionalProperties": False,
}


def matches(failure: Failure, violation: dict) -> bool:
    checks = violation["check"] if isinstance(violation["check"], list) else [violation["check"]]
    return (failure.kind == violation["kind"] and failure.check in checks
            and ("path" not in violation or failure.path == violation["path"])
            and ("detail" not in violation or violation["detail"] in failure.message))


def check_corpus(schemas: SchemaSet, contract: Contract, examples_dir: Path) -> tuple[list, dict]:
    failures: list[str] = []
    stats = Counter()
    try:
        index = load_strict(examples_dir / "index.json")
    except (StrictJSONError, OSError) as exc:
        return [f"examples/index.json: {exc}"], stats
    for error in Draft202012Validator(INDEX_SCHEMA).iter_errors(index):
        failures.append(f"examples/index.json: {pointer(error.absolute_path) or '/'}: {error.message}")
    if failures:
        return failures, stats
    if index["spec_version"] != contract.spec_version:
        failures.append(f"examples/index.json: spec_version is {contract.spec_version}, as in contract.json")
    entries = index["examples"]
    listed = [entry["file"] for entry in entries]
    for file in sorted({f for f in listed if listed.count(f) > 1}):
        failures.append(f"examples/index.json: {file} is listed more than once")
    present = {f"{d}/{p.name}" for d in ("valid", "invalid") for p in (examples_dir / d).glob("*")}
    for file in sorted(present - set(listed)):
        failures.append(f"examples/{file}: not listed in index.json")
    for file in sorted(set(listed) - present):
        failures.append(f"examples/index.json: {file} does not exist")
    stray = sorted(p.name for p in examples_dir.iterdir() if p.name not in ("index.json", "valid", "invalid"))
    for name in stray:
        failures.append(f"examples/{name}: only index.json, valid/ and invalid/ belong in examples/")
    coverage = {name: Counter() for name in SCHEMA_NAMES if name != "common"}
    catalog, source = rule_catalog(examples_dir.parent)
    for entry in entries:
        file, kind, expect = entry["file"], entry["schema"], entry["expect"]
        if not file.startswith(expect + "/"):
            failures.append(f"examples/{file}: expect {expect} but the file is not under {expect}/")
        if entry["rule"] not in catalog:
            failures.append(f"examples/{file}: rule {entry['rule']} is not in the 2.0 rule catalog ({source})")
        path = examples_dir / file
        if not path.is_file():
            continue
        try:
            found = validate_document(schemas, contract, kind, path.read_bytes())
        except KeyError as exc:
            failures.append(f"examples/{file}: {exc.args[0]}")
            continue
        base_name = kind.partition("#")[0]
        if base_name in coverage:
            coverage[base_name][expect] += 1
        stats[expect] += 1
        if expect == "valid":
            for failure in found:
                failures.append(f"examples/{file}: expected valid, but {failure}")
            continue
        violation = entry["violation"]
        if not found:
            failures.append(f"examples/{file}: expected invalid ({violation['kind']}:{violation['check']}), but it passes")
        elif not any(matches(f, violation) for f in found):
            failures.append(f"examples/{file}: does not fail for the stated reason {violation}; it fails with: " + "; ".join(map(str, found)))
        else:
            for failure in found:
                if not matches(failure, violation):
                    failures.append(f"examples/{file}: also fails for another reason: {failure}")
    for name, counts in coverage.items():
        for expect in ("valid", "invalid"):
            if not counts[expect]:
                failures.append(f"examples: the {name} schema has no {expect} example")
    return failures, stats


# ---------------------------------------------------------------------------------------------------- main


def load_contract(contract_dir: Path) -> Contract:
    return Contract(load_strict(contract_dir / "contract.json"))


def run_corpus(contract_dir: Path) -> int:
    try:
        contract = load_contract(contract_dir)
    except (StrictJSONError, OSError, KeyError) as exc:
        print(f"FAIL: contract.json: {exc}")
        return 1
    schemas = SchemaSet(contract_dir)
    schemas.load()
    failures = list(schemas.failures)
    agreement, comparisons = ([], 0)
    if not schemas.failures:
        agreement, comparisons = agreement_failures(schemas, contract)
        failures += agreement
    corpus, stats = ([], Counter())
    if not schemas.failures:
        corpus, stats = check_corpus(schemas, contract, contract_dir / "examples")
        failures += corpus
    print(f"check-contract: contract {contract.data['contract_version']} (spec {contract.spec_version}), {len(schemas.schemas)} schemas")
    print(f"  schemas:            {'ok' if not schemas.failures else 'FAIL'} - metaschema draft 2020-12, ids, {schemas.ref_count} $refs resolved offline")
    print(f"  contract agreement: {'ok' if not agreement and not schemas.failures else 'FAIL'} - {comparisons} comparisons with contract.json")
    print(f"  examples:           {'ok' if not corpus and not schemas.failures else 'FAIL'} - {stats['valid']} valid pass, "
          f"{stats['invalid']} invalid fail for their stated reason")
    if failures:
        print(f"\n{len(failures)} problem(s):")
        for failure in failures:
            print(f"  - {failure}")
        print("FAIL")
        return 1
    print("PASS")
    return 0


def run_documents(contract_dir: Path, kind: str, files: list, lines: bool) -> int:
    try:
        contract = load_contract(contract_dir)
    except (StrictJSONError, OSError, KeyError) as exc:
        print(f"FAIL: contract.json: {exc}")
        return 1
    schemas = SchemaSet(contract_dir)
    schemas.load()
    if schemas.failures:
        print("FAIL: the schemas themselves do not pass; run check-contract.py without --schema first")
        return 1
    try:
        schemas.validator(kind)
    except KeyError as exc:
        print(f"usage: {exc.args[0]}")
        return 2
    problems = 0
    for name in files:
        raw = sys.stdin.buffer.read() if name == "-" else Path(name).read_bytes()
        documents = [(f"{name}:{i}", line) for i, line in enumerate(raw.split(b"\n"), 1) if line.strip()] if lines else [(name, raw)]
        for label, document in documents:
            found = validate_document(schemas, contract, kind, document)
            if found:
                problems += len(found)
                for failure in found:
                    print(f"{label}: {failure}")
            else:
                print(f"{label}: ok")
    return 1 if problems else 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Check the ai-native-cli-spec 2.0 schemas and examples, or validate documents against them.")
    parser.add_argument("--contract-dir", type=Path, default=Path(__file__).resolve().parent.parent / "contract",
                        help="directory holding contract.json, schemas/ and examples/ (default: ../contract next to this script)")
    parser.add_argument("--schema", help="validate FILES against this schema (e.g. envelope, receipt, envelope#/$defs/stream_line)")
    parser.add_argument("--lines", action="store_true", help="with --schema: every non-empty line of a file is one document (NDJSON)")
    parser.add_argument("files", nargs="*", help="documents to validate with --schema; - reads stdin")
    args = parser.parse_args(argv)
    if args.schema is None:
        if args.files:
            parser.error("files need --schema")
        return run_corpus(args.contract_dir)
    if not args.files:
        parser.error("--schema needs at least one file")
    return run_documents(args.contract_dir, args.schema, args.files, args.lines)


if __name__ == "__main__":
    sys.exit(main())
