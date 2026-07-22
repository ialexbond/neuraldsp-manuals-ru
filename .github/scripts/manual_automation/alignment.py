from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from .canonical import _semantic_children, semantic_payload
from .translate import TranslationError, _localized_unit_element, _resolve_path


def _path_tuple(path_text: str) -> tuple[int, ...]:
    if path_text in {"", "root"}:
        return ()
    return tuple(int(item) for item in path_text.split("."))


def _path_text(path: tuple[int, ...]) -> str:
    return ".".join(map(str, path)) or "root"


def _node_paths(root: Tag) -> dict[int, tuple[int, ...]]:
    result: dict[int, tuple[int, ...]] = {id(root): ()}

    def visit(node: Tag, base: tuple[int, ...]) -> None:
        for index, child in enumerate(_semantic_children(node)):
            path = base + (index,)
            result[id(child)] = path
            if isinstance(child, Tag):
                visit(child, path)

    visit(root, ())
    return result


def _locator_for(template_root: Tag, target: Tag | NavigableString, target_path: str) -> dict[str, Any]:
    paths = _node_paths(template_root)
    current = target if isinstance(target, Tag) else target.parent
    while isinstance(current, Tag):
        attribute = None
        if current.get("data-source-id"):
            attribute = "data-source-id"
        elif current.get("data-image-id"):
            attribute = "data-image-id"
        if attribute:
            anchor_path = paths.get(id(current))
            if anchor_path is None:
                break
            absolute = _path_tuple(target_path)
            if absolute[: len(anchor_path)] != anchor_path:
                break
            locator: dict[str, Any] = {
                "attribute": attribute,
                "value": str(current.get(attribute)),
                "source_anchor_path": _path_text(anchor_path),
            }
            if isinstance(target, Tag):
                if target is current:
                    locator["target_tag"] = target.name
                    locator["target_ordinal"] = -1
                else:
                    matching = current.find_all(target.name)
                    locator["target_tag"] = target.name
                    locator["target_ordinal"] = next(
                        index for index, item in enumerate(matching) if item is target
                    )
            return locator
        if current is template_root:
            break
        current = current.parent
    raise TranslationError(f"No durable localized locator for semantic path {target_path}")


def _validate_locator(
    localized_document: BeautifulSoup,
    locator: dict[str, Any],
    expected_type: type,
) -> None:
    anchor = localized_document.find(attrs={locator["attribute"]: locator["value"]})
    if not isinstance(anchor, Tag):
        raise TranslationError(
            f"Localized anchor is missing: {locator['attribute']}={locator['value']}"
        )
    if expected_type is Tag:
        ordinal = int(locator.get("target_ordinal", -1))
        if ordinal == -1:
            target: Tag | None = anchor
        else:
            matches = anchor.find_all(locator["target_tag"])
            target = matches[ordinal] if ordinal < len(matches) else None
        if not isinstance(target, Tag):
            raise TranslationError(f"Localized locator does not resolve to an element: {locator}")


def align_snapshot(
    snapshot: dict[str, Any], source_template_html: Path, localized_html: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_template = BeautifulSoup(
        source_template_html.read_text(encoding="utf-8"), "html.parser"
    )
    localized_document = BeautifulSoup(localized_html.read_text(encoding="utf-8"), "html.parser")
    aligned = copy.deepcopy(snapshot)
    failures: list[str] = []

    for unit in aligned["units"]:
        try:
            template_element = _localized_unit_element(source_template, unit)
            template_payload = semantic_payload(template_element, snapshot["source_url"])
            if template_payload["skeleton"] != unit["skeleton"]:
                raise TranslationError("source template semantic tree differs from the live source")
            template_text = {row["path"]: row for row in template_payload["text_nodes"]}
            for row in unit["text_nodes"]:
                template_row = template_text.get(row["path"])
                if template_row is None or template_row["text"] != row["text"]:
                    raise TranslationError(f"source text mismatch at {row['path']}")
                target = _resolve_path(template_element, row["path"])
                locator = _locator_for(template_element, target, row["path"])
                _validate_locator(localized_document, locator, NavigableString)
                row["locator"] = locator

            template_attributes = {
                (row["path"], row["tag"], row["name"]): row
                for row in template_payload["attributes"]
            }
            for row in unit["attributes"]:
                key = row["path"], row["tag"], row["name"]
                if key not in template_attributes:
                    raise TranslationError(f"source attribute mismatch at {row['path']}:{row['name']}")
                target = _resolve_path(template_element, row["path"])
                locator = _locator_for(template_element, target, row["path"])
                _validate_locator(localized_document, locator, Tag)
                row["locator"] = locator
        except Exception as exc:
            failures.append(f"{unit['key']}: {exc}")

    return aligned, {
        "aligned": not failures,
        "unit_count": len(aligned["units"]),
        "failures": failures,
    }
