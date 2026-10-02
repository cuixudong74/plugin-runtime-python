import pytest

from plugin_runtime.version import Version, VersionRange


def test_version_parse_and_str():
    v = Version.parse("1.2.3")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert str(v) == "1.2.3"
    assert str(Version.parse("1.0.0-rc.1")) == "1.0.0-rc.1"


def test_version_ordering():
    assert Version.parse("1.2.3") < Version.parse("1.2.10")
    assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0")
    assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0-alpha.1")
    assert Version.parse("2.0.0") > Version.parse("1.9.9")


@pytest.mark.parametrize("bad", ["1.0", "1", "a.b.c", "1.0.0.0", "", 12])
def test_invalid_version_rejected(bad):
    with pytest.raises(ValueError):
        Version.parse(bad)


def test_range_comparators():
    r = VersionRange.parse(">=1.0.0,<2.0.0")
    assert r.contains(Version.parse("1.5.0"))
    assert not r.contains(Version.parse("2.0.0"))
    assert not r.contains(Version.parse("0.9.0"))


def test_range_caret_tilde_star_bare():
    assert VersionRange.parse("^1.2.0").contains(Version.parse("1.9.0"))
    assert not VersionRange.parse("^1.2.0").contains(Version.parse("2.0.0"))
    assert VersionRange.parse("~1.2.0").contains(Version.parse("1.2.9"))
    assert not VersionRange.parse("~1.2.0").contains(Version.parse("1.3.0"))
    assert VersionRange.parse("*").contains(Version.parse("9.9.9"))
    assert VersionRange.parse("1.2.3").contains(Version.parse("1.2.3"))
    assert not VersionRange.parse("1.2.3").contains(Version.parse("1.2.4"))


def test_invalid_range_rejected():
    with pytest.raises(ValueError):
        VersionRange.parse(">=1.0")
    with pytest.raises(ValueError):
        VersionRange.parse(5)
