from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag


IGNORED_TAGS = {"script", "style", "noscript", "template"}
SEMANTIC_ATTRIBUTES = {
    "a": ("href",),
    "img": ("src", "alt"),
    "source": ("src", "srcset", "type"),
    "td": ("colspan", "rowspan"),
    "th": ("colspan", "rowspan", "scope"),
}
PRESENTATION_QUERY_KEYS = {
    "auto",
    "dpr",
    "fit",
    "fm",
    "h",
    "height",
    "q",
    "quality",
    "w",
    "width",
}


class SourceFormatError(RuntimeError):
    """Raised when the upstream page no longer has the expected manual structure."""


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(value: str, base_url: str = "") -> str:
    if not value:
        return ""
    absolute = urljoin(base_url, value)
    parsed = urlsplit(absolute)
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in PRESENTATION_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(query), parsed.fragment)
    )


def _semantic_children(node: Tag) -> list[Tag | NavigableString]:
    children: list[Tag | NavigableString] = []
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            if normalize_text(str(child)):
                children.append(child)
            continue
        if isinstance(child, Tag) and child.name not in IGNORED_TAGS:
            children.append(child)
    return children


def semantic_payload(element: Tag, base_url: str) -> dict[str, Any]:
    canonical_parts: list[str] = []
    skeleton: list[str] = []
    text_nodes: list[dict[str, str]] = []
    attributes: list[dict[str, str]] = []

    def walk(node: Tag, path: tuple[int, ...]) -> None:
        path_text = ".".join(map(str, path)) or "root"
        tag_name = node.name.lower()
        skeleton.append(f"{path_text}:{tag_name}")
        canonical_parts.append(f"<{tag_name}>")

        for attribute in SEMANTIC_ATTRIBUTES.get(tag_name, ()):
            raw_value = node.get(attribute)
            if raw_value is None:
                continue
            value = " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            if attribute in {"href", "src", "srcset"}:
                value = normalize_url(value, base_url)
            else:
                value = normalize_text(value)
            attributes.append(
                {"path": path_text, "tag": tag_name, "name": attribute, "value": value}
            )
            canonical_parts.append(f"@{attribute}={json.dumps(value, ensure_ascii=False)}")

        for index, child in enumerate(_semantic_children(node)):
            child_path = path + (index,)
            child_path_text = ".".join(map(str, child_path))
            if isinstance(child, NavigableString):
                text = normalize_text(str(child))
                text_nodes.append({"path": child_path_text, "text": text, "raw": str(child)})
                canonical_parts.append(json.dumps(text, ensure_ascii=False))
            else:
                walk(child, child_path)
        canonical_parts.append(f"</{tag_name}>")

    walk(element, ())
    canonical = "".join(canonical_parts)
    return {
        "canonical": canonical,
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "skeleton": skeleton,
        "text_nodes": text_nodes,
        "attributes": attributes,
    }


def _snapshot_content_hash(units: list[dict[str, Any]]) -> str:
    digest_input = "\n".join(f"{unit['key']}:{unit['hash']}" for unit in units)
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def _source_html_sha256(source_html: str) -> str:
    return hashlib.sha256(source_html.encode("utf-8")).hexdigest()


