from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any


STATE_FILE = "state.json"
DOCUMENT_FILE = "web/document.html"
ASSET_DIRECTORY = "assets"


class StateError(RuntimeError):
    """Raised when an automation state archive is absent, unsafe, or incomplete."""


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if destination != target and destination not in target.parents:
                raise StateError(f"Unsafe path in state archive: {member.filename}")
        bundle.extractall(destination)


def validate_state_directory(directory: Path) -> dict[str, Any]:
    state_path = directory / STATE_FILE
    document_path = directory / DOCUMENT_FILE
    asset_path = directory / ASSET_DIRECTORY
    missing = [
        str(path.relative_to(directory))
        for path in (state_path, document_path, asset_path)
        if not path.exists()
    ]
    if missing:
        raise StateError("State archive is incomplete: " + ", ".join(missing))
    state = read_json(state_path)
    if state.get("schema_version") != 1 or "snapshot" not in state:
        raise StateError("Unsupported or invalid automation state schema.")
    return state


def unpack_state(archive: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    safe_extract(archive, destination)
    return validate_state_directory(destination)


def create_archive(source_directory: Path, destination: Path) -> None:
    validate_state_directory(source_directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=destination.stem + "-", suffix=".zip", dir=destination.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in sorted(source_directory.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(source_directory).as_posix())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_state_directory(
    destination: Path,
    manual_slug: str,
    snapshot: dict[str, Any],
    localized_html: Path,
    asset_directory: Path,
    baseline_pdf: Path,
    baseline_metrics: dict[str, Any],
) -> dict[str, Any]:
    if destination.exists():
        shutil.rmtree(destination)
    (destination / "web").mkdir(parents=True)
    shutil.copy2(localized_html, destination / DOCUMENT_FILE)
    shutil.copytree(asset_directory, destination / ASSET_DIRECTORY)
    edition_match = re.search(r"_rev(\d{4}-\d{2}-\d{2})\.pdf$", baseline_pdf.name)
    state = {
        "schema_version": 1,
        "manual": manual_slug,
        "snapshot": snapshot,
        "localized_document": DOCUMENT_FILE,
        "asset_directory": ASSET_DIRECTORY,
        "baseline_pdf": {
            "name": baseline_pdf.name,
            **baseline_metrics,
        },
        "last_published_at": (
            edition_match.group(1) if edition_match else snapshot["fetched_at"]
        ),
    }
    write_json(destination / STATE_FILE, state)
    return state
