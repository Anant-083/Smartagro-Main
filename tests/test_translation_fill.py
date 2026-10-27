import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from fill_missing_translations import PICKER, SRC, main, parse_translations
from shared import LANG_NAMES


class TranslationFillTests(unittest.TestCase):
    def test_fill_script_covers_every_non_english_picker_language(self):
        self.assertEqual(set(PICKER), set(LANG_NAMES) - {"en"})

    def test_parser_reads_all_current_language_blocks_and_english_keys(self):
        parsed = parse_translations(SRC)

        self.assertTrue(set(LANG_NAMES) - {"sa"} <= set(parsed))
        self.assertGreaterEqual(len(parsed["en"]), 200)

    def test_help_option_exits_without_running_translation_generation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            main(["--help"])

        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("default: all non-", output.getvalue())
        self.assertIn("English languages", output.getvalue())

    def test_unknown_language_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
            main(["not-a-language"])

        self.assertEqual(exit_info.exception.code, 2)

    def test_missing_translation_provider_key_fails_before_generation(self):
        with (
            patch("fill_missing_translations.GEMINI_TRANSLATE_KEY", ""),
            patch("fill_missing_translations.GROQ_API_KEY", ""),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as exit_info,
        ):
            main(["hi"])

        self.assertEqual(exit_info.exception.code, 2)

    def test_generated_translation_asset_is_loaded_and_precached(self):
        root = Path(SRC).parents[2]
        generated_asset = root / "static" / "js" / "translations-generated.js"
        service_worker = (root / "static" / "service-worker.js").read_text(encoding="utf-8")
        for page in ("index.html", "diagnose.html", "market.html", "alerts.html", "offline.html"):
            template = (root / "templates" / page).read_text(encoding="utf-8")
            self.assertIn("translations-generated.js", template)

        self.assertTrue(generated_asset.is_file())
        self.assertIn("/static/js/translations-generated.js", service_worker)


if __name__ == "__main__":
    unittest.main()
