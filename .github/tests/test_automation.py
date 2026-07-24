from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import fitz
from bs4 import BeautifulSoup


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / ".github" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

from manual_automation.canonical import extract_snapshot  # noqa: E402
from manual_automation.cli import command_validate  # noqa: E402
from manual_automation.diffing import classify_change  # noqa: E402
from manual_automation.pdf import (  # noqa: E402
    PdfValidationError,
    _apply_print_layout_guards,
    _chapter_opener_audit,
    _heading_orphan_audit,
    _page_top_content_audit,
    pdf_metrics,
    validate_pdf,
)
from manual_automation.repository import (  # noqa: E402
    RepositoryPolicyError,
    expected_pdf_name,
    manual_catalog,
    parse_pdf_name,
    update_readme,
    validate_manual_directory,
)
from manual_automation.state import StateError, safe_extract  # noqa: E402
from manual_automation.translate import apply_safe_update  # noqa: E402


TEST_CONFIG = {
    "expected_chapter_count": 2,
    "minimum_section_count": 3,
    "maximum_changed_unit_count": 2,
    "maximum_changed_unit_ratio": 0.5,
}


class ChapterOpenerAuditTests(unittest.TestCase):
    @staticmethod
    def _document(
        chapter_count: int = 12, title_only_chapter: int | None = None
    ) -> fitz.Document:
        document = fitz.open()
        toc: list[list[object]] = [[1, "Chapters", 1]]
        for chapter_number in range(1, chapter_count + 1):
            title = f"{chapter_number:02d} Chapter {chapter_number}"
            page = document.new_page()
            text = title
            if chapter_number != title_only_chapter:
                text += f"\nSubstantive content for chapter {chapter_number}."
            page.insert_text((72, 72), text)
            toc.append([2, title, chapter_number])
        document.set_toc(toc)
        return document

    def test_twelve_chapters_with_content_are_valid(self) -> None:
        with self._document() as document:
            audit = _chapter_opener_audit(document)

        self.assertTrue(audit["valid"])
        self.assertEqual(12, audit["chapter_count"])
        self.assertEqual([], audit["title_only_pages"])

    def test_title_only_chapter_is_invalid(self) -> None:
        with self._document(title_only_chapter=6) as document:
            audit = _chapter_opener_audit(document)

        self.assertFalse(audit["valid"])
        self.assertEqual([6], audit["title_only_pages"])

    def test_wrong_chapter_count_is_invalid(self) -> None:
        with self._document(chapter_count=11) as document:
            audit = _chapter_opener_audit(document)

        self.assertFalse(audit["valid"])
        self.assertEqual(11, audit["chapter_count"])

    def test_eight_chapter_manual_is_valid_when_configured(self) -> None:
        with self._document(chapter_count=8) as document:
            audit = _chapter_opener_audit(document, expected_chapter_count=8)

        self.assertTrue(audit["valid"])
        self.assertEqual(8, audit["chapter_count"])


