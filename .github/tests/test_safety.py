from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / ".github" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

from manual_automation.canonical import (  # noqa: E402
    _snapshot_integrity_hash,
    _source_html_sha256,
    extract_snapshot,
)
from manual_automation.cli import (  # noqa: E402
    _clean_work_directory,
    _translations,
    _validate_state_manual,
    _validate_update_inputs,
)
from manual_automation.diffing import classify_change  # noqa: E402
from manual_automation.translate import (  # noqa: E402
    TranslationError,
    _replace_inner_html,
    _validate_translated_items,
)


class TranslationSafetyTests(unittest.TestCase):
    def test_changed_numbers_are_rejected(self) -> None:
        items = [
            {
                "id": "text:0",
                "source": "Use USB 3 on Quad Cortex.",
                "previous_source": "Use USB 2 on Quad Cortex.",
                "previous_translation": "Используйте USB 2 на Quad Cortex.",
            }
        ]
        with self.assertRaises(TranslationError):
            _validate_translated_items(
                items, {"text:0": "Используйте USB 4 на Quad Cortex."}
            )

    def test_protected_product_name_is_rejected_when_missing(self) -> None:
        items = [
            {
                "id": "text:0",
                "source": "Connect Quad Cortex over USB.",
                "previous_source": "Connect Quad Cortex.",
                "previous_translation": "Подключите Quad Cortex.",
            }
        ]
        with self.assertRaises(TranslationError):
            _validate_translated_items(
                items, {"text:0": "Подключите устройство по USB."}
            )

    def test_nano_cortex_name_is_protected(self) -> None:
        items = [
            {
                "id": "text:0",
                "source": "Connect Nano Cortex over USB.",
                "previous_source": "Connect Nano Cortex.",
                "previous_translation": "Подключите Nano Cortex.",
            }
        ]
        with self.assertRaises(TranslationError):
            _validate_translated_items(
                items, {"text:0": "Подключите устройство по USB."}
            )

    def test_inline_markup_reordering_is_rejected(self) -> None:
        soup = BeautifulSoup("<p>Before <strong>value</strong>.</p>", "html.parser")
        paragraph = soup.p
        assert paragraph is not None
        with self.assertRaises(TranslationError):
            _replace_inner_html(
                paragraph,
                "<strong>значение</strong> перед текстом.",
                "До <strong>значения</strong>.",
            )


