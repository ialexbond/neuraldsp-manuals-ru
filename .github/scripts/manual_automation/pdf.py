from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import fitz
from bs4 import BeautifulSoup, Doctype, Tag
from fontTools.ttLib import TTFont


class PdfValidationError(RuntimeError):
    """Raised when rendering or PDF validation fails."""


_TOP_CONTENT_DPI = 144
_TOP_CONTENT_MAX_Y_PT = 90.0
_TOP_CONTENT_GRAY_THRESHOLD = 252
_TOP_CONTENT_LEFT_PT = 80.0
_TOP_CONTENT_RIGHT_PT = 515.0
_TOP_CONTENT_MIN_PIXELS_PER_ROW = 8

_ORPHAN_HEADING_DPI = 144
_ORPHAN_HEADING_LOWER_PAGE_RATIO = 0.70
_ORPHAN_HEADING_LEFT_PT = 80.0
_ORPHAN_HEADING_RIGHT_PT = 515.0
_ORPHAN_HEADING_BOTTOM_PT = 771.0
_ORPHAN_HEADING_MIN_SIZE_PT = 13.5
_ORPHAN_HEADING_UPPERCASE_MIN_SIZE_PT = 12.0
_ORPHAN_HEADING_UPPERCASE_MAX_CHARACTERS = 80
_ORPHAN_HEADING_UPPERCASE_MAX_WORDS = 8
_ORPHAN_HEADING_GRAY_THRESHOLD = 252
_ORPHAN_HEADING_MIN_PIXELS_PER_ROW = 8

_PRINT_LAYOUT_GUARD_STYLE_ID = "manual-print-layout-guards"
_PRINT_LAYOUT_GUARD_CSS = """
@media print {
  img.dUoPhj {
    break-after: auto !important;
    page-break-after: auto !important;
    break-inside: avoid-page;
    page-break-inside: avoid;
  }
  .keep-heading-with-next {
    break-inside: avoid-page;
    page-break-inside: avoid;
  }
  .keep-heading-with-next > :first-child {
    margin-block-start: 0;
    margin-top: 0;
  }
  li.manual-toc-chapter-splittable {
    break-inside: auto !important;
    page-break-inside: auto !important;
  }
  a.manual-toc-row-chapter {
    break-after: avoid-page !important;
    page-break-after: avoid !important;
  }
}
""".strip()


def _page_top_content_audit(document: fitz.Document) -> dict[str, Any]:
    """Measure first body content on each rendered nonempty page.

    Pages are rendered at 144 dpi in grayscale. The scan covers x=80..515 pt,
    the established central body region. Any visible content counts: text,
    images, rules, callouts, and vector drawings. Two consecutive rows with at
    least eight pixels darker than 252 identify the top of that content.
    """
    scale = _TOP_CONTENT_DPI / 72
    findings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, page in enumerate(document):
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csGRAY,
            alpha=False,
        )
        # A fully white page is handled by the blank-page audit.
        if min(pixmap.samples, default=255) == 255:
            continue
        left = max(0, min(pixmap.width, round(_TOP_CONTENT_LEFT_PT * scale)))
        right = max(left, min(pixmap.width, round(_TOP_CONTENT_RIGHT_PT * scale)))
        samples = pixmap.samples
        rows = [
            sum(
                value < _TOP_CONTENT_GRAY_THRESHOLD
                for value in samples[
                    y * pixmap.stride + left : y * pixmap.stride + right
                ]
            )
            >= _TOP_CONTENT_MIN_PIXELS_PER_ROW
            for y in range(pixmap.height)
        ]
        first_row: int | None = None
        for row_index in range(len(rows) - 1):
            if rows[row_index] and rows[row_index + 1]:
                first_row = row_index
                break
        y_pt = round(first_row / scale, 2) if first_row is not None else None
        finding = {"page": index + 1, "first_content_y_pt": y_pt}
        findings.append(finding)
        if y_pt is None or y_pt > _TOP_CONTENT_MAX_Y_PT:
            failures.append(finding)
    return {
        "dpi": _TOP_CONTENT_DPI,
        "maximum_y_pt": _TOP_CONTENT_MAX_Y_PT,
        "pages": findings,
        "failures": failures,
    }


