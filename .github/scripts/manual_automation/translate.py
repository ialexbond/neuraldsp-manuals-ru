from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from .canonical import _semantic_children, normalize_text, unit_map


class TranslationError(RuntimeError):
    """Raised when a changed source unit cannot be translated safely."""


PROTECTED_TERMS = (
    "Quad Cortex",
    "Neural Capture",
    "Cortex Cloud",
    "Cortex Control",
    "CorOS",
    "MIDI",
    "USB",
    "XLR",
    "TRS",
    "CPU",
    "I/O",
    "CC#",
)


def _resolve_path(root: Tag, path_text: str) -> Tag | NavigableString:
    if path_text in {"", "root"}:
        return root
    current: Tag | NavigableString = root
    for raw_index in path_text.split("."):
        if not isinstance(current, Tag):
            raise TranslationError(f"Semantic path crosses a text node: {path_text}")
        children = _semantic_children(current)
        index = int(raw_index)
        if index >= len(children):
            raise TranslationError(
                f"Semantic path is absent from localized HTML: {path_text}"
            )
        current = children[index]
    return current


def _extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for output in response.get("output", []):
        for content in output.get("content", []):
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)


def _parse_json_response(text: str) -> dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslationError(
            "The translation model did not return valid JSON."
        ) from exc
    rows = value.get("translations") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise TranslationError("The translation response has no translations array.")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise TranslationError("The translation response contains an invalid item.")
        translated = row.get("translation")
        if not isinstance(translated, str) or not normalize_text(translated):
            raise TranslationError(f"Translation {row['id']} is empty or invalid.")
        if row["id"] in result:
            raise TranslationError(f"Translation id is duplicated: {row['id']}")
        result[row["id"]] = translated
    return result


def _plain_text(value: str) -> str:
    return normalize_text(BeautifulSoup(value, "html.parser").get_text(" "))


def _validate_translated_items(
    items: list[dict[str, str]], translated: dict[str, str]
) -> None:
    for item in items:
        item_id = item["id"]
        source_text = _plain_text(item["source"])
        translated_value = translated[item_id]
        translated_text = _plain_text(translated_value)
        source_numbers = Counter(re.findall(r"\d+", source_text))
        translated_numbers = Counter(re.findall(r"\d+", translated_text))
        if source_numbers != translated_numbers:
            raise TranslationError(
                f"Translation {item_id} changed numeric tokens: "
                f"source={dict(source_numbers)}, translation={dict(translated_numbers)}"
            )
        missing_terms = [
            term
            for term in PROTECTED_TERMS
            if term.casefold() in source_text.casefold()
            and term.casefold() not in translated_text.casefold()
        ]
        if missing_terms:
            raise TranslationError(
                f"Translation {item_id} changed protected terms: {', '.join(missing_terms)}"
            )
        if (
            not item_id.startswith("html:")
            and BeautifulSoup(translated_value, "html.parser").find()
        ):
            raise TranslationError(
                f"Translation {item_id} introduced unexpected HTML markup."
            )


def translate_items(
    items: list[dict[str, str]],
    *,
    section_title: str,
    api_key: str,
    model: str,
    endpoint: str = "https://api.openai.com/v1/responses",
) -> dict[str, str]:
    if not items:
        return {}
    if not api_key:
        raise TranslationError("OPENAI_API_KEY is required for a changed manual.")
    instructions = (
        "You are localizing an official Neural DSP Quad Cortex user manual into natural, "
        "technically accurate Russian. Translate only the supplied changed fragments. Fragments "
        "may contain inline HTML. Use previous_translation as the markup template and preserve "
        "every one of its HTML tags and attributes while updating its Russian text. "
        "Use the surrounding section and previous Russian wording as context. Preserve product "
        "names, UI labels written in uppercase, MIDI notation, units, numbers, email addresses, "
        "and URLs. Prefer established Russian music-equipment terminology over literal wording. "
        "Do not add explanations. Return strict JSON with the shape "
        '{"translations":[{"id":"...","translation":"..."}]} and every input id exactly once.'
    )
    payload = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(
            {"section": section_title, "fragments": items}, ensure_ascii=False
        ),
    }
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    if response.status_code >= 400:
        raise TranslationError(
            f"OpenAI API returned HTTP {response.status_code}: {response.text[:500]}"
        )
    translated = _parse_json_response(_extract_output_text(response.json()))
    expected = {item["id"] for item in items}
    if set(translated) != expected:
        missing = sorted(expected - set(translated))
        extra = sorted(set(translated) - expected)
        raise TranslationError(
            f"Translation ids do not match. Missing={missing}; extra={extra}"
        )
    _validate_translated_items(items, translated)
    return translated


