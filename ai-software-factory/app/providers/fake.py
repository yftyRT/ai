"""Deterministic fake provider used by unit/e2e tests (no paid inference).

It understands the structured prompts produced by the planner / agents / reviewer and
returns valid JSON so the whole pipeline can run offline. Behaviour can be scripted:

* `handlers`      : {substring: fn(prompt) -> str}  - first matching handler wins
* `fail_next`     : number of upcoming calls that raise a retryable ProviderError
* `broken_once`   : task ids whose FIRST answer contains a syntax error (exercises the repair loop)
* `healthy`       : health() result
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Callable

from .base import ChatMessage, ChatResult, ModelProvider, ProviderError


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_ARCH_MD = (
    "# textstats architecture\n\n"
    "* `textstats/core.py` - `analyze(text) -> dict` returns chars/words/lines.\n"
    "* `textstats/__main__.py` - CLI printing the result as JSON.\n"
    "* `tests/` - unit tests.\n"
)

_PLAN = {
    "project_name": "textstats",
    "summary": "A small Python library and CLI that counts characters, words and lines of a text.",
    "language": "python",
    "architecture": _ARCH_MD,
    "tasks": [
        {"id": "arch", "title": "Architecture spec", "agent": "architect",
         "description": "Write ARCHITECTURE.md, spec/interfaces.md, .env.example and .gitignore.",
         "depends_on": [], "acceptance_criteria": ["ARCHITECTURE.md describes the modules", ".env.example exists"],
         "owned_files": ["ARCHITECTURE.md", "spec/**", ".env.example", ".gitignore"], "est_seconds": 30},
        {"id": "core", "title": "Core library and CLI", "agent": "backend",
         "description": "Implement textstats.core.analyze and the CLI in textstats/__main__.py.",
         "depends_on": ["arch"], "acceptance_criteria": ["analyze() returns counts", "CLI prints JSON"],
         "owned_files": ["textstats/**", "requirements.txt"], "est_seconds": 60},
        {"id": "tests", "title": "Unit tests", "agent": "tester",
         "description": "Write unit tests for analyze() and the CLI.",
         "depends_on": ["core"], "acceptance_criteria": ["tests pass"],
         "owned_files": ["tests/**", "conftest.py"], "est_seconds": 45},
        {"id": "docs", "title": "README", "agent": "docs",
         "description": "Write README.md with install, usage and test instructions.",
         "depends_on": ["core"], "acceptance_criteria": ["README documents usage"],
         "owned_files": ["README.md", "docs/**"], "est_seconds": 30},
    ],
}

_GOOD_CORE = (
    "def analyze(text: str) -> dict:\n"
    "    \"\"\"Return character, word and line counts.\"\"\"\n"
    "    return {'chars': len(text), 'words': len(text.split()), 'lines': len(text.splitlines())}\n"
)
_BAD_CORE = (  # valid syntax, wrong behaviour: only real tests can find it
    "def analyze(text: str) -> dict:\n"
    "    return {'chars': len(text), 'words': len(text), 'lines': len(text.splitlines())}\n")

_FILES: dict[str, dict[str, str]] = {
    "arch": {
        "ARCHITECTURE.md": _ARCH_MD,
        "spec/interfaces.md": "`analyze(text: str) -> dict` with keys chars, words, lines.\n",
        ".env.example": "# no environment variables are required\n",
        ".gitignore": "__pycache__/\n*.pyc\n.env\n.venv/\n",
    },
    "core": {
        "textstats/__init__.py": "from .core import analyze\n\n__all__ = ['analyze']\n",
        "textstats/core.py": _GOOD_CORE,
        "textstats/__main__.py": (
            "import argparse\nimport json\nimport sys\n\nfrom .core import analyze\n\n\n"
            "def main(argv=None):\n"
            "    p = argparse.ArgumentParser(prog='textstats', description='Count chars, words and lines')\n"
            "    p.add_argument('text', nargs='?', help='text to analyse (default: stdin)')\n"
            "    a = p.parse_args(argv)\n"
            "    text = a.text if a.text is not None else sys.stdin.read()\n"
            "    print(json.dumps(analyze(text)))\n\n\n"
            "if __name__ == '__main__':\n    main()\n"),
        "requirements.txt": "# no third-party runtime dependencies\n",
    },
    "tests": {
        "tests/__init__.py": "",
        "tests/test_core.py": (
            "import unittest\n\nfrom textstats import analyze\n\n\n"
            "class AnalyzeTests(unittest.TestCase):\n"
            "    def test_counts(self):\n"
            "        self.assertEqual(analyze('a b\\nc'), {'chars': 5, 'words': 3, 'lines': 2})\n\n"
            "    def test_empty(self):\n"
            "        self.assertEqual(analyze('')['words'], 0)\n\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n"),
    },
    "docs": {
        "README.md": "# textstats\n\nUsage: `python -m textstats 'hello world'`\n\nRun the tests: `python -m unittest discover`\n",
    },
}


class FakeProvider(ModelProvider):
    def __init__(self, model_id: str = "fake-model", latency_s: float = 0.0):
        self.model_id = model_id
        self.latency_s = latency_s
        self.handlers: dict[str, Callable[[str], str]] = {}
        self.calls: list[str] = []
        self.fail_next = 0
        self.broken_once: set[str] = set()
        self._broken_emitted: set[str] = set()
        self.healthy = True
        self.in_flight = 0
        self.max_in_flight_seen = 0

    async def health(self) -> tuple[bool, str]:
        return (True, "ready") if self.healthy else (False, "fake model is down")

    async def chat(self, messages: list[ChatMessage], *, max_tokens: int = 4096, temperature: float = 0.2,
                   json_mode: bool = False) -> ChatResult:
        prompt = "\n".join(m.content for m in messages)
        self.calls.append(prompt)
        self.in_flight += 1
        self.max_in_flight_seen = max(self.max_in_flight_seen, self.in_flight)
        try:
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ProviderError("simulated transient model failure", retryable=True)
            if not self.healthy:
                raise ProviderError("model server unavailable", retryable=True)
            for marker, fn in self.handlers.items():
                if marker in prompt:
                    text = fn(prompt)
                    break
            else:
                text = self._default(prompt)
            return ChatResult(text=text, prompt_tokens=_estimate_tokens(prompt), completion_tokens=_estimate_tokens(text))
        finally:
            self.in_flight -= 1

    # ------------------------------------------------------------------ defaults
    def _default(self, prompt: str) -> str:
        if "### REQUEST: PLAN" in prompt:
            return self._plan(prompt)
        if "### REQUEST: REVIEW" in prompt:
            return json.dumps({"summary": "Small, coherent project.", "findings": [
                {"severity": "low", "file": "README.md", "evidence": "no license section",
                 "explanation": "The README does not state a license.", "suggested_fix": "Add a license section."}]})
        if "### REQUEST: TASK" in prompt:
            return self._task(prompt)
        return json.dumps({"files": [], "notes": "unrecognised request"})

    def _plan(self, prompt: str) -> str:
        return json.dumps(_PLAN)

    def _task(self, prompt: str) -> str:
        m = re.search(r"^TASK_ID: (\S+)", prompt, re.M)
        tid = m.group(1) if m else ""
        files = dict(_FILES.get(tid, {}))
        if tid in self.broken_once and tid not in self._broken_emitted and "### REPAIR REQUEST" not in prompt:
            self._broken_emitted.add(tid)
            files["textstats/core.py"] = _BAD_CORE
        return json.dumps({"files": [{"path": p, "content": c} for p, c in files.items()], "notes": f"fake output for {tid}"})