def _place_toc_continuation_before_section(
    document: BeautifulSoup, before_section: str | None
) -> bool:
    """Split one long TOC chapter at a stable section without orphaning rows."""
    if before_section is None:
        return False

    continuations = document.select("li.manual-toc-continuation")
    if len(continuations) != 1:
        raise PdfValidationError(
            "Configured TOC pagination requires exactly one continuation header."
        )

    target_link = document.select_one(
        f'a.manual-toc-row-section[data-toc-target-source-id="{before_section}"]'
    )
    if not isinstance(target_link, Tag):
        raise PdfValidationError(
            f"Configured TOC continuation target section {before_section!r} was not found."
        )
    target_item = target_link.find_parent("li")
    chapter = target_link.find_parent("li", class_="manual-toc-chapter")
    if not isinstance(target_item, Tag) or not isinstance(chapter, Tag):
        raise PdfValidationError(
            "Configured TOC continuation target is outside a chapter section list."
        )

    changed = False
    chapter_classes = list(chapter.get("class", []))
    if "manual-toc-chapter-splittable" not in chapter_classes:
        chapter["class"] = [*chapter_classes, "manual-toc-chapter-splittable"]
        changed = True
    continuation = continuations[0]
    if target_item.find_previous_sibling("li") is not continuation:
        target_item.insert_before(continuation.extract())
        changed = True
    return changed


def _apply_print_layout_guards(
    document_path: Path, toc_continuation_before_section: str | None = None
) -> dict[str, Any]:
    """Keep short heading leads together without making whole sections atomic."""
    source = document_path.read_text(encoding="utf-8")
    document = BeautifulSoup(source, "html.parser")
    changed = False
    toc_continuation_placed = _place_toc_continuation_before_section(
        document, toc_continuation_before_section
    )
    changed = changed or toc_continuation_placed
    doctypes = [
        node for node in document.contents if isinstance(node, Doctype)
    ]
    for duplicate in doctypes[1:]:
        duplicate.extract()
        changed = True

    style = document.find("style", id=_PRINT_LAYOUT_GUARD_STYLE_ID)
    if not isinstance(style, Tag):
        if not isinstance(document.head, Tag):
            raise PdfValidationError("Localized manual HTML has no head element.")
        style = document.new_tag("style", id=_PRINT_LAYOUT_GUARD_STYLE_ID)
        document.head.append(style)
        changed = True
    if style.get_text() != _PRINT_LAYOUT_GUARD_CSS:
        style.string = _PRINT_LAYOUT_GUARD_CSS
        changed = True

    pairs: list[tuple[Tag, Tag]] = []
    heading_names = {"h1", "h2", "h3", "h4", "h5", "h6"}
    for parent in document.select("#manual-print-root .erHMFr"):
        children = [
            child for child in parent.children if isinstance(child, Tag)
        ]
        for label, following in zip(children, children[1:], strict=False):
            is_heading = label.name in heading_names
            is_image = (
                following.name == "img"
                and "dUoPhj" in following.get("class", [])
            )
            is_introductory_paragraph = (
                is_heading
                and following.name == "p"
                and bool(following.get_text(" ", strip=True))
            )
            label_text = " ".join(
                label.get_text(" ", strip=True).split()
            )
            if (
                is_introductory_paragraph
                or is_image
                and (
                    is_heading
                    or _is_short_uppercase_label(label_text)
                )
            ):
                pairs.append((label, following))

    for label, following in pairs:
        wrapper = document.new_tag(
            "div", attrs={"class": "keep-heading-with-next"}
        )
        label.insert_before(wrapper)
        wrapper.append(label.extract())
        wrapper.append(following.extract())
        changed = True

    if changed:
        serialized = str(document)
        if not serialized.lstrip().lower().startswith("<!doctype html>"):
            serialized = "<!DOCTYPE html>\n" + serialized
        document_path.write_text(
            serialized,
            encoding="utf-8",
        )
    return {
        "grouped_pairs": len(pairs),
        "toc_continuation_placed": toc_continuation_placed,
        "changed": changed,
    }