def _localized_unit_element(document: BeautifulSoup, unit: dict[str, Any]) -> Tag:
    if unit["kind"] == "section":
        element = document.find(id=unit["source_id"])
    else:
        source_id = f"ch{int(unit['chapter']):02d}-0001"
        heading = document.find(attrs={"data-source-id": source_id})
        element = (
            heading.parent
            if isinstance(heading, Tag) and isinstance(heading.parent, Tag)
            else None
        )
    if not isinstance(element, Tag):
        raise TranslationError(f"Localized element is missing for {unit['key']}")
    return element


def _resolve_localized_row(
    document: BeautifulSoup,
    localized_element: Tag,
    row: dict[str, Any],
) -> Tag | NavigableString:
    locator = row.get("locator")
    if not isinstance(locator, dict):
        return _resolve_path(localized_element, row["path"])
    anchor = document.find(attrs={locator["attribute"]: locator["value"]})
    if not isinstance(anchor, Tag):
        raise TranslationError(
            f"Localized anchor is missing: {locator['attribute']}={locator['value']}"
        )
    ordinal = int(locator.get("target_ordinal", -1))
    if ordinal == -1:
        return anchor
    matches = anchor.find_all(locator["target_tag"])
    if ordinal >= len(matches):
        raise TranslationError(f"Localized attribute target is missing: {locator}")
    return matches[ordinal]


def _inner_html(element: Tag) -> str:
    return "".join(str(child) for child in element.contents)


def _replace_inner_html(element: Tag, translated_html: str, expected_html: str) -> None:
    translated_fragment = BeautifulSoup(translated_html, "html.parser")
    source_fragment = BeautifulSoup(expected_html, "html.parser")

    def signature(node: Tag) -> tuple[object, ...]:
        children: list[object] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                if normalize_text(str(child)):
                    children.append("#text")
            elif isinstance(child, Tag):
                children.append(signature(child))
        attributes = tuple(
            sorted((name, str(value)) for name, value in node.attrs.items())
        )
        return node.name, attributes, tuple(children)

    source_signature = tuple(
        signature(tag) if isinstance(tag, Tag) else "#text"
        for tag in source_fragment.contents
        if isinstance(tag, Tag) or normalize_text(str(tag))
    )
    translated_signature = tuple(
        signature(tag) if isinstance(tag, Tag) else "#text"
        for tag in translated_fragment.contents
        if isinstance(tag, Tag) or normalize_text(str(tag))
    )
    if source_signature != translated_signature:
        raise TranslationError(
            "Translated HTML changed inline markup structure or attributes."
        )
    element.clear()
    for child in list(translated_fragment.contents):
        element.append(child)


def _source_element(unit: dict[str, Any]) -> Tag:
    fragment = BeautifulSoup(unit["source_html"], "html.parser")
    element = fragment.find()
    if not isinstance(element, Tag):
        raise TranslationError(f"Stored source HTML is invalid for {unit['key']}")
    return element


def _with_source_whitespace(source_raw: str, translated: str) -> str:
    leading = re.match(r"^\s*", source_raw).group(0)
    trailing = re.search(r"\s*$", source_raw).group(0)
    return f"{leading}{translated.strip()}{trailing}"


def _extension_for_asset(url: str, content_type: str) -> str:
    path_suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", path_suffix):
        return path_suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return guessed or ".bin"


def _download_asset(url: str, asset_directory: Path) -> str:
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    extension = _extension_for_asset(url, response.headers.get("content-type", ""))
    relative = Path("automation") / f"{digest[:20]}{extension}"
    target = asset_directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(response.content)
    return "../assets/" + relative.as_posix()


