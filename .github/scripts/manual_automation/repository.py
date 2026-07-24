from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


STATUS_START = "<!-- MANUAL_STATUS:START -->"
STATUS_END = "<!-- MANUAL_STATUS:END -->"
PUBLIC_RELEASES_URL = (
    "https://github.com/ialexbond/neuraldsp-manuals-ru/releases"
)


class RepositoryPolicyError(RuntimeError):
    """Raised when generated repository contents violate publication policy."""


def expected_pdf_name(config: dict[str, Any], version: str, edition_date: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RepositoryPolicyError(f"Invalid upstream version: {version}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", edition_date):
        raise RepositoryPolicyError(f"Invalid Russian edition date: {edition_date}")
    return config["pdf_name_template"].format(version=version, date=edition_date)


def parse_pdf_name(config: dict[str, Any], filename: str) -> tuple[str, str]:
    pattern = re.escape(config["pdf_name_template"])
    pattern = pattern.replace(
        re.escape("{version}"), r"(?P<version>\d+\.\d+\.\d+)"
    )
    pattern = pattern.replace(
        re.escape("{date}"), r"(?P<date>\d{4}-\d{2}-\d{2})"
    )
    match = re.fullmatch(pattern, filename)
    if not match:
        raise RepositoryPolicyError(
            f"Published PDF filename does not match the configured format: {filename}"
        )
    return match.group("version"), match.group("date")


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


def manual_catalog(repository: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    config_directory = repository / ".github" / "manuals"
    for config_path in sorted(config_directory.glob("*.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        pdf_directory = repository / config["pdf_directory"]
        pdf_files = sorted(pdf_directory.glob("*.pdf"))
        if not pdf_files:
            continue
        if len(pdf_files) != 1:
            raise RepositoryPolicyError(
                f"Expected one published PDF for {config['display_name']}, "
                f"found {len(pdf_files)}."
            )
        version, edition_date = parse_pdf_name(config, pdf_files[0].name)
        year, month, day = edition_date.split("-")
        release_tag = config["release_tag_template"].format(
            version=version,
            date=edition_date,
        )
        rows.append(
            {
                "category": config["category"],
                "display_name": config["display_name"],
                "version": version,
                "edition_date": f"{day}.{month}.{year}",
                "source_url": config["source_url"],
                "pdf_path": pdf_files[0].relative_to(repository).as_posix(),
                "release_url": f"{PUBLIC_RELEASES_URL}/tag/{release_tag}",
            }
        )
    return sorted(rows, key=lambda item: item["display_name"].casefold())


def update_readme(
    readme_path: Path,
    *,
    rows: list[dict[str, str]],
) -> None:
    content = readme_path.read_text(encoding="utf-8")
    if content.count(STATUS_START) != 1 or content.count(STATUS_END) != 1:
        raise RepositoryPolicyError(
            "Служебные маркеры таблицы статуса в README отсутствуют или дублируются."
        )
    if content.index(STATUS_START) > content.index(STATUS_END):
        raise RepositoryPolicyError(
            "Служебные маркеры таблицы статуса в README расположены в неверном порядке."
        )
    if not rows:
        raise RepositoryPolicyError("The manual catalog cannot be empty.")
    lines = [
        STATUS_START,
        "| Категория | Продукт | Версия оригинала | Русская редакция | Статус | Релиз |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item["display_name"].casefold()):
        lines.append(
            f"| {row['category']} | {row['display_name']} | "
            f"[{row['version']}]({row['source_url']}) | {row['edition_date']} | "
            f"Опубликовано | [Открыть релиз]({row['release_url']}) |"
        )
    lines.append(STATUS_END)
    block = "\n".join(lines)
    pattern = re.compile(
        re.escape(STATUS_START) + r".*?" + re.escape(STATUS_END), re.DOTALL
    )
    readme_path.write_text(pattern.sub(block, content), encoding="utf-8")
