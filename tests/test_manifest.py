"""Tests for manifest parsing and validation.

Each rejection reason must be distinguishable (a specific error subclass),
and a rejected manifest must never be loaded.
"""

import json
import os

import pytest

from plugin_runtime.errors import (
    ManifestDependencyError,
    ManifestMissingError,
    ManifestParseError,
    ManifestSchemaError,
    ManifestVersionError,
)
from plugin_runtime.manifest import load_manifest, parse_manifest


VALID = {
    "id": "my-plugin",
    "version": "1.2.3",
    "entrypoint": "my_plugin:create_plugin",
    "dependencies": {"other": ">=1.0.0,<2.0.0"},
    "capabilities": ["payments"],
    "requires": ["logger"],
}


class TestParseManifest:
    def test_valid(self):
        m = parse_manifest(VALID)
        assert m.id == "my-plugin"
        assert str(m.version) == "1.2.3"
        assert m.capabilities == ["payments"]
        assert m.requires == ["logger"]
        assert "other" in m.dependencies

    def test_missing_id(self):
        data = {k: v for k, v in VALID.items() if k != "id"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "id"

    def test_missing_version(self):
        data = {k: v for k, v in VALID.items() if k != "version"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "version"

    def test_missing_entrypoint(self):
        data = {k: v for k, v in VALID.items() if k != "entrypoint"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "entrypoint"

    def test_bad_version(self):
        data = {**VALID, "version": "not-a-version"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "version"

    def test_bad_version_type(self):
        data = {**VALID, "version": 123}
        with pytest.raises(ManifestSchemaError):
            parse_manifest(data)

    def test_bad_dependency_range(self):
        data = {**VALID, "dependencies": {"other": ">=not-a-version"}}
        with pytest.raises(ManifestDependencyError) as exc:
            parse_manifest(data)
        assert exc.value.dependency == "other"

    def test_dependencies_not_object(self):
        data = {**VALID, "dependencies": "not-an-object"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "dependencies"

    def test_capabilities_not_list(self):
        data = {**VALID, "capabilities": "payments"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "capabilities"

    def test_capability_empty_string(self):
        data = {**VALID, "capabilities": [""]}
        with pytest.raises(ManifestSchemaError):
            parse_manifest(data)

    def test_requires_not_list(self):
        data = {**VALID, "requires": "logger"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "requires"

    def test_invalid_id_characters(self):
        data = {**VALID, "id": "my plugin!"}
        with pytest.raises(ManifestSchemaError) as exc:
            parse_manifest(data)
        assert exc.value.field == "id"

    def test_empty_id(self):
        data = {**VALID, "id": ""}
        with pytest.raises(ManifestSchemaError):
            parse_manifest(data)

    def test_not_an_object(self):
        with pytest.raises(ManifestSchemaError):
            parse_manifest([1, 2, 3])

    def test_optional_description(self):
        m = parse_manifest(VALID)
        assert m.description == ""
        data = {**VALID, "description": "a plugin"}
        m = parse_manifest(data)
        assert m.description == "a plugin"

    def test_to_dict_roundtrip(self):
        m = parse_manifest(VALID)
        d = m.to_dict()
        assert d["id"] == "my-plugin"
        assert d["version"] == "1.2.3"


class TestLoadManifest:
    def test_load_from_file(self, tmp_path):
        p = tmp_path / "plugin.json"
        p.write_text(json.dumps(VALID))
        m = load_manifest(str(p))
        assert m.id == "my-plugin"
        assert m.source == str(p)

    def test_load_from_directory(self, tmp_path):
        p = tmp_path / "plugin.json"
        p.write_text(json.dumps(VALID))
        m = load_manifest(str(tmp_path))
        assert m.id == "my-plugin"

    def test_missing_file(self, tmp_path):
        with pytest.raises(ManifestMissingError):
            load_manifest(str(tmp_path / "nonexistent.json"))

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "plugin.json"
        p.write_text("{not valid json")
        with pytest.raises(ManifestParseError):
            load_manifest(str(p))

    def test_missing_manifest_in_directory(self, tmp_path):
        with pytest.raises(ManifestMissingError):
            load_manifest(str(tmp_path))
