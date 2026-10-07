"""按编号批准报销单（approve）的端到端测试。

从项目根目录执行：

    python -m unittest test_expense_approve -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / approve / list /
summary / export 公开命令：提交与批准分别在独立进程中完成并退出后，再由另一
个新进程对同一 SQLite 数据库查询，验证批准结果已持久落盘。每个用例使用独立
的临时 SQLite 文件与虚构人员；仅使用 Python 标准库，不访问网络或第三方包；
金额一律按整数分（整数比较）校验，不按人民币元或浮点数比较。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 旧库（五列结构）建表语句，没有 attachment_note 列。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

MAX_ID = 2**63 - 1


def run_cli(db_path: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def run_cli_no_db(*args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（不带 --db），用于测试缺少 --db。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def submit(
    db_path: Path,
    submitter: str,
    purpose: str,
    amount: str,
    attachment_note: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "submit",
        "--submitter", submitter,
        "--purpose", purpose,
        "--amount", amount,
    ]
    if attachment_note is not None:
        argv.extend(["--attachment-note", attachment_note])
    return run_cli(db_path, *argv)


def approve(db_path: Path | str, raw_id: str) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "approve", "--id", raw_id)


def list_records(db_path: Path, submitter: str) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "list", "--submitter", submitter)


class ApproveDemoScenarioTest(unittest.TestCase):
    """需求给定的演示场景：演示甲两笔费用，批准第一笔后查询、汇总与导出。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_approve_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "demo.sqlite"

    def test_approve_demo_scenario(self) -> None:
        # 用已有 submit 准备演示甲的 12.30 元交通费（附“演示车票”）与 0.01 元餐费。
        first = submit(self.db_path, "演示甲", "交通费", "12.30", "演示车票")
        self.assertEqual(first.returncode, 0, msg=f"submit 失败: {first.stderr}")
        second = submit(self.db_path, "演示甲", "餐费", "0.01")
        self.assertEqual(second.returncode, 0, msg=f"submit 失败: {second.stderr}")
        self.assertEqual(json.loads(first.stdout)["id"], 1)
        self.assertEqual(json.loads(second.stdout)["id"], 2)

        # 批准编号 1：退出 0，标准错误为空，标准输出为一行记录 JSON，
        # 字段与 list 单条记录一致，仅 status 由 pending 变为 approved。
        result = approve(self.db_path, "1")
        self.assertEqual(result.returncode, 0, msg=f"approve 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1, msg="标准输出应为一行 JSON")
        record = json.loads(result.stdout)
        self.assertEqual(
            record,
            {
                "id": 1,
                "submitter": "演示甲",
                "purpose": "交通费",
                "amount_minor": 1230,
                "status": "approved",
                "attachment_note": "演示车票",
            },
        )

        # 新进程查询演示甲：第一笔 approved 且说明原样保留，第二笔仍 pending。
        listing = list_records(self.db_path, "演示甲")
        self.assertEqual(listing.returncode, 0, msg=listing.stderr)
        records = json.loads(listing.stdout)
        self.assertEqual(records[0]["status"], "approved")
        self.assertEqual(records[0]["attachment_note"], "演示车票")
        self.assertEqual(records[0]["amount_minor"], 1230)
        self.assertEqual(records[1]["status"], "pending")
        self.assertEqual(records[1]["amount_minor"], 1)
        self.assertNotIn("attachment_note", records[1])
        self.assertEqual([r["id"] for r in records], [1, 2])

        # 汇总仍统计全部记录：两笔、1231 分；预算 15 元时余额 269 分。
        summary = run_cli(self.db_path, "summary", "--submitter", "演示甲", "--budget", "15")
        self.assertEqual(summary.returncode, 0, msg=summary.stderr)
        summary_payload = json.loads(summary.stdout)
        self.assertEqual(summary_payload["count"], 2)
        self.assertEqual(summary_payload["total_amount_minor"], 1231)
        self.assertEqual(summary_payload["budget_amount_minor"], 1500)
        self.assertEqual(summary_payload["remaining_amount_minor"], 269)

        # export 仍为五列 CSV，并输出记录的实际状态。
        exported = run_cli(self.db_path, "export", "--submitter", "演示甲")
        self.assertEqual(exported.returncode, 0, msg=exported.stderr)
        rows = list(csv.reader(io.StringIO(exported.stdout)))
        self.assertEqual(
            rows,
            [
                ["id", "submitter", "purpose", "amount_minor", "status"],
                ["1", "演示甲", "交通费", "1230", "approved"],
                ["2", "演示甲", "餐费", "1", "pending"],
            ],
        )


class ApproveIdempotencyTest(unittest.TestCase):
    """重复批准已 approved 的记录：返回同一记录、退出 0、不改变记录。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_approve_idem_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "idem.sqlite"
        created = submit(self.db_path, "幂等测试·虚构甲", "用途", "12.30", "便签")
        self.assertEqual(created.returncode, 0, msg=created.stderr)
        first = approve(self.db_path, "1")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.first_stdout = first.stdout

    def _db_row(self) -> tuple:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

    def test_repeated_approve_returns_same_record_without_change(self) -> None:
        row_before = self._db_row()
        for raw_id in ("1", "01", "0001"):
            with self.subTest(raw_id=raw_id):
                result = approve(self.db_path, raw_id)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, self.first_stdout)
                self.assertEqual(json.loads(result.stdout)["status"], "approved")
                self.assertEqual(self._db_row(), row_before)

    def test_approved_record_visible_from_new_process(self) -> None:
        """批准进程退出后，新进程查询仍能看到 approved。"""
        listing = list_records(self.db_path, "幂等测试·虚构甲")
        records = json.loads(listing.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "approved")


class ApproveIdValidationTest(unittest.TestCase):
    """--id 校验规则与“编号错误先于数据库操作”的优先级。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_approve_id_")
        self.addCleanup(self._tmpdir.cleanup)
        # 数据库父目录故意不存在：编号有效时会以数据库错误（退出 1）失败；
        # 编号无效时必须先报编号错误（退出 2），且不创建任何文件或目录。
        self.bad_db = Path(self._tmpdir.name) / "missing_parent" / "nope.sqlite"
        self.assertFalse(self.bad_db.parent.exists())

    def assert_invalid_id(self, raw_id: str) -> None:
        result = approve(self.bad_db, raw_id)
        self.assertEqual(
            result.returncode, 2,
            msg=f"编号 {raw_id!r} 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertIn("编号无效", result.stderr)
        # 编号校验先于数据库操作：不得创建数据库文件或其父目录。
        self.assertFalse(
            self.bad_db.exists(), msg=f"编号无效时不得创建数据库文件: {raw_id!r}"
        )
        self.assertFalse(
            self.bad_db.parent.exists(),
            msg=f"编号无效时不得创建数据库父目录: {raw_id!r}",
        )

    def test_invalid_ids_are_rejected_before_database(self) -> None:
        invalid_ids = [
            "",            # 空串
            "   ", "\t\n", # 仅空白
            "0", "0000",   # 零（含前导零）
            "-1",          # 负数
            "1.5", "1.0",  # 小数
            "+1",          # 带正号
            "１２",        # 全角（非 ASCII）数字
            "abc", "1a", "a1", "0x1", "1e0", "1_000", "1 2",  # 非纯数字或含内部空白
            "9223372036854775808",   # 上限 + 1
            "99999999999999999999",  # 越界
        ]
        for raw_id in invalid_ids:
            with self.subTest(raw_id=raw_id):
                self.assert_invalid_id(raw_id)

    def test_outer_whitespace_is_stripped_before_validation(self) -> None:
        """首尾空白去除后为合法编号：通过校验，对坏路径直接报数据库错误。

        编号校验先于数据库操作，故在父目录缺失的坏路径上，合法编号应得到
        退出码 1（数据库操作失败），而不是编号错误；首尾空白不影响判定。
        """
        for raw_id in (" 1", "1 ", "\t1\n", "  001  "):
            with self.subTest(raw_id=raw_id):
                result = approve(self.bad_db, raw_id)
                self.assertEqual(result.returncode, 1, msg=result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("数据库操作失败", result.stderr)

    def test_boundary_and_leading_zeros_are_accepted_as_syntax(self) -> None:
        """上限值与前导零通过编号校验：新库场景下落到“报销单不存在”（退出 2）。"""
        fresh_db = Path(self._tmpdir.name) / "fresh.sqlite"
        for raw_id in ("1", "001", str(MAX_ID)):
            with self.subTest(raw_id=raw_id):
                result = approve(fresh_db, raw_id)
                self.assertEqual(result.returncode, 2, msg=result.stderr)
                self.assertIn("报销单不存在", result.stderr)
                self.assertEqual(result.stdout, "")
                # 父目录存在的新库沿用现有建库行为（文件被创建，表为空）。
                self.assertTrue(fresh_db.exists())


class ApproveMissingArgumentsTest(unittest.TestCase):
    """缺少 --db、--id 或其参数值：退出 2 并指出缺少的参数。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_approve_miss_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "miss.sqlite"
        created = submit(self.db_path, "缺参测试·虚构甲", "用途", "1")
        self.assertEqual(created.returncode, 0, msg=created.stderr)

    def test_missing_db(self) -> None:
        result = run_cli_no_db("approve", "--id", "1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--db", result.stderr)

    def test_missing_id_option(self) -> None:
        result = run_cli(self.db_path, "approve")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--id", result.stderr)

    def test_missing_id_value(self) -> None:
        result = run_cli(self.db_path, "approve", "--id")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--id", result.stderr)


class ApproveDatabaseCasesTest(unittest.TestCase):
    """编号不存在、状态非法、旧库兼容、数据库故障等场景。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_approve_db_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "cases.sqlite"
        created = submit(self.db_path, "用例·虚构甲", "用途", "12.30")
        self.assertEqual(created.returncode, 0, msg=created.stderr)

    def test_nonexistent_id(self) -> None:
        result = approve(self.db_path, "99")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("报销单不存在", result.stderr)
        # 不新增记录：仍只有提交的那一笔，状态不变。
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT id, status FROM expenses").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "pending")])

    def test_unexpected_status_cannot_be_approved(self) -> None:
        """状态既不是 pending 也不是 approved 时退出 2，提示“当前状态不能批准”。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO expenses"
                " (submitter, purpose, amount_minor, status, attachment_note)"
                " VALUES (?, ?, ?, ?, ?)",
                ("用例·虚构乙", "用途", 100, "rejected", None),
            )
            conn.commit()
        finally:
            conn.close()
        result = approve(self.db_path, "2")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("当前状态不能批准", result.stderr)
        conn = sqlite3.connect(self.db_path)
        try:
            status = conn.execute(
                "SELECT status FROM expenses WHERE id = 2"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "rejected")

    def test_new_database_with_existing_parent_is_created_then_not_found(self) -> None:
        fresh_db = Path(self._tmpdir.name) / "brand-new.sqlite"
        self.assertFalse(fresh_db.exists())
        result = approve(fresh_db, "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("报销单不存在", result.stderr)
        self.assertTrue(fresh_db.exists(), msg="应沿用现有建库行为创建新库")
        conn = sqlite3.connect(fresh_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_legacy_five_column_database(self) -> None:
        """旧五列库：批准后保留编号、提交人、用途、整数分金额，不输出说明字段。"""
        legacy_db = Path(self._tmpdir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO expenses"
                " (id, submitter, purpose, amount_minor, status)"
                " VALUES (7, '旧库·虚构甲', '交通费', 1230, 'pending')"
            )
            conn.commit()
        finally:
            conn.close()

        result = approve(legacy_db, "007")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)
        self.assertEqual(
            record,
            {
                "id": 7,
                "submitter": "旧库·虚构甲",
                "purpose": "交通费",
                "amount_minor": 1230,
                "status": "approved",
            },
            msg="旧记录没有附件说明，不应输出 attachment_note",
        )
        self.assertNotIn("attachment_note", record)

        # 新进程复查：已落盘为 approved，说明列已补齐且为 NULL。
        again = approve(legacy_db, "7")
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        self.assertEqual(again.stdout, result.stdout)
        conn = sqlite3.connect(legacy_db)
        try:
            row = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses WHERE id = 7"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (7, "旧库·虚构甲", "交通费", 1230, "approved", None))

    def test_database_open_failure_exits_1(self) -> None:
        """数据库无法打开时退出 1 并报告数据库操作失败，标准输出为空。"""
        unreachable = Path(self._tmpdir.name) / "no_such_parent" / "x.sqlite"
        result = approve(unreachable, "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("数据库操作失败", result.stderr)

    def test_database_save_failure_exits_1_and_keeps_pending(self) -> None:
        """只读数据库无法保存时退出 1，记录保持 pending。"""
        self.db_path.chmod(0o444)
        try:
            result = approve(self.db_path, "1")
        finally:
            self.db_path.chmod(0o644)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("数据库操作失败", result.stderr)
        conn = sqlite3.connect(self.db_path)
        try:
            status = conn.execute(
                "SELECT status FROM expenses WHERE id = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
