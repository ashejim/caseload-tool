"""Tests for the one-time settings.json schema migration.

Covers config.load_settings() / _migrate_settings():
  - A fresh install (no settings.json) is born at CURRENT_SETTINGS_VERSION with
    the current defaults (API texting ON) and writes no file.
  - An existing install saved under the old default (text_send_via_api False,
    no settings_version key == v0) is flipped ON once and stamped to CURRENT.
  - The migration does NOT re-run: a file already at CURRENT keeps a user's
    deliberate opt-out (text_send_via_api False) instead of re-flipping it.

Each test runs against an isolated config dir via CASELOAD_CONFIG_DIR so the
real settings.json is never touched.

Run: python tests/test_settings_migration.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_config_module():
    """Import src.config bound to a brand-new, empty config dir. Reloaded per
    test so SETTINGS_PATH points at the isolated temp dir."""
    import importlib
    os.environ["CASELOAD_CONFIG_DIR"] = tempfile.mkdtemp()
    import src.config as cfg
    return importlib.reload(cfg)


def test_fresh_install_defaults_to_api_and_writes_no_file():
    cfg = _fresh_config_module()
    assert not cfg.SETTINGS_PATH.exists()
    s = cfg.load_settings()
    assert s.text_send_via_api is True
    assert s.settings_version == cfg.CURRENT_SETTINGS_VERSION
    # Reading defaults must not create a file (only save_settings does).
    assert not cfg.SETTINGS_PATH.exists()


def test_old_install_off_is_flipped_and_stamped():
    cfg = _fresh_config_module()
    # v0 file: old default, no settings_version key, API explicitly off.
    cfg.SETTINGS_PATH.write_text(
        json.dumps({"advanced_mode": True, "text_send_via_api": False}))
    s = cfg.load_settings()
    assert s.text_send_via_api is True                    # migrated on
    assert s.settings_version == cfg.CURRENT_SETTINGS_VERSION
    assert s.advanced_mode is True                        # other fields intact
    # Stamp persisted so the migration won't run again.
    on_disk = json.loads(cfg.SETTINGS_PATH.read_text())
    assert on_disk["settings_version"] == cfg.CURRENT_SETTINGS_VERSION
    assert on_disk["text_send_via_api"] is True


def test_migration_runs_once_respecting_later_opt_out():
    cfg = _fresh_config_module()
    # Already migrated (version stamped) AND the user turned API back off.
    cfg.SETTINGS_PATH.write_text(json.dumps({
        "settings_version": cfg.CURRENT_SETTINGS_VERSION,
        "text_send_via_api": False,
    }))
    s = cfg.load_settings()
    assert s.text_send_via_api is False                   # not re-flipped


def test_garbage_version_is_treated_as_v0():
    cfg = _fresh_config_module()
    cfg.SETTINGS_PATH.write_text(json.dumps({
        "settings_version": "nonsense",
        "text_send_via_api": False,
    }))
    s = cfg.load_settings()
    assert s.text_send_via_api is True                    # migrated on
    assert s.settings_version == cfg.CURRENT_SETTINGS_VERSION


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
