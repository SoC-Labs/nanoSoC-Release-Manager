"""The files of one repository snapshot that the quality tools read, lexed once and shared (Q1, Q2, Q3, Q4).

A file is read when its language is known (languages.language_of), it is not excluded by the settings, it is
not larger than `max_file_bytes`, and it decodes as text (no NUL byte in its first 8 KiB). Skipped files are
counted by reason so a report never hides what it did not read.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nanosoc_rm.quality.languages import is_excluded, language_of
from nanosoc_rm.quality.lexers import Lexed, lex
from nanosoc_rm.quality.settings import Settings
from nanosoc_rm.quality.source import FileEntry, Source

__all__ = ["Doc", "Corpus", "build"]


@dataclass
class Doc:
    entry: FileEntry
    language: str
    text: str
    lexed: Lexed

    @property
    def path(self) -> str:
        return self.entry.path

    @property
    def lines(self) -> list[str]:
        lines = self.text.split("\n")
        if lines and lines[-1] == "" and self.text.endswith("\n"):
            lines.pop()
        return lines


@dataclass
class Corpus:
    source: Source
    settings: Settings
    docs: list[Doc] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def code_docs(self) -> list[Doc]:
        return [d for d in self.docs if d.language != "markdown"]

    def markdown_docs(self) -> list[Doc]:
        return [d for d in self.docs if d.language == "markdown"]

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def build(source: Source, settings: Settings) -> Corpus:
    c = Corpus(source, settings)
    exclude = list(settings.get("exclude") or [])
    limit = int(settings.get("max_file_bytes") or 0)
    want, sniff = [], []
    for e in source.files():
        if e.mode == "120000":
            continue
        if is_excluded(e.path, exclude):
            c.skip("excluded")
            continue
        lang = language_of(e.path)
        if lang is None:
            if "." not in e.path.rsplit("/", 1)[-1] and e.size and (not limit or e.size <= limit):
                sniff.append(e)
            continue
        if limit and e.size > limit:
            c.skip("larger than max_file_bytes")
            continue
        want.append((e, lang))
    if sniff:
        heads = source.read_many([e.path for e in sniff])
        for e in sniff:
            lang = language_of(e.path, heads.get(e.path))
            if lang is not None:
                want.append((e, lang))
    data = source.read_many([e.path for e, _ in want])
    for e, lang in sorted(want, key=lambda t: t[0].path):
        raw = data.get(e.path)
        if raw is None:
            c.skip("unreadable")
            continue
        if b"\0" in raw[:8192]:
            c.skip("binary")
            continue
        text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
        c.docs.append(Doc(e, lang, text, lex(lang, text)))
    return c