class TranslationInputTests(unittest.TestCase):
    def test_translation_file_must_match_changed_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "translations.json"
            path.write_text(
                json.dumps(
                    {
                        "units": {
                            "section:Global-Features": {
                                "text:0": "Обновлённый перевод."
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            translations = _translations(str(path), ["section:Global-Features"])

            self.assertEqual(
                "Обновлённый перевод.",
                translations["section:Global-Features"]["text:0"],
            )

    def test_translation_file_rejects_missing_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "translations.json"
            path.write_text(json.dumps({"units": {}}), encoding="utf-8")

            with self.assertRaises(RuntimeError):
                _translations(str(path), ["section:Global-Features"])


class WorkDirectorySafetyTests(unittest.TestCase):
    def test_repository_root_cannot_be_deleted(self) -> None:
        with self.assertRaises(RuntimeError):
            _clean_work_directory(str(REPOSITORY), "check")

    def test_marked_automation_directory_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / ".automation"
            work = parent / "check"
            work.mkdir(parents=True)
            (work / "sentinel.txt").write_text("temporary", encoding="utf-8")
            resolved = _clean_work_directory(str(work), "check")
            self.assertEqual(work.resolve(), resolved)
            self.assertFalse(work.exists())

    def test_manual_specific_automation_directory_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / ".automation" / "nano-cortex"
            work = parent / "check"
            work.mkdir(parents=True)
            (work / "sentinel.txt").write_text("temporary", encoding="utf-8")
            resolved = _clean_work_directory(str(work), "check", "nano-cortex")
            self.assertEqual(work.resolve(), resolved)
            self.assertFalse(work.exists())


class StateIdentityTests(unittest.TestCase):
    def test_state_archive_must_match_manual_slug(self) -> None:
        with self.assertRaises(RuntimeError):
            _validate_state_manual(
                {"manual": "quad-cortex"},
                {"slug": "nano-cortex"},
            )

    def test_matching_manual_slug_is_allowed(self) -> None:
        _validate_state_manual(
            {"manual": "nano-cortex"},
            {"slug": "nano-cortex"},
        )

    @staticmethod
    def _update_inputs() -> tuple[dict, dict, dict, dict]:
        config = {
            "source_url": "https://example.test/manual",
            "expected_chapter_count": 2,
            "minimum_section_count": 3,
            "maximum_changed_unit_count": 1,
            "maximum_changed_unit_ratio": 1.0,
        }
        baseline = extract_snapshot(str(FIXTURES / "baseline.html"), 2)
        candidate = extract_snapshot(str(FIXTURES / "changed-text.html"), 2)
        baseline["source_url"] = config["source_url"]
        baseline["upstream_version"] = "1.0.0"
        candidate["source_url"] = config["source_url"]
        candidate["upstream_version"] = "1.1.0"
        baseline["integrity_hash"] = _snapshot_integrity_hash(baseline)
        candidate["integrity_hash"] = _snapshot_integrity_hash(candidate)
        state = {"snapshot": baseline}
        report = classify_change(baseline, candidate, config)
        return state, candidate, report, config

    def test_update_report_must_match_current_baseline_hash(self) -> None:
        state, candidate, report, config = self._update_inputs()
        report["baseline_hash"] = "stale"

        with self.assertRaisesRegex(RuntimeError, "baseline_hash"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_report_must_match_supplied_candidate_hash(self) -> None:
        state, candidate, report, config = self._update_inputs()
        report["candidate_hash"] = "different-candidate"

        with self.assertRaisesRegex(RuntimeError, "candidate_hash"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_report_must_match_snapshot_identity_and_source(self) -> None:
        state, candidate, report, config = self._update_inputs()
        report["source_url"] = "https://example.test/other-manual"

        with self.assertRaisesRegex(RuntimeError, "report_source_url"):
            _validate_update_inputs(state, candidate, report, config)

    def test_matching_update_report_is_allowed(self) -> None:
        state, candidate, report, config = self._update_inputs()

        _validate_update_inputs(state, candidate, report, config)

    def test_update_report_change_lists_are_recomputed(self) -> None:
        state, candidate, report, config = self._update_inputs()
        report["changed_units"] = ["section:forged"]

        with self.assertRaisesRegex(RuntimeError, "changed_units"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_snapshots_must_use_configured_source(self) -> None:
        state, candidate, report, config = self._update_inputs()
        state["snapshot"]["source_url"] = "https://example.test/other-manual"
        state["snapshot"]["integrity_hash"] = _snapshot_integrity_hash(
            state["snapshot"]
        )

        with self.assertRaisesRegex(RuntimeError, "baseline_source_url"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_candidate_must_use_configured_source(self) -> None:
        state, candidate, report, config = self._update_inputs()
        candidate["source_url"] = "https://example.test/other-manual"
        candidate["integrity_hash"] = _snapshot_integrity_hash(candidate)

        with self.assertRaisesRegex(RuntimeError, "candidate_source_url"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_forged_data_attribute_in_source_html(self) -> None:
        state, candidate, report, config = self._update_inputs()
        unit = candidate["units"][0]
        unit["source_html"] = unit["source_html"].replace(
            ">", ' data-forged="true">', 1
        )

        with self.assertRaisesRegex(
            RuntimeError, "Candidate snapshot.*source_html_sha256"
        ):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_forged_script_in_source_html(self) -> None:
        state, candidate, report, config = self._update_inputs()
        unit = candidate["units"][0]
        unit["source_html"] = unit["source_html"].replace(
            "</p>", "</p><script>forged()</script>", 1
        )

        with self.assertRaisesRegex(
            RuntimeError, "Candidate snapshot.*source_html_sha256"
        ):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_rehashed_source_html_with_stale_integrity_hash(
        self,
    ) -> None:
        state, candidate, report, config = self._update_inputs()
        unit = candidate["units"][0]
        unit["source_html"] = unit["source_html"].replace(
            ">", ' data-forged="true">', 1
        )
        unit["source_html_sha256"] = _source_html_sha256(unit["source_html"])

        with self.assertRaisesRegex(RuntimeError, "Candidate snapshot.*integrity_hash"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_rehashed_snapshot_with_stale_report(self) -> None:
        state, candidate, report, config = self._update_inputs()
        unit = candidate["units"][0]
        unit["source_html"] = unit["source_html"].replace(
            "</p>", "</p><script>forged()</script>", 1
        )
        unit["source_html_sha256"] = _source_html_sha256(unit["source_html"])
        candidate["integrity_hash"] = _snapshot_integrity_hash(candidate)

        with self.assertRaisesRegex(RuntimeError, "candidate_integrity_hash"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_candidate_raw_tampering_with_unchanged_hashes(
        self,
    ) -> None:
        state, candidate, report, config = self._update_inputs()
        candidate["units"][0]["text_nodes"][0]["raw"] = "Forged candidate text."

        with self.assertRaisesRegex(RuntimeError, "Candidate snapshot.*text_nodes"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_candidate_text_tampering_with_unchanged_hashes(
        self,
    ) -> None:
        state, candidate, report, config = self._update_inputs()
        candidate["units"][0]["text_nodes"][0]["text"] = "Forged candidate text."

        with self.assertRaisesRegex(RuntimeError, "Candidate snapshot.*text_nodes"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_baseline_raw_tampering_with_unchanged_hashes(
        self,
    ) -> None:
        state, candidate, report, config = self._update_inputs()
        state["snapshot"]["units"][0]["text_nodes"][0]["raw"] = (
            "Forged baseline text."
        )

        with self.assertRaisesRegex(RuntimeError, "Baseline snapshot.*text_nodes"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_forged_unit_and_content_hashes(self) -> None:
        state, candidate, _report, config = self._update_inputs()
        candidate["units"][0]["hash"] = "0" * 64
        digest_input = "\n".join(
            f"{unit['key']}:{unit['hash']}" for unit in candidate["units"]
        )
        candidate["content_hash"] = hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()
        forged_report = classify_change(state["snapshot"], candidate, config)

        with self.assertRaisesRegex(RuntimeError, "Candidate snapshot.*hash"):
            _validate_update_inputs(state, candidate, forged_report, config)

    def test_update_rejects_forged_content_hash(self) -> None:
        state, candidate, _report, config = self._update_inputs()
        candidate["content_hash"] = "0" * 64
        forged_report = classify_change(state["snapshot"], candidate, config)

        with self.assertRaisesRegex(RuntimeError, "Candidate snapshot.*content_hash"):
            _validate_update_inputs(state, candidate, forged_report, config)

    def test_update_rejects_duplicate_unit_keys(self) -> None:
        state, candidate, report, config = self._update_inputs()
        candidate["units"].append(copy.deepcopy(candidate["units"][0]))

        with self.assertRaisesRegex(RuntimeError, "duplicate unit key"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_rejects_inconsistent_section_count(self) -> None:
        state, candidate, report, config = self._update_inputs()
        candidate["section_count"] += 1

        with self.assertRaisesRegex(RuntimeError, "section_count"):
            _validate_update_inputs(state, candidate, report, config)

    def test_update_allows_alignment_locator_metadata(self) -> None:
        state, candidate, report, config = self._update_inputs()
        state["snapshot"]["units"][0]["text_nodes"][0]["locator"] = {
            "attribute": "data-source-id",
            "value": "ch01-0001",
            "source_anchor_path": "root",
        }

        _validate_update_inputs(state, candidate, report, config)


if __name__ == "__main__":
    unittest.main()
