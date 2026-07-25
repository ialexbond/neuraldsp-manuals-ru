from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from manual_automation.cli import build_parser, command_check_all  # noqa: E402
from manual_automation.state import write_json  # noqa: E402


class CheckAllCommandTests(unittest.TestCase):
    @staticmethod
    def _arguments(root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            config_directory=str(root / "configs"),
            automation_directory=str(root / ".automation"),
            repository=str(root),
            result=str(root / "aggregate.json"),
        )

    @staticmethod
    def _write_config(
        root: Path, slug: str, catalog_order: int
    ) -> Path:
        path = root / "configs" / f"{slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "slug": slug,
                    "display_name": slug.title(),
                    "catalog_order": catalog_order,
                    "source_url": f"https://example.test/{slug}",
                    "expected_chapter_count": 1,
                    "pdf_directory": f"manuals/{slug}",
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _touch_archive(root: Path, slug: str) -> None:
        archive = root / ".automation" / f"{slug}-state-v1.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"test")

    def test_parser_exposes_check_all(self) -> None:
        args = build_parser().parse_args(
            [
                "check-all",
                "--config-directory",
                ".github/manuals",
                "--automation-directory",
                ".automation",
            ]
        )

        self.assertEqual("check-all", args.command)
        self.assertIs(args.handler, command_check_all)

    def test_all_manuals_are_checked_in_catalog_order_and_errors_do_not_stop_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_config(root, "second", 20)
            self._write_config(root, "first", 10)
            self._write_config(root, "third", 30)
            for slug in ("first", "second", "third"):
                self._touch_archive(root, slug)
            checked: list[str] = []

            def fake_check_manual(**kwargs: object) -> dict[str, object]:
                config = kwargs["config"]
                assert isinstance(config, dict)
                slug = str(config["slug"])
                checked.append(slug)
                if slug == "second":
                    raise RuntimeError("source unavailable")
                snapshot = Path(str(kwargs["snapshot_output"]))
                write_json(snapshot, {"slug": slug})
                return {"schema_version": 1, "status": "unchanged"}

            with (
                patch(
                    "manual_automation.cli._check_manual",
                    side_effect=fake_check_manual,
                ),
                patch(
                    "manual_automation.cli._validate_published_state",
                    return_value={"status": "matching"},
                ),
                patch("sys.stdout", new=io.StringIO()),
            ):
                exit_code = command_check_all(self._arguments(root))

            result = json.loads(
                (root / "aggregate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", result["status"])
            self.assertEqual(["first", "second", "third"], checked)
            self.assertEqual(
                ["first", "second", "third"],
                [manual["slug"] for manual in result["manuals"]],
            )
            failed = result["manuals"][1]
            self.assertEqual("error", failed["status"])
            self.assertEqual("check_failed", failed["error_kind"])
            self.assertIn("source unavailable", failed["error"])
            for slug in ("first", "second", "third"):
                self.assertTrue(
                    (root / ".automation" / slug / "check" / "report.json").is_file()
                )

    def test_detected_changes_are_successful_and_have_separate_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_config(root, "safe", 20)
            self._write_config(root, "review", 10)
            for slug in ("safe", "review"):
                self._touch_archive(root, slug)

            def fake_check_manual(**kwargs: object) -> dict[str, object]:
                config = kwargs["config"]
                assert isinstance(config, dict)
                slug = str(config["slug"])
                snapshot = Path(str(kwargs["snapshot_output"]))
                write_json(snapshot, {"slug": slug})
                status = "safe_change" if slug == "safe" else "review_required"
                return {"schema_version": 1, "status": status}

            with (
                patch(
                    "manual_automation.cli._check_manual",
                    side_effect=fake_check_manual,
                ),
                patch(
                    "manual_automation.cli._validate_published_state",
                    return_value={"status": "matching"},
                ),
                patch("sys.stdout", new=io.StringIO()),
            ):
                exit_code = command_check_all(self._arguments(root))

            result = json.loads(
                (root / "aggregate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(0, exit_code)
            self.assertEqual("changes_detected", result["status"])
            self.assertEqual(
                ["review_required", "safe_change"],
                [manual["status"] for manual in result["manuals"]],
            )
            for manual in result["manuals"]:
                self.assertTrue(Path(manual["snapshot"]).is_file())
                self.assertTrue(Path(manual["report"]).is_file())

    def test_missing_archive_is_reported_and_other_manuals_are_still_checked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_config(root, "missing", 10)
            self._write_config(root, "available", 20)
            self._touch_archive(root, "available")
            checked: list[str] = []

            def fake_check_manual(**kwargs: object) -> dict[str, object]:
                config = kwargs["config"]
                assert isinstance(config, dict)
                slug = str(config["slug"])
                checked.append(slug)
                write_json(Path(str(kwargs["snapshot_output"])), {"slug": slug})
                return {"schema_version": 1, "status": "unchanged"}

            with (
                patch(
                    "manual_automation.cli._check_manual",
                    side_effect=fake_check_manual,
                ),
                patch(
                    "manual_automation.cli._validate_published_state",
                    return_value={"status": "matching"},
                ),
                patch("sys.stdout", new=io.StringIO()),
            ):
                exit_code = command_check_all(self._arguments(root))

            result = json.loads(
                (root / "aggregate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", result["status"])
            self.assertEqual(["available"], checked)
            missing = result["manuals"][0]
            self.assertEqual("error", missing["status"])
            self.assertEqual("missing_state_archive", missing["error_kind"])
            self.assertIn(
                "missing-state-v1.zip",
                json.loads(Path(missing["report"]).read_text(encoding="utf-8"))[
                    "error"
                ],
            )

    def test_state_pdf_mismatch_blocks_but_upstream_is_still_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_config(root, "drifted", 10)
            self._touch_archive(root, "drifted")
            checked: list[str] = []

            def fake_check_manual(**kwargs: object) -> dict[str, object]:
                config = kwargs["config"]
                assert isinstance(config, dict)
                slug = str(config["slug"])
                checked.append(slug)
                write_json(Path(str(kwargs["snapshot_output"])), {"slug": slug})
                return {"schema_version": 1, "status": "unchanged"}

            with (
                patch(
                    "manual_automation.cli._validate_published_state",
                    side_effect=RuntimeError(
                        "Candidate state does not match the merged PDF"
                    ),
                ),
                patch(
                    "manual_automation.cli._check_manual",
                    side_effect=fake_check_manual,
                ),
                patch("sys.stdout", new=io.StringIO()),
            ):
                exit_code = command_check_all(self._arguments(root))

            result = json.loads(
                (root / "aggregate.json").read_text(encoding="utf-8")
            )
            manual = result["manuals"][0]
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", result["status"])
            self.assertEqual(["drifted"], checked)
            self.assertEqual("unchanged", manual["status"])
            self.assertEqual("error", manual["state_pdf"]["status"])
            self.assertEqual(
                "state_pdf_mismatch",
                manual["state_pdf"]["error_kind"],
            )
            self.assertIn(
                "does not match",
                manual["state_pdf"]["error"],
            )
            self.assertTrue(Path(manual["snapshot"]).is_file())
            self.assertTrue(Path(manual["report"]).is_file())
            self.assertTrue(Path(manual["state_pdf_report"]).is_file())

    def test_empty_configuration_directory_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "configs").mkdir()
            with patch("sys.stdout", new=io.StringIO()):
                exit_code = command_check_all(self._arguments(root))

            result = json.loads(
                (root / "aggregate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", result["status"])
            self.assertEqual([], result["manuals"])
            self.assertIn("No manual configuration files", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