def _snapshot_integrity_hash(snapshot: dict[str, Any]) -> str:
    chapter_fields = ("number", "source_id", "title", "unit_key", "section_keys")
    unit_fields = (
        "key",
        "kind",
        "chapter",
        "source_id",
        "title",
        "hash",
        "source_html_sha256",
    )
    payload = {
        "source_url": snapshot.get("source_url"),
        "upstream_version": snapshot.get("upstream_version"),
        "page_title": snapshot.get("page_title"),
        "chapter_count": snapshot.get("chapter_count"),
        "section_count": snapshot.get("section_count"),
        "chapters": [
            {field: chapter.get(field) for field in chapter_fields}
            for chapter in snapshot.get("chapters", [])
        ],
        "units": [
            {field: unit.get(field) for field in unit_fields}
            for unit in snapshot.get("units", [])
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_source(source: str, timeout: int = 60) -> tuple[str, str]:
    candidate = Path(source)
    if candidate.exists():
        return candidate.read_text(encoding="utf-8"), candidate.resolve().as_uri()

    request_url = source.split("#", 1)[0]
    headers = {
        "User-Agent": "neuraldsp-manuals-ru/1.0 (+https://github.com/ialexbond/neuraldsp-manuals-ru)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(request_url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.text, response.url
        except requests.RequestException as exc:
            error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"Unable to download {request_url}: {error}") from error


def _chapter_number(heading: Tag, fallback: int) -> int:
    text = normalize_text(heading.get_text(" ", strip=True))
    match = re.match(r"^(?:0\s*)?(\d{1,2})\b", text)
    return int(match.group(1)) if match else fallback


def _chapter_title(heading: Tag) -> str:
    text = normalize_text(heading.get_text(" ", strip=True))
    return re.sub(r"^(?:0\s*)?\d{1,2}\s*", "", text).strip()


def _find_main_sections(article: Tag) -> list[Tag]:
    sections: list[Tag] = []
    for candidate in article.find_all("div", id=True):
        if candidate.find_parent("article") is not article:
            continue
        heading = candidate.find(["h3", "h2"])
        if heading is None:
            continue
        parent = candidate.parent
        nested_below_another_stable_id = False
        while isinstance(parent, Tag) and parent is not article:
            if parent.get("id"):
                nested_below_another_stable_id = True
                break
            parent = parent.parent
        if nested_below_another_stable_id:
            continue
        sections.append(candidate)
    return sections


def extract_snapshot(source: str, expected_chapters: int | None = None) -> dict[str, Any]:
    html, resolved_url = _load_source(source)
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main", id="main-content") or soup.find("main")
    if main is None:
        raise SourceFormatError("The upstream page no longer contains the manual's main element.")

    articles = main.find_all("article")
    if expected_chapters is not None and len(articles) != expected_chapters:
        raise SourceFormatError(
            f"Expected {expected_chapters} chapters, but the upstream page contains {len(articles)}."
        )

    page_title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    version_match = re.search(r"(?:Manual\s+|CorOS\s+)(\d+\.\d+\.\d+)", page_title, re.I)
    if version_match is None:
        manual_heading = main.find("h1")
        heading_text = normalize_text(manual_heading.get_text(" ", strip=True)) if manual_heading else ""
        version_match = re.search(r"(\d+\.\d+\.\d+)", heading_text)
    version = version_match.group(1) if version_match else "unknown"

    units: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for fallback_number, article in enumerate(articles, start=1):
        heading = article.find("h2", id=True)
        if heading is None:
            raise SourceFormatError(f"Chapter {fallback_number} has no stable h2 identifier.")
        number = _chapter_number(heading, fallback_number)
        source_id = str(heading.get("id"))
        chapter_key = f"chapter:{source_id}"
        chapter_element = heading.parent if isinstance(heading.parent, Tag) else heading
        payload = semantic_payload(chapter_element, resolved_url)
        source_html = str(chapter_element)
        unit = {
            "key": chapter_key,
            "kind": "chapter",
            "chapter": number,
            "source_id": source_id,
            "title": _chapter_title(heading),
            "source_html": source_html,
            "source_html_sha256": _source_html_sha256(source_html),
            **payload,
        }
        units.append(unit)
        seen_keys.add(chapter_key)

        section_keys: list[str] = []
        for section in _find_main_sections(article):
            section_id = str(section.get("id"))
            key = f"section:{section_id}"
            if key in seen_keys:
                raise SourceFormatError(f"Duplicate stable section identifier: {section_id}")
            section_heading = section.find(["h3", "h2"])
            payload = semantic_payload(section, resolved_url)
            source_html = str(section)
            units.append(
                {
                    "key": key,
                    "kind": "section",
                    "chapter": number,
                    "source_id": section_id,
                    "title": normalize_text(section_heading.get_text(" ", strip=True)),
                    "source_html": source_html,
                    "source_html_sha256": _source_html_sha256(source_html),
                    **payload,
                }
            )
            seen_keys.add(key)
            section_keys.append(key)

        chapters.append(
            {
                "number": number,
                "source_id": source_id,
                "title": _chapter_title(heading),
                "unit_key": chapter_key,
                "section_keys": section_keys,
            }
        )

    snapshot = {
        "schema_version": 1,
        "source_url": resolved_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "upstream_version": version,
        "page_title": page_title,
        "chapter_count": len(chapters),
        "section_count": sum(len(chapter["section_keys"]) for chapter in chapters),
        "content_hash": _snapshot_content_hash(units),
        "chapters": chapters,
        "units": units,
    }
    snapshot["integrity_hash"] = _snapshot_integrity_hash(snapshot)
    return snapshot


def validate_snapshot_integrity(
    snapshot: dict[str, Any], *, label: str = "Snapshot"
) -> None:
    def fail(message: str) -> None:
        raise RuntimeError(f"{label} snapshot integrity check failed: {message}")

    source_url = snapshot.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        fail("source_url must be a non-empty string")

    units = snapshot.get("units")
    if not isinstance(units, list):
        fail("units must be a list")

    verified_units: list[dict[str, Any]] = []
    units_by_key: dict[str, dict[str, Any]] = {}
    text_fields = ("path", "text", "raw")
    attribute_fields = ("path", "tag", "name", "value")

    def base_rows(
        value: Any, fields: tuple[str, ...], unit_key: str, field_name: str
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            fail(f"{unit_key} {field_name} must be a list")
        result: list[dict[str, Any]] = []
        allowed = {*fields, "locator"}
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                fail(f"{unit_key} {field_name}[{index}] must be an object")
            missing = [field for field in fields if field not in row]
            unexpected = sorted(set(row) - allowed)
            if missing or unexpected:
                fail(
                    f"{unit_key} {field_name}[{index}] fields are invalid; "
                    f"missing={missing}, unexpected={unexpected}"
                )
            if "locator" in row and not isinstance(row["locator"], dict):
                fail(f"{unit_key} {field_name}[{index}] locator must be an object")
            result.append({field: row[field] for field in fields})
        return result

    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            fail(f"units[{index}] must be an object")
        key = unit.get("key")
        if not isinstance(key, str) or not key:
            fail(f"units[{index}] has no valid key")
        if key in units_by_key:
            fail(f"duplicate unit key: {key}")

        kind = unit.get("kind")
        source_id = unit.get("source_id")
        if kind not in {"chapter", "section"} or not isinstance(source_id, str):
            fail(f"{key} has invalid kind or source_id")
        if key != f"{kind}:{source_id}":
            fail(f"{key} does not match its kind and source_id")
        chapter = unit.get("chapter")
        if not isinstance(chapter, int) or isinstance(chapter, bool):
            fail(f"{key} has an invalid chapter number")

        source_html = unit.get("source_html")
        if not isinstance(source_html, str) or not source_html:
            fail(f"{key} has no valid source_html")
        if unit.get("source_html_sha256") != _source_html_sha256(source_html):
            fail(f"{key} source_html_sha256 does not match source_html")
        fragment = BeautifulSoup(source_html, "html.parser")
        roots = [child for child in fragment.contents if isinstance(child, Tag)]
        if len(roots) != 1:
            fail(f"{key} source_html must contain exactly one root element")
        recalculated = semantic_payload(roots[0], source_url)

        for field in ("canonical", "hash", "skeleton"):
            if unit.get(field) != recalculated[field]:
                fail(f"{key} {field} does not match source_html")
        if base_rows(unit.get("text_nodes"), text_fields, key, "text_nodes") != (
            recalculated["text_nodes"]
        ):
            fail(f"{key} text_nodes do not match source_html")
        if base_rows(
            unit.get("attributes"), attribute_fields, key, "attributes"
        ) != recalculated["attributes"]:
            fail(f"{key} attributes do not match source_html")

        units_by_key[key] = unit
        verified_units.append({"key": key, "hash": recalculated["hash"]})

    chapters = snapshot.get("chapters")
    if not isinstance(chapters, list):
        fail("chapters must be a list")
    if snapshot.get("chapter_count") != len(chapters):
        fail("chapter_count does not match chapters")

    chapter_unit_keys = {
        key for key, unit in units_by_key.items() if unit["kind"] == "chapter"
    }
    section_unit_keys = {
        key for key, unit in units_by_key.items() if unit["kind"] == "section"
    }
    if snapshot.get("section_count") != len(section_unit_keys):
        fail("section_count does not match section units")

    referenced_chapters: set[str] = set()
    referenced_sections: set[str] = set()
    chapter_numbers: set[int] = set()
    chapter_source_ids: set[str] = set()
    for index, chapter_row in enumerate(chapters):
        if not isinstance(chapter_row, dict):
            fail(f"chapters[{index}] must be an object")
        number = chapter_row.get("number")
        source_id = chapter_row.get("source_id")
        unit_key = chapter_row.get("unit_key")
        section_keys = chapter_row.get("section_keys")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(source_id, str)
            or not isinstance(unit_key, str)
            or not isinstance(section_keys, list)
            or not all(isinstance(key, str) for key in section_keys)
        ):
            fail(f"chapters[{index}] has invalid fields")
        if number in chapter_numbers or source_id in chapter_source_ids:
            fail(f"chapters[{index}] duplicates a chapter identity")
        if unit_key in referenced_chapters:
            fail(f"chapters[{index}] duplicates unit_key {unit_key}")
        chapter_unit = units_by_key.get(unit_key)
        if (
            chapter_unit is None
            or chapter_unit["kind"] != "chapter"
            or chapter_unit["chapter"] != number
            or chapter_unit["source_id"] != source_id
        ):
            fail(f"chapters[{index}] does not match chapter unit {unit_key}")
        if chapter_row.get("title") != chapter_unit.get("title"):
            fail(f"chapters[{index}] title does not match chapter unit {unit_key}")

        chapter_numbers.add(number)
        chapter_source_ids.add(source_id)
        referenced_chapters.add(unit_key)
        for section_key in section_keys:
            if section_key in referenced_sections:
                fail(f"section unit is referenced more than once: {section_key}")
            section_unit = units_by_key.get(section_key)
            if (
                section_unit is None
                or section_unit["kind"] != "section"
                or section_unit["chapter"] != number
            ):
                fail(f"chapters[{index}] does not match section unit {section_key}")
            referenced_sections.add(section_key)

    if referenced_chapters != chapter_unit_keys:
        fail("chapters do not reference every chapter unit exactly once")
    if referenced_sections != section_unit_keys:
        fail("chapters do not reference every section unit exactly once")

    recalculated_content_hash = _snapshot_content_hash(verified_units)
    if snapshot.get("content_hash") != recalculated_content_hash:
        fail("content_hash does not match verified units")
    if snapshot.get("integrity_hash") != _snapshot_integrity_hash(snapshot):
        fail("integrity_hash does not match snapshot identity")


def unit_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["key"]: unit for unit in snapshot["units"]}