def _changed_attributes(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[tuple[dict[str, str] | None, dict[str, str] | None]]:
    def key(row: dict[str, str]) -> tuple[str, str, str]:
        return row["path"], row["tag"], row["name"]

    old = {key(row): row for row in baseline["attributes"]}
    new = {key(row): row for row in candidate["attributes"]}
    return [
        (old.get(item), new.get(item))
        for item in sorted(set(old) | set(new))
        if old.get(item) != new.get(item)
    ]


def _refresh_cover_metadata(
    document: BeautifulSoup, version: str, edition_date: str
) -> None:
    date_display = edition_date.split("T", 1)[0]
    year, month, day = date_display.split("-")
    localized_date = f"{day}.{month}.{year}"
    cover = document.find(attrs={"data-source-id": "cover-0001"})
    if isinstance(cover, Tag):
        cover.string = re.sub(r"\d+\.\d+\.\d+", version, cover.get_text())
    edition = document.select_one(".manual-toc-edition")
    if isinstance(edition, Tag):
        edition.string = f"Русская редакция — {localized_date}"
    for span in document.select(".manual-toc-continuation-meta span"):
        if "Редакция" in span.get_text():
            span.string = f"Редакция {localized_date}"


def _refresh_toc_labels(document: BeautifulSoup) -> None:
    for row in document.select("a.manual-toc-row[href^='#']"):
        target_id = row.get("href", "")[1:]
        target = document.find(id=target_id)
        label = row.select_one(".manual-toc-label")
        if not isinstance(target, Tag) or not isinstance(label, Tag):
            continue
        title = normalize_text(target.get_text(" ", strip=True))
        if "manual-toc-row-chapter" in row.get("class", []):
            title = re.sub(r"^\d{1,2}\s+", "", title)
        label.string = title


def apply_safe_update(
    *,
    state: dict[str, Any],
    candidate_snapshot: dict[str, Any],
    changed_keys: list[str],
    document_path: Path,
    asset_directory: Path,
    api_key: str | None = None,
    model: str | None = None,
    translator=None,
    edition_date: str,
) -> dict[str, Any]:
    document = BeautifulSoup(document_path.read_text(encoding="utf-8"), "html.parser")
    baseline_units = unit_map(state["snapshot"])
    candidate_units = unit_map(candidate_snapshot)
    translation_count = 0

    for key in changed_keys:
        baseline = baseline_units[key]
        candidate = candidate_units[key]
        if baseline["skeleton"] != candidate["skeleton"]:
            raise TranslationError(f"Refusing to merge a structural change in {key}")
        localized_element = _localized_unit_element(document, baseline)
        baseline_source = _source_element(baseline)
        candidate_source = _source_element(candidate)
        old_text = {row["path"]: row for row in baseline["text_nodes"]}
        new_text = {row["path"]: row for row in candidate["text_nodes"]}
        items: list[dict[str, str]] = []
        item_paths: dict[str, tuple[str, Any]] = {}
        changed_rows = [
            old_text[path]
            for path in sorted(old_text.keys() & new_text.keys())
            if old_text[path]["text"] != new_text[path]["text"]
        ]
        locator_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        legacy_rows: list[dict[str, str]] = []
        for row in changed_rows:
            locator = row.get("locator")
            if not isinstance(locator, dict):
                legacy_rows.append(row)
                continue
            group_key = (
                locator["attribute"],
                locator["value"],
                locator["source_anchor_path"],
            )
            locator_groups[group_key] = locator

        for locator in locator_groups.values():
            baseline_anchor = _resolve_path(
                baseline_source, locator["source_anchor_path"]
            )
            candidate_anchor = _resolve_path(
                candidate_source, locator["source_anchor_path"]
            )
            localized_anchor = document.find(
                attrs={locator["attribute"]: locator["value"]}
            )
            if not all(
                isinstance(item, Tag)
                for item in (baseline_anchor, candidate_anchor, localized_anchor)
            ):
                raise TranslationError(f"Translation anchor is invalid: {locator}")
            item_id = f"html:{len(items)}"
            previous_translation = _inner_html(localized_anchor)
            items.append(
                {
                    "id": item_id,
                    "source": _inner_html(candidate_anchor),
                    "previous_source": _inner_html(baseline_anchor),
                    "previous_translation": previous_translation,
                }
            )
            item_paths[item_id] = (
                "html",
                {
                    "locator": locator,
                    "expected_html": previous_translation,
                },
            )

        for source_row in legacy_rows:
            path = source_row["path"]
            localized_node = _resolve_localized_row(
                document, localized_element, source_row
            )
            if not isinstance(localized_node, NavigableString):
                raise TranslationError(f"Expected localized text at {key}:{path}")
            item_id = f"text:{len(items)}"
            items.append(
                {
                    "id": item_id,
                    "source": new_text[path]["text"],
                    "previous_source": old_text[path]["text"],
                    "previous_translation": normalize_text(str(localized_node)),
                }
            )
            item_paths[item_id] = ("text", path)

        attribute_changes = _changed_attributes(baseline, candidate)
        for old_attribute, new_attribute in attribute_changes:
            if (
                new_attribute
                and new_attribute["name"] == "alt"
                and new_attribute["value"]
            ):
                item_id = f"alt:{len(items)}"
                items.append(
                    {
                        "id": item_id,
                        "source": new_attribute["value"],
                        "previous_source": old_attribute["value"]
                        if old_attribute
                        else "",
                        "previous_translation": "",
                    }
                )
                item_paths[item_id] = ("alt", new_attribute["path"])

        if translator is None:
            translated = translate_items(
                items,
                section_title=candidate["title"],
                api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
                model=model or os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            )
        else:
            translated = translator(items, candidate["title"])
        translation_count += len(items)

        for item_id, value in translated.items():
            kind, binding = item_paths[item_id]
            if kind == "html":
                locator = binding["locator"]
                localized_anchor = document.find(
                    attrs={locator["attribute"]: locator["value"]}
                )
                if not isinstance(localized_anchor, Tag):
                    raise TranslationError(
                        f"Localized translation anchor is missing: {locator}"
                    )
                _replace_inner_html(localized_anchor, value, binding["expected_html"])
                continue
            path = binding
            source_row = old_text[path] if kind == "text" else None
            if source_row is None:
                source_row = next(
                    row
                    for row in baseline["attributes"]
                    if row["path"] == path and row["name"] == "alt"
                )
            localized_node = _resolve_localized_row(
                document, localized_element, source_row
            )
            if kind == "text":
                if not isinstance(localized_node, NavigableString):
                    raise TranslationError(f"Expected localized text at {key}:{path}")
                localized_node.replace_with(
                    _with_source_whitespace(new_text[path]["raw"], value)
                )
            elif kind == "alt":
                if not isinstance(localized_node, Tag):
                    raise TranslationError(f"Expected localized image at {key}:{path}")
                localized_node["alt"] = value

        for old_attribute, new_attribute in attribute_changes:
            row = new_attribute or old_attribute
            if row is None or row["name"] == "alt":
                continue
            locator_row = old_attribute or new_attribute
            localized_node = _resolve_localized_row(
                document, localized_element, locator_row
            )
            if not isinstance(localized_node, Tag):
                raise TranslationError(
                    f"Expected localized element at {key}:{row['path']}"
                )
            if new_attribute is None:
                localized_node.attrs.pop(row["name"], None)
            elif row["name"] == "src":
                localized_node["src"] = _download_asset(
                    new_attribute["value"], asset_directory
                )
                localized_node.attrs.pop("srcset", None)
            else:
                localized_node[row["name"]] = new_attribute["value"]

        # Re-resolve once to ensure the source tree was not accidentally modified.
        if baseline_source.name != candidate_source.name:
            raise TranslationError(f"Unexpected root element change in {key}")

    _refresh_cover_metadata(
        document, candidate_snapshot["upstream_version"], edition_date
    )
    _refresh_toc_labels(document)
    document_path.write_text(str(document), encoding="utf-8")
    # Stable source paths keep the same durable localization anchors. Carry them
    # forward so the next monthly run can update a later text revision as well.
    for candidate_unit in candidate_snapshot["units"]:
        baseline_unit = baseline_units[candidate_unit["key"]]
        baseline_text = {row["path"]: row for row in baseline_unit["text_nodes"]}
        for row in candidate_unit["text_nodes"]:
            locator = baseline_text.get(row["path"], {}).get("locator")
            if locator:
                row["locator"] = locator
        baseline_attributes = {
            (row["path"], row["tag"], row["name"]): row
            for row in baseline_unit["attributes"]
        }
        for row in candidate_unit["attributes"]:
            locator = baseline_attributes.get(
                (row["path"], row["tag"], row["name"]), {}
            ).get("locator")
            if locator:
                row["locator"] = locator
    state["snapshot"] = candidate_snapshot
    state["last_published_at"] = edition_date
    return {
        "translated_fragment_count": translation_count,
        "changed_unit_count": len(changed_keys),
    }
