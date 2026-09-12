import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hometax_login.errors import LoginError
from hometax_login.write_journal import WriteJournal

PLAN_A = "plan-aaaaaaaaaaaa"
PLAN_B = "plan-bbbbbbbbbbbb"
TARGET_A = "a" * 64
TARGET_B = "b" * 64
PAYLOAD_A = "1" * 64
PAYLOAD_B = "2" * 64


def test_content_guard_blocks_different_reference_atomically(tmp_path):
    path = db_path(tmp_path)
    common = "c" * 64

    def begin(plan, target):
        try:
            return WriteJournal(path).begin(plan, target, PAYLOAD_A, guard_keys=(common,))
        except LoginError as error:
            return error.code

    WriteJournal(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(begin, PLAN_A, TARGET_A)
        b = pool.submit(begin, PLAN_B, TARGET_B)
        assert sorted([a.result(), b.result()]) == ["WRITE_OUTCOME_UNKNOWN", "started"]


def test_v1_migration_preserves_unknown_and_blocks_new_content_guard(tmp_path):
    path = db_path(tmp_path)
    journal = WriteJournal(path)
    journal.begin(PLAN_A, TARGET_A, PAYLOAD_A)
    journal.finish(PLAN_A, "unknown")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE write_guards")
        conn.execute("PRAGMA user_version = 1")
    migrated = WriteJournal(path)
    with pytest.raises(LoginError) as caught:
        migrated.begin(PLAN_B, TARGET_B, PAYLOAD_B, guard_keys=("c" * 64,))
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert status_of(path, PLAN_A) == "unknown"


def db_path(tmp_path):
    return tmp_path / "journal" / "write-attempts.sqlite3"


def status_of(path, plan_id):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT status FROM write_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]


def test_initializes_private_file_and_survives_restart(tmp_path):
    path = db_path(tmp_path)

    journal = WriteJournal(path)
    assert path.exists()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    assert status_of(path, PLAN_A) == "in_flight"

    restarted = WriteJournal(path)
    with pytest.raises(LoginError) as caught:
        restarted.begin(PLAN_A, TARGET_A, PAYLOAD_A)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert caught.value.status == 409


def test_rejects_existing_zero_byte_database_file(tmp_path):
    path = db_path(tmp_path)
    path.parent.mkdir()
    path.touch()

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)
    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"
    assert caught.value.status == 503


def test_same_plan_complete_is_idempotent(tmp_path):
    path = db_path(tmp_path)
    journal = WriteJournal(path)

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    journal.finish(PLAN_A, "complete")

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "complete"


