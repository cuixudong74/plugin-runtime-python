"""Semantic versions and version ranges used by manifests and the resolver."""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

_COMPARATOR_RE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\S+)$")


@dataclass(frozen=True)
class Version:
    """A semantic version ``major.minor.patch`` with optional prerelease.

    Ordering follows semver: a prerelease sorts before the same release,
    and prerelease identifiers compare numerically when both are numeric.
    """

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "Version":
        """Parse a version string; raises ValueError on invalid input."""
        if not isinstance(text, str):
            raise ValueError(f"version must be a string, got {type(text).__name__}")
        m = _VERSION_RE.match(text.strip())
        if not m:
            raise ValueError(f"invalid semantic version: {text!r}")
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)

    def _key(self):
        # Release versions sort after their prereleases.
        if not self.prerelease:
            pre_key = (1,)
        else:
            pre_key = (0,) + tuple(
                (0, int(p)) if p.isdigit() else (1, p) for p in self.prerelease
            )
        return (self.major, self.minor, self.patch, pre_key)

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return not self.__le__(other)

    def __ge__(self, other: "Version") -> bool:
        return not self.__lt__(other)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        return base


@dataclass(frozen=True)
class VersionRange:
    """A set of versions, e.g. ``>=1.2.0,<2.0.0``.

    Supported syntax: comma-separated comparators (``>=``, ``<=``, ``>``,
    ``<``, ``==``, ``!=``), bare versions (``1.2.3`` == ``==1.2.3``),
    caret (``^1.2.3``), tilde (``~1.2.3``) and ``*``.
    """

    comparators: tuple[tuple[str, "Version"], ...]

    @classmethod
    def any(cls) -> "VersionRange":
        return cls(())

    @classmethod
    def parse(cls, text: str) -> "VersionRange":
        """Parse a range expression; raises ValueError on invalid input."""
        if not isinstance(text, str):
            raise ValueError(f"version range must be a string, got {type(text).__name__}")
        text = text.strip()
        if text in ("", "*"):
            return cls.any()
        comparators: list[tuple[str, Version]] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"empty comparator in range {text!r}")
            if part.startswith("^"):
                v = Version.parse(part[1:])
                upper = Version(v.major + 1, 0, 0) if v.major else (
                    Version(0, v.minor + 1, 0) if v.minor else Version(0, 0, v.patch + 1)
                )
                comparators.append((">=", v))
                comparators.append(("<", upper))
                continue
            if part.startswith("~"):
                v = Version.parse(part[1:])
                comparators.append((">=", v))
                comparators.append(("<", Version(v.major, v.minor + 1, 0)))
                continue
            m = _COMPARATOR_RE.match(part)
            if not m:
                raise ValueError(f"invalid comparator {part!r} in range {text!r}")
            op = m.group(1) or "=="
            comparators.append((op, Version.parse(m.group(2))))
        return cls(tuple(comparators))

    def contains(self, version: Version) -> bool:
        for op, bound in self.comparators:
            if op == ">=" and not version >= bound:
                return False
            if op == "<=" and not version <= bound:
                return False
            if op == ">" and not version > bound:
                return False
            if op == "<" and not version < bound:
                return False
            if op == "==" and not version == bound:
                return False
            if op == "!=" and not version != bound:
                return False
        return True

    def __str__(self) -> str:
        if not self.comparators:
            return "*"
        return ",".join(f"{op}{v}" for op, v in self.comparators)
