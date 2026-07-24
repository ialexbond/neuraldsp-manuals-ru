from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .alignment import align_snapshot
from .canonical import extract_snapshot, validate_snapshot_integrity
from .diffing import classify_change
from .pdf import build_pdf, pdf_metrics, validate_pdf
from .repository import (
    expected_pdf_name,
    manual_catalog,
    parse_pdf_name,
    replace_manual_pdf,
    update_readme,
    validate_manual_directory,
)
from .state import (
    DOCUMENT_FILE,
    STATE_FILE,
    create_archive,
    initialize_state_directory,
    read_json,
    unpack_state,
    write_json,
)
from .translate import apply_safe_update


def _config(path: str) -> dict[str, Any]:
    return read_json(Path(path))


def _write_result(value: dict[str, Any], path: str | None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def _translations(path: str, changed_keys: list[str]) -> dict[str, dict[str, str]]:
    payload = read_json(Path(path).resolve())
    units = payload.get("units")
    if not isinstance(units, dict):
        raise RuntimeError("Translation file must contain a units object.")
    expected = set(changed_keys)
    actual = set(units)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"Translation units do not match the change report. Missing={missing}; extra={extra}"
        )
    result: dict[str, dict[str, str]] = {}
    for key, values in units.items():
        if not isinstance(key, str) or not isinstance(values, dict):
            raise RuntimeError(f"Translations for {key!r} must be an object.")
        if not all(
            isinstance(item_id, str) and isinstance(value, str)
            for item_id, value in values.items()
        ):
            raise RuntimeError(f"Translations for {key} must contain text values.")
        result[key] = values
    return result


def _validate_state_manual(state: dict[str, Any], config: dict[str, Any]) -> None:
    expected = str(config["slug"])
    actual = state.get("manual")
    if actual != expected:
        raise RuntimeError(
            f"State archive belongs to {actual!r}; expected {expected!r}."
        )


