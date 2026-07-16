#!/usr/bin/env python3
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCE = ROOT / "references" / "option-structures.md"

FORBIDDEN_TERMS = [
    "".join(parts)
    for parts in [
        ("G", "PT"),
        ("g", "pt"),
        ("Cl", "aude"),
        ("cl", "aude"),
        ("Co", "dex"),
        ("co", "dex"),
        ("Open", "AI"),
        ("open", "ai"),
        ("Chat", "G", "PT"),
        ("chat", "g", "pt"),
    ]
]

REQUIRED_REFERENCE_HEADINGS = [
    "## 1 通用说明",
    "## 2 方向性期权",
    "## 3 垂直价差",
    "## 4 波动率与区间",
    "## 5 障碍期权",
    "## 6 二元与数字期权",
]


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def check_skill_frontmatter(text: str) -> None:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        fail("SKILL.md missing YAML frontmatter")
    frontmatter = match.group(1)
    if "name: option-helper" not in frontmatter:
        fail("frontmatter name must be option-helper")
    if "description:" not in frontmatter:
        fail("frontmatter missing description")


def check_forbidden_terms(paths: list[Path]) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for term in FORBIDDEN_TERMS:
            if term in text:
                fail(f"found platform-specific term {term!r} in {path.relative_to(ROOT)}")


def main() -> None:
    if not SKILL.exists():
        fail("SKILL.md not found")
    if not REFERENCE.exists():
        fail("references/option-structures.md not found")

    skill_text = SKILL.read_text(encoding="utf-8")
    reference_text = REFERENCE.read_text(encoding="utf-8")

    check_skill_frontmatter(skill_text)

    if "references/option-structures.md" not in skill_text:
        fail("SKILL.md must point to references/option-structures.md")

    for heading in REQUIRED_REFERENCE_HEADINGS:
        if heading not in reference_text:
            fail(f"reference missing heading: {heading}")

    check_forbidden_terms([SKILL, REFERENCE])

    print("OK: option-helper skill structure is valid")


if __name__ == "__main__":
    main()
