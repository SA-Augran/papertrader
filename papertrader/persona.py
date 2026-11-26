# -*- coding: utf-8 -*-
"""persona.py — 角色加载器：自带一个可玩的默认角色，也可替换成自己的。

对齐高星项目（ai-hedge-fund 名人 persona / tiny-roommate 自带 4 角色）的做法：
角色是项目自带的、开箱即用的，用户不需要先造一个；想换再换。
persona 只是文字模板，不硬编码在逻辑里——替换文件即换角色。
"""
from pathlib import Path

BUNDLED_CHARACTERS = Path(__file__).parent / "characters"


def list_characters():
    """返回内置 + 用户目录下的所有角色名（stem）。"""
    names = set()
    if BUNDLED_CHARACTERS.exists():
        names.update(p.stem for p in BUNDLED_CHARACTERS.glob("*.md"))
    return sorted(names)


def load_persona(name_or_path="default"):
    """按名字加载内置角色，或直接传一个 .md 路径。找不到回落默认。"""
    p = Path(name_or_path)
    candidates = []
    if p.suffix == ".md" and p.exists():
        candidates.append(p)
    else:
        candidates.append(BUNDLED_CHARACTERS / f"{name_or_path}.md")
        candidates.append(BUNDLED_CHARACTERS / "default.md")
    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8").strip()
    return "你是经营自己模拟盘的角色。口语、简短、诚实，每笔操作说得出理由。"