class PdfMetricsTests(unittest.TestCase):
    def test_white_drawing_is_blank_but_black_drawing_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf_path = Path(temporary) / "drawings.pdf"
            with fitz.open() as document:
                white_page = document.new_page()
                white_page.draw_rect(
                    fitz.Rect(72, 72, 216, 216),
                    color=(1, 1, 1),
                    fill=(1, 1, 1),
                )
                black_page = document.new_page()
                black_page.draw_rect(
                    fitz.Rect(72, 72, 216, 216),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
                document.save(pdf_path)

            metrics = pdf_metrics(pdf_path)

        self.assertEqual([1], metrics["blank_pages"])


class PdfValidationTests(unittest.TestCase):
    CONFIG = {
        "expected_chapter_count": 2,
        "minimum_pdf_pages": 10,
        "minimum_internal_link_count": 5,
        "maximum_pdf_page_delta_ratio": 0.2,
    }
    VALID_METRICS = {
        "sha256": "abc",
        "page_count": 10,
        "page_sizes": [[595.0, 842.0]],
        "blank_pages": [],
        "internal_link_count": 5,
        "external_link_count": 0,
        "invalid_link_count": 0,
        "bookmark_count": 3,
        "invalid_bookmark_count": 0,
        "chapter_openers": {"valid": True, "title_only_pages": []},
        "tagged": True,
        "image_placement_count": 1,
        "cyrillic_character_count": 100,
        "out_of_bounds_text_blocks": [],
        "text_block_overlaps": [],
        "page_top_content": {"maximum_y_pt": 90.0, "failures": []},
        "heading_orphans": [],
        "rendered_page_count": 10,
        "fonts": {"valid": True},
        "file_size": 1000,
    }

    def test_repository_validation_uses_reference_fonts(self) -> None:
        pdf_path = Path("manual.pdf")
        font_directory = Path("reference-fonts")
        with patch(
            "manual_automation.pdf.pdf_metrics",
            return_value=copy.deepcopy(self.VALID_METRICS),
        ) as mocked_metrics:
            result = validate_pdf(
                pdf_path,
                None,
                self.CONFIG,
                font_directory=font_directory,
            )

        self.assertTrue(result["valid"])
        mocked_metrics.assert_called_once_with(pdf_path, font_directory, 2)

    def test_every_repository_pdf_invariant_is_enforced(self) -> None:
        invalid_cases = {
            "minimum page count": {"page_count": 9},
            "blank pages": {"blank_pages": [2]},
            "consistent page size": {"page_sizes": [[595.0, 842.0], [612.0, 792.0]]},
            "A4 page size": {"page_sizes": [[612.0, 792.0]]},
            "minimum internal links": {"internal_link_count": 4},
            "valid links": {"invalid_link_count": 1},
            "valid bookmarks": {"invalid_bookmark_count": 1},
            "chapter openers": {
                "chapter_openers": {"valid": False, "title_only_pages": [3]}
            },
            "tagged PDF": {"tagged": False},
            "render every page": {"rendered_page_count": 9},
            "in-bounds text": {"out_of_bounds_text_blocks": [{"page": 1}]},
            "non-overlapping text": {"text_block_overlaps": [{"page": 1}]},
            "top page content": {
                "page_top_content": {
                    "maximum_y_pt": 90.0,
                    "failures": [{"page": 1, "first_content_y_pt": 120.0}],
                }
            },
            "orphan headings": {
                "heading_orphans": [
                    {
                        "page": 4,
                        "text": "Standalone heading",
                        "bbox": [100.0, 650.0, 260.0, 675.0],
                        "font_size_pt": 18.0,
                        "candidate_type": "heading",
                    }
                ]
            },
            "accepted fonts": {"fonts": {"valid": False}},
        }
        for label, overrides in invalid_cases.items():
            with self.subTest(invariant=label):
                metrics = copy.deepcopy(self.VALID_METRICS)
                metrics.update(overrides)
                with patch(
                    "manual_automation.pdf.pdf_metrics",
                    return_value=metrics,
                ):
                    with self.assertRaises(PdfValidationError):
                        validate_pdf(
                            Path("manual.pdf"),
                            None,
                            self.CONFIG,
                            font_directory=Path("reference-fonts"),
                        )

    def test_validate_repository_propagates_pdf_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            pdf_directory = repository / "manuals" / "example"
            pdf_directory.mkdir(parents=True)
            pdf_path = pdf_directory / "Example_RU_v1.0.0_rev2026-07-24.pdf"
            pdf_path.write_bytes(b"%PDF")
            config = {
                **self.CONFIG,
                "pdf_directory": "manuals/example",
                "pdf_name_template": "Example_RU_v{version}_rev{date}.pdf",
            }
            config_path = repository / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args = argparse.Namespace(
                config=str(config_path),
                repository=str(repository),
                result=None,
            )

            with patch(
                "manual_automation.cli.validate_pdf",
                side_effect=PdfValidationError("invalid PDF"),
            ) as mocked_validation:
                with self.assertRaisesRegex(PdfValidationError, "invalid PDF"):
                    command_validate(args)

        mocked_validation.assert_called_once_with(
            pdf_path,
            None,
            config,
            font_directory=repository / ".github" / "assets" / "fonts",
        )


class PageTopContentAuditTests(unittest.TestCase):
    @staticmethod
    def _audit_page(
        text_baseline: float, *, callout: bool = False, vector_block: bool = False
    ) -> dict[str, object]:
        with fitz.open() as document:
            page = document.new_page(width=595, height=842)
            if callout:
                page.draw_rect(
                    fitz.Rect(100, 24, 495, 72),
                    color=(0.88, 0.88, 0.88),
                    fill=(0.88, 0.88, 0.88),
                )
            if vector_block:
                page.draw_rect(
                    fitz.Rect(220, 24, 375, 72),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
            page.insert_text(
                (100, text_baseline),
                "Light gray body content",
                color=(0.85, 0.85, 0.85),
            )
            return _page_top_content_audit(document)

    def test_light_gray_content_at_top_passes(self) -> None:
        audit = self._audit_page(80)

        self.assertEqual([], audit["failures"])
        self.assertIsNotNone(audit["pages"][0]["first_content_y_pt"])
        self.assertLessEqual(audit["pages"][0]["first_content_y_pt"], 90)

    def test_late_body_content_fails_with_page_and_y_position(self) -> None:
        audit = self._audit_page(130)

        self.assertEqual(1, len(audit["failures"]))
        self.assertEqual(1, audit["failures"][0]["page"])
        self.assertGreater(audit["failures"][0]["first_content_y_pt"], 90)

    def test_callout_and_vector_blocks_count_as_top_content(self) -> None:
        for options in ({"callout": True}, {"vector_block": True}):
            with self.subTest(options=options):
                audit = self._audit_page(130, **options)
                self.assertEqual([], audit["failures"])
                self.assertLess(audit["pages"][0]["first_content_y_pt"], 90)


class HeadingOrphanAuditTests(unittest.TestCase):
    @staticmethod
    def _audit_page(
        text: str,
        *,
        font_size: float,
        bold: bool,
        content_below: bool = False,
    ) -> list[dict[str, object]]:
        with fitz.open() as document:
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (100, 650),
                text,
                fontsize=font_size,
                fontname="hebo" if bold else "helv",
            )
            if content_below:
                page.draw_rect(
                    fitz.Rect(100, 700, 300, 730),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
            return _heading_orphan_audit(document)

    def test_bold_eighteen_point_orphan_is_detected(self) -> None:
        findings = self._audit_page(
            "Standalone heading",
            font_size=18,
            bold=True,
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("Standalone heading", findings[0]["text"])
        self.assertEqual("heading", findings[0]["candidate_type"])

    def test_uppercase_twelve_point_orphan_is_detected(self) -> None:
        findings = self._audit_page(
            "ANALOG OUTPUTS AUTO-SUM",
            font_size=12,
            bold=True,
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("uppercase_label", findings[0]["candidate_type"])

    def test_heading_with_rendered_content_below_passes(self) -> None:
        findings = self._audit_page(
            "Heading with diagram",
            font_size=18,
            bold=True,
            content_below=True,
        )

        self.assertEqual([], findings)

    def test_normal_body_line_is_not_a_candidate(self) -> None:
        findings = self._audit_page(
            "Normal body line",
            font_size=12,
            bold=False,
        )

        self.assertEqual([], findings)


class PrintLayoutGuardTests(unittest.TestCase):
    def test_groups_heading_leads_and_is_idempotent(self) -> None:
        source = """<!DOCTYPE html>
<!DOCTYPE html>
<html><head></head><body>
<div id="manual-print-root"><div class="erHMFr">
  <h3>Section heading</h3><p>Short introductory paragraph.</p>
  <h4>Diagram heading</h4><img class="dUoPhj" src="diagram.svg">
  <p>ANALOG OUTPUTS AUTO-SUM</p><img class="dUoPhj" src="control.svg">
  <p>Ordinary body copy.</p><img class="dUoPhj" src="example.svg">
</div></div>
</body></html>
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "document.html"
            path.write_text(source, encoding="utf-8")

            first = _apply_print_layout_guards(path)
            once = path.read_text(encoding="utf-8")
            second = _apply_print_layout_guards(path)
            twice = path.read_text(encoding="utf-8")

        soup = BeautifulSoup(once, "html.parser")
        groups = soup.select(".keep-heading-with-next")
        self.assertEqual(3, len(groups))
        self.assertEqual(
            ["h3", "p"],
            [child.name for child in groups[0].find_all(recursive=False)],
        )
        self.assertEqual(
            ["h4", "img"],
            [child.name for child in groups[1].find_all(recursive=False)],
        )
        self.assertEqual(
            ["p", "img"],
            [child.name for child in groups[2].find_all(recursive=False)],
        )
        self.assertIsNotNone(
            soup.find("style", id="manual-print-layout-guards")
        )
        self.assertEqual(
            {
                "grouped_pairs": 3,
                "toc_continuation_placed": False,
                "changed": True,
            },
            first,
        )
        self.assertEqual(
            {
                "grouped_pairs": 0,
                "toc_continuation_placed": False,
                "changed": False,
            },
            second,
        )
        self.assertEqual(1, once.lower().count("<!doctype html>"))
        self.assertEqual(once, twice)

    def test_places_toc_continuation_before_configured_section(self) -> None:
        source = """<!DOCTYPE html>
<html><head></head><body>
<div id="manual-print-root">
  <ol class="manual-toc-chapters">
    <li class="manual-toc-continuation">Continuation</li>
    <li class="manual-toc-chapter">
      <a class="manual-toc-row-chapter">
        <span class="manual-toc-chapter-number">5</span>
      </a>
    </li>
    <li class="manual-toc-chapter">
      <a class="manual-toc-row-chapter">
        <span class="manual-toc-chapter-number">6</span>
      </a>
      <ol class="manual-toc-sections">
        <li><a class="manual-toc-row-section"
          data-toc-target-source-id="ch06-0003">First section</a></li>
        <li><a class="manual-toc-row-section"
          data-toc-target-source-id="ch06-0095">Split target</a></li>
      </ol>
    </li>
  </ol>
</div>
</body></html>
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "document.html"
            path.write_text(source, encoding="utf-8")

            first = _apply_print_layout_guards(
                path, toc_continuation_before_section="ch06-0095"
            )
            second = _apply_print_layout_guards(
                path, toc_continuation_before_section="ch06-0095"
            )
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")

        target = soup.select_one(
            'a[data-toc-target-source-id="ch06-0095"]'
        ).find_parent("li")
        continuation = soup.select_one("li.manual-toc-continuation")
        chapter = target.find_parent("li", class_="manual-toc-chapter")
        self.assertIs(target.find_previous_sibling("li"), continuation)
        self.assertIn("manual-toc-chapter-splittable", chapter.get("class", []))
        self.assertTrue(first["toc_continuation_placed"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["toc_continuation_placed"])
        self.assertFalse(second["changed"])


class CanonicalizationTests(unittest.TestCase):
    def test_cosmetic_markup_does_not_change_hashes(self) -> None:
        baseline = extract_snapshot(str(FIXTURES / "baseline.html"), 2)
        cosmetic = extract_snapshot(str(FIXTURES / "cosmetic.html"), 2)
        self.assertEqual(baseline["content_hash"], cosmetic["content_hash"])
        self.assertEqual(3, baseline["section_count"])

    def test_single_text_change_is_safe(self) -> None:
        baseline = extract_snapshot(str(FIXTURES / "baseline.html"), 2)
        candidate = extract_snapshot(str(FIXTURES / "changed-text.html"), 2)
        report = classify_change(baseline, candidate, TEST_CONFIG)
        self.assertEqual("safe_change", report["status"])
        self.assertEqual(["section:Global-Features"], report["changed_units"])

    def test_semantic_tree_change_is_blocked(self) -> None:
        baseline = extract_snapshot(str(FIXTURES / "baseline.html"), 2)
        candidate = extract_snapshot(str(FIXTURES / "structural-change.html"), 2)
        report = classify_change(baseline, candidate, TEST_CONFIG)
        self.assertEqual("blocked", report["status"])
        self.assertEqual(["section:Global-Features"], report["skeleton_changes"])

    def test_section_with_misleveled_h2_is_still_discovered(self) -> None:
        html = """<!doctype html><html><head><title>Manual 1.0.0</title></head>
        <body><main id="main-content"><h1>Manual 1.0.0</h1><article>
        <div><h2 id="Chapter-One"><span>01</span><br>Chapter One</h2>
        <p>Chapter introduction.</p></div>
        <div id="Misleveled-Section"><div><h2>Misleveled Section</h2>
        <p>Section content.</p></div></div>
        </article></main></body></html>"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "manual.html"
            source.write_text(html, encoding="utf-8")
            snapshot = extract_snapshot(str(source), 1)

        self.assertEqual(1, snapshot["section_count"])
        self.assertEqual(
            ["section:Misleveled-Section"],
            snapshot["chapters"][0]["section_keys"],
        )


class TranslationMergeTests(unittest.TestCase):
    def test_only_changed_text_node_is_replaced(self) -> None:
        baseline = extract_snapshot(str(FIXTURES / "baseline.html"), 2)
        candidate = extract_snapshot(str(FIXTURES / "changed-text.html"), 2)
        localized = """<!doctype html><html><body>
        <h2 class="manual-toc-title" id="manual-toc-title">Содержание</h2>
        <p class="manual-toc-edition">Русская редакция — 23.07.2026</p>
        <a class="manual-toc-row manual-toc-row-section" href="#target-global"><span class="manual-toc-label">Основные возможности</span><span class="manual-toc-page">4</span></a>
        <div><section><div><h2 data-source-id="ch01-0001" id="heading-ch01-0001">01 Добро пожаловать</h2><p>Введение.</p></div></section></div>
        <div id="Global-Features"><h3 id="target-global">Основные возможности</h3><p>Мощный процессор.</p></div>
        <div id="Cloud"><h3>Cortex Cloud</h3><p>Просматривайте пресеты.</p></div>
        <div><section><div><h2 data-source-id="ch02-0001" id="heading-ch02-0001">02 Обзор</h2><p>Обзор устройства.</p></div></section></div>
        <div id="Dimensions"><h3>Размеры устройства</h3><p>Ширина — <strong>29 см</strong>.</p></div>
        </body></html>"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "document.html"
            document.write_text(localized, encoding="utf-8")
            assets = root / "assets"
            assets.mkdir()
            state = {"snapshot": baseline}
            result = apply_safe_update(
                state=state,
                candidate_snapshot=candidate,
                changed_keys=["section:Global-Features"],
                document_path=document,
                asset_directory=assets,
                translations_by_unit={
                    "section:Global-Features": {"text:0": "Более мощный процессор."}
                },
                edition_date="2026-08-23",
            )
            output = document.read_text(encoding="utf-8")
            self.assertIn("Более мощный процессор.", output)
            self.assertIn("Просматривайте пресеты.", output)
            self.assertIn("23.08.2026", output)
            self.assertEqual(1, result["translated_fragment_count"])


class RepositoryPolicyTests(unittest.TestCase):
    def test_public_filename_contains_version_and_revision_date(self) -> None:
        config = {
            "pdf_name_template": "Quad_Cortex_User_Manual_RU_v{version}_rev{date}.pdf"
        }
        self.assertEqual(
            "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf",
            expected_pdf_name(config, "4.1.0", "2026-08-23"),
        )
        self.assertEqual(
            ("4.1.0", "2026-08-23"),
            parse_pdf_name(
                config,
                "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf",
            ),
        )

    def test_readme_status_block_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "README.md"
            path.write_text(
                "before\n<!-- MANUAL_STATUS:START -->\nold\n<!-- MANUAL_STATUS:END -->\nafter\n",
                encoding="utf-8",
            )
            update_readme(
                path,
                rows=[
                    {
                        "category": "Устройство",
                        "display_name": "Quad Cortex",
                        "version": "4.1.0",
                        "edition_date": "23.08.2026",
                        "pdf_path": (
                            "manuals/quad-cortex/"
                            "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf"
                        ),
                        "source_url": "https://example.test/manual",
                    },
                    {
                        "category": "Плагин",
                        "display_name": "Example Plugin",
                        "version": "1.2.0",
                        "edition_date": "24.08.2026",
                        "pdf_path": "manuals/example-plugin/Example_Plugin_RU_v1.2.0_rev2026-08-24.pdf",
                        "source_url": "https://example.test/plugin",
                    },
                ],
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("4.1.0", content)
            self.assertIn("23.08.2026", content)
            self.assertIn("Русская редакция", content)
            self.assertIn("Опубликовано", content)
            self.assertIn("| Устройство | Quad Cortex |", content)
            self.assertIn("| Плагин | Example Plugin |", content)
            self.assertIn("[Скачать PDF]", content)
            self.assertIn(
                "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf?raw=1",
                content,
            )
            self.assertNotIn("\nold\n", content)
            self.assertTrue(content.startswith("before\n"))
            self.assertTrue(content.endswith("\nafter\n"))

    def test_readme_status_block_rejects_reversed_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "README.md"
            path.write_text(
                "<!-- MANUAL_STATUS:END -->\nold\n<!-- MANUAL_STATUS:START -->\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RepositoryPolicyError, "расположены в неверном порядке"
            ):
                update_readme(
                    path,
                    rows=[
                        {
                            "category": "Устройство",
                            "display_name": "Quad Cortex",
                            "version": "4.1.0",
                            "edition_date": "23.08.2026",
                            "pdf_path": (
                                "manuals/quad-cortex/"
                                "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf"
                            ),
                            "source_url": "https://example.test/manual",
                        }
                    ],
                )

    def test_manual_catalog_reads_every_published_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            config_directory = repository / ".github" / "manuals"
            config_directory.mkdir(parents=True)
            fixtures = [
                {
                    "slug": "quad-cortex",
                    "display_name": "Quad Cortex",
                    "category": "Устройство",
                    "source_url": "https://example.test/quad-cortex",
                    "pdf_directory": "manuals/quad-cortex",
                    "pdf_name_template": "Quad_Cortex_RU_v{version}_rev{date}.pdf",
                    "filename": "Quad_Cortex_RU_v4.0.0_rev2026-07-23.pdf",
                },
                {
                    "slug": "example-plugin",
                    "display_name": "Example Plugin",
                    "category": "Плагин",
                    "source_url": "https://example.test/plugin",
                    "pdf_directory": "manuals/example-plugin",
                    "pdf_name_template": "Example_Plugin_RU_v{version}_rev{date}.pdf",
                    "filename": "Example_Plugin_RU_v1.2.0_rev2026-08-24.pdf",
                },
            ]
            for item in fixtures:
                config = {
                    key: value for key, value in item.items() if key != "filename"
                }
                (config_directory / f"{item['slug']}.json").write_text(
                    json.dumps(config), encoding="utf-8"
                )
                pdf_directory = repository / item["pdf_directory"]
                pdf_directory.mkdir(parents=True)
                (pdf_directory / item["filename"]).write_bytes(b"%PDF")

            rows = manual_catalog(repository)

            self.assertEqual(
                ["Example Plugin", "Quad Cortex"],
                [row["display_name"] for row in rows],
            )
            self.assertEqual("24.08.2026", rows[0]["edition_date"])
            self.assertEqual("23.07.2026", rows[1]["edition_date"])

    def test_repository_readme_keeps_search_content_and_update_markers(self) -> None:
        content = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        self.assertEqual(1, content.count("<!-- MANUAL_STATUS:START -->"))
        self.assertEqual(1, content.count("<!-- MANUAL_STATUS:END -->"))
        self.assertLess(
            content.index("<!-- MANUAL_STATUS:START -->"),
            content.index("<!-- MANUAL_STATUS:END -->"),
        )
        self.assertIn("# Руководства Neural DSP на русском языке", content)
        self.assertIn("устройств, плагинов и программ Neural DSP", content)
        self.assertIn("| Устройство | Quad Cortex |", content)
        self.assertIn("неофициальные русские переводы", content)

    def test_manual_directory_rejects_non_pdf_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (
                directory / "Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf"
            ).write_bytes(b"pdf")
            (directory / "source.html").write_text("not allowed", encoding="utf-8")
            with self.assertRaises(RepositoryPolicyError):
                validate_manual_directory(
                    directory, "Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf"
                )

    def test_state_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "bad")
            with self.assertRaises(StateError):
                safe_extract(archive, root / "state")


class WorkflowContractTests(unittest.TestCase):
    @staticmethod
    def _run_release_metadata(changed_files: list[dict[str, str]]) -> str:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "release-manual.yml"
        ).read_text(encoding="utf-8")
        script = workflow.split(
            """python - <<'PY' >> "$GITHUB_OUTPUT"
""",
            1,
        )[1].split("\n          PY", 1)[0]
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "example/manuals",
                    "PR_NUMBER": "42",
                },
                clear=False,
            ),
            patch("pathlib.Path.cwd", return_value=REPOSITORY),
            patch(
                "subprocess.check_output",
                side_effect=[
                    json.dumps([changed_files]),
                    str(len(changed_files)),
                ],
            ),
            patch("sys.stdout", output),
        ):
            exec(compile(textwrap.dedent(script), "release-metadata", "exec"), {})
        return output.getvalue()

    def test_release_publishes_only_the_changed_verified_pdf(self) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "release-manual.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("validate-repository", workflow)
        self.assertIn('"manuals/**/*.pdf"', workflow)
        self.assertIn("Expected exactly one non-removed manual PDF", workflow)
        self.assertIn('changed_file.get("status") != "removed"', workflow)
        self.assertIn('pdf_posix_path.parts[0] != "manuals"', workflow)
        self.assertIn("(repository / pdf_path).is_file()", workflow)
        self.assertIn('config["release_tag_template"].format', workflow)
        self.assertIn('gh release upload "$TAG" "$PDF_PATH" --clobber', workflow)
        self.assertIn('gh release create "$TAG" "$PDF_PATH"', workflow)
        self.assertIn(
            "$DISPLAY_NAME $VERSION — инструкция на русском языке (PDF)", workflow
        )
        self.assertIn(
            "Полное руководство пользователя Neural DSP $DISPLAY_NAME", workflow
        )
        self.assertNotIn("automation-state", workflow)
        self.assertNotIn("STATE_TAG", workflow)
        self.assertNotIn("STATE_ASSET", workflow)
        self.assertNotIn("validate-state-pdf", workflow)
        self.assertNotIn("gh run download", workflow)

    def test_release_reads_the_complete_pr_file_list_independent_of_merge_method(
        self,
    ) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "release-manual.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("pull-requests: read", workflow)
        self.assertIn('"--paginate"', workflow)
        self.assertIn('"--slurp"', workflow)
        self.assertIn('f"{endpoint}/files?per_page=100"', workflow)
        self.assertIn('"--jq",\n                      ".changed_files"', workflow)
        self.assertIn("paginated file list", workflow)
        self.assertNotIn('"git",\n                  "diff"', workflow)
        self.assertNotIn('f"{merge_sha}^1"', workflow)

    def test_release_allows_replacing_an_old_pdf_only_in_the_same_directory(
        self,
    ) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "release-manual.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("pdf_candidates.append((changed_file, current_pdf_path))", workflow)
        self.assertIn("if len(pdf_candidates) != 1", workflow)
        self.assertIn("pdf_directory = pdf_posix_path.parent", workflow)
        self.assertIn("previous_pdf_path.parent != pdf_directory", workflow)
        self.assertIn('changed_file.get("status") != "removed"', workflow)
        self.assertIn("path.parent != pdf_directory", workflow)
        self.assertIn(
            "Removed PDF is outside the selected manual directory",
            workflow,
        )
        self.assertIn(
            "Refusing to release with another changed or renamed PDF",
            workflow,
        )

    def test_release_version_replacement_selects_only_the_new_pdf(self) -> None:
        published_pdf = next(
            path
            for path in sorted(REPOSITORY.glob("manuals/*/*.pdf"))
            if path.is_file()
        )
        candidate = published_pdf.relative_to(REPOSITORY).as_posix()
        old_pdf = (
            f"{published_pdf.parent.relative_to(REPOSITORY).as_posix()}/"
            "Old_User_Manual_RU_v0.9.0_rev2026-01-01.pdf"
        )

        initial_output = self._run_release_metadata(
            [{"filename": candidate, "status": "added"}]
        )
        replacement_output = self._run_release_metadata(
            [
                {"filename": old_pdf, "status": "removed"},
                {"filename": candidate, "status": "added"},
            ]
        )

        self.assertIn(f"pdf={candidate}", initial_output)
        self.assertIn(f"pdf={candidate}", replacement_output)

        with self.assertRaisesRegex(
            SystemExit,
            "Removed PDF is outside the selected manual directory",
        ):
            self._run_release_metadata(
                [
                    {
                        "filename": "manuals/another-manual/Old.pdf",
                        "status": "removed",
                    },
                    {"filename": candidate, "status": "added"},
                ]
            )

        with self.assertRaisesRegex(
            SystemExit,
            "renamed from another directory",
        ):
            self._run_release_metadata(
                [
                    {
                        "filename": candidate,
                        "previous_filename": "manuals/another-manual/Old.pdf",
                        "status": "renamed",
                    }
                ]
            )

    def test_pull_requests_have_a_required_validation_workflow(self) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "validate-pull-request.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("name: Проверка репозитория", workflow)
        self.assertIn("python -m unittest discover -s .github/tests -v", workflow)
        self.assertIn("validate-repository", workflow)
        self.assertIn("contents: read", workflow)

    def test_public_readme_contains_only_reader_facing_information(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

        self.assertIn("устройств, плагинов и программ Neural DSP", readme)
        self.assertIn("Quad Cortex** и **Nano Cortex", readme)
        self.assertIn("## Руководство Nano Cortex", readme)
        self.assertIn("В каталог попадают только законченные переводы", readme)
        self.assertFalse((REPOSITORY / "docs" / "AUTOMATION.md").exists())


if __name__ == "__main__":
    unittest.main()
