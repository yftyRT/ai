"""Content + license validation for imported Skills.

Imported Skills are UNTRUSTED input. These checks are deterministic rules; an LLM may help
rank quality elsewhere but is never the authority for security or licensing decisions.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .loader import NAME_RE, SkillFormatError, SkillLoader

# ----------------------------------------------------------------- licensing
PERMISSIVE = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "cc0-1.0", "unlicense", "0bsd",
    "cc-by-4.0", "cc-by-3.0", "python-2.0", "zlib", "bsl-1.0",
}
WEAK_COPYLEFT = {"mpl-2.0", "lgpl-2.1", "lgpl-3.0", "lgpl-2.1-only", "lgpl-3.0-only", "cc-by-sa-4.0", "cc-by-sa-3.0", "epl-2.0"}
STRONG_COPYLEFT = {"gpl-2.0", "gpl-3.0", "gpl-2.0-only", "gpl-3.0-only", "gpl-2.0-or-later", "gpl-3.0-or-later", "agpl-3.0", "agpl-3.0-only"}
ATTRIBUTION_REQUIRED = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "cc-by-4.0", "cc-by-3.0", "zlib", "0bsd"} - {"0bsd"}


@dataclass
class LicenseDecision:
    spdx: str
    category: str  # permissive | weak_copyleft | strong_copyleft | unknown | restricted
    allowed: bool  # may be imported at all
    auto_ok: bool  # may be activated automatically under auto_strict policy
    needs_attribution: bool
    reason: str


def check_license(spdx: Optional[str], allow_copyleft: bool = False) -> LicenseDecision:
    raw = (spdx or "").strip()
    key = raw.lower()
    if key in {"", "noassertion", "none", "other", "unknown", "null"}:
        return LicenseDecision(raw or "unknown", "unknown", False, False, False, "license is missing or unclear")
    if re.search(r"(^|[-_])nc([-_]|$)|non-?commercial|proprietary|commercial", key):
        return LicenseDecision(raw, "restricted", False, False, False, "non-commercial/proprietary license")
    if key in PERMISSIVE:
        return LicenseDecision(raw, "permissive", True, True, key in ATTRIBUTION_REQUIRED, "permissive license")
    if key in WEAK_COPYLEFT:
        return LicenseDecision(raw, "weak_copyleft", True, False, True, "weak copyleft: manual administrator approval only")
    if key in STRONG_COPYLEFT:
        if allow_copyleft:
            return LicenseDecision(raw, "strong_copyleft", True, False, True, "strong copyleft allowed by configuration; manual approval only")
        return LicenseDecision(raw, "strong_copyleft", False, False, True, "strong copyleft licenses are rejected by default")
    return LicenseDecision(raw, "unknown", False, False, False, f"unrecognised license identifier {raw!r}")


# ------------------------------------------------------------ content scanning
@dataclass
class ScanFinding:
    category: str
    severity: str  # reject | review | warn
    pattern: str
    excerpt: str


_RULES: list[tuple[str, str, str, str]] = [
    # (category, severity, label, regex)
    ("credential_theft", "reject", "read ssh keys", r"(?i)(cat|cp|tar|scp|curl[^\n]*@)\s*[^\n]*(~|\$HOME|/root|/home/\w+)/\.ssh"),
    ("credential_theft", "reject", "cloud credentials", r"(?i)\.aws/credentials|\.config/gcloud|\.kube/config|\.docker/config\.json|\.npmrc|\.netrc|\.git-credentials"),
    ("credential_theft", "reject", "dump environment", r"(?i)(printenv|\benv\b|\bset\b)\s*\|\s*(curl|nc|wget)|(cat|type)\s+[^\n]*\.env\b[^\n]*\|\s*(curl|nc)"),
    ("credential_theft", "reject", "collect tokens", r"(?i)(send|post|upload|exfiltrate|forward|email)\b[^\n]{0,60}\b(api[_ -]?keys?|tokens?|passwords?|secrets?|credentials)\b[^\n]{0,60}\b(to|at)\b\s+(https?://|a remote|an external|the attacker)"),
    ("exfiltration", "reject", "pipe to shell", r"(?i)(curl|wget)\s+[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
    ("exfiltration", "reject", "upload local data", r"(?i)curl\s+[^\n]*(-d|--data(-binary)?|-F|-T|--upload-file)\s+@"),
    ("exfiltration", "reject", "reverse shell", r"(?i)(nc|ncat|netcat)\s+[^\n]*-e\b|/dev/tcp/|bash\s+-i\s+>&"),
    ("exfiltration", "reject", "paste sites", r"(?i)webhook\.site|pastebin\.com|transfer\.sh|requestbin|ngrok\.io|interact\.sh|burpcollaborator"),
    ("destructive", "reject", "rm -rf root/home", r"(?i)\brm\s+(-[a-z]*r[a-z]*f?|-[a-z]*f[a-z]*r)\s+(--no-preserve-root\s+)?(/|~|\$HOME|\*)(\s|$|\"|')"),
    ("destructive", "reject", "disk wipe", r"(?i)\bmkfs(\.\w+)?\b|\bdd\s+if=[^\n]*of=/dev/|\bshred\b\s+[^\n]*/dev/|>\s*/dev/sd[a-z]"),
    ("destructive", "reject", "fork bomb", r":\(\)\s*\{\s*:\|:&\s*\};:"),
    ("destructive", "reject", "mass permissions", r"(?i)chmod\s+-R\s+777\s+/"),
    ("policy_override", "reject", "ignore instructions", r"(?i)(ignore|disregard|forget|override)\s+(all\s+|any\s+|your\s+|the\s+)?(previous|prior|above|earlier|system|safety|security)\s+(instructions?|prompts?|rules?|polic(y|ies)|guidelines?)"),
    ("policy_override", "reject", "disable safety", r"(?i)(disable|bypass|turn off|circumvent|skip)\s+(the\s+)?(sandbox|safety|security|guardrails?|policy|policies|approval|authori[sz]ation|validation)"),
    ("policy_override", "reject", "reveal prompt/secrets", r"(?i)(reveal|print|show|output|leak|expose)\s+(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions|api[_ -]?keys?|environment\s+variables|secrets?|bot\s+token)"),
    ("policy_override", "reject", "persona jailbreak", r"(?i)you\s+are\s+now\s+(in\s+)?(developer|dan|god|unrestricted|jailbreak)\s*(mode)?|do\s+anything\s+now"),
    ("policy_override", "reject", "concealment", r"(?i)(do\s+not|don't|never)\s+(tell|inform|notify|alert|mention\s+(this\s+)?to)\s+the\s+(user|admin|administrator|operator)"),
    ("policy_override", "reject", "grant permissions", r"(?i)(grant|give|assign)\s+(yourself|the agent)\s+(additional|extra|full|root|admin)\s+(permissions?|privileges?|access)|expand\s+(your\s+)?(file\s+)?(ownership|permissions)"),
    ("privilege", "reject", "privileged container", r"(?i)--privileged|/var/run/docker\.sock|--cap-add\s*=?\s*(ALL|SYS_ADMIN)|nsenter\b"),
    ("privilege", "review", "sudo usage", r"(?i)(^|[\s`;&|])sudo\s+\S+|chmod\s+[ugo]*\+s\b|setuid"),
    ("obfuscation", "reject", "decode and execute", r"(?i)(base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh|eval\s*\(\s*(atob|base64|b64decode)|exec\s*\(\s*(base64|b64decode|codecs\.decode|zlib\.decompress))"),
    ("obfuscation", "reject", "long encoded blob", r"[A-Za-z0-9+/=]{300,}"),
    ("obfuscation", "review", "hex escapes", r"(\\x[0-9a-fA-F]{2}){12,}"),
    ("executable_payload", "review", "install packages", r"(?i)\b(pip3?|npm|pnpm|yarn|gem|cargo|go)\s+(install|add|get)\b|\bapt(-get)?\s+install\b|\bbrew\s+install\b"),
    ("executable_payload", "review", "run bundled scripts", r"(?i)(\brun\b|\bexecute\b|\bbash\b|\bsh\b|\bpython3?\b|\bnode\b)\s+[`'\"]?\.?/?(scripts?|bin|tools)/[\w./\-]+\.(sh|py|js|ts|ps1)"),
    ("executable_payload", "review", "download tools", r"(?i)\b(curl|wget)\s+[^\n]*https?://"),
]
_COMPILED = [(c, s, l, re.compile(rx, re.MULTILINE)) for c, s, l, rx in _RULES]

# zero-width and bidi control characters used to hide instructions
_HIDDEN = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}


@dataclass
class ValidationReport:
    ok: bool = True
    rejected_reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)  # require administrator approval
    warnings: list[str] = field(default_factory=list)
    findings: list[ScanFinding] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def requires_admin_review(self) -> bool:
        return bool(self.review_reasons)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rejected_reasons": self.rejected_reasons,
            "review_reasons": self.review_reasons,
            "warnings": self.warnings,
            "size_bytes": self.size_bytes,
            "findings": [f.__dict__ for f in self.findings],
        }


def _excerpt(text: str, start: int, end: int) -> str:
    s = max(0, start - 20)
    e = min(len(text), end + 20)
    return text[s:e].replace("\n", " ")[:160]


def scan_content(text: str) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for cat, sev, label, rx in _COMPILED:
        for m in rx.finditer(text):
            findings.append(ScanFinding(cat, sev, label, _excerpt(text, m.start(), m.end())))
            break  # one hit per rule is enough
    hidden = [c for c in text if ord(c) in _HIDDEN]
    if hidden:
        findings.append(ScanFinding("obfuscation", "reject", "hidden/bidi control characters", f"{len(hidden)} characters"))
    ctrl = [c for c in text if unicodedata.category(c) == "Cc" and c not in "\n\r\t"]
    if ctrl:
        findings.append(ScanFinding("obfuscation", "reject", "control characters", f"{len(ctrl)} characters"))
    return findings


def validate_skill_content(
    text: str,
    *,
    max_bytes: int = 65536,
    expected_name: Optional[str] = None,
) -> ValidationReport:
    rep = ValidationReport()
    raw = text.encode("utf-8", "replace")
    rep.size_bytes = len(raw)
    if rep.size_bytes > max_bytes:
        rep.rejected_reasons.append(f"file too large: {rep.size_bytes} bytes > {max_bytes}")
    if rep.size_bytes == 0:
        rep.rejected_reasons.append("file is empty")
    if "\x00" in text:
        rep.rejected_reasons.append("binary content (NUL byte)")
    if rep.rejected_reasons:
        rep.ok = False
        return rep
    try:
        skill = SkillLoader(strict=False).parse(text)
        if not NAME_RE.match(skill.name):
            rep.rejected_reasons.append(f"unsafe skill name {skill.name!r}")
        if expected_name and skill.name != expected_name:
            rep.warnings.append(f"skill name {skill.name!r} differs from expected {expected_name!r}")
        if len(skill.body) < 80:
            rep.rejected_reasons.append("skill body is too short to be useful")
    except SkillFormatError as exc:
        rep.rejected_reasons.append(f"invalid SKILL.md format: {exc}")
    for f in scan_content(text):
        rep.findings.append(f)
        msg = f"{f.category}: {f.pattern} ({f.excerpt})"
        if f.severity == "reject":
            rep.rejected_reasons.append(msg)
        elif f.severity == "review":
            rep.review_reasons.append(msg)
        else:
            rep.warnings.append(msg)
    rep.ok = not rep.rejected_reasons
    return rep
