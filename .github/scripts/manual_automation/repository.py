from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


STATUS_START = "<!-- MANUAL_STATUS:START -->"
STATUS_END = "<!-- MANUAL_STATUS:END -->"


class RepositoryPolicyError(RuntimeError):
    """Raised when generated repository contents violate publication policy."""


def expected_pdf_name(config: dict[str, Any], version: str, edition_date: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RepositoryPolicyError(f"Invalid upstream version: {version}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", edition_date):
        raise RepositoryPolicyError(f"Invalid Russian edition date: {edition_date}")
    return config["pdf_name_template"].format(version=version, date=edition_date)


def replace_manual_pdf(
    pdf_directory: Path, generated_pdf: Path, expected_name: str
) -> Path:
    if not generated_pdf.is_file() or generated_pdf.stat().st_size == 0:
        raise RepositoryPolicyError(
            f"Generated PDF is missing or empty: {generated_pdf}"
        )
    pdf_directory.mkdir(parents=True, exist_ok=True)
    destination = pdf_directory / expected_name
    with tempfile.NamedTemporaryFile(
        prefix=expected_name + ".", suffix=".tmp", dir=pdf_directory, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(generated_pdf, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    for existing in pdf_directory.glob("*.pdf"):
        if existing.resolve() != destination.resolve():
            existing.unlink()
    return destination


def validate_manual_directory(
    pdf_directory: Path, expected_name: str
) -> dict[str, Any]:
    files = sorted(path for path in pdf_directory.rglob("*") if path.is_file())
    pdf_files = [path for path in files if path.suffix.lower() == ".pdf"]
    non_pdf_files = [path for path in files if path.suffix.lower() != ".pdf"]
    errors: list[str] = []
    if len(pdf_files) != 1:
        errors.append(f"expected exactly one PDF, found {len(pdf_files)}")
    elif pdf_files[0].name != expected_name:
        errors.append(f"expected {expected_name}, found {pdf_files[0].name}")
    if non_pdf_files:
        errors.append(
            "non-PDF files are not allowed in the manual directory: "
            + ", ".join(path.name for path in non_pdf_files)
        )
    if errors:
        raise RepositoryPolicyError("; ".join(errors))
    return {
        "pdf": pdf_files[0].as_posix(),
        "file_count": len(files),
        "valid": True,
    }


def update_readme(
    readme_path: Path,
    *,
    version: str,
    edition_date: str,
    pdf_path: str,
    source_url: str,
) -> None:
    content = readme_path.read_text(encoding="utf-8")
    if content.count(STATUS_START) != 1 or content.count(STATUS_END) != 1:
        raise RepositoryPolicyError("README status markers are missing or duplicated.")
    block = "\n".join(
        [
            STATUS_START,
            "| Product | Upstream manual | Russian edition | Status | Download |",
            "| --- | --- | --- | --- | --- |",
            (
                f"| Quad Cortex | [{version}]({source_url}) | {edition_date} | "
                f"Current after automated validation | [PDF]({pdf_path}) |"
            ),
            STATUS_END,
        ]
    )
    pattern = re.compile(
        re.escape(STATUS_START) + r".*?" + re.escape(STATUS_END), re.DOTALL
    )
    readme_path.write_text(pattern.sub(block, content), encoding="utf-8")
