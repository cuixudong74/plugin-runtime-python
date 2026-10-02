import pytest

from plugin_runtime.errors import ManifestError, ManifestErrorReason as R
from plugin_runtime.manifest import PluginManifest


def parse(raw):
    return PluginManifest.from_json(raw)


def test_valid_manifest_roundtrip():
    m = parse(
        '{"id":"text-search","version":"1.2.0","entrypoint":"pkg:Plugin",'
        '"description":"d","capabilities":["search"],'
        '"permissions":["http.get"],"dependencies":{"tokenizer":"^1.0.0"}}'
    )
    assert m.plugin_id == "text-search"
    assert str(m.version) == "1.2.0"
    assert m.capabilities == ("search",)
    assert m.permissions == ("http.get",)
    assert m.dependencies[0].plugin_id == "tokenizer"
    assert PluginManifest.from_dict(m.to_dict()) == m


@pytest.mark.parametrize(
    "raw,reason",
    [
        (None, R.MANIFEST_MISSING),
        ("", R.MANIFEST_MISSING),
        ("   ", R.MANIFEST_MISSING),
        ("{not json", R.MANIFEST_NOT_JSON),
        ('[1,2]', R.MANIFEST_NOT_OBJECT),
        ('"str"', R.MANIFEST_NOT_OBJECT),
        ('{"version":"1.0.0"}', R.MISSING_FIELD),
        ('{"id":"a"}', R.MISSING_FIELD),
        ('{"id":"","version":"1.0.0"}', R.INVALID_FIELD_TYPE),
        ('{"id":"Bad_Id","version":"1.0.0"}', R.INVALID_PLUGIN_ID),
        ('{"id":"a","version":"1.0"}', R.INVALID_VERSION),
        ('{"id":"a","version":1}', R.INVALID_FIELD_TYPE),
        ('{"id":"a","version":"1.0.0","capabilities":"x"}', R.INVALID_FIELD_TYPE),
        ('{"id":"a","version":"1.0.0","capabilities":["Bad Cap"]}', R.INVALID_CAPABILITY),
        ('{"id":"a","version":"1.0.0","permissions":["!!"]}', R.INVALID_PERMISSION),
        ('{"id":"a","version":"1.0.0","dependencies":["b"]}', R.INVALID_FIELD_TYPE),
        ('{"id":"a","version":"1.0.0","dependencies":{"B":"*"}}', R.INVALID_DEPENDENCY),
        ('{"id":"a","version":"1.0.0","dependencies":{"b":">=x"}}', R.INVALID_VERSION_RANGE),
        ('{"id":"a","version":"1.0.0","dependencies":{"a":"*"}}', R.SELF_DEPENDENCY),
    ],
)
def test_invalid_manifests_have_distinguishable_reasons(raw, reason):
    with pytest.raises(ManifestError) as exc_info:
        parse(raw)
    assert exc_info.value.reason == reason


def test_reasons_are_distinct_values():
    reasons = {r.value for r in R}
    assert len(reasons) == len(R)
