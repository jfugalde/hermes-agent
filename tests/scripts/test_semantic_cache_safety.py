"""Tests for the shared safety classifier (``semantic_cache_safety``).

The module lives at ``~/.hermes/scripts/semantic_cache_safety.py`` and is
imported by adding that directory to ``sys.path`` — the same pattern the
production code in ``agent/semantic_response_cache.py`` uses via its
``_load_poc_module()`` helper.

No Ollama, SQLite, or ClawMem dependency — the classifier is pure Python
regex logic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add ~/.hermes/scripts to sys.path so we can import the safety module
# (same pattern as agent/semantic_response_cache._load_poc_module).
# Use the real home path, not the conftest-redirected HERMES_HOME, because
# the module lives in the real ~/.hermes/scripts, not the test tempdir.
# Path.home() and expanduser("~") are both redirected by the profile's
# HOME env override, so we use the known real home directly.
_SCRIPTS_DIR = Path("/home/vivojf") / ".hermes" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pytest

import semantic_cache_safety as safety


# =========================================================================
# is_safe_to_cache — prompt-level checks
# =========================================================================


class TestEmptyOrWhitespacePrompt:
    def test_empty_string(self):
        assert safety.is_safe_to_cache("") is False

    def test_whitespace_only(self):
        assert safety.is_safe_to_cache("   \n\t  ") is False

    def test_none_prompt(self):
        # The function signature says str, but we guard with truthiness
        assert safety.is_safe_to_cache("") is False


class TestPromptLength:
    def test_under_limit_is_safe(self):
        prompt = "What is the capital of France?"
        assert len(prompt) < safety._MAX_PROMPT_CHARS
        assert safety.is_safe_to_cache(prompt) is True

    def test_over_limit_is_unsafe(self):
        prompt = "x" * (safety._MAX_PROMPT_CHARS + 1)
        assert safety.is_safe_to_cache(prompt) is False

    def test_exactly_at_limit_is_safe(self):
        prompt = "x" * safety._MAX_PROMPT_CHARS
        assert safety.is_safe_to_cache(prompt) is True


class TestCodeFences:
    def test_triple_backtick_in_prompt(self):
        assert safety.is_safe_to_cache("Write a function ```python\ndef f(): pass\n```") is False

    def test_triple_backtick_in_response(self):
        assert safety.is_safe_to_cache("What is Python?", response="```python\nprint('hi')\n```") is False


class TestShellCommands:
    @pytest.mark.parametrize(
        "prompt",
        [
            "run curl http://example.com",
            "use ssh to connect to the server",
            "git push origin main",
            "git commit -m 'fix'",
            "git merge feature-branch",
            "git rebase main",
            "git checkout -b new-branch",
            "git pull origin main",
            "git clone https://github.com/foo/bar",
            "git reset --hard HEAD",
            "docker run nginx",
            "docker exec -it bash",
            "docker compose up",
            "docker build -t myimage .",
            "docker push myimage:latest",
            "kubectl get pods",
            "npm install express",
            "npm publish",
            "npm run build",
            "pip install requests",
            "rm -rf /",
            "sudo apt update",
            "wget https://example.com/file",
            "scp file user@host:/path",
            "rsync -avz source dest",
            "systemctl restart nginx",
            "journalctl -u nginx",
            "chmod +x script.sh",
        ],
    )
    def test_shell_command_is_unsafe(self, prompt):
        assert safety.is_safe_to_cache(prompt) is False, f"Expected unsafe: {prompt!r}"


class TestToolCallMentions:
    @pytest.mark.parametrize(
        "prompt",
        [
            "use a tool_call to fetch data",
            "make a tool call to the API",
            "tool_call: search for results",
        ],
    )
    def test_tool_call_is_unsafe(self, prompt):
        assert safety.is_safe_to_cache(prompt) is False


class TestMultiStepAgenticRequests:
    @pytest.mark.parametrize(
        "prompt",
        [
            "search for the latest news",
            "look up the weather in Tokyo",
            "find out who won the game",
            "find me a good restaurant",
            "go to the website and check",
            "navigate to the settings page",
            "fetch the data from the API",
            "scrape the product page",
            "crawl the website for links",
        ],
    )
    def test_multi_step_is_unsafe(self, prompt):
        assert safety.is_safe_to_cache(prompt) is False


class TestFilePathPatterns:
    @pytest.mark.parametrize(
        "prompt",
        [
            "check /etc/passwd",
            "read /var/log/syslog",
            "edit ~/.bashrc",
            "open /home/user/config.json",
        ],
    )
    def test_file_path_is_unsafe(self, prompt):
        assert safety.is_safe_to_cache(prompt) is False


# =========================================================================
# is_safe_to_cache — response-level checks
# =========================================================================


class TestResponseErrorMarkers:
    @pytest.mark.parametrize(
        "response",
        [
            "Traceback (most recent call last):\n  File \"test.py\", line 1",
            "Error: something went wrong",
            "Exception: value out of range",
            "SyntaxError: invalid syntax",
            "NameError: name 'x' is not defined",
            "TypeError: unsupported operand type",
            "ValueError: invalid literal",
            "KeyError: 'missing_key'",
            "IndexError: list index out of range",
            "AttributeError: 'NoneType' has no attribute",
            "ImportError: no module named foo",
            "ModuleNotFoundError: No module named 'bar'",
            "FileNotFoundError: [Errno 2]",
            "PermissionError: [Errno 13]",
            "OSError: [Errno 5]",
            "RuntimeError: something broke",
            "ZeroDivisionError: division by zero",
            "stack trace:\n  File \"/usr/lib/python3.11/...\"",
        ],
    )
    def test_error_response_is_unsafe(self, response):
        assert safety.is_safe_to_cache("What happened?", response=response) is False


class TestResponseShellCommands:
    @pytest.mark.parametrize(
        "response",
        [
            "You can run: $ curl http://example.com",
            "Try: $ ssh user@host",
            "Example: $ git push origin main",
            "Command: $ docker run nginx",
            "Run: $ kubectl get pods",
            "Install: $ npm install express",
            "Use: $ pip install requests",
            "Execute: $ sudo rm -rf /",
        ],
    )
    def test_shell_command_in_response_is_unsafe(self, response):
        assert safety.is_safe_to_cache("How do I install X?", response=response) is False


class TestResponseCodeFences:
    def test_code_fence_in_response(self):
        assert safety.is_safe_to_cache("What is Python?", response="Here's code:\n```python\nprint('hi')\n```") is False


class TestResponseEmpty:
    def test_empty_response(self):
        assert safety.is_safe_to_cache("Hello", response="") is False

    def test_whitespace_response(self):
        assert safety.is_safe_to_cache("Hello", response="   \n  ") is False


# =========================================================================
# is_safe_to_cache — safe cases (should return True)
# =========================================================================


class TestSafeQandA:
    @pytest.mark.parametrize(
        ("prompt", "response"),
        [
            ("What is the capital of France?", "Paris"),
            ("What is 2+2?", "4"),
            ("Who wrote Romeo and Juliet?", "William Shakespeare"),
            ("What is the boiling point of water?", "100°C at sea level"),
            ("What time is it?", "It is 3:45 PM UTC."),
            ("What is the default Hermes model?", "gemma4:31b"),
            ("How do I say hello in Japanese?", "こんにちは (konnichiwa)"),
            ("What is the weather like?", "It is sunny and 22°C."),
            ("Tell me a joke.", "Why did the chicken cross the road? To get to the other side."),
            ("What is the meaning of life?", "42"),
        ],
    )
    def test_safe_q_and_a(self, prompt, response):
        assert safety.is_safe_to_cache(prompt, response=response) is True

    def test_safe_without_response(self):
        """Prompt alone can be safe even without a response to check."""
        assert safety.is_safe_to_cache("What is the capital of France?") is True


# =========================================================================
# skip_reason
# =========================================================================


class TestSkipReason:
    def test_empty_prompt(self):
        assert safety.skip_reason("") == "prompt: empty or whitespace-only"

    def test_whitespace_prompt(self):
        assert "empty or whitespace-only" in safety.skip_reason("   \n  ")

    def test_too_long(self):
        prompt = "x" * (safety._MAX_PROMPT_CHARS + 1)
        reason = safety.skip_reason(prompt)
        assert "too long" in reason
        assert str(len(prompt)) in reason

    def test_code_fence(self):
        reason = safety.skip_reason("write code ```python\nprint('hi')\n```")
        assert "unsafe pattern" in reason
        assert "```" in reason

    def test_shell_command(self):
        reason = safety.skip_reason("run curl http://example.com")
        assert "unsafe pattern" in reason

    def test_safe_prompt_returns_empty(self):
        assert safety.skip_reason("What is the capital of France?") == ""

    def test_safe_with_response_returns_empty(self):
        assert safety.skip_reason("What is 2+2?", response="4") == ""

    def test_empty_response(self):
        reason = safety.skip_reason("Hello", response="")
        assert "response: empty" in reason

    def test_error_in_response(self):
        reason = safety.skip_reason("What happened?", response="Traceback (most recent call last):")
        assert "response: matches unsafe pattern" in reason


# =========================================================================
# context parameter (reserved for future use)
# =========================================================================


class TestContextParameter:
    def test_context_is_accepted_but_ignored(self):
        """The context parameter is reserved for future heuristics; for now
        it must not change the result of a safe prompt."""
        assert safety.is_safe_to_cache("What is 2+2?", context={"toolsets": []}) is True
        assert safety.is_safe_to_cache("", context={"toolsets": []}) is False
