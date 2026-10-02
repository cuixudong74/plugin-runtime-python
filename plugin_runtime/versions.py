"""Semantic version parsing, comparison and range matching.

Supported range syntax (comma-separated constraints, all must hold):

    1.2.3            exact version
    >=1.0.0          greater-or-equal
    >1.0.0           greater
    <2.0.0           less
    <=2.0.0          less-or-equal
    ^1.2.0           compatible with 1.2.0 (>=1.2.0, <2.0.0)
    ~1.2.3           approximately 1.2.3  (>=1.2.3, <1.3.0)

Examples:
    ">=1.0.0,<2.0.0"
    "^1.2.0"
    "~1.2.3"
    "1.2.3"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import total_ordering
from typing import List, Tuple

from .errors import ManifestVersionError

_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z.-]+))?"
    r"(?:\+([0-9A-Za-z.-]+))?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: Tuple[object, ...] = ()
    build: str = ""

    # -- parsing -----------------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str):
            raise ManifestVersionError(
                f"version must be a string, got {type(text).__name__}"
            )
        text = text.strip()
        m = _VERSION_RE.match(text)
        if not m:
            raise ManifestVersionError(
                f"invalid version {text!r}: expected 'major.minor.patch' "
                f"(e.g. 1.2.3) with optional prerelease/build metadata"
            )
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        pre_raw = m.group(4)
        build = m.group(5) or ""
        prerelease: Tuple[object, ...] = ()
        if pre_raw is not None:
            parts: List[object] = []
            for part in pre_raw.split("."):
                if part.isdigit():
                    parts.append((0, int(part)))  # numeric prerelease
                else:
                    parts.append((1, part))       # alphanumeric prerelease
            prerelease = tuple(parts)
        return cls(major, minor, patch, prerelease, build)

    # -- comparison --------------------------------------------------------

    def _key(self):
        # prerelease: absent (released) sorts *after* any prerelease of the
        # same [major, minor, patch]; prerelease identifiers sort numerically
        # before alphanumeric ones.
        if self.prerelease:
            pre = tuple(
                (0, p[1]) if p[0] == 0 else (1, p[1]) for p in self.prerelease
            )
            has_pre = 0
        else:
            pre = ()
            has_pre = 1
        return (self.major, self.minor, self.patch, has_pre, pre)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            parts = []
            for p in self.prerelease:
                parts.append(str(p[1]))
            s += "-" + ".".join(parts)
        if self.build:
            s += "+" + self.build
        return s

    # -- range helpers -----------------------------------------------------

    def next_major(self) -> "Version":
        return Version(self.major + 1, 0, 0)

    def next_minor(self) -> "Version":
        return Version(self.major, self.minor + 1, 0)


@dataclass(frozen=True)
class Range:
    """A single constraint, e.g. >=1.0.0."""

    op: str
    version: Version

    def satisfies(self, version: Version) -> bool:
        v = version
        if self.op == ">=":
            return v >= self.version
        if self.op == ">":
            return v > self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == "<":
            return v < self.version
        if self.op == "==":
            return v == self.version
        raise AssertionError(f"unknown operator {self.op!r}")

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


@dataclass(frozen=True)
class RangeSet:
    """A set of constraints that must all hold (comma-separated)."""

    ranges: Tuple[Range, ...] = field(default_factory=tuple)
    raw: str = ""

    def satisfies(self, version: Version) -> bool:
        return all(r.satisfies(version) for r in self.ranges)

    def __str__(self) -> str:
        return self.raw or ",".join(str(r) for r in self.ranges)

    def __bool__(self) -> bool:
        return bool(self.ranges)


def _parse_partial(text: str) -> Version:
    """Parse a version that may omit the patch (e.g. '1.2' -> 1.2.0)."""
    text = text.strip()
    parts = text.split(".")
    if len(parts) == 2:
        text = text + ".0"
    elif len(parts) == 1:
        text = text + ".0.0"
    return Version.parse(text)


def _parse_single_constraint(text: str) -> Range:
    text = text.strip()
    if not text:
        raise ManifestVersionError("empty version constraint")
    for op in (">=", "<=", ">", "<", "=="):
        if text.startswith(op):
            ver_text = text[len(op):].strip()
            return Range(op, _parse_partial(ver_text))
    # exact version (no operator)
    return Range("==", _parse_partial(text))


def parse_range(text: str) -> RangeSet:
    """Parse a comma-separated constraint string into a RangeSet."""
    if not isinstance(text, str):
        raise ManifestVersionError(
            f"version range must be a string, got {type(text).__name__}"
        )
    text = text.strip()
    if not text:
        raise ManifestVersionError("empty version range")
    ranges: List[Range] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ManifestVersionError(f"empty constraint in range {text!r}")
        if part.startswith("^"):
            ver = _parse_partial(part[1:].strip())
            if ver.major > 0:
                upper = ver.next_major()
            elif ver.minor > 0:
                upper = ver.next_minor()
            else:
                upper = Version(ver.major, ver.minor, ver.patch + 1)
            ranges.append(Range(">=", ver))
            ranges.append(Range("<", upper))
        elif part.startswith("~"):
            ver = _parse_partial(part[1:].strip())
            upper = ver.next_minor()
            ranges.append(Range(">=", ver))
            ranges.append(Range("<", upper))
        else:
            ranges.append(_parse_single_constraint(part))
    return RangeSet(tuple(ranges), raw=text)
