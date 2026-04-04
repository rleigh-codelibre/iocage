"""Unit tests for RapSheet parsing and layer hash computation."""
import json
import os
import tempfile

import pytest

from iocage_lib.ioc_build import RapSheetParser, LayerHasher, RapSheet
from iocage_lib.ioc_exceptions import RapSheetParseError


# --- Test Data ---

VALID_RAPSHEET = {
    "from": "15.0-RELEASE",
    "metadata": {
        "maintainer": "Test User <test@example.com>",
        "description": "Test jail",
        "labels": {
            "version": "1.0",
            "app": "test"
        }
    },
    "properties": {
        "boot": 1,
        "allow_raw_sockets": 1
    },
    "steps": [
        {"env": {"APP_PORT": "8080", "APP_ENV": "production"}},
        {"run": "pkg install -y nginx"},
        {"run": "sysrc nginx_enable=YES"},
        {"env": {"LOG_LEVEL": "info"}},
        {"run": "mkdir -p /usr/local/www/data"}
    ]
}

MINIMAL_RAPSHEET = {
    "from": "15.0-RELEASE",
    "steps": [
        {"run": "echo hello"}
    ]
}


def write_rapsheet(data, tmpdir=None):
    """Write a RapSheet dict to a temporary file and return the path."""
    fd, path = tempfile.mkstemp(suffix='.json', dir=tmpdir)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    return path


# --- RapSheetParser Tests ---

class TestRapSheetParser:

    def test_valid_rapsheet(self):
        path = write_rapsheet(VALID_RAPSHEET)
        try:
            result = RapSheetParser(path).parse()
            assert isinstance(result, RapSheet)
            assert result.from_source == "15.0-RELEASE"
            assert result.metadata['maintainer'] == "Test User <test@example.com>"
            assert result.properties['boot'] == 1
            assert len(result.steps) == 5
        finally:
            os.unlink(path)

    def test_minimal_rapsheet(self):
        path = write_rapsheet(MINIMAL_RAPSHEET)
        try:
            result = RapSheetParser(path).parse()
            assert result.from_source == "15.0-RELEASE"
            assert result.metadata == {}
            assert result.properties == {}
            assert len(result.steps) == 1
        finally:
            os.unlink(path)

    def test_missing_file(self):
        with pytest.raises(RapSheetParseError, match='not found'):
            RapSheetParser('/nonexistent/RapSheet.json').parse()

    def test_invalid_json(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as f:
            f.write('{ invalid json }')
        try:
            with pytest.raises(RapSheetParseError, match='Invalid JSON'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_missing_from(self):
        data = {"steps": [{"run": "echo hello"}]}
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_missing_steps(self):
        data = {"from": "15.0-RELEASE"}
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_empty_steps(self):
        data = {"from": "15.0-RELEASE", "steps": []}
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_unknown_step_type(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"unknown": "value"}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError, match='unknown step type'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_reserved_step_type(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"copy": {"src": ".", "dest": "/app"}}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError, match='not yet implemented'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_empty_run_command(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"run": ""}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError, match='non-empty string'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_empty_env_dict(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"env": {}}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError, match='non-empty object'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_non_string_env_value(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"env": {"PORT": 8080}}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError, match='must be a string'):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)

    def test_step_with_multiple_keys(self):
        data = {
            "from": "15.0-RELEASE",
            "steps": [{"run": "echo hi", "env": {"A": "1"}}]
        }
        path = write_rapsheet(data)
        try:
            with pytest.raises(RapSheetParseError):
                RapSheetParser(path).parse()
        finally:
            os.unlink(path)


# --- LayerHasher Tests ---

