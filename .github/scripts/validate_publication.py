from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


class PublicationValidationError(RuntimeError):
    pass


def _manual_pdf(value: str) -> PurePosixPath | None:
    path = PurePosixPath(value)
    if (
        path.suffix.casefold() != ".pdf"
        or len(path.parts) < 3
        or path.parts[0] != "manuals"
        or ".." in path.parts
    ):
        return None
    return path


def validate_manual_pdf_changes(
    entries: list[tuple[str, ...]],
) -> list[PurePosixPath]:
    candidates: list[PurePosixPath] = []
    removed: list[PurePosixPath] = []
    for entry in entries:
        if not entry:
            continue
        status = entry[0]
        if status.startswith("R") and len(entry) == 3:
            previous = _manual_pdf(entry[1])
            current = _manual_pdf(entry[2])
            if previous is not None:
                removed.append(previous)
            if current is not None:
                candidates.append(current)
            continue
        if len(entry) != 2:
            raise PublicationValidationError(
                f"Unexpected git change entry: {entry!r}"
            )
        path = _manual_pdf(entry[1])
        if path is None:
            continue
        if status == "D":
            removed.append(path)
        elif status in {"A", "M", "T"}:
            candidates.append(path)

    if len(candidates) > 1:
        raise PublicationValidationError(
            "Each pull request may publish only one manual PDF; found: "
            + ", ".join(map(str, candidates))
        )
    if removed and not candidates:
        raise PublicationValidationError(
            "A published manual PDF cannot be removed without a replacement."
        )
    if candidates:
        selected_directory = candidates[0].parent
        outside = [path for path in removed if path.parent != selected_directory]
        if outside:
            raise PublicationValidationError(
                "A replacement pull request may remove PDFs only from its selected "
                "manual directory: "
                + ", ".join(map(str, outside))
            )
    return candidates


def validate_configuration_coverage(repository: Path) -> dict[str, int]:
    manual_root = repository / "manuals"
    config_root = repository / ".github" / "manuals"
    manual_directories = {
        directory.relative_to(repository).as_posix()
        for directory in manual_root.iterdir()
        if directory.is_dir() and any(path.is_file() for path in directory.rglob("*"))
    }

    configured: dict[str, list[Path]] = {}
    for config_path in sorted(config_root.glob("*.json")):
        try:
            config: Any = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationValidationError(
                f"Unable to read manual configuration {config_path}: {exc}"
            ) from exc
        if not isinstance(config, dict) or not isinstance(
            config.get("pdf_directory"), str
        ):
            raise PublicationValidationError(
                f"Manual configuration has no valid pdf_directory: {config_path}"
            )
        directory = PurePosixPath(config["pdf_directory"])
        if (
            len(directory.parts) != 2
            or directory.parts[0] != "manuals"
            or ".." in directory.parts
        ):
            raise PublicationValidationError(
                f"Manual configuration points outside a product directory: {config_path}"
            )
        configured.setdefault(directory.as_posix(), []).append(config_path)

    duplicates = {
        directory: paths for directory, paths in configured.items() if len(paths) != 1
    }
    missing = sorted(manual_directories - configured.keys())
    stale = sorted(configured.keys() - manual_directories)
    if duplicates or missing or stale:
        details = []
        if missing:
            details.append("missing configurations: " + ", ".join(missing))
        if stale:
            details.append("configurations without a manual: " + ", ".join(stale))
        if duplicates:
            details.append(
                "duplicate configurations: "
                + ", ".join(
                    f"{directory} ({len(paths)})"
                    for directory, paths in sorted(duplicates.items())
                )
            )
        raise PublicationValidationError("; ".join(details))

    return {
        "manual_directory_count": len(manual_directories),
        "configuration_count": len(configured),
    }


def _git_entries(repository: Path, base: str, head: str) -> list[tuple[str, ...]]:
    output = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            base,
            head,
            "--",
            "manuals",
        ],
        cwd=repository,
        text=True,
        encoding="utf-8",
    )
    return [
        tuple(part for part in line.split("\t") if part)
        for line in output.splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()

    repository = Path(args.repository).resolve()
    candidates = validate_manual_pdf_changes(
        _git_entries(repository, args.base, args.head)
    )
    coverage = validate_configuration_coverage(repository)
    result = {
        **coverage,
        "changed_manual_pdfs": [path.as_posix() for path in candidates],
        "status": "valid",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
