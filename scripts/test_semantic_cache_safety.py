#!/usr/bin/env python3
"""Standalone unit tests for the semantic cache safety classifier.

Runnable directly::

    python3 ~/.hermes/scripts/test_semantic_cache_safety.py

Or via pytest::

    python3 -m pytest ~/.hermes/scripts/test_semantic_cache_safety.py -v

Tests the ``is_safe_to_cache`` and ``skip_reason`` functions from
``semantic_cache_safety`` with known-good and known-bad inputs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add ~/.hermes/scripts to sys.path so we can import the safety module
_SCRIPTS_DIR = Path(os.environ.get("HOME", "/home/vivojf")) / ".hermes" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import semantic_cache_safety as safety


# =========================================================================
# is_safe_to_cache — prompt-level checks
# =========================================================================


def test_empty_string():
    assert safety.is_safe_to_cache("") is False


def test_whitespace_only():
    assert safety.is_safe_to_cache("   \n\t  ") is False


def test_under_limit_is_safe():
    prompt = "What is the capital of France?"
    assert len(prompt) < safety._MAX_PROMPT_CHARS
    assert safety.is_safe_to_cache(prompt) is True


def test_over_limit_is_unsafe():
    prompt = "x" * (safety._MAX_PROMPT_CHARS + 1)
    assert safety.is_safe_to_cache(prompt) is False


def test_exactly_at_limit_is_safe():
    prompt = "x" * safety._MAX_PROMPT_CHARS
    assert safety.is_safe_to_cache(prompt) is True


def test_code_fence_in_prompt():
    assert safety.is_safe_to_cache("Write a function ```python\ndef f(): pass\n```") is False


def test_code_fence_in_response():
    assert safety.is_safe_to_cache("What is Python?", response="```python\nprint('hi')\n```") is False


# -------------------------------------------------------------------------
# Shell commands (parametrized via loop)
# -------------------------------------------------------------------------

_SHELL_COMMANDS = [
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
]


def test_shell_commands_are_unsafe():
    for cmd in _SHELL_COMMANDS:
        assert safety.is_safe_to_cache(cmd) is False, f"Expected unsafe: {cmd!r}"


# -------------------------------------------------------------------------
# Tool-call mentions
# -------------------------------------------------------------------------

_TOOL_CALLS = [
    "use a tool_call to fetch data",
    "make a tool call to the API",
    "tool_call: search for results",
]


def test_tool_call_mentions_are_unsafe():
    for prompt in _TOOL_CALLS:
        assert safety.is_safe_to_cache(prompt) is False


# -------------------------------------------------------------------------
# Multi-step agentic requests
# -------------------------------------------------------------------------

_MULTI_STEP = [
    "search for the latest news",
    "look up the weather in Tokyo",
    "find out who won the game",
    "find me a good restaurant",
    "go to the website and check",
    "navigate to the settings page",
    "fetch the data from the API",
    "scrape the product page",
    "crawl the website for links",
]


def test_multi_step_requests_are_unsafe():
    for prompt in _MULTI_STEP:
        assert safety.is_safe_to_cache(prompt) is False


# -------------------------------------------------------------------------
# File-path patterns
# -------------------------------------------------------------------------

_FILE_PATHS = [
    "check /etc/passwd",
    "read /var/log/syslog",
    "edit ~/.bashrc",
    "open /home/user/config.json",
]


def test_file_paths_are_unsafe():
    for prompt in _FILE_PATHS:
        assert safety.is_safe_to_cache(prompt) is False


# =========================================================================
# is_safe_to_cache — response-level checks
# =========================================================================

_ERROR_RESPONSES = [
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
]


def test_error_responses_are_unsafe():
    for resp in _ERROR_RESPONSES:
        assert safety.is_safe_to_cache("What happened?", response=resp) is False


_SHELL_IN_RESPONSE = [
    "You can run: $ curl http://example.com",
    "Try: $ ssh user@host",
    "Example: $ git push origin main",
    "Command: $ docker run nginx",
    "Run: $ kubectl get pods",
    "Install: $ npm install express",
    "Use: $ pip install requests",
    "Execute: $ sudo rm -rf /",
]


def test_shell_commands_in_response_are_unsafe():
    for resp in _SHELL_IN_RESPONSE:
        assert safety.is_safe_to_cache("How do I install X?", response=resp) is False


def test_empty_response():
    assert safety.is_safe_to_cache("Hello", response="") is False


def test_whitespace_response():
    assert safety.is_safe_to_cache("Hello", response="   \n  ") is False


# =========================================================================
# Safe cases (should return True)
# =========================================================================

_SAFE_Q_AND_A = [
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
]


def test_safe_q_and_a():
    for prompt, response in _SAFE_Q_AND_A:
        assert safety.is_safe_to_cache(prompt, response=response) is True


def test_safe_without_response():
    assert safety.is_safe_to_cache("What is the capital of France?") is True


# =========================================================================
# skip_reason
# =========================================================================


def test_skip_reason_empty_prompt():
    assert safety.skip_reason("") == "prompt: empty or whitespace-only"


def test_skip_reason_whitespace():
    assert "empty or whitespace-only" in safety.skip_reason("   \n  ")


def test_skip_reason_too_long():
    prompt = "x" * (safety._MAX_PROMPT_CHARS + 1)
    reason = safety.skip_reason(prompt)
    assert "too long" in reason
    assert str(len(prompt)) in reason


def test_skip_reason_code_fence():
    reason = safety.skip_reason("write code ```python\nprint('hi')\n```")
    assert "unsafe pattern" in reason
    assert "```" in reason


def test_skip_reason_shell_command():
    reason = safety.skip_reason("run curl http://example.com")
    assert "unsafe pattern" in reason


def test_skip_reason_safe_returns_empty():
    assert safety.skip_reason("What is the capital of France?") == ""


def test_skip_reason_safe_with_response():
    assert safety.skip_reason("What is 2+2?", response="4") == ""


def test_skip_reason_empty_response():
    reason = safety.skip_reason("Hello", response="")
    assert "response: empty" in reason


def test_skip_reason_error_in_response():
    reason = safety.skip_reason("What happened?", response="Traceback (most recent call last):")
    assert "response: matches unsafe pattern" in reason


# =========================================================================
# context parameter (reserved for future use)
# =========================================================================


def test_context_is_accepted_but_ignored():
    assert safety.is_safe_to_cache("What is 2+2?", context={"toolsets": []}) is True
    assert safety.is_safe_to_cache("", context={"toolsets": []}) is False


# =========================================================================
# Main — run all tests when executed directly
# =========================================================================

def _run_all():
    """Discover and run every test_* function in this module."""
    import inspect
    this_module = sys.modules[__name__]
    tests = [
        (name, fn)
        for name, fn in inspect.getmembers(this_module, inspect.isfunction)
        if name.startswith("test_")
    ]
    tests.sort(key=lambda t: t[0])

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")

    total = passed + failed
    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{total} passed")
    if failed:
        print(f"  {failed} FAILURE(S)")
        sys.exit(1)
    else:
        print("  ALL PASSED")


if __name__ == "__main__":
    _run_all()