class TestLayerHasher:

    def test_canonical_json_sorted_keys(self):
        result = LayerHasher.canonical_json({"b": "2", "a": "1"})
        assert result == '{"a":"1","b":"2"}'

    def test_canonical_json_nested(self):
        result = LayerHasher.canonical_json(
            {"env": {"Z": "last", "A": "first"}}
        )
        assert result == '{"env":{"A":"first","Z":"last"}}'

    def test_compute_hashes_basic(self):
        steps = [
            {"run": "echo hello"},
            {"run": "echo world"}
        ]
        hashes = LayerHasher.compute_hashes("15.0-RELEASE", steps)

        assert len(hashes) == 2
        assert hashes[0]['index'] == 0
        assert hashes[1]['index'] == 1
        assert hashes[0]['step'] == steps[0]
        assert hashes[1]['step'] == steps[1]

        # Each hash should be 64 hex chars
        assert len(hashes[0]['layer_hash']) == 64
        assert len(hashes[1]['layer_hash']) == 64

        # hash12 should be first 12 chars
        assert hashes[0]['hash12'] == hashes[0]['layer_hash'][:12]

        # Hashes should be different
        assert hashes[0]['layer_hash'] != hashes[1]['layer_hash']

    def test_hash_chain_deterministic(self):
        steps = [{"run": "echo hello"}, {"run": "echo world"}]
        h1 = LayerHasher.compute_hashes("15.0-RELEASE", steps)
        h2 = LayerHasher.compute_hashes("15.0-RELEASE", steps)

        assert h1[0]['layer_hash'] == h2[0]['layer_hash']
        assert h1[1]['layer_hash'] == h2[1]['layer_hash']

    def test_changing_step_invalidates_subsequent(self):
        steps_a = [
            {"run": "echo hello"},
            {"run": "echo world"},
            {"run": "echo done"}
        ]
        steps_b = [
            {"run": "echo hello"},
            {"run": "echo CHANGED"},
            {"run": "echo done"}
        ]

        hashes_a = LayerHasher.compute_hashes("15.0-RELEASE", steps_a)
        hashes_b = LayerHasher.compute_hashes("15.0-RELEASE", steps_b)

        # Step 0 should be the same
        assert hashes_a[0]['layer_hash'] == hashes_b[0]['layer_hash']
        # Step 1 should differ (different command)
        assert hashes_a[1]['layer_hash'] != hashes_b[1]['layer_hash']
        # Step 2 should also differ (parent hash changed)
        assert hashes_a[2]['layer_hash'] != hashes_b[2]['layer_hash']

    def test_different_from_changes_all_hashes(self):
        steps = [{"run": "echo hello"}]
        h1 = LayerHasher.compute_hashes("15.0-RELEASE", steps)
        h2 = LayerHasher.compute_hashes("14.0-RELEASE", steps)

        assert h1[0]['layer_hash'] != h2[0]['layer_hash']

    def test_env_key_order_irrelevant(self):
        """Env keys should be sorted before hashing for determinism."""
        steps_a = [{"env": {"A": "1", "B": "2"}}]
        steps_b = [{"env": {"B": "2", "A": "1"}}]

        h1 = LayerHasher.compute_hashes("15.0-RELEASE", steps_a)
        h2 = LayerHasher.compute_hashes("15.0-RELEASE", steps_b)

        assert h1[0]['layer_hash'] == h2[0]['layer_hash']

    def test_parent_hash_chain(self):
        steps = [
            {"run": "step1"},
            {"run": "step2"},
            {"run": "step3"}
        ]
        hashes = LayerHasher.compute_hashes("15.0-RELEASE", steps)

        # First step's parent should be the base hash
        import hashlib
        base_hash = hashlib.sha256(b'from:15.0-RELEASE').hexdigest()
        assert hashes[0]['parent_hash'] == base_hash

        # Each subsequent step's parent should be the previous step's hash
        assert hashes[1]['parent_hash'] == hashes[0]['layer_hash']
        assert hashes[2]['parent_hash'] == hashes[1]['layer_hash']

    def test_mixed_step_types(self):
        steps = [
            {"env": {"PORT": "80"}},
            {"run": "pkg install -y nginx"},
            {"env": {"APP_ENV": "prod"}},
            {"run": "sysrc nginx_enable=YES"}
        ]
        hashes = LayerHasher.compute_hashes("15.0-RELEASE", steps)

        assert len(hashes) == 4
        # All hashes should be unique
        hash_set = {h['layer_hash'] for h in hashes}
        assert len(hash_set) == 4