def _is_bold_span(span: dict[str, Any]) -> bool:
    font_name = str(span.get("font") or "")
    return bool(int(span.get("flags", 0)) & fitz.TEXT_FONT_BOLD) or bool(
        re.search(r"(?:bold|semibold|demibold|black)", font_name, re.IGNORECASE)
    )


def _is_short_uppercase_label(text: str) -> bool:
    letters = [character for character in text if character.isalpha()]
    return (
        bool(letters)
        and all(character.isupper() for character in letters)
        and len(text) <= _ORPHAN_HEADING_UPPERCASE_MAX_CHARACTERS
        and len(text.split()) <= _ORPHAN_HEADING_UPPERCASE_MAX_WORDS
    )


def _clip_has_substantive_content(page: fitz.Page, clip: fitz.Rect) -> bool:
    """Return whether a rendered body clip contains visible material."""
    if clip.is_empty or clip.height <= 0:
        return False
    scale = _ORPHAN_HEADING_DPI / 72
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csGRAY,
        alpha=False,
        clip=clip,
    )
    samples = pixmap.samples
    previous_row_has_content = False
    for row_index in range(pixmap.height):
        row_start = row_index * pixmap.stride
        row_has_content = (
            sum(
                value < _ORPHAN_HEADING_GRAY_THRESHOLD
                for value in samples[row_start : row_start + pixmap.width]
            )
            >= _ORPHAN_HEADING_MIN_PIXELS_PER_ROW
        )
        if previous_row_has_content and row_has_content:
            return True
        previous_row_has_content = row_has_content
    return False


def _heading_orphan_audit(document: fitz.Document) -> list[dict[str, Any]]:
    """Find standalone lower-page headings with no rendered content below.

    Only pages containing a candidate are rendered, and only the central body
    clip below that candidate is rasterized. This makes vector drawings and
    images count as following content without paying the cost of rendering
    every page a second time.
    """
    findings: list[dict[str, Any]] = []
    for page_index, page in enumerate(document):
        lower_page_y = page.rect.height * _ORPHAN_HEADING_LOWER_PAGE_RATIO
        body_right = min(_ORPHAN_HEADING_RIGHT_PT, page.rect.x1)
        body_bottom = min(_ORPHAN_HEADING_BOTTOM_PT, page.rect.y1)
        for block in page.get_text("dict", sort=True).get("blocks", []):
            if int(block.get("type", -1)) != 0:
                continue
            lines = [
                line
                for line in block.get("lines", [])
                if any(
                    str(span.get("text") or "").strip()
                    for span in line.get("spans", [])
                )
            ]
            if len(lines) != 1:
                continue
            spans = [
                span
                for span in lines[0].get("spans", [])
                if str(span.get("text") or "").strip()
            ]
            if not spans or not all(_is_bold_span(span) for span in spans):
                continue
            text = " ".join(
                str(span.get("text") or "").strip() for span in spans
            ).strip()
            font_size = max(float(span.get("size", 0.0)) for span in spans)
            if font_size >= _ORPHAN_HEADING_MIN_SIZE_PT:
                candidate_type = "heading"
            elif (
                font_size >= _ORPHAN_HEADING_UPPERCASE_MIN_SIZE_PT
                and _is_short_uppercase_label(text)
            ):
                candidate_type = "uppercase_label"
            else:
                continue
            rectangle = fitz.Rect(lines[0].get("bbox", block.get("bbox")))
            if (
                rectangle.y0 < lower_page_y
                or rectangle.y1 + 4 >= body_bottom
                or rectangle.x1 <= _ORPHAN_HEADING_LEFT_PT
                or rectangle.x0 >= body_right
            ):
                continue
            below = fitz.Rect(
                _ORPHAN_HEADING_LEFT_PT,
                rectangle.y1 + 4,
                body_right,
                body_bottom,
            )
            if _clip_has_substantive_content(page, below):
                continue
            findings.append(
                {
                    "page": page_index + 1,
                    "text": text,
                    "bbox": [round(value, 2) for value in rectangle],
                    "font_size_pt": round(font_size, 2),
                    "candidate_type": candidate_type,
                }
            )
    return findings


