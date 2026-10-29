# Vendored from ai-native-cli-spec template/python/aicli.
# Do not edit in tool repos; run scripts/sync-spec.js.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Sean Guo

"""aicli implements ai-native-cli-spec 2.0 for Python command-line tools built on
click (typer apps work through typer.main.get_command): the JSON envelope and
error contract, the command registry with reference, help and agent-friendly
parsing, the shared dry-run/confirm write gate with its operation journal and
scope files, the built-in commands (update included), settings and profiles and
the conformance suite. Tools vendor this package into <pkg>/_aicli with
scripts/sync-spec.js and import it relatively (from ._aicli import App, Meta,
WriteSpec). The API mirrors the Go library's in snake_case."""

from __future__ import annotations

from .app import App, Identity, current_app, set_resolved
from .builtins import Check
from .cmdhelpers import output_format, parse_duration, parse_time_arg, wants_field
from .conformance import TB, Harness, Recorder, check_coverage, run_conformance
from .contract_gen import SCHEMA_VERSION, SPEC_VERSION
from .envelope import Notice
from .errors import (
    Candidate,
    CodeSpec,
    Error,
    ambiguous,
    forbidden,
    from_http,
    human_required,
    new_error,
    not_found,
    usage,
    validation,
)
from .gate import (
    BatchItem,
    BatchSpec,
    Block,
    FieldChange,
    ItemOutcome,
    NotDispatchedError,
    Op,
    Preview,
    RejectedError,
    Target,
    WriteSpec,
    not_dispatched,
    rejected,
)
from .query import Page
from .receipt import Outcome, Result
from .registry import (
    ARTIFACT,
    CHANGE,
    CONFIGURE,
    DELIVER,
    DRAFT,
    EXPERIMENTAL,
    OBSERVE,
    PERSIST,
    STABLE,
    VERIFIED,
    Meta,
    ParamDoc,
    SchemaDef,
)
from .selfupdate import (
    Version,
    check_update_target,
    compare_versions,
    package_manager_args,
    parse_version,
)
from .settings import Setting
from .stream import OutputFile, OutputSpec, Stream
from .update import (
    TrustRootUnavailable,
    UpdateConfig,
    detect_install_method,
    verify_sigstore_bundle,
)
from .validate import (
    ContractViolation,
    validate_changelog,
    validate_context,
    validate_doctor,
    validate_envelope,
    validate_operation_view,
    validate_reference_command,
    validate_reference_full,
    validate_reference_index,
    validate_stream,
)

__all__ = [
    "ARTIFACT",
    "CHANGE",
    "CONFIGURE",
    "DELIVER",
    "DRAFT",
    "EXPERIMENTAL",
    "OBSERVE",
    "PERSIST",
    "SCHEMA_VERSION",
    "SPEC_VERSION",
    "STABLE",
    "TB",
    "VERIFIED",
    "App",
    "BatchItem",
    "BatchSpec",
    "Block",
    "Candidate",
    "Check",
    "CodeSpec",
    "ContractViolation",
    "Error",
    "FieldChange",
    "Harness",
    "Identity",
    "ItemOutcome",
    "Meta",
    "NotDispatchedError",
    "Notice",
    "Op",
    "Outcome",
    "OutputFile",
    "OutputSpec",
    "Page",
    "ParamDoc",
    "Preview",
    "Recorder",
    "RejectedError",
    "Result",
    "SchemaDef",
    "Setting",
    "Stream",
    "Target",
    "TrustRootUnavailable",
    "UpdateConfig",
    "Version",
    "WriteSpec",
    "ambiguous",
    "check_coverage",
    "check_update_target",
    "compare_versions",
    "current_app",
    "detect_install_method",
    "forbidden",
    "from_http",
    "human_required",
    "new_error",
    "not_dispatched",
    "not_found",
    "output_format",
    "package_manager_args",
    "parse_duration",
    "parse_time_arg",
    "parse_version",
    "rejected",
    "run_conformance",
    "set_resolved",
    "usage",
    "validate_changelog",
    "validate_context",
    "validate_doctor",
    "validate_envelope",
    "validate_operation_view",
    "validate_reference_command",
    "validate_reference_full",
    "validate_reference_index",
    "validate_stream",
    "validation",
    "verify_sigstore_bundle",
    "wants_field",
]
