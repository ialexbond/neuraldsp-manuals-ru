from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
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


def iter_semantic_nodes(
    root: Tag,
) -> Iterator[tuple[tuple[int, ...], Tag | NavigableString]]:
    def visit(node: Tag, path: tuple[int, ...]) -> Iterator[tuple[tuple[int, ...], Tag | NavigableString]]:
        yield path, node
        for index, child in enumerate(_semantic_children(node)):
            child_path = path + (index,)
            yield child_path, child
            if isinstance(child, Tag):
                yield from visit_children(child, child_path)

    def visit_children(
        node: Tag, path: tuple[int, ...]
    ) -> Iterator[tuple[tuple[int, ...], Tag | NavigableString]]:
        for index, child in enumerate(_semantic_children(node)):
            child_path = path + (index,)
            yield child_path, child
            if isinstance(child, Tag):
                yield from visit_children(child, child_path)

    yield from visit(root, ())


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
        heading = candidate.find("h3")
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
        unit = {
            "key": chapter_key,
            "kind": "chapter",
            "chapter": number,
            "source_id": source_id,
            "title": _chapter_title(heading),
            "source_html": str(chapter_element),
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
            section_heading = section.find("h3")
            payload = semantic_payload(section, resolved_url)
            units.append(
                {
                    "key": key,
                    "kind": "section",
                    "chapter": number,
                    "source_id": section_id,
                    "title": normalize_text(section_heading.get_text(" ", strip=True)),
                    "source_html": str(section),
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

    digest_input = "\n".join(f"{unit['key']}:{unit['hash']}" for unit in units)
    return {
        "schema_version": 1,
        "source_url": resolved_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "upstream_version": version,
        "page_title": page_title,
        "chapter_count": len(chapters),
        "section_count": sum(len(chapter["section_keys"]) for chapter in chapters),
        "content_hash": hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        "chapters": chapters,
        "units": units,
    }


def unit_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["key"]: unit for unit in snapshot["units"]}
