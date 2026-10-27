"""Pre-generate diagnosis UI and glossary translations, resumably.

Run from the project root with:
    python generate_diagnose_translations.py

Use one or more language codes to generate a subset. Translation API keys are
read from the same environment variables as the running application.
"""
import argparse
import time

from diagnose_translation_terms import DIAGNOSE_GLOSSARY_TERMS, DIAGNOSE_UI_TERMS
from shared import (
    GEMINI_TRANSLATE_KEY,
    GROQ_API_KEY,
    LANG_NAMES,
    _translate_terms_chunk_full,
    load_saved_translations,
    save_new_translations,
)

CHUNK_SIZE = 50
RETRY_PASSES = 3
PAUSE_BETWEEN_CHUNKS = 3
PAUSE_BETWEEN_PASSES = 15
TERMS = list(dict.fromkeys(DIAGNOSE_UI_TERMS + DIAGNOSE_GLOSSARY_TERMS))


def generate_language(lang_code):
    lang_name = LANG_NAMES[lang_code]
    for attempt in range(1, RETRY_PASSES + 1):
        saved = load_saved_translations(lang_code)
        missing = [term for term in TERMS if not saved.get(term)]
        if not missing:
            print(f"[{lang_code}] complete ({len(TERMS)} terms)")
            return

        print(f"[{lang_code}] pass {attempt}: {len(missing)} terms remain")
        for start in range(0, len(missing), CHUNK_SIZE):
            chunk = missing[start:start + CHUNK_SIZE]
            translated, returned = _translate_terms_chunk_full(
                chunk,
                lang_name,
                "Diagnosis UI and common crop-disease glossary. "
                "Use concise, natural wording for Indian farmers.",
                lang_code,
            )
            completed = {
                term: translated[term]
                for term in chunk
                if term in returned and translated.get(term, "").strip()
            }
            if completed:
                save_new_translations(lang_code, completed)
            print(f"   saved {len(completed)}/{len(chunk)} terms in this chunk")
            if start + CHUNK_SIZE < len(missing):
                time.sleep(PAUSE_BETWEEN_CHUNKS)

        remaining = [
            term for term in TERMS
            if not load_saved_translations(lang_code).get(term)
        ]
        if not remaining:
            print(f"[{lang_code}] complete ({len(TERMS)} terms)")
            return
        if attempt < RETRY_PASSES:
            time.sleep(PAUSE_BETWEEN_PASSES)

    missing = [
        term for term in TERMS
        if not load_saved_translations(lang_code).get(term)
    ]
    if missing:
        print(f"[{lang_code}] incomplete: {len(missing)} terms remain; rerun to resume")
    else:
        print(f"[{lang_code}] complete ({len(TERMS)} terms)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("languages", nargs="*", choices=sorted(LANG_NAMES))
    args = parser.parse_args()

    if not (GEMINI_TRANSLATE_KEY or GROQ_API_KEY):
        parser.error("Set GEMINI_API_KEY_TRANSLATE (or GEMINI_API_KEY) or GROQ_API_KEY first.")

    languages = args.languages or [code for code in LANG_NAMES if code != "en"]
    for lang_code in languages:
        if lang_code == "en":
            print("[en] source language; no translation file needed")
            continue
        generate_language(lang_code)


if __name__ == "__main__":
    main()