def test_default_allows_new_plan_after_same_target_is_complete(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    journal.finish(PLAN_A, "complete")

    assert journal.begin(PLAN_B, TARGET_A, PAYLOAD_B) == "started"


def test_unique_target_blocks_new_plan_after_same_target_is_complete(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "started"
    journal.finish(PLAN_A, "complete")

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_B, TARGET_A, PAYLOAD_B, unique_target=True)
    assert caught.value.code == "WRITE_TARGET_COMPLETE"
    assert caught.value.status == 409


def test_unique_target_keeps_same_plan_complete_idempotent(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "started"
    journal.finish(PLAN_A, "complete")

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "complete"


def test_unique_target_same_plan_complete_with_different_hash_is_conflict(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "started"
    journal.finish(PLAN_A, "complete")

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_A, TARGET_A, PAYLOAD_B, unique_target=True)
    assert caught.value.code == "WRITE_IDEMPOTENCY_CONFLICT"
    assert caught.value.status == 409


def test_new_plan_is_blocked_when_same_target_outcome_is_unknown(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_B, TARGET_A, PAYLOAD_B)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert caught.value.status == 409


def test_unique_target_keeps_unknown_outcome_guard(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "started"

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_B, TARGET_A, PAYLOAD_B, unique_target=True)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert caught.value.status == 409


def test_different_target_is_allowed_while_another_target_is_in_flight(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    assert journal.begin(PLAN_B, TARGET_B, PAYLOAD_B) == "started"


def test_unique_target_allows_different_target(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A, unique_target=True) == "started"
    journal.finish(PLAN_A, "complete")

    assert journal.begin(PLAN_B, TARGET_B, PAYLOAD_B, unique_target=True) == "started"


def test_same_plan_with_different_hash_is_conflict(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_A, TARGET_A, PAYLOAD_B)
    assert caught.value.code == "WRITE_IDEMPOTENCY_CONFLICT"
    assert caught.value.status == 409


def test_rejected_plan_cannot_be_retried(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    journal.finish(PLAN_A, "rejected")

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_A, TARGET_A, PAYLOAD_A)
    assert caught.value.code == "WRITE_REJECTED"
    assert caught.value.status == 409


def test_unknown_plan_continues_to_block_same_target(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    journal.finish(PLAN_A, "unknown")

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_B, TARGET_A, PAYLOAD_B)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert caught.value.status == 409


def test_corrupt_database_fails_closed(tmp_path):
    path = db_path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)
    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"
    assert caught.value.status == 503
    assert str(path) not in caught.value.message


def test_begin_validates_hash_inputs(tmp_path):
    journal = WriteJournal(db_path(tmp_path))

    with pytest.raises(LoginError) as caught:
        journal.begin(PLAN_A, "not-a-hash", PAYLOAD_A)
    assert caught.value.code == "WRITE_JOURNAL_INVALID"
    assert caught.value.status == 422


def test_finish_only_moves_in_flight_to_final_state(tmp_path):
    path = db_path(tmp_path)
    journal = WriteJournal(path)

    assert journal.begin(PLAN_A, TARGET_A, PAYLOAD_A) == "started"
    journal.finish(PLAN_A, "complete")
    journal.finish(PLAN_A, "complete")

    with pytest.raises(LoginError) as caught:
        journal.finish(PLAN_A, "unknown")
    assert caught.value.code == "WRITE_ALREADY_FINALIZED"
    assert status_of(path, PLAN_A) == "complete"


def test_only_one_concurrent_begin_can_start_for_same_target(tmp_path):
    path = db_path(tmp_path)
    WriteJournal(path)

    def begin(plan_id):
        journal = WriteJournal(path)
        try:
            return journal.begin(plan_id, TARGET_A, PAYLOAD_A)
        except LoginError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, [PLAN_A, PLAN_B]))

    assert sorted(results) == ["WRITE_OUTCOME_UNKNOWN", "started"]


def test_schema_mismatch_fails_closed(tmp_path):
    path = db_path(tmp_path)
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE write_attempts (plan_id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 1")

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)
    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"


def test_existing_nonempty_database_without_attempt_table_fails_closed(tmp_path):
    path = db_path(tmp_path)
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)
    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"


def test_file_permissions_are_repaired_on_existing_database(tmp_path):
    path = db_path(tmp_path)
    WriteJournal(path)
    os.chmod(path, 0o644)

    WriteJournal(path)

    assert path.stat().st_mode & 0o777 == 0o600


def test_existing_parent_permissions_are_not_changed_and_are_rejected(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    os.chmod(parent, 0o755)

    with pytest.raises(LoginError) as caught:
        WriteJournal(parent / "write-attempts.sqlite3")

    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"
    assert parent.stat().st_mode & 0o777 == 0o755


def test_database_symlink_is_rejected(tmp_path):
    path = db_path(tmp_path)
    path.parent.mkdir()
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not used")
    path.symlink_to(target)

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)

    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"


def test_parent_symlink_is_rejected(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    os.chmod(real_parent, 0o700)
    link_parent = tmp_path / "link"
    link_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(LoginError) as caught:
        WriteJournal(link_parent / "write-attempts.sqlite3")

    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"


def test_connection_setup_failure_closes_open_connection(tmp_path, monkeypatch):
    path = db_path(tmp_path)
    closed = []

    class FailingConnection:
        def execute(self, statement, *args):
            if statement == "PRAGMA foreign_keys = ON":
                raise sqlite3.OperationalError("simulated pragma failure")
            return None

        def close(self):
            closed.append(True)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: FailingConnection())

    with pytest.raises(LoginError) as caught:
        WriteJournal(path)

    assert caught.value.code == "WRITE_JOURNAL_UNAVAILABLE"
    assert closed == [True]