def _validate_update_inputs(
    state: dict[str, Any],
    candidate: dict[str, Any],
    report: dict[str, Any],
    config: dict[str, Any],
) -> None:
    baseline = state.get("snapshot")
    if not isinstance(baseline, dict):
        raise RuntimeError("State archive has no valid baseline snapshot.")

    validate_snapshot_integrity(baseline, label="Baseline")
    validate_snapshot_integrity(candidate, label="Candidate")
    expected_report = classify_change(baseline, candidate, config)
    comparisons: dict[str, tuple[Any, Any]] = {
        field: (report.get(field), expected_report.get(field))
        for field in (
            "schema_version",
            "status",
            "baseline_hash",
            "candidate_hash",
            "baseline_integrity_hash",
            "candidate_integrity_hash",
            "baseline_version",
            "upstream_version",
            "changed_units",
            "added_units",
            "removed_units",
            "skeleton_changes",
            "attribute_shape_changes",
        )
    }
    configured_source = config.get("source_url")
    comparisons.update(
        {
            "baseline_source_url": (
                baseline.get("source_url"),
                configured_source,
            ),
            "candidate_source_url": (
                candidate.get("source_url"),
                configured_source,
            ),
            "report_source_url": (
                report.get("source_url"),
                configured_source,
            ),
            "baseline_schema_version": (
                baseline.get("schema_version"),
                expected_report.get("schema_version"),
            ),
            "candidate_schema_version": (
                candidate.get("schema_version"),
                expected_report.get("schema_version"),
            ),
        }
    )
    mismatches = {
        field: {"report_or_state": values[0], "expected": values[1]}
        for field, values in comparisons.items()
        if values[0] is None or values[1] is None or values[0] != values[1]
    }
    if mismatches:
        raise RuntimeError(
            "Change report and candidate do not match the current state snapshot: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )


def _clean_work_directory(
    path: str, expected_leaf: str, manual_slug: str | None = None
) -> Path:
    directory = Path(path).resolve()
    protected = {Path.cwd().resolve(), Path.home().resolve(), Path(directory.anchor)}
    if directory in protected or any(directory in item.parents for item in protected):
        raise RuntimeError(
            f"Refusing to delete a protected work directory: {directory}"
        )
    direct_automation_child = directory.parent.name == ".automation"
    manual_automation_child = (
        manual_slug is not None
        and directory.parent.name == manual_slug
        and directory.parent.parent.name == ".automation"
    )
    if directory.name != expected_leaf or not (
        direct_automation_child or manual_automation_child
    ):
        raise RuntimeError(
            "Work directory must be a marked automation directory ending in "
            f"{expected_leaf}: {directory}"
        )
    if directory.exists():
        shutil.rmtree(directory)
    return directory


def command_snapshot(args: argparse.Namespace) -> int:
    config = _config(args.config)
    snapshot = extract_snapshot(
        args.source or config["source_url"], config["expected_chapter_count"]
    )
    _write_result(snapshot, args.output)
    return 0


def command_bootstrap(args: argparse.Namespace) -> int:
    config = _config(args.config)
    source = args.source or config["source_url"]
    snapshot = extract_snapshot(source, config["expected_chapter_count"])
    if snapshot["section_count"] < config["minimum_section_count"]:
        raise RuntimeError(
            f"Only {snapshot['section_count']} stable sections were found during bootstrap."
        )
    localized_html = Path(args.localized_html).resolve()
    source_template_html = Path(args.source_template_html).resolve()
    asset_directory = Path(args.asset_directory).resolve()
    baseline_pdf = Path(args.baseline_pdf).resolve()
    snapshot, alignment = align_snapshot(snapshot, source_template_html, localized_html)
    if not alignment["aligned"]:
        preview = "; ".join(alignment["failures"][:10])
        raise RuntimeError(
            f"Localized HTML cannot be safely aligned with the source: {preview}"
        )

    metrics = pdf_metrics(
        baseline_pdf,
        asset_directory / "fonts",
        int(config["expected_chapter_count"]),
    )
    with tempfile.TemporaryDirectory(prefix="manual-state-bootstrap-") as temporary:
        state_directory = Path(temporary) / "state"
        initialize_state_directory(
            state_directory,
            str(config["slug"]),
            snapshot,
            localized_html,
            asset_directory,
            baseline_pdf,
            metrics,
        )
        create_archive(state_directory, Path(args.output).resolve())
    result = {
        "status": "bootstrapped",
        "archive": str(Path(args.output).resolve()),
        "upstream_version": snapshot["upstream_version"],
        "chapter_count": snapshot["chapter_count"],
        "section_count": snapshot["section_count"],
        "alignment": alignment,
        "baseline_pdf": metrics,
    }
    _write_result(result, args.result)
    return 0


def command_check(args: argparse.Namespace) -> int:
    config = _config(args.config)
    work_directory = _clean_work_directory(
        args.work_directory, "check", str(config["slug"])
    )
    state_directory = work_directory / "state"
    state = unpack_state(Path(args.state_archive).resolve(), state_directory)
    _validate_state_manual(state, config)
    candidate = extract_snapshot(
        args.source or config["source_url"], config["expected_chapter_count"]
    )
    report = classify_change(state["snapshot"], candidate, config)
    write_json(Path(args.snapshot_output).resolve(), candidate)
    _write_result(report, args.report)
    return 0


def command_update(args: argparse.Namespace) -> int:
    config = _config(args.config)
    report = read_json(Path(args.report).resolve())
    if report.get("status") != "safe_change":
        raise RuntimeError(
            f"Update requires safe_change status, got {report.get('status')}"
        )
    candidate = read_json(Path(args.snapshot).resolve())
    edition_date = args.edition_date or date.today().isoformat()
    work_directory = _clean_work_directory(
        args.work_directory, "update", str(config["slug"])
    )
    state_directory = work_directory / "state"
    state = unpack_state(Path(args.state_archive).resolve(), state_directory)
    _validate_state_manual(state, config)
    _validate_update_inputs(state, candidate, report, config)
    document_path = state_directory / DOCUMENT_FILE
    translations = _translations(args.translations, report["changed_units"])
    update_summary = apply_safe_update(
        state=state,
        candidate_snapshot=candidate,
        changed_keys=report["changed_units"],
        document_path=document_path,
        asset_directory=state_directory / state["asset_directory"],
        translations_by_unit=translations,
        edition_date=edition_date,
    )
    output_pdf = Path(args.output_pdf).resolve()
    build = build_pdf(
        document_path,
        output_pdf,
        config,
        baseline=state.get("baseline_pdf"),
    )
    state["baseline_pdf"] = {
        "name": expected_pdf_name(config, candidate["upstream_version"], edition_date),
        **build["validation"],
    }
    state["last_change_report"] = report
    write_json(state_directory / STATE_FILE, state)
    create_archive(state_directory, Path(args.output_state).resolve())
    result = {
        "status": "updated",
        "upstream_version": candidate["upstream_version"],
        "edition_date": edition_date,
        "pdf_name": expected_pdf_name(
            config, candidate["upstream_version"], edition_date
        ),
        "pdf": str(output_pdf),
        "state_archive": str(Path(args.output_state).resolve()),
        "update": update_summary,
        "build": build,
    }
    _write_result(result, args.result)
    return 0


def command_publish(args: argparse.Namespace) -> int:
    config = _config(args.config)
    result = read_json(Path(args.update_result).resolve())
    version = result["upstream_version"]
    name = expected_pdf_name(config, version, result["edition_date"])
    repository = Path(args.repository).resolve()
    pdf_directory = repository / config["pdf_directory"]
    destination = replace_manual_pdf(pdf_directory, Path(result["pdf"]), name)
    relative_pdf = destination.relative_to(repository).as_posix()
    update_readme(
        repository / "README.md",
        rows=manual_catalog(repository),
    )
    policy = validate_manual_directory(pdf_directory, name)
    _write_result(
        {
            "status": "published_to_worktree",
            "upstream_version": version,
            "edition_date": result["edition_date"],
            "pdf_path": relative_pdf,
            "policy": policy,
        },
        args.result,
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    config = _config(args.config)
    repository = Path(args.repository).resolve()
    pdf_directory = repository / config["pdf_directory"]
    pdf_files = list(pdf_directory.glob("*.pdf"))
    if len(pdf_files) != 1:
        validate_manual_directory(pdf_directory, "<missing>")
    version, edition_date = parse_pdf_name(config, pdf_files[0].name)
    name = expected_pdf_name(config, version, edition_date)
    policy = validate_manual_directory(pdf_directory, name)
    font_directory = repository / ".github" / "assets" / "fonts"
    validation = validate_pdf(
        pdf_files[0],
        None,
        config,
        font_directory=font_directory,
    )
    _write_result({"status": "valid", "policy": policy, "pdf": validation}, args.result)
    return 0


def command_validate_state_pdf(args: argparse.Namespace) -> int:
    config = _config(args.config)
    pdf_path = Path(args.pdf).resolve()
    edition_date = args.edition_date
    with tempfile.TemporaryDirectory(prefix="manual-state-validate-") as temporary:
        state = unpack_state(
            Path(args.state_archive).resolve(), Path(temporary) / "state"
        )
        _validate_state_manual(state, config)
    version = state["snapshot"]["upstream_version"]
    expected_name = expected_pdf_name(config, version, edition_date)
    baseline = state.get("baseline_pdf", {})
    actual = pdf_metrics(
        pdf_path,
        expected_chapter_count=int(config["expected_chapter_count"]),
    )
    comparisons = {
        "filename": (pdf_path.name, expected_name),
        "state_filename": (baseline.get("name"), expected_name),
        "sha256": (baseline.get("sha256"), actual["sha256"]),
        "page_count": (baseline.get("page_count"), actual["page_count"]),
        "internal_link_count": (
            baseline.get("internal_link_count"),
            actual["internal_link_count"],
        ),
        "file_size": (baseline.get("file_size"), actual["file_size"]),
        "published_date": (state.get("last_published_at"), edition_date),
    }
    mismatches = {
        name: {"state": values[0], "merged_pdf": values[1]}
        for name, values in comparisons.items()
        if values[0] != values[1]
    }
    if mismatches:
        raise RuntimeError(
            "Candidate state does not match the merged PDF: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    _write_result(
        {
            "status": "matching",
            "pdf": actual,
            "source_hash": state["snapshot"]["content_hash"],
            "edition_date": edition_date,
        },
        args.result,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual update automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="Create a canonical upstream snapshot"
    )
    snapshot.add_argument("--config", required=True)
    snapshot.add_argument("--source")
    snapshot.add_argument("--output", required=True)
    snapshot.set_defaults(handler=command_snapshot)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Create the first external state archive"
    )
    bootstrap.add_argument("--config", required=True)
    bootstrap.add_argument("--source")
    bootstrap.add_argument("--localized-html", required=True)
    bootstrap.add_argument("--source-template-html", required=True)
    bootstrap.add_argument("--asset-directory", required=True)
    bootstrap.add_argument("--baseline-pdf", required=True)
    bootstrap.add_argument("--output", required=True)
    bootstrap.add_argument("--result")
    bootstrap.set_defaults(handler=command_bootstrap)

    check = subparsers.add_parser(
        "check", help="Compare upstream content with saved state"
    )
    check.add_argument("--config", required=True)
    check.add_argument("--state-archive", required=True)
    check.add_argument("--source")
    check.add_argument("--work-directory", required=True)
    check.add_argument("--snapshot-output", required=True)
    check.add_argument("--report", required=True)
    check.set_defaults(handler=command_check)

    update = subparsers.add_parser(
        "update", help="Translate, render, and validate a safe change"
    )
    update.add_argument("--config", required=True)
    update.add_argument("--state-archive", required=True)
    update.add_argument("--snapshot", required=True)
    update.add_argument("--report", required=True)
    update.add_argument("--translations", required=True)
    update.add_argument("--work-directory", required=True)
    update.add_argument("--output-pdf", required=True)
    update.add_argument("--output-state", required=True)
    update.add_argument("--edition-date")
    update.add_argument("--result", required=True)
    update.set_defaults(handler=command_update)

    publish = subparsers.add_parser(
        "publish", help="Replace the one published PDF and README row"
    )
    publish.add_argument("--config", required=True)
    publish.add_argument("--update-result", required=True)
    publish.add_argument("--repository", required=True)
    publish.add_argument("--result")
    publish.set_defaults(handler=command_publish)

    validate = subparsers.add_parser(
        "validate-repository", help="Enforce repository publication policy"
    )
    validate.add_argument("--config", required=True)
    validate.add_argument("--repository", required=True)
    validate.add_argument("--result")
    validate.set_defaults(handler=command_validate)

    validate_state_pdf = subparsers.add_parser(
        "validate-state-pdf", help="Bind a candidate state archive to its merged PDF"
    )
    validate_state_pdf.add_argument("--config", required=True)
    validate_state_pdf.add_argument("--state-archive", required=True)
    validate_state_pdf.add_argument("--pdf", required=True)
    validate_state_pdf.add_argument("--edition-date", required=True)
    validate_state_pdf.add_argument("--result")
    validate_state_pdf.set_defaults(handler=command_validate_state_pdf)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
