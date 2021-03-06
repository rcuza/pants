# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from dataclasses import dataclass
from typing import Tuple

from pants.backend.python.lint.bandit.skip_field import SkipBanditField
from pants.backend.python.lint.bandit.subsystem import Bandit
from pants.backend.python.target_types import InterpreterConstraintsField, PythonSources
from pants.backend.python.util_rules import pex
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.pex import PexRequest, PexRequirements, VenvPex, VenvPexProcess
from pants.core.goals.lint import (
    LINTER_REPORT_DIR,
    LintReport,
    LintRequest,
    LintResult,
    LintResults,
    LintSubsystem,
)
from pants.core.util_rules.config_files import ConfigFiles, ConfigFilesRequest
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.fs import (
    CreateDigest,
    Digest,
    DigestSubset,
    Directory,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import FieldSet, Target
from pants.engine.unions import UnionRule
from pants.python.python_setup import PythonSetup
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class BanditFieldSet(FieldSet):
    required_fields = (PythonSources,)

    sources: PythonSources
    interpreter_constraints: InterpreterConstraintsField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipBanditField).value


class BanditRequest(LintRequest):
    field_set_type = BanditFieldSet


@dataclass(frozen=True)
class BanditPartition:
    field_sets: Tuple[BanditFieldSet, ...]
    interpreter_constraints: InterpreterConstraints


def generate_argv(source_files: SourceFiles, bandit: Bandit) -> Tuple[str, ...]:
    args = []
    if bandit.config is not None:
        args.append(f"--config={bandit.config}")
    args.extend(bandit.args)
    args.extend(source_files.files)
    return tuple(args)


@rule(level=LogLevel.DEBUG)
async def bandit_lint_partition(
    partition: BanditPartition, bandit: Bandit, lint_subsystem: LintSubsystem
) -> LintResult:
    bandit_pex_get = Get(
        VenvPex,
        PexRequest(
            output_filename="bandit.pex",
            internal_only=True,
            requirements=PexRequirements(bandit.all_requirements),
            interpreter_constraints=partition.interpreter_constraints,
            main=bandit.main,
        ),
    )

    config_files_get = Get(ConfigFiles, ConfigFilesRequest, bandit.config_request)
    source_files_get = Get(
        SourceFiles, SourceFilesRequest(field_set.sources for field_set in partition.field_sets)
    )
    # Ensure that the empty report dir exists.
    report_directory_digest_get = Get(Digest, CreateDigest([Directory(LINTER_REPORT_DIR)]))

    bandit_pex, config_files, report_directory, source_files = await MultiGet(
        bandit_pex_get, config_files_get, report_directory_digest_get, source_files_get
    )

    input_digest = await Get(
        Digest,
        MergeDigests(
            (source_files.snapshot.digest, config_files.snapshot.digest, report_directory)
        ),
    )

    result = await Get(
        FallibleProcessResult,
        VenvPexProcess(
            bandit_pex,
            argv=generate_argv(source_files, bandit),
            input_digest=input_digest,
            description=f"Run Bandit on {pluralize(len(partition.field_sets), 'file')}.",
            output_directories=(LINTER_REPORT_DIR,),
            level=LogLevel.DEBUG,
        ),
    )

    report: LintReport | None = None
    report_snapshot = await Get(
        Snapshot, DigestSubset(result.output_digest, PathGlobs([f"{LINTER_REPORT_DIR}/**"]))
    )
    if report_snapshot.files:
        report_snapshot = await Get(
            Snapshot, RemovePrefix(report_snapshot.digest, LINTER_REPORT_DIR)
        )
        report = LintReport(report_snapshot.digest)

    return LintResult.from_fallible_process_result(
        result,
        partition_description=str(sorted(str(c) for c in partition.interpreter_constraints)),
        report=report,
    )


@rule(desc="Lint with Bandit", level=LogLevel.DEBUG)
async def bandit_lint(
    request: BanditRequest, bandit: Bandit, python_setup: PythonSetup
) -> LintResults:
    if bandit.skip:
        return LintResults([], linter_name="Bandit")

    # NB: Bandit output depends upon which Python interpreter version it's run with
    # ( https://github.com/PyCQA/bandit#under-which-version-of-python-should-i-install-bandit). We
    # batch targets by their constraints to ensure, for example, that all Python 2 targets run
    # together and all Python 3 targets run together.
    constraints_to_field_sets = InterpreterConstraints.group_field_sets_by_constraints(
        request.field_sets, python_setup
    )
    partitioned_results = await MultiGet(
        Get(LintResult, BanditPartition(partition_field_sets, partition_compatibility))
        for partition_compatibility, partition_field_sets in constraints_to_field_sets.items()
    )
    return LintResults(partitioned_results, linter_name="Bandit")


def rules():
    return [*collect_rules(), UnionRule(LintRequest, BanditRequest), *pex.rules()]
