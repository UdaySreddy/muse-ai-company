"""Write-side content safety scan for the memory subsystem (single source of truth).

Direction-layer entries are compiled into the system prompt of **every** dispatched
agent, which makes `memory_add` an injection amplifier: one poisoned entry reaches
every future sub-agent of the team. The scan therefore runs at write time, where the
author is still present to fix the content — not at injection time, where it is both
too late and too hot a path.

Three families are checked:

- invisible Unicode (zero-width, bidi override, deprecated format chars, tag block)
  — the classic way to smuggle text a human reviewer cannot see;
- instruction-override phrasing (Chinese and English) and forged role/turn markers
  — the payload shape of a prompt-injection entry;
- credential shapes (private key headers, provider key formats, key=value secrets)
  — a direction entry is the worst place in the system to park a secret.

Task memos take the invisible-Unicode scan only: they are a high-frequency path and
are not compiled into anyone's system prompt.

Hooks never import this module. They are pure-stdlib processes by design, and the
pattern table must not be duplicated into them — the check belongs on the write side.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ================================================================
# Pattern tables
# ================================================================

# Invisible / formatting code points, as inclusive ranges. Anything here is
# unreadable to a human reviewer yet fully visible to the model, so it is rejected
# regardless of intent. Expressed as code points rather than a literal character
# class on purpose: literal invisible characters in the source would be unreviewable
# in exactly the way this check exists to prevent.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x200B, 0x200F),  # zero-width space/non-joiner/joiner + LTR/RTL marks
    (0x202A, 0x202E),  # bidi embedding / override
    (0x2060, 0x2064),  # word joiner + invisible operators
    (0x2066, 0x206F),  # bidi isolates + deprecated format characters
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xE0000, 0xE007F),  # Unicode tag block (ASCII smuggling)
)

# Instruction-override phrasing and forged conversation structure. Kept narrow on
# purpose: a legitimate direction entry *is* an instruction ("all output in Chinese"),
# so only the override/exfiltration shapes are matched, never plain imperatives.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction override (en)",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)*"
            r"(?:previous|prior|preceding|above|earlier|system|original)\s+"
            r"(?:instruction|instructions|prompt|prompts|rule|rules|message|messages)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction override (zh)",
        re.compile(
            r"(?:\u5ffd\u7565|\u65e0\u89c6|\u5fd8\u6389|\u5fd8\u8bb0|\u629b\u5f00"
            r"|\u8986\u76d6|\u4e0d\u8981\u7406\u4f1a|\u4e0d\u7528\u7406\u4f1a)"
            r"[^\u3002\uff1b\n]{0,8}"
            r"(?:\u4e0a\u9762|\u4ee5\u4e0a|\u4e0a\u8ff0|\u4e4b\u524d|\u5148\u524d"
            r"|\u524d\u9762|\u539f\u6709|\u6240\u6709|\u5168\u90e8)"
            r"[^\u3002\uff1b\n]{0,8}"
            r"(?:\u6307\u4ee4|\u6307\u793a|\u63d0\u793a\u8bcd|\u7cfb\u7edf\u63d0\u793a"
            r"|\u89c4\u5219|\u547d\u4ee4|\u7ea6\u675f|\u8bbe\u5b9a)"
        ),
    ),
    (
        "system prompt exfiltration (en)",
        re.compile(
            r"\b(?:reveal|print|show|output|repeat|dump|leak)\s+(?:me\s+)?"
            r"(?:your\s+|the\s+|all\s+)*"
            r"(?:system\s+prompt|system\s+message|initial\s+instructions|"
            r"hidden\s+instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "system prompt exfiltration (zh)",
        re.compile(
            r"(?:\u8f93\u51fa|\u6253\u5370|\u590d\u8ff0|\u6cc4\u9732|\u5c55\u793a"
            r"|\u544a\u8bc9\u6211|\u8d34\u51fa)[^\u3002\uff1b\n]{0,8}"
            r"(?:\u7cfb\u7edf\u63d0\u793a|\u7cfb\u7edf\u6307\u4ee4|\u521d\u59cb\u6307\u4ee4"
            r"|\u9690\u85cf\u6307\u4ee4|\u5b8c\u6574\u63d0\u793a\u8bcd)"
        ),
    ),
    (
        "forged role / turn marker",
        re.compile(r"<\|[^|>\n]{1,32}\|>|\[/?INST\]|<<\s*SYS\s*>>"),
    ),
    (
        "persona takeover",
        re.compile(
            r"\bfrom\s+now\s+on[, ]+you\s+are\b|\byou\s+are\s+now\s+(?:a|an|in)\b"
            r"|(?:\u4ece\u73b0\u5728\u8d77|\u4ece\u6b64\u4ee5\u540e|\u63a5\u4e0b\u6765)"
            r"[\uff0c,]?\s*\u4f60(?:\u5c31)?\u662f",
            re.IGNORECASE,
        ),
    ),
    (
        "jailbreak marker",
        re.compile(
            r"\bDAN\s+mode\b|\bjailbreak\b|\bdeveloper\s+mode\s+enabled\b"
            r"|\u8d8a\u72f1\u6a21\u5f0f|\u5f00\u53d1\u8005\u6a21\u5f0f\u5df2\u542f\u7528",
            re.IGNORECASE,
        ),
    ),
)

# Credential shapes. Matched values are never echoed back — only the family name and
# the offset, so the rejection message cannot become a second copy of the secret.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key header", re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-shaped API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{28,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    (
        "key=value secret",
        re.compile(
            r"(?:api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret"
            r"|password|passwd)\s*[:=]\s*[\"']?[A-Za-z0-9/+=_\-]{20,}",
            re.IGNORECASE,
        ),
    ),
)

_CATEGORY_ADVICE = {
    "invisible_unicode": (
        "Please remove the invisible characters and rewrite (usually carried in by copying from a web page/terminal) — "
        "content invisible to the naked eye is not"
        "allowed into storage: the reviewing human cannot see it, while a model reading the memory accepts it "
        "wholesale."
    ),
    "prompt_injection": (
        "Direction-layer entries are standing instructions for every dispatched agent and do not accept content in the "
        "shape of 'override existing instructions / extract the system prompt / "
        "forge dialogue roles'. If it really is a normal phrasing, please rewrite it without that sentence pattern."
    ),
    "credential": (
        "Credentials do not enter the memory layer. Please rewrite it as a 'pointer entry' — write only the trigger "
        "condition and the file path where the credential lives,"
        "and keep the secret itself in that file."
    ),
}


@dataclass(frozen=True)
class SafetyFinding:
    """One rejection reason: which family fired, and where."""

    category: str  # invisible_unicode / prompt_injection / credential
    pattern: str  # human-readable pattern name
    position: int  # character offset of the match in the scanned text
    excerpt: str = ""  # short excerpt, empty for credentials (never echo a secret)

    @property
    def message(self) -> str:
        """Rejection text handed back to the caller (agent-readable)."""
        head = f"Content-safety scan refused the write: matched {self.pattern} (at character {self.position + 1})"
        if self.excerpt:
            head += f": {self.excerpt}"
        return f"{head}. {_CATEGORY_ADVICE.get(self.category, '')}"


def _describe_char(ch: str) -> str:
    """Render one invisible code point as `U+XXXX (NAME)`."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "UNNAMED FORMAT CHARACTER"
    return f"U+{ord(ch):04X} ({name})"


def scan_invisible(text: str) -> SafetyFinding | None:
    """Scan for invisible/formatting code points. Returns the first finding or None."""
    for index, ch in enumerate(text or ""):
        code_point = ord(ch)
        if any(low <= code_point <= high for low, high in _INVISIBLE_RANGES):
            return SafetyFinding(
                category="invisible_unicode",
                pattern=f"invisible character {_describe_char(ch)}",
                position=index,
            )
    return None


def scan_direction_content(text: str) -> SafetyFinding | None:
    """Full write-side scan for direction-layer content. Returns the first finding.

    Order is deliberate: invisible characters first, because they can hide the very
    phrases the other two families look for.
    """
    text = text or ""
    finding = scan_invisible(text)
    if finding is not None:
        return finding

    for name, pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return SafetyFinding(
                category="prompt_injection",
                pattern=f"prompt-injection pattern '{name}'",
                position=match.start(),
                excerpt=f"'{match.group()[:40]}'",
            )

    for name, pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            # No excerpt: echoing the match would duplicate the secret into the log.
            return SafetyFinding(
                category="credential",
                pattern=f"credential shape '{name}'",
                position=match.start(),
            )

    return None
