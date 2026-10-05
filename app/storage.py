"""执行会话持久化：SQLite（进程重启后会话与已确认记录仍在）。

设计要点：
- 单文件 SQLite，默认 /data/sessions.db（容器内挂命名卷），可用环境变量
  DISPATCH_SESSION_DB 覆盖；无外部依赖，随 Compose 一起起；
- 所有写事务走 process 内 RLock + BEGIN IMMEDIATE：同一进程的并发上报在
  临界区内串行判定，只有一份能写入；SQLite 文件锁同时防多进程抢写；
- 记录按 (session_id, idx) 主键落库，天然防重复下标；整批 INSERT 在同一
  事务内，任一条失败即回滚，满足「整批不入库」；
- session_batches 按 (session_id, start_index) 主键记录每个已确认批次的
  边界（start_index + batch_size）与首次成功时的完整回执（receipt_json），
  与逐段记录在同一写事务内落库：重试判定以批次边界为准，只有起点、长度、
  内容三者都与某已确认整批一致才算原样重试（deduped），且回执保持首次
  成功时的值不随后续批次漂移；
- session_revisions 按 (session_id, revision_no) 主键保存计量表更正：
  每次提交存原始变更（下标 + 完整替换记录）与叠加后的整段修订台账，
  修订号与会话进度无关、从 1 起单调递增；最初上报的 session_records 与
  session_batches 永不被更正改写；
- session_settlement_vouchers 按 (session_id, voucher_no) 记录出单次序，
  (session_id, revision_no) 主键保证同一修订号只出一张凭证（重试幂等）：
  每张凭证冻结出单时该修订的整段台账（ledger_json）与完整结算结果
  （settlement_json）、首张/应补/应退计费信息与出单时间；凭证一经落盘
  即不可变，后续更正只新增修订行，从不改写已有凭证；
- 旧库只有 sessions / session_records 两张表时，session_batches、
  session_revisions 与 session_settlement_vouchers 由 CREATE TABLE IF NOT
  EXISTS 自动补齐（空表）：查询与从 next_index 续报不受影响；已确认区间内
  没有批次边界可核对，不按前缀猜测去重。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

# 单条记录为半小时实测，计划最长 200 段，记录列只存 JSON，无需额外规模设计。
# session_batches 为批次边界与首收回执表；session_revisions 为计量表更正表
# （存每次提交的原始变更与叠加后的整段修订台账）；session_settlement_vouchers
# 为逐版结算凭证表（冻结该版台账、完整结算结果与首单/补退计费），均为后加的
# 新表，旧库打开时由 IF NOT EXISTS 自动补建。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    dispatch_json TEXT NOT NULL,
    plan_snapshot_json TEXT NOT NULL,
    audit_tolerance_kwh REAL NOT NULL,
    num_periods INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_records (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (session_id, idx),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS session_batches (
    session_id TEXT NOT NULL,
    start_index INTEGER NOT NULL,
    batch_size INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY (session_id, start_index),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS session_revisions (
    session_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    changes_json TEXT NOT NULL,
    ledger_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, revision_no),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS session_settlement_vouchers (
    voucher_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    voucher_no INTEGER NOT NULL,
    revision_no INTEGER NOT NULL,
    current_revision_at_issue INTEGER NOT NULL,
    ledger_json TEXT NOT NULL,
    settlement_json TEXT NOT NULL,
    total_cost_yuan REAL NOT NULL,
    billing_type TEXT NOT NULL,
    amount_yuan REAL NOT NULL,
    relative_to_revision INTEGER,
    relative_to_voucher_no INTEGER,
    issued_at TEXT NOT NULL,
    PRIMARY KEY (session_id, revision_no),
    UNIQUE (session_id, voucher_no),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_vouchers_order
    ON session_settlement_vouchers(session_id, voucher_no);
"""


def default_db_path() -> str:
    """数据库路径：环境变量优先；容器内优先可写的 /data，否则落到工作目录 data/。"""
    env = os.environ.get("DISPATCH_SESSION_DB")
    if env:
        return env
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/sessions.db"
    return os.path.join(os.getcwd(), "data", "sessions.db")


