"""Tests for cron.delivery_sanitizer.

The fixtures are the REAL payloads from the 2026-10-17..19 nihongo cron failures, not invented
ones. That is deliberate: an earlier fix in this project shipped 13 green tests against fixtures
its author made up, and the live data still broke it. These tests quote what the model actually
produced.
"""

from __future__ import annotations

import pytest

from cron.delivery_sanitizer import (
    DEFAULT_MAX_CHARS,
    degraded_run_notice,
    is_error_shaped,
    sanitize_agent_response,
    strip_prelude,
    truncate_repetition,
)


# ─── Real payload 1: the fabricated missing file ────────────────────────────────────────────
REAL_FABRICATED_ERROR = (
    "FileNotFound: No such file or directory: "
    "'/home/vivojf/.hermes/profiles/nihongo-jose/scripts/full-reading.py'"
)

# The file in that message existed, was executable, and ran correctly when tried by hand.
REAL_TTS_ERROR = (
    "FileNotFound: /home/vivojf/.hermes/profiles/nihongo-jose/cache/audio/"
    "tts_20261019_140157_831824.mp3"
)

# ─── Real payload 2: skill documentation echoed, lesson buried at the end ───────────────────
REAL_DOC_DUMP_PREFIX = """## Response Evaluation

When student replies to any session:

1. **Acknowledge effort first** (brief, in Spanish)
2. **Check accuracy**:
   - Particle errors → mark in memory, correct with "→"
   - Conjugation errors → correct + remind rule

## Session Structure

**Key Vocabulary**:
**Grammar**:
**Structure**:

---

## Session Structure

**Key Vocabulary**:
**Grammar**:
**Structure**:
"""

REAL_LESSON_BODY = """📐 文法ドリル — 名詞の 名詞

📖 Patrón: Nの N ||"el N de N" — posesión y origen||

Modelo: これは 北海道の おみやげです。
これは ほっかいどうの おみやげです。

||Esto es un souvenir de Hokkaido.||

🔄 Transforma estas frases:

1. これは わたしの カメラです。→ ahora es la cámara de Ale
2. それは ホセの かさですか。→ cambia ホセ por アレ

🔒 Respuestas:
1. ||これは アレさんの カメラです。||

📝 Mándame nota de voz, no texto. Quiero oírte."""


class TestErrorShaped:
    @pytest.mark.parametrize("payload", [REAL_FABRICATED_ERROR, REAL_TTS_ERROR])
    def test_real_failures_are_recognised(self, payload):
        assert is_error_shaped(payload) is True

    @pytest.mark.parametrize("payload", [REAL_FABRICATED_ERROR, REAL_TTS_ERROR])
    def test_error_bodies_never_reach_the_student(self, payload):
        """The whole point: a bare exception is NOT a lesson."""
        result = sanitize_agent_response(payload)
        assert result.text == ""
        assert result.degraded_reason == "error_shaped"

    def test_traceback_is_recognised(self):
        assert is_error_shaped("Traceback (most recent call last):\n  File ...") is True

    def test_errno_form_is_recognised(self):
        assert is_error_shaped("[Errno 2] No such file or directory: '/x'") is True

    def test_a_lesson_mentioning_an_error_is_NOT_flagged(self):
        """Narrow on purpose: the error must be the BODY, not a word inside it."""
        lesson = REAL_LESSON_BODY + "\n\nSi sale 'Error: timeout', reintenta."
        assert is_error_shaped(lesson) is False
        assert sanitize_agent_response(lesson).text != ""


class TestPreludeStripping:
    def test_real_doc_dump_is_removed_and_the_lesson_kept(self):
        result = sanitize_agent_response(REAL_DOC_DUMP_PREFIX + "\n" + REAL_LESSON_BODY)

        assert result.text.startswith("📐 文法ドリル")
        assert "## Response Evaluation" not in result.text
        assert "**Key Vocabulary**" not in result.text
        assert "📝 Mándame nota de voz" in result.text
        assert result.degraded_reason == "documentation_dump"
        assert result.removed_chars > 0

    def test_clean_message_is_untouched(self):
        result = sanitize_agent_response(REAL_LESSON_BODY)
        assert result.text == REAL_LESSON_BODY
        assert result.degraded_reason is None
        assert result.is_degraded is False

    def test_leading_markdown_headings_dropped_when_no_marker_present(self):
        text = "## Some Heading\n\nprose about the procedure\n\n---\n\nContenido real aquí."
        cleaned, dropped = strip_prelude(text)
        assert dropped is True
        assert cleaned.startswith("Contenido real")


class TestRepetition:
    def test_the_observed_25x_template_is_cut(self):
        """A template reproduced 25 times is the observed degeneration, not a lesson."""
        block = "**Key Vocabulary**: los mismos quince elementos de siempre aquí"
        payload = "📐 Lección\n\n" + "\n".join([block] * 25)

        result = sanitize_agent_response(payload)
        assert result.text.count(block) <= 3
        assert "repetition" in (result.degraded_reason or "")

    def test_separators_and_short_lines_may_repeat(self):
        """`---` and short bullets repeat legitimately; they must not be truncated."""
        payload = "📐 Lección\n\n" + ("uno\n---\n" * 20)
        cleaned, cut = truncate_repetition(payload)
        assert cut is False
        assert cleaned == payload


class TestSizeCap:
    def test_long_message_is_capped_and_marked(self):
        payload = "📐 Lección\n\n" + ("palabra " * (DEFAULT_MAX_CHARS // 4))
        result = sanitize_agent_response(payload)
        assert len(result.text) <= DEFAULT_MAX_CHARS + 100
        assert "recortado" in result.text
        assert "too_long" in (result.degraded_reason or "")

    def test_custom_cap_is_respected(self):
        result = sanitize_agent_response("📐 " + ("x" * 5000), max_chars=500)
        assert len(result.text) < 600


class TestEmptyAndNotice:
    @pytest.mark.parametrize("payload", [None, "", "   \n\n  "])
    def test_empty_input_is_reported(self, payload):
        assert sanitize_agent_response(payload).degraded_reason == "empty"

    def test_notice_names_the_job_and_the_reason(self):
        notice = degraded_run_notice("文法ドリル — José", "error_shaped")
        assert "文法ドリル — José" in notice
        assert "error_shaped" in notice
        assert "no se envió nada" in notice

    def test_notice_escapes_backticks_so_it_cannot_break_markdown(self):
        notice = degraded_run_notice("job`name", "error_shaped")
        assert "job`name" not in notice
        assert "job'name" in notice
