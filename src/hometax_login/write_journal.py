from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Literal

from .errors import LoginError

WriteStatus = Literal["in_flight", "unknown", "complete", "rejected"]
FinishStatus = Literal["unknown", "complete", "rejected"]

_SCHEMA_VERSION = 2
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{16,100}$")


class WriteJournal:
    """Durable idempotency journal for external HomeTax write attempts."""

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout
        self._created = self._prepare_path()
        self._with_connection(self._ensure_schema, check_schema=False)

    def begin(
        self,
        plan_id: str,
        target_key: str,
        payload_hash: str,
        *,
        unique_target: bool = False,
        guard_keys: tuple[str, ...] = (),
    ) -> Literal["started", "complete"]:
        self._validate_plan_id(plan_id)
        self._validate_hash("target_key", target_key)
        self._validate_hash("payload_hash", payload_hash)
        for guard in guard_keys:
            self._validate_hash("guard_key", guard)

        def run(conn: sqlite3.Connection) -> Literal["started", "complete"]:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT target_key, payload_hash, status
                FROM write_attempts
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row:
                existing_target, existing_payload, status = row
                if existing_target != target_key or existing_payload != payload_hash:
                    raise LoginError(
                        "WRITE_IDEMPOTENCY_CONFLICT",
                        "동일 작업 ID의 거래처 또는 요청 내용이 다릅니다.",
                        409,
                    )
                if status == "complete":
                    return "complete"
                if status in {"in_flight", "unknown"}:
                    raise LoginError(
                        "WRITE_OUTCOME_UNKNOWN",
                        "같은 대상 쓰기 작업의 최종 결과를 확인해야 합니다.",
                        409,
                    )
                raise LoginError(
                    "WRITE_REJECTED",
                    "거절된 쓰기 작업은 재시도할 수 없습니다.",
                    409,
                )

            if guard_keys:
                # Old journals do not contain content hashes. Never guess that an unresolved
                # legacy dispatch is unrelated: its outcome must be reconciled before issuing.
                legacy = conn.execute(
                    """SELECT 1 FROM write_attempts a
                       WHERE status IN ('in_flight', 'unknown')
                       AND NOT EXISTS (SELECT 1 FROM write_guards g WHERE g.plan_id = a.plan_id)
                       LIMIT 1"""
                ).fetchone()
                placeholders = ",".join("?" for _ in guard_keys)
                guarded = conn.execute(
                    f"""SELECT 1 FROM write_guards g JOIN write_attempts a USING(plan_id)
                        WHERE g.guard_key IN ({placeholders})
                        AND a.status IN ('in_flight', 'unknown') LIMIT 1""",
                    guard_keys,
                ).fetchone()
                if legacy or guarded:
                    raise LoginError(
                        "WRITE_OUTCOME_UNKNOWN",
                        "미확정 쓰기 기록이 있습니다. 기존 발행 결과를 먼저 확인하세요.",
                        409,
                    )

            blocking = conn.execute(
                """
                SELECT 1
                FROM write_attempts
                WHERE target_key = ?
                  AND status IN ('in_flight', 'unknown')
                  AND plan_id <> ?
                LIMIT 1
                """,
                (target_key, plan_id),
            ).fetchone()
            if blocking:
                raise LoginError(
                    "WRITE_OUTCOME_UNKNOWN",
                    "같은 대상 쓰기 작업의 최종 결과를 확인해야 합니다.",
                    409,
                )

            if unique_target:
                complete = conn.execute(
                    """
                    SELECT 1
                    FROM write_attempts
                    WHERE target_key = ?
                      AND status = 'complete'
                      AND plan_id <> ?
                    LIMIT 1
                    """,
                    (target_key, plan_id),
                ).fetchone()
                if complete:
                    raise LoginError(
                        "WRITE_TARGET_COMPLETE",
                        "이미 완료된 대상 쓰기 작업입니다.",
                        409,
                    )

            conn.execute(
                """
                INSERT INTO write_attempts (plan_id, target_key, payload_hash, status)
                VALUES (?, ?, ?, 'in_flight')
                """,
                (plan_id, target_key, payload_hash),
            )
            conn.executemany(
                "INSERT INTO write_guards (plan_id, guard_key) VALUES (?, ?)",
                [(plan_id, key) for key in dict.fromkeys((target_key, *guard_keys))],
            )
            return "started"

        return self._with_connection(run)

    def finish(self, plan_id: str, status: FinishStatus) -> None:
        self._validate_plan_id(plan_id)
        if status not in {"unknown", "complete", "rejected"}:
            raise LoginError("WRITE_JOURNAL_INVALID", "저널 상태 값이 올바르지 않습니다.", 422)

        def run(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM write_attempts WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if not row:
                raise LoginError(
                    "WRITE_ATTEMPT_NOT_FOUND",
                    "기록된 거래처 쓰기 작업이 없습니다.",
                    404,
                )
            current = row[0]
            if current == status:
                return
            if current != "in_flight":
                raise LoginError(
                    "WRITE_ALREADY_FINALIZED",
                    "이미 최종 처리된 거래처 쓰기 작업입니다.",
                    409,
                )
            conn.execute(
                """
                UPDATE write_attempts
                SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE plan_id = ? AND status = 'in_flight'
                """,
                (status, plan_id),
            )

        self._with_connection(run)

    def _prepare_path(self) -> bool:
        try:
            parent = self.path.parent
            parent_existed = parent.exists()
            if parent_existed:
                if parent.is_symlink() or not parent.is_dir():
                    raise self._unavailable()
                if parent.stat(follow_symlinks=False).st_mode & 0o077:
                    raise self._unavailable()
            else:
                parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if parent.is_symlink() or not parent.is_dir():
                    raise self._unavailable()
                os.chmod(parent, 0o700)

            if self.path.exists():
                if self.path.is_symlink():
                    raise self._unavailable()
                info = self.path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                    raise self._unavailable()
                os.chmod(self.path, 0o600)
                return False

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
            return True
        except OSError as exc:
            raise self._unavailable() from exc

    def _connect(self) -> sqlite3.Connection:
        conn = None
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = FULL")
            return conn
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise self._unavailable() from exc

    def _with_connection(self, action, *, check_schema: bool = True):
        conn = self._connect()
        try:
            self._check_database(conn)
            if check_schema:
                self._check_schema(conn)
            result = action(conn)
            conn.commit()
            return result
        except LoginError:
            try:
                conn.rollback()
            finally:
                raise
        except (OSError, sqlite3.Error) as exc:
            try:
                conn.rollback()
            finally:
                raise self._unavailable() from exc
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        has_table = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'write_attempts'
            """
        ).fetchone()
        if has_table:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
                # Validate the known old table before an additive, atomic migration.
                self._check_attempt_columns(conn)
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
                    self._create_guards(conn)
                    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._check_schema(conn)
            return
        if not self._created:
            raise self._unavailable()

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE write_attempts (
                plan_id TEXT PRIMARY KEY,
                target_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('in_flight', 'unknown', 'complete', 'rejected')
                ),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX write_attempts_target_open_idx
            ON write_attempts (target_key)
            WHERE status IN ('in_flight', 'unknown')
            """
        )
        self._create_guards(conn)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._check_schema(conn)

    def _check_database(self, conn: sqlite3.Connection) -> None:
        result = conn.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise self._unavailable()

    def _check_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        self._check_attempt_columns(conn)
        guards = {row[1] for row in conn.execute("PRAGMA table_info(write_guards)")}
        if version != _SCHEMA_VERSION or guards != {"plan_id", "guard_key"}:
            raise self._unavailable()

    @staticmethod
    def _create_guards(conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE write_guards (
                plan_id TEXT NOT NULL REFERENCES write_attempts(plan_id),
                guard_key TEXT NOT NULL, PRIMARY KEY (plan_id, guard_key))"""
        )
        conn.execute("CREATE INDEX write_guards_key_idx ON write_guards(guard_key)")

    def _check_attempt_columns(self, conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(write_attempts)").fetchall()
        names = {row[1] for row in columns}
        expected = {"plan_id", "target_key", "payload_hash", "status", "created_at", "updated_at"}
        if names != expected:
            raise self._unavailable()

        bad_status = conn.execute(
            """
            SELECT 1
            FROM write_attempts
            WHERE status NOT IN ('in_flight', 'unknown', 'complete', 'rejected')
            LIMIT 1
            """
        ).fetchone()
        if bad_status:
            raise self._unavailable()

    @staticmethod
    def _validate_hash(name: str, value: str) -> None:
        if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
            raise LoginError(
                "WRITE_JOURNAL_INVALID",
                f"{name} 값은 sha256 hex 64자여야 합니다.",
                422,
            )

    @staticmethod
    def _validate_plan_id(plan_id: str) -> None:
        if not isinstance(plan_id, str) or not _PLAN_ID_RE.fullmatch(plan_id):
            raise LoginError("WRITE_JOURNAL_INVALID", "작업 ID 형식이 올바르지 않습니다.", 422)

    @staticmethod
    def _unavailable() -> LoginError:
        return LoginError(
            "WRITE_JOURNAL_UNAVAILABLE",
            "거래처 쓰기 저널을 사용할 수 없습니다.",
            503,
        )
