from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from manual_automation.cli import _clean_work_directory  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
