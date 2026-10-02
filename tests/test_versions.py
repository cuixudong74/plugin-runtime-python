"""Tests for version parsing, comparison and range matching."""

import pytest

from plugin_runtime.errors import ManifestVersionError
from plugin_runtime.versions import RangeSet, Version, parse_range


class TestVersionParsing:
    def test_basic(self):
        v = Version.parse("1.2.3")
        assert (v.major, v.minor, v.patch) == (1, 2, 3)
        assert str(v) == "1.2.3"

    def test_prerelease(self):
        v = Version.parse("1.0.0-alpha.1")
        assert v.prerelease
        assert str(v) == "1.0.0-alpha.1"

    def test_build_metadata(self):
        v = Version.parse("1.0.0+build.42")
        assert v.build == "build.42"
        assert str(v) == "1.0.0+build.42"

    def test_invalid_empty(self):
        with pytest.raises(ManifestVersionError):
            Version.parse("")

    def test_invalid_two_parts(self):
        with pytest.raises(ManifestVersionError):
            Version.parse("1.2")

    def test_invalid_letters(self):
        with pytest.raises(ManifestVersionError):
            Version.parse("1.2.x")

    def test_invalid_negative(self):
        with pytest.raises(ManifestVersionError):
            Version.parse("-1.0.0")

    def test_invalid_leading_v(self):
        with pytest.raises(ManifestVersionError):
            Version.parse("v1.0.0")

    def test_non_string(self):
        with pytest.raises(ManifestVersionError):
            Version.parse(123)


class TestVersionComparison:
    def test_equal(self):
        assert Version.parse("1.2.3") == Version.parse("1.2.3")

    def test_major(self):
        assert Version.parse("2.0.0") > Version.parse("1.9.9")

    def test_minor(self):
        assert Version.parse("1.3.0") > Version.parse("1.2.9")

    def test_patch(self):
        assert Version.parse("1.2.4") > Version.parse("1.2.3")

    def test_prerelease_lower_than_release(self):
        assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0")

    def test_prerelease_numeric_ordering(self):
        assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.2")

    def test_prerelease_numeric_vs_alpha(self):
        # numeric identifiers have lower precedence than alphanumeric
        assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.beta")

    def test_sorting(self):
        versions = [Version.parse("1.0.0"), Version.parse("2.0.0"),
                    Version.parse("0.9.0"), Version.parse("1.0.0-alpha")]
        assert sorted(versions) == [
            Version.parse("0.9.0"),
            Version.parse("1.0.0-alpha"),
            Version.parse("1.0.0"),
            Version.parse("2.0.0"),
        ]


class TestRanges:
    def test_exact(self):
        r = parse_range("1.2.3")
        assert r.satisfies(Version.parse("1.2.3"))
        assert not r.satisfies(Version.parse("1.2.4"))

    def test_gte(self):
        r = parse_range(">=1.0.0")
        assert r.satisfies(Version.parse("1.0.0"))
        assert r.satisfies(Version.parse("2.0.0"))
        assert not r.satisfies(Version.parse("0.9.0"))

    def test_gt(self):
        r = parse_range(">1.0.0")
        assert r.satisfies(Version.parse("1.0.1"))
        assert not r.satisfies(Version.parse("1.0.0"))

    def test_lt(self):
        r = parse_range("<2.0.0")
        assert r.satisfies(Version.parse("1.9.9"))
        assert not r.satisfies(Version.parse("2.0.0"))

    def test_lte(self):
        r = parse_range("<=2.0.0")
        assert r.satisfies(Version.parse("2.0.0"))
        assert not r.satisfies(Version.parse("2.0.1"))

    def test_compound(self):
        r = parse_range(">=1.0.0,<2.0.0")
        assert r.satisfies(Version.parse("1.5.0"))
        assert not r.satisfies(Version.parse("2.0.0"))
        assert not r.satisfies(Version.parse("0.9.0"))

    def test_caret(self):
        r = parse_range("^1.2.0")
        assert r.satisfies(Version.parse("1.2.0"))
        assert r.satisfies(Version.parse("1.9.9"))
        assert not r.satisfies(Version.parse("2.0.0"))
        assert not r.satisfies(Version.parse("1.1.0"))

    def test_caret_zero_minor(self):
        r = parse_range("^0.2.0")
        assert r.satisfies(Version.parse("0.2.5"))
        assert not r.satisfies(Version.parse("0.3.0"))

    def test_caret_zero_patch(self):
        r = parse_range("^0.0.2")
        assert r.satisfies(Version.parse("0.0.2"))
        assert not r.satisfies(Version.parse("0.0.3"))

    def test_tilde(self):
        r = parse_range("~1.2.3")
        assert r.satisfies(Version.parse("1.2.3"))
        assert r.satisfies(Version.parse("1.2.9"))
        assert not r.satisfies(Version.parse("1.3.0"))

    def test_tilde_minor_only(self):
        r = parse_range("~1.2")
        assert r.satisfies(Version.parse("1.2.0"))
        assert not r.satisfies(Version.parse("1.3.0"))

    def test_invalid_range(self):
        with pytest.raises(ManifestVersionError):
            parse_range("")
        with pytest.raises(ManifestVersionError):
            parse_range(">=1.0.0,")
        with pytest.raises(ManifestVersionError):
            parse_range(">=abc")

    def test_range_str_roundtrip(self):
        r = parse_range(">=1.0.0,<2.0.0")
        assert str(r) == ">=1.0.0,<2.0.0"
