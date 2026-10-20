"""Edge TTS transient-failure retry.

The failure this locks down, from the live gateway log on 2026-10-20 03:01:12 UTC:

    aiohttp.client_exceptions.WSServerHandshakeError: 403, message='Invalid response status',
    url='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud...'
    WARNING agent.tool_executor: Tool text_to_speech returned error

One transient handshake refusal cost an entire evening tutoring session its audio. The identical
call succeeded minutes later, from the same host, on the same edge-tts 7.3.1 — so this is
flakiness at the vendor, not a configuration error, and it must be retried rather than propagated.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.tts_tool_providers import (
    _EDGE_RETRY_ATTEMPTS,
    _edge_save_with_retry,
    _is_transient_edge_failure,
)


class _Handshake403(Exception):
    """Shaped like aiohttp's WSServerHandshakeError: it carries a .status."""

    def __init__(self, status: int = 403):
        super().__init__("403, message='Invalid response status'")
        self.status = status


# Give the class the real name so the name-based branch is exercised too.
_Handshake403.__name__ = "WSServerHandshakeError"


class _FakeCommunicate:
    """``save()`` that fails a configurable number of times, then writes."""

    def __init__(self, failures: list[BaseException], *, write_bytes: int = 8):
        self._failures = list(failures)
        self._write_bytes = write_bytes
        self.save_calls = 0

    async def save(self, path: str) -> None:
        self.save_calls += 1
        if self._failures:
            raise self._failures.pop(0)
        with open(path, "wb") as handle:
            handle.write(b"\x00" * self._write_bytes)


def _run(communicate_factory, output_path, attempts=_EDGE_RETRY_ATTEMPTS):
    return asyncio.run(
        _edge_save_with_retry(communicate_factory, output_path, base_delay=0.01)
    )


class TestTransientClassification:
    @pytest.mark.parametrize("status", [403, 408, 429, 500, 502, 503, 504])
    def test_handshake_statuses_are_transient(self, status):
        assert _is_transient_edge_failure(_Handshake403(status)) is True

    def test_real_aiohttp_error_is_transient(self):
        """The actual class from the log line, not a stand-in."""
        aiohttp = pytest.importorskip("aiohttp")

        class _RequestInfo:  # WSServerHandshakeError only needs .real_url for str()
            real_url = "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud"

        exc = aiohttp.WSServerHandshakeError(
            _RequestInfo(), (), status=403, message="Invalid response status"
        )
        assert _is_transient_edge_failure(exc) is True

    @pytest.mark.parametrize("exc", [
        asyncio.TimeoutError(),
        ConnectionResetError("peer went away"),
    ])
    def test_network_errors_are_transient(self, exc):
        assert _is_transient_edge_failure(exc) is True

    @pytest.mark.parametrize("exc", [
        ValueError("unknown voice ja-JP-NotAVoice"),
        ImportError("edge_tts missing"),
        KeyError("voice"),
    ])
    def test_configuration_errors_are_NOT_retried(self, exc):
        """A bad voice or a missing module will fail identically three times; do not waste them."""
        assert _is_transient_edge_failure(exc) is False


class TestRetryBehaviour:
    def test_transient_failure_then_success(self, tmp_path):
        out = str(tmp_path / "take.mp3")
        fake = _FakeCommunicate([_Handshake403(403)])

        assert _run(lambda: fake, out) == out
        assert fake.save_calls == 2  # failed once, succeeded on the retry
        assert (tmp_path / "take.mp3").read_bytes()

    def test_two_transient_failures_then_success(self, tmp_path):
        out = str(tmp_path / "take.mp3")
        fake = _FakeCommunicate([_Handshake403(403), _Handshake403(429)])

        assert _run(lambda: fake, out) == out
        assert fake.save_calls == 3

    def test_exhausting_attempts_raises_the_last_error(self, tmp_path):
        out = str(tmp_path / "take.mp3")
        fake = _FakeCommunicate([_Handshake403(403)] * _EDGE_RETRY_ATTEMPTS)

        with pytest.raises(_Handshake403):
            _run(lambda: fake, out)
        assert fake.save_calls == _EDGE_RETRY_ATTEMPTS

    def test_configuration_error_raises_immediately(self, tmp_path):
        """No retry budget is spent on a failure that cannot succeed."""
        out = str(tmp_path / "take.mp3")
        fake = _FakeCommunicate([ValueError("unknown voice")])

        with pytest.raises(ValueError):
            _run(lambda: fake, out)
        assert fake.save_calls == 1

    def test_truncated_take_is_removed_before_retrying(self, tmp_path):
        """A half-written file from the failed attempt must not survive into the retry."""
        out = tmp_path / "take.mp3"
        out.write_bytes(b"TRUNCATED")  # simulate the partial write left by the 403

        class _Checking(_FakeCommunicate):
            async def save(self, path):
                self.save_calls += 1
                if self._failures:
                    raise self._failures.pop(0)
                # At this point the retry has already cleaned up; assert it happened.
                assert not out.exists(), "the truncated take was left behind"
                out.write_bytes(b"\x00" * 8)

        fake = _Checking([_Handshake403(403)])
        assert _run(lambda: fake, str(out)) == str(out)
        assert out.read_bytes() == b"\x00" * 8

    def test_success_on_first_attempt_does_not_sleep_or_retry(self, tmp_path):
        out = str(tmp_path / "take.mp3")
        fake = _FakeCommunicate([])

        assert _run(lambda: fake, out) == out
        assert fake.save_calls == 1