def render_html(document_path: Path, output_pdf: Path) -> dict[str, Any]:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    if node is None:
        raise PdfValidationError("Node.js is required for the pinned PDF renderer.")
    renderer = Path(__file__).resolve().parents[1] / "render_pdf.js"
    completed = subprocess.run(
        [node, str(renderer), str(document_path.resolve()), str(output_pdf.resolve())],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0:
        raise PdfValidationError(
            "Pinned PDF renderer failed: " + completed.stderr.strip()[:2000]
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PdfValidationError("Pinned PDF renderer returned invalid JSON.") from exc


def internal_links(pdf_path: Path) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        for source_page, page in enumerate(document):
            for link in page.get_links():
                destination = link.get("page", -1)
                # Chromium emits named internal destinations (LINK_NAMED) rather than
                # plain GOTO links, but PyMuPDF resolves both to a concrete page.
                if destination < 0:
                    continue
                rectangle = link.get("from")
                links.append(
                    {
                        "source_page": source_page,
                        "destination_page": destination,
                        "x": float(rectangle.x0) if rectangle else 0.0,
                        "y": float(rectangle.y0) if rectangle else 0.0,
                    }
                )
    links.sort(key=lambda item: (item["source_page"], item["y"], item["x"]))
    return links


def fill_toc_page_numbers(document_path: Path, preview_pdf: Path) -> dict[str, int]:
    soup = BeautifulSoup(document_path.read_text(encoding="utf-8"), "html.parser")
    rows = soup.select("a.manual-toc-row[href^='#']")
    links = internal_links(preview_pdf)
    if len(links) < len(rows):
        raise PdfValidationError(
            f"Preview contains {len(links)} internal links for {len(rows)} table-of-contents rows."
        )
    toc_links = links[: len(rows)]
    mapping: dict[str, int] = {}
    for row, link in zip(rows, toc_links, strict=True):
        target = row.get("href", "")[1:]
        page_number = int(link["destination_page"]) + 1
        page_label = row.select_one(".manual-toc-page")
        if not isinstance(page_label, Tag):
            raise PdfValidationError(f"TOC row {target} has no page-number element.")
        page_label.string = str(page_number)
        mapping[target] = page_number
    document_path.write_text(str(soup), encoding="utf-8")
    return mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_font_name(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name).replace("-", " ").strip()


def _chapter_opener_audit(
    document: fitz.Document,
    expected_chapter_count: int = 12,
    chapter_mode: str = "numbered_level_two",
) -> dict[str, Any]:
    chapters: list[dict[str, Any]] = []
    title_only_pages: list[int] = []
    for item in document.get_toc(simple=True):
        if len(item) < 3:
            continue
        level = int(item[0])
        normalized_title = re.sub(
            r"[^\w]+", "", str(item[1]).casefold(), flags=re.UNICODE
        )
        if chapter_mode == "top_level_bookmarks":
            if level != 1 or not normalized_title:
                continue
        elif chapter_mode == "numbered_level_two":
            if level != 2 or not re.match(r"^\d{2}", normalized_title):
                continue
        else:
            raise PdfValidationError(f"Unknown PDF chapter mode: {chapter_mode}")
        if not 1 <= int(item[2]) <= document.page_count:
            continue
        page_number = int(item[2])
        page_text = re.sub(
            r"[^\w]+",
            "",
            document[page_number - 1].get_text("text").casefold(),
            flags=re.UNICODE,
        )
        remaining_content = page_text.replace(normalized_title, "", 1)
        title_only = len(remaining_content) < 8
        chapters.append(
            {
                "title": str(item[1]),
                "page": page_number,
                "title_only": title_only,
            }
        )
        if title_only:
            title_only_pages.append(page_number)
    return {
        "chapter_count": len(chapters),
        "title_only_pages": title_only_pages,
        "chapters": chapters,
        "valid": len(chapters) == expected_chapter_count and not title_only_pages,
    }


def _font_audit(document: fitz.Document, font_directory: Path | None) -> dict[str, Any]:
    expected_files = {
        "IBMPlexSans": "IBMPlexSans-Regular.ttf",
        "IBMPlexSans Bold": "IBMPlexSans-Bold.ttf",
        "IBMPlexSans Italic": "IBMPlexSans-Italic.ttf",
        "IBMPlexSans BoldItalic": "IBMPlexSans-BoldItalic.ttf",
    }
    required_embedded = {"IBMPlexSans", "IBMPlexSans Bold"}
    xrefs: dict[int, str] = {}
    for page in document:
        for font in page.get_fonts(full=True):
            xrefs[int(font[0])] = str(font[3])
    names = sorted({_normalize_font_name(name) for name in xrefs.values()})
    unexpected = sorted(set(names) - set(expected_files))
    # Chromium only embeds a face when the document actually uses it. Regular
    # and bold are mandatory in every manual; italic faces remain optional but
    # are still checked against the accepted font files whenever embedded.
    missing = sorted(required_embedded - set(names))
    embedded_failures: list[str] = []
    metric_failures: list[dict[str, Any]] = []
    source_fonts: dict[str, TTFont] = {}
    try:
        if font_directory is not None:
            for name, filename in expected_files.items():
                source_path = font_directory / filename
                if not source_path.exists():
                    metric_failures.append(
                        {"name": name, "reason": "source font missing"}
                    )
                else:
                    source_fonts[name] = TTFont(source_path, lazy=False)
        for xref, raw_name in sorted(xrefs.items()):
            name = _normalize_font_name(raw_name)
            if name not in expected_files:
                continue
            extracted_name, _extension, _font_type, data = document.extract_font(xref)
            if not data:
                embedded_failures.append(extracted_name)
                continue
            if font_directory is None or name not in source_fonts:
                continue
            embedded = TTFont(io.BytesIO(data), lazy=False)
            source = source_fonts[name]
            try:
                fields = {
                    "units_per_em": (
                        embedded["head"].unitsPerEm,
                        source["head"].unitsPerEm,
                    ),
                    "width_class": (
                        embedded["OS/2"].usWidthClass,
                        source["OS/2"].usWidthClass,
                    ),
                    "weight_class": (
                        embedded["OS/2"].usWeightClass,
                        source["OS/2"].usWeightClass,
                    ),
                    "average_width": (
                        embedded["OS/2"].xAvgCharWidth,
                        source["OS/2"].xAvgCharWidth,
                    ),
                    "maximum_width": (
                        embedded["hhea"].advanceWidthMax,
                        source["hhea"].advanceWidthMax,
                    ),
                }
                mismatches = {
                    field: {"embedded": values[0], "source": values[1]}
                    for field, values in fields.items()
                    if values[0] != values[1]
                }
                embedded_cmap = embedded.getBestCmap() or {}
                source_cmap = source.getBestCmap() or {}
                width_mismatches: list[int] = []
                for codepoint, glyph in embedded_cmap.items():
                    source_glyph = source_cmap.get(codepoint)
                    if (
                        source_glyph is None
                        or embedded["hmtx"][glyph][0] != source["hmtx"][source_glyph][0]
                    ):
                        width_mismatches.append(codepoint)
                if mismatches or width_mismatches or not embedded_cmap:
                    metric_failures.append(
                        {
                            "name": name,
                            "metric_mismatches": mismatches,
                            "width_mismatch_codepoints": width_mismatches,
                        }
                    )
            finally:
                embedded.close()
    finally:
        for source in source_fonts.values():
            source.close()
    return {
        "names": names,
        "unexpected": unexpected,
        "missing": missing,
        "embedded_failures": embedded_failures,
        "metric_failures": metric_failures,
        "valid": not unexpected
        and not missing
        and not embedded_failures
        and not metric_failures,
    }


def pdf_metrics(
    pdf_path: Path,
    font_directory: Path | None = None,
    expected_chapter_count: int = 12,
    *,
    chapter_mode: str = "numbered_level_two",
) -> dict[str, Any]:
    with fitz.open(pdf_path) as document:
        sizes = sorted(
            {
                (round(page.rect.width, 2), round(page.rect.height, 2))
                for page in document
            }
        )
        blank_pages: list[int] = []
        out_of_bounds: list[dict[str, Any]] = []
        overlaps: list[dict[str, Any]] = []
        image_placements = 0
        cyrillic_characters = 0
        rendered_pages = 0
        invalid_links = 0
        external_links = 0
        for index, page in enumerate(document):
            text = page.get_text("text")
            images = page.get_images(full=True)
            if not text.strip() and not images:
                preview = page.get_pixmap(
                    matrix=fitz.Matrix(0.2, 0.2),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                if min(preview.samples, default=255) >= 250:
                    blank_pages.append(index + 1)
            image_placements += len(images)
            cyrillic_characters += len(re.findall(r"[А-Яа-яЁё]", text))
            blocks: list[tuple[fitz.Rect, str]] = []
            for block in page.get_text("blocks", sort=True):
                if len(block) < 7 or int(block[6]) != 0 or not str(block[4]).strip():
                    continue
                rectangle = fitz.Rect(block[:4])
                blocks.append((rectangle, str(block[4]).strip()))
                tolerance = 0.75
                if (
                    rectangle.x0 < page.rect.x0 - tolerance
                    or rectangle.y0 < page.rect.y0 - tolerance
                    or rectangle.x1 > page.rect.x1 + tolerance
                    or rectangle.y1 > page.rect.y1 + tolerance
                ):
                    out_of_bounds.append(
                        {
                            "page": index + 1,
                            "bbox": [round(value, 3) for value in rectangle],
                            "text": str(block[4]).replace("\n", " ")[:120],
                        }
                    )
            for left_index, (left, left_text) in enumerate(blocks):
                for right, right_text in blocks[left_index + 1 :]:
                    intersection = left & right
                    if intersection.is_empty:
                        continue
                    area = max(0.0, intersection.width) * max(0.0, intersection.height)
                    if intersection.height > 2.0 and area > 8.0:
                        overlaps.append(
                            {
                                "page": index + 1,
                                "area": round(area, 3),
                                "first": left_text.replace("\n", " ")[:100],
                                "second": right_text.replace("\n", " ")[:100],
                            }
                        )
            for link in page.get_links():
                kind = int(link.get("kind", 0))
                destination = link.get("page")
                if kind in {fitz.LINK_GOTO, fitz.LINK_NAMED}:
                    if (
                        not isinstance(destination, int)
                        or not 0 <= destination < document.page_count
                    ):
                        invalid_links += 1
                elif kind == fitz.LINK_URI:
                    external_links += 1
                    if not str(link.get("uri") or "").strip():
                        invalid_links += 1
            page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
            rendered_pages += 1
        outline = document.get_toc(simple=False)
        chapter_openers = _chapter_opener_audit(
            document,
            expected_chapter_count,
            chapter_mode,
        )
        invalid_bookmarks = sum(
            len(item) < 3 or not 1 <= int(item[2]) <= document.page_count
            for item in outline
        )
        fonts = _font_audit(document, font_directory)
        page_top_content = _page_top_content_audit(document)
        heading_orphans = _heading_orphan_audit(document)
        raw_pdf = pdf_path.read_bytes()
        return {
            "sha256": _sha256(pdf_path),
            "page_count": document.page_count,
            "page_sizes": [list(size) for size in sizes],
            "blank_pages": blank_pages,
            "internal_link_count": len(internal_links(pdf_path)),
            "external_link_count": external_links,
            "invalid_link_count": invalid_links,
            "bookmark_count": len(outline),
            "invalid_bookmark_count": invalid_bookmarks,
            "chapter_openers": chapter_openers,
            "tagged": b"/StructTreeRoot" in raw_pdf,
            "image_placement_count": image_placements,
            "cyrillic_character_count": cyrillic_characters,
            "out_of_bounds_text_blocks": out_of_bounds,
            "text_block_overlaps": overlaps,
            "page_top_content": page_top_content,
            "heading_orphans": heading_orphans,
            "rendered_page_count": rendered_pages,
            "fonts": fonts,
            "file_size": pdf_path.stat().st_size,
        }


def validate_pdf(
    pdf_path: Path,
    document_path: Path | None,
    config: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    font_directory: Path | None = None,
) -> dict[str, Any]:
    if font_directory is None:
        if document_path is None:
            raise PdfValidationError(
                "A reference font directory is required for PDF validation."
            )
        font_directory = document_path.parents[1] / "assets" / "fonts"
    metrics_kwargs: dict[str, Any] = {}
    if "pdf_chapter_mode" in config:
        metrics_kwargs["chapter_mode"] = str(config["pdf_chapter_mode"])
    metrics = pdf_metrics(
        pdf_path,
        font_directory,
        int(
            config.get(
                "expected_pdf_chapter_count",
                config["expected_chapter_count"],
            )
        ),
        **metrics_kwargs,
    )
    errors: list[str] = []
    if metrics["page_count"] < config["minimum_pdf_pages"]:
        errors.append(
            f"PDF has {metrics['page_count']} pages; at least {config['minimum_pdf_pages']} are required"
        )
    if metrics["blank_pages"]:
        errors.append(
            "blank pages detected: " + ", ".join(map(str, metrics["blank_pages"]))
        )
    if len(metrics["page_sizes"]) != 1:
        errors.append(f"inconsistent page sizes: {metrics['page_sizes']}")
    elif not any(
        all(
            abs(actual - expected) <= 1.0
            for actual, expected in zip(
                metrics["page_sizes"][0], expected_size, strict=True
            )
        )
        for expected_size in ((595.0, 842.0), (842.0, 595.0))
    ):
        errors.append(f"PDF page size is not A4: {metrics['page_sizes'][0]}")
    if metrics["internal_link_count"] < config["minimum_internal_link_count"]:
        errors.append(
            f"only {metrics['internal_link_count']} internal links were found; "
            f"at least {config['minimum_internal_link_count']} are required"
        )
    if metrics["invalid_link_count"]:
        errors.append(f"{metrics['invalid_link_count']} invalid PDF links were found")
    if metrics["invalid_bookmark_count"]:
        errors.append(
            f"{metrics['invalid_bookmark_count']} invalid PDF bookmarks were found"
        )
    if not metrics["chapter_openers"]["valid"]:
        errors.append(
            "chapter openers must start on a new page and include following content; "
            f"title-only pages={metrics['chapter_openers']['title_only_pages']}"
        )
    if config.get("require_tagged_pdf", True) and not metrics["tagged"]:
        errors.append("PDF is not tagged")
    if metrics["rendered_page_count"] != metrics["page_count"]:
        errors.append("not every PDF page rendered successfully")
    if metrics["out_of_bounds_text_blocks"]:
        errors.append(
            f"{len(metrics['out_of_bounds_text_blocks'])} text blocks extend outside page bounds"
        )
    preserved_source_layout = (
        config.get("pdf_validation_profile") == "preserved_source_layout"
    )
    if metrics["text_block_overlaps"] and not preserved_source_layout:
        errors.append(
            f"{len(metrics['text_block_overlaps'])} material text-block overlaps were found"
        )
    if metrics["page_top_content"]["failures"]:
        locations = ", ".join(
            f"page {item['page']} y="
            f"{item['first_content_y_pt'] if item['first_content_y_pt'] is not None else 'not found'} pt"
            for item in metrics["page_top_content"]["failures"]
        )
        errors.append(
            "first central body content must begin at or above "
            f"{metrics['page_top_content']['maximum_y_pt']:.0f} pt from the top "
            f"({locations})"
        )
    if metrics["heading_orphans"]:
        locations = ", ".join(
            f"page {item['page']} ({item['text']})"
            for item in metrics["heading_orphans"]
        )
        errors.append(
            "standalone headings must remain with substantive following content "
            f"({locations})"
        )
    if not metrics["fonts"]["valid"] and not preserved_source_layout:
        errors.append(
            "embedded IBM Plex Sans faces or metrics do not match the accepted fonts"
        )
    if baseline and baseline.get("page_count"):
        delta = abs(metrics["page_count"] - int(baseline["page_count"])) / int(
            baseline["page_count"]
        )
        if delta > config["maximum_pdf_page_delta_ratio"]:
            errors.append(
                f"page count changed by {delta:.1%}; the allowed delta is "
                f"{config['maximum_pdf_page_delta_ratio']:.1%}"
            )
        for field in ("bookmark_count", "image_placement_count"):
            expected = baseline.get(field)
            if expected is not None and metrics[field] != expected:
                errors.append(
                    f"{field} changed from {expected} to {metrics[field]} despite a stable source structure"
                )
        baseline_cyrillic = int(baseline.get("cyrillic_character_count", 0))
        if (
            baseline_cyrillic
            and metrics["cyrillic_character_count"] < baseline_cyrillic * 0.9
        ):
            errors.append("searchable Cyrillic text dropped by more than 10%")

    if document_path is not None:
        soup = BeautifulSoup(document_path.read_text(encoding="utf-8"), "html.parser")
        expected_pages = [
            int(row.get_text(strip=True)) for row in soup.select(".manual-toc-page")
        ]
        links = internal_links(pdf_path)[: len(expected_pages)]
        actual_pages = [link["destination_page"] + 1 for link in links]
        if len(actual_pages) != len(expected_pages) or actual_pages != expected_pages:
            errors.append(
                "visible TOC page numbers do not match their PDF link destinations"
            )

    result = {"valid": not errors, "errors": errors, **metrics}
    if errors:
        raise PdfValidationError(json.dumps(result, ensure_ascii=False))
    return result


def build_pdf(
    document_path: Path,
    output_pdf: Path,
    config: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    continuation_section = config.get("toc_continuation_before_section")
    layout_guards = _apply_print_layout_guards(
        document_path,
        str(continuation_section) if continuation_section is not None else None,
    )
    expected_playwright = str(config.get("playwright_version", ""))
    preview = output_pdf.with_name(output_pdf.stem + "-preview.pdf")
    preview_status = render_html(document_path, preview)
    actual_playwright = str(preview_status.get("playwrightVersion", ""))
    if expected_playwright and actual_playwright != expected_playwright:
        preview.unlink(missing_ok=True)
        raise PdfValidationError(
            f"Playwright {actual_playwright} is installed; the accepted toolchain requires "
            f"{expected_playwright}."
        )
    toc_mapping = fill_toc_page_numbers(document_path, preview)
    render_status = render_html(document_path, output_pdf)
    preview.unlink(missing_ok=True)
    validation = validate_pdf(output_pdf, document_path, config, baseline)
    return {
        "layout_guards": layout_guards,
        "toc_mapping": toc_mapping,
        "preview_resources": preview_status,
        "resources": render_status,
        "validation": validation,
    }
