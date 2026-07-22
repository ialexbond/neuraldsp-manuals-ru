from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import fitz


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / ".github" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

from manual_automation.canonical import extract_snapshot  # noqa: E402
from manual_automation.diffing import classify_change  # noqa: E402
from manual_automation.pdf import _chapter_opener_audit, pdf_metrics  # noqa: E402
from manual_automation.repository import (  # noqa: E402
    RepositoryPolicyError,
    expected_pdf_name,
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

        def fake_translator(items, _title):
            return {item["id"]: "Более мощный процессор." for item in items}

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
                translator=fake_translator,
                edition_date="2026-08-23",
            )
            output = document.read_text(encoding="utf-8")
            self.assertIn("Более мощный процессор.", output)
            self.assertIn("Просматривайте пресеты.", output)
            self.assertIn("23.08.2026", output)
            self.assertEqual(1, result["translated_fragment_count"])


class RepositoryPolicyTests(unittest.TestCase):
    def test_public_filename_contains_version_and_revision_date(self) -> None:
        config = {"pdf_name_template": "Quad_Cortex_User_Manual_RU_v{version}_rev{date}.pdf"}
        self.assertEqual(
            "Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf",
            expected_pdf_name(config, "4.1.0", "2026-08-23"),
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
                version="4.1.0",
                edition_date="2026-08-23",
                pdf_path="manuals/quad-cortex/Quad_Cortex_User_Manual_RU_v4.1.0_rev2026-08-23.pdf",
                source_url="https://example.test/manual",
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("4.1.0", content)
            self.assertIn("2026-08-23", content)
            self.assertNotIn("\nold\n", content)

    def test_manual_directory_rejects_non_pdf_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf").write_bytes(b"pdf")
            (directory / "source.html").write_text("not allowed", encoding="utf-8")
            with self.assertRaises(RepositoryPolicyError):
                validate_manual_directory(directory, "Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf")

    def test_state_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "bad")
            with self.assertRaises(StateError):
                safe_extract(archive, root / "state")


class WorkflowContractTests(unittest.TestCase):
    def test_check_workflow_is_manual_only_and_has_no_paid_api_contract(self) -> None:
        check_workflow = (REPOSITORY / ".github" / "workflows" / "check-quad-cortex.yml").read_text(
            encoding="utf-8"
        )
        release_workflow = (
            REPOSITORY / ".github" / "workflows" / "release-quad-cortex.yml"
        ).read_text(encoding="utf-8")

        self.assertRegex(
            check_workflow,
            r"(?m)^on:\n  workflow_dispatch:\n\npermissions:",
        )
        self.assertNotIn("schedule:", check_workflow)
        self.assertNotIn("cron:", check_workflow)
        self.assertIn("contents: read", check_workflow)
        self.assertNotIn("contents: write", check_workflow)
        self.assertNotIn("OPENAI_API_KEY", check_workflow)
        self.assertNotIn("OPENAI_MODEL", check_workflow)
        self.assertNotIn("manual_sync.py update", check_workflow)
        self.assertIn("group: quad-cortex-manual-state", check_workflow)
        self.assertIn("group: quad-cortex-manual-state", release_workflow)

    def test_manual_check_is_read_only_diagnostics(self) -> None:
        workflow = (REPOSITORY / ".github" / "workflows" / "check-quad-cortex.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("manual_sync.py check", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertNotIn("manual_sync.py publish", workflow)
        self.assertNotIn("git commit", workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("gh issue", workflow)
        self.assertNotIn("gh pr", workflow)

    def test_release_binds_candidate_state_to_the_merged_pdf(self) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "release-quad-cortex.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("validate-state-pdf", workflow)
        self.assertIn('--state-archive "$RUNNER_TEMP/state-candidate/$STATE_ASSET"', workflow)
        self.assertIn('--edition-date "$EDITION_DATE"', workflow)
        self.assertIn("steps.state_pdf.outcome == 'success'", workflow)
        self.assertIn("codex/bootstrap-quad-cortex-automation", workflow)
        self.assertIn("mode=bootstrap", workflow)

    def test_pull_requests_have_a_required_validation_workflow(self) -> None:
        workflow = (
            REPOSITORY / ".github" / "workflows" / "validate-pull-request.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("name: Repository validation", workflow)
        self.assertIn("python -m unittest discover -s .github/tests -v", workflow)
        self.assertIn("validate-repository", workflow)
        self.assertIn("contents: read", workflow)

    def test_operational_documentation_assigns_scheduling_to_codex(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        documentation = (REPOSITORY / "docs" / "AUTOMATION.md").read_text(encoding="utf-8")

        self.assertIn(
            "local Codex automation is the **only automatic scheduler and source-change detector**",
            readme,
        )
        self.assertIn("existing Codex plan", readme)
        self.assertIn("does not use `OPENAI_API_KEY`", readme)
        self.assertIn("has no schedule", readme)

        self.assertIn(
            "local Codex automation is the **only automatic scheduler and source-change detector**",
            documentation,
        )
        self.assertIn("diagnostic backup only", documentation)
        self.assertIn("exactly one trigger: `workflow_dispatch`", documentation)
        self.assertIn("does **not** use `OPENAI_API_KEY`", documentation)
        self.assertIn("separately billed translation API", documentation)
        self.assertNotIn("60 days", documentation)
        self.assertIn("$env:PYTHONUTF8='1'", documentation)
        self.assertIn(
            "Human review and an explicit merge are mandatory", documentation
        )


if __name__ == "__main__":
    unittest.main()
