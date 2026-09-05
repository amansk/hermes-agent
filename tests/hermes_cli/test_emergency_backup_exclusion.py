"""Emergency pre-update state.db snapshots stay out of profile clones and exports.

The desktop updater's pre-flight guard (#68474, #97994) plants
``state.db.pre-update-emergency-<timestamp>.bak`` next to each guarded
database — the root home's and every ``profiles/<name>/`` home's. Each is a
full, stale copy of that home's state.db kept solely for in-place disaster
recovery, so profile clones and profile exports must skip them (``hermes
backup`` already prefix-excludes them in ``hermes_cli/backup.py``): carrying one
along re-ships a multi-GB database per snapshot and, for clones, resurrects the
SOURCE profile's session state in the copy.
"""

import tarfile
from pathlib import Path

from hermes_constants import is_pre_update_emergency_db_backup
from hermes_cli.profiles import _clone_all_copytree_ignore, export_profile

_BAK_NAME = "state.db.pre-update-emergency-2026-08-29T16-00-00-000Z.bak"


class TestPredicate:

    def test_matches_emergency_snapshot_names(self):
        assert is_pre_update_emergency_db_backup(_BAK_NAME)

    def test_rejects_the_live_database_and_sidecars(self):
        assert not is_pre_update_emergency_db_backup("state.db")
        assert not is_pre_update_emergency_db_backup("state.db-wal")
        assert not is_pre_update_emergency_db_backup("state.db-shm")

    def test_rejects_other_bak_files(self):
        assert not is_pre_update_emergency_db_backup("config.yaml.bak")
        assert not is_pre_update_emergency_db_backup("notes.bak")

    def test_rejects_prefix_without_bak_suffix(self):
        assert not is_pre_update_emergency_db_backup(
            "state.db.pre-update-emergency-2026-08-29"
        )


class TestCloneExclusion:

    def test_clone_all_ignores_emergency_snapshot_at_profile_root(self, tmp_path):
        source = tmp_path / "profiles" / "coder"
        source.mkdir(parents=True)
        ignore = _clone_all_copytree_ignore(source)

        ignored = ignore(str(source), ["config.yaml", _BAK_NAME, "SOUL.md"])

        assert _BAK_NAME in ignored
        assert "config.yaml" not in ignored
        assert "SOUL.md" not in ignored

    def test_clone_all_keeps_lookalike_names_below_root(self, tmp_path):
        # The snapshots only ever live at the home root; a user file with the
        # same name deeper in the tree is their data and must survive.
        source = tmp_path / "profiles" / "coder"
        nested = source / "skills" / "notes"
        nested.mkdir(parents=True)
        ignore = _clone_all_copytree_ignore(source)

        assert _BAK_NAME not in ignore(str(nested), [_BAK_NAME])


class TestExportExclusion:

    def test_named_profile_export_excludes_emergency_snapshot(
        self, tmp_path, monkeypatch
    ):
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        (profile_dir / _BAK_NAME).write_text("stale db bytes")
        # Same name deeper in the tree is user data and must survive (root-only rule,
        # matching the clone-all rule).
        nested = profile_dir / "skills" / "notes"
        nested.mkdir(parents=True)
        (nested / _BAK_NAME).write_text("user file")

        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda n: profile_dir
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.validate_profile_name", lambda n: None
        )

        result = export_profile("testprofile", str(tmp_path / "export.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()

        assert any("config.yaml" in n for n in names)
        assert not any(n.endswith("/" + _BAK_NAME) and "/skills/" not in n for n in names), (
            "root emergency state.db snapshot must NOT be exported"
        )
        assert any(n.endswith("skills/notes/" + _BAK_NAME) for n in names), (
            "nested lookalike is user data and must survive export"
        )