class SessionStore:
    """SQLite 会话存储。方法均为短事务，调用方见 app/sessions.py。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            # WAL：读不阻塞写、写不阻塞读；同一时刻仍只有一个写事务。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """加进程锁后开启 IMMEDIATE 事务，异常自动回滚。

        会话追加的「查重 — 边界核对 — 审计 — 入库」整段都在此临界区内完成，
        保证并发上报只有一份生效。
        """
        conn = self._connect()
        try:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_session(
        self,
        session_id: str,
        dispatch_json: str,
        plan_snapshot_json: str,
        audit_tolerance_kwh: float,
        num_periods: int,
        created_at: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions"
                "(session_id, dispatch_json, plan_snapshot_json, audit_tolerance_kwh, "
                "num_periods, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    dispatch_json,
                    plan_snapshot_json,
                    audit_tolerance_kwh,
                    num_periods,
                    created_at,
                ),
            )
            conn.commit()

    def get_session_row(self, session_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()

    def list_session_rows(self) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT s.*, "
                    " (SELECT COUNT(*) FROM session_records r WHERE r.session_id = s.session_id)"
                    " AS confirmed_periods, "
                    " (SELECT MAX(stored_at) FROM session_records r WHERE r.session_id = s.session_id)"
                    " AS updated_at, "
                    " (SELECT record_json FROM session_records r WHERE r.session_id = s.session_id"
                    "  ORDER BY r.idx DESC LIMIT 1) AS last_record_json"
                    " FROM sessions s ORDER BY s.created_at, s.session_id"
                ).fetchall()
            )

    def count_records(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM session_records WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["c"])

    def count_records_readonly(self, session_id: str) -> int:
        """短连接只读计数（写事务之外使用）。"""
        with self._lock, self._connect() as conn:
            return self.count_records(conn, session_id)

    def get_records_from(
        self, conn: sqlite3.Connection, session_id: str, start_index: int
    ) -> List[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT idx, record_json FROM session_records "
                "WHERE session_id = ? AND idx >= ? ORDER BY idx",
                (session_id, start_index),
            ).fetchall()
        )

    def get_all_records(self, session_id: str) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT idx, record_json, stored_at FROM session_records "
                    "WHERE session_id = ? ORDER BY idx",
                    (session_id,),
                ).fetchall()
            )

    def insert_record(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        idx: int,
        record_json: str,
        stored_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO session_records(session_id, idx, record_json, stored_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, idx, record_json, stored_at),
        )

    def get_batch(
        self, conn: sqlite3.Connection, session_id: str, start_index: int
    ) -> Optional[sqlite3.Row]:
        """取 start_index 处已确认批次的边界与首收回执；无边界记录返回 None。"""
        return conn.execute(
            "SELECT start_index, batch_size, receipt_json, stored_at FROM session_batches "
            "WHERE session_id = ? AND start_index = ?",
            (session_id, start_index),
        ).fetchone()

    def insert_batch(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        start_index: int,
        batch_size: int,
        receipt_json: str,
        stored_at: str,
    ) -> None:
        """与逐段记录同一事务写入批次边界与首次成功回执（重试时原样取回）。"""
        conn.execute(
            "INSERT INTO session_batches"
            "(session_id, start_index, batch_size, receipt_json, stored_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, start_index, batch_size, receipt_json, stored_at),
        )

    def get_latest_revision(
        self, conn: sqlite3.Connection, session_id: str
    ) -> Optional[sqlite3.Row]:
        """取当前最新修订行（修订号最大的一条）；从未更正返回 None（视为修订号 0）。"""
        return conn.execute(
            "SELECT revision_no, changes_json, ledger_json, created_at"
            " FROM session_revisions WHERE session_id = ?"
            " ORDER BY revision_no DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def list_revision_rows(self, session_id: str) -> List[sqlite3.Row]:
        """按修订号升序列出会话的全部更正行（首条即修订号 1）。"""
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT revision_no, changes_json, ledger_json, created_at"
                    " FROM session_revisions WHERE session_id = ?"
                    " ORDER BY revision_no",
                    (session_id,),
                ).fetchall()
            )

    def get_current_revision_no(self, session_id: str) -> int:
        """当前修订号：无更正为 0，否则为最大修订号。"""
        with self._lock, self._connect() as conn:
            row = self.get_latest_revision(conn, session_id)
            return int(row["revision_no"]) if row is not None else 0

    def insert_revision(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        revision_no: int,
        changes_json: str,
        ledger_json: str,
        created_at: str,
    ) -> None:
        """与「核对修订号、整段重核」同一事务原子写入新修订号、变更与整段台账。"""
        conn.execute(
            "INSERT INTO session_revisions"
            "(session_id, revision_no, changes_json, ledger_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, revision_no, changes_json, ledger_json, created_at),
        )

    def get_all_records_in_txn(
        self, conn: sqlite3.Connection, session_id: str
    ) -> List[sqlite3.Row]:
        """写事务内取全部最初上报记录（按下标升序）；修订号 0 的台账即它。"""
        return list(
            conn.execute(
                "SELECT idx, record_json, stored_at FROM session_records "
                "WHERE session_id = ? ORDER BY idx",
                (session_id,),
            ).fetchall()
        )

    def get_revision_row(
        self, conn: sqlite3.Connection, session_id: str, revision_no: int
    ) -> Optional[sqlite3.Row]:
        """写事务内取指定修订号的更正行；不存在（含修订号 0）返回 None。"""
        return conn.execute(
            "SELECT revision_no, changes_json, ledger_json, created_at"
            " FROM session_revisions WHERE session_id = ? AND revision_no = ?",
            (session_id, revision_no),
        ).fetchone()

    def count_vouchers(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM session_settlement_vouchers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["c"])

    def get_voucher(
        self, conn: sqlite3.Connection, session_id: str, revision_no: int
    ) -> Optional[sqlite3.Row]:
        """按修订号取已出凭证；该修订未出单返回 None（重试幂等判定以此为准）。"""
        return conn.execute(
            "SELECT * FROM session_settlement_vouchers"
            " WHERE session_id = ? AND revision_no = ?",
            (session_id, revision_no),
        ).fetchone()

    def get_latest_voucher(
        self, conn: sqlite3.Connection, session_id: str
    ) -> Optional[sqlite3.Row]:
        """按出单次序取最后一张凭证（voucher_no 最大）；从未出单返回 None。"""
        return conn.execute(
            "SELECT * FROM session_settlement_vouchers"
            " WHERE session_id = ? ORDER BY voucher_no DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def list_voucher_rows(self, session_id: str) -> List[sqlite3.Row]:
        """按出单次序（voucher_no 升序）列出会话的全部凭证。"""
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM session_settlement_vouchers"
                    " WHERE session_id = ? ORDER BY voucher_no",
                    (session_id,),
                ).fetchall()
            )

    def insert_voucher(
        self,
        conn: sqlite3.Connection,
        voucher_id: str,
        session_id: str,
        voucher_no: int,
        revision_no: int,
        current_revision_at_issue: int,
        ledger_json: str,
        settlement_json: str,
        total_cost_yuan: float,
        billing_type: str,
        amount_yuan: float,
        relative_to_revision: Optional[int],
        relative_to_voucher_no: Optional[int],
        issued_at: str,
    ) -> None:
        """与「核对修订号、取该版台账、核算」同一事务原子落盘凭证。

        (session_id, revision_no) 主键使同修订并发出单只有一份成功；
        (session_id, voucher_no) 唯一键使出单次序号不重复。
        """
        conn.execute(
            "INSERT INTO session_settlement_vouchers"
            "(voucher_id, session_id, voucher_no, revision_no, current_revision_at_issue,"
            " ledger_json, settlement_json, total_cost_yuan, billing_type, amount_yuan,"
            " relative_to_revision, relative_to_voucher_no, issued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                voucher_id,
                session_id,
                voucher_no,
                revision_no,
                current_revision_at_issue,
                ledger_json,
                settlement_json,
                total_cost_yuan,
                billing_type,
                amount_yuan,
                relative_to_revision,
                relative_to_voucher_no,
                issued_at,
            ),
        )


_store: Optional[SessionStore] = None
_store_lock = threading.Lock()


def configure_store(db_path: Optional[str]) -> Optional[SessionStore]:
    """替换全局存储（测试用同一路径重建即可模拟进程重启）；

    传 None 表示重置为「未初始化」状态，下一次 get_store() 再按默认路径懒加载，
    避免测试拆卸时意外创建工作目录下的 data/sessions.db。
    """
    global _store
    with _store_lock:
        _store = SessionStore(db_path) if db_path is not None else None
    return _store


def get_store() -> SessionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SessionStore()
    return _store
