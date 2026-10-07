"""list 可选 --status 状态筛选的回归测试。

从项目根目录执行：

    python -m unittest test_expense_status

全部断言通过时退出码为 0，任一断言失败时为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用四个独立
提交进程建立固定样例（演示甲三笔、演示乙一笔），再由另外两个进程批准第二、
第四笔，随后每一次 list 都在全新进程中进行，验证筛选结果在进程退出后仍可
重复读取。状态校验边界（空串、纯空白、大小写不同、其他取值、缺少参数值、
校验顺序与数据库父目录缺失）同样经由公开命令验证；旧库兼容部分先直接用
SQLite 按五列结构建库并写入一条 pending 记录，再让产品命令在其上筛选。

约束：
- 每个用例使用各自独立的临时 SQLite 文件与虚构人员，结束后自动清理，
  重复执行不依赖任何已有演示数据；
- 仅使用 Python 标准库，不访问网络或第三方包；
- 金额一律按整数分（整数）核对，不按人民币元或浮点数解释；
- 不修改产品代码、README 或其他入口，本文件只验证既有行为。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 虚构人员，避免与任何真实数据冲突。
SUBMITTER_A = "演示甲"
SUBMITTER_B = "演示乙"
SUBMITTER_UNKNOWN = "演示丙"

# 第一笔交通费随单保存的附件说明。
NOTE_TICKET = "演示车票"

# 固定样例的提交顺序（提交人、用途、命令行金额、整数分金额、附件说明）。
# 1 演示甲 交通费 12.30 元（1230 分，带说明“演示车票”）
# 2 演示乙 办公费    5 元（ 500 分，无说明）
# 3 演示甲 餐费    0.01 元（   1 分，无说明）
# 4 演示甲 材料费    2 元（ 200 分，无说明）
FIXED_SUBMISSIONS = [
    (SUBMITTER_A, "交通费", "12.30", 1230, NOTE_TICKET),
    (SUBMITTER_B, "办公费", "5", 500, None),
    (SUBMITTER_A, "餐费", "0.01", 1, None),
    (SUBMITTER_A, "材料费", "2", 200, None),
]

# 第二、第四笔随后被批准。下列六元组为批准落盘后数据库内的最终内容
# （id、提交人、用途、整数分金额、状态、附件说明），按 id 升序。
EXPECTED_ROWS = [
    (1, SUBMITTER_A, "交通费", 1230, "pending", NOTE_TICKET),
    (2, SUBMITTER_B, "办公费", 500, "approved", None),
    (3, SUBMITTER_A, "餐费", 1, "pending", None),
    (4, SUBMITTER_A, "材料费", 200, "approved", None),
]

# 旧库（五列结构）建表语句：没有 attachment_note 列。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

# 旧库预置的一条 pending 记录：id 7、演示甲、办公费、500 分。
LEGACY_ROW = (7, SUBMITTER_A, "办公费", 500, "pending")


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def submit(
    db_path: Path,
    submitter: str,
    purpose: str,
    amount: str,
    note: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "submit",
        "--submitter", submitter,
        "--purpose", purpose,
        "--amount", amount,
    ]
    if note is not None:
        argv += ["--attachment-note", note]
    return run_cli(db_path, *argv)


def approve(db_path: Path, expense_id: str) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "approve", "--id", expense_id)


# 省略 --status 的哨兵：区别于显式传入空串（空串是待校验的非法值）。
_STATUS_UNSET = object()


def list_records(
    db_path: Path, submitter: str, status: object = _STATUS_UNSET
) -> subprocess.CompletedProcess[str]:
    """在新进程中执行 list；status 为 _STATUS_UNSET 时省略 --status。"""
    argv = ["list", "--submitter", submitter]
    if status is not _STATUS_UNSET:
        argv += ["--status", status]
    return run_cli(db_path, *argv)


def expected_record(row: tuple) -> dict[str, object]:
    """把六元组样例转换为对外记录：无说明时只有五个字段。"""
    expense_id, submitter, purpose, amount_minor, status, note = row
    record: dict[str, object] = {
        "id": expense_id,
        "submitter": submitter,
        "purpose": purpose,
        "amount_minor": amount_minor,
        "status": status,
    }
    if note is not None:
        record["attachment_note"] = note
    return record


class StatusFilterFixedSampleTest(unittest.TestCase):
    """固定样例上的状态筛选：pending/approved/省略状态与记录内容核对。"""

    def setUp(self) -> None:
        # 每个用例独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_status_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "status.sqlite"

        # 依次在四个独立进程中提交固定样例，并逐笔核对提交输出。
        for expected_id, (submitter, purpose, amount, amount_minor, note) in enumerate(
            FIXED_SUBMISSIONS, start=1
        ):
            result = submit(self.db_path, submitter, purpose, amount, note)
            self.assertEqual(
                result.returncode, 0, msg=f"第 {expected_id} 笔提交失败: {result.stderr}"
            )
            self.assertEqual(result.stderr, "")
            record = json.loads(result.stdout)
            self.assertEqual(record["id"], expected_id)
            self.assertEqual(record["submitter"], submitter)
            self.assertEqual(record["purpose"], purpose)
            self.assertIsInstance(record["amount_minor"], int)
            self.assertEqual(record["amount_minor"], amount_minor)
            self.assertEqual(record["status"], "pending")
            if note is None:
                self.assertNotIn("attachment_note", record)
            else:
                self.assertEqual(record["attachment_note"], note)

        # 在新进程中批准第二、第四笔。
        for approved_id in (2, 4):
            result = approve(self.db_path, str(approved_id))
            self.assertEqual(
                result.returncode,
                0,
                msg=f"批准第 {approved_id} 笔失败: {result.stderr}",
            )
            self.assertEqual(result.stderr, "")
            record = json.loads(result.stdout)
            self.assertEqual(record["id"], approved_id)
            self.assertEqual(record["status"], "approved")

        # id -> 对外记录字典的完整期望。
        self.expected = {
            row[0]: expected_record(row) for row in EXPECTED_ROWS
        }

    # -- 辅助 ------------------------------------------------------------

    def assert_success_array(self, result: subprocess.CompletedProcess[str]) -> list:
        """成功查询：退出 0，标准错误为空，标准输出恰为一行 JSON 数组。"""
        self.assertEqual(
            result.returncode, 0, msg=f"list 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "", msg="list 成功时标准错误应为空")
        # print() 恰加一个换行；去掉行尾换行后正文内不得再有换行。
        self.assertTrue(
            result.stdout.endswith("\n"),
            msg=f"输出应以换行结束: {result.stdout!r}",
        )
        body = result.stdout[:-1]
        self.assertNotIn("\n", body, msg="标准输出应只有一行 JSON")
        payload = json.loads(body)  # 非法 JSON 会在此抛出
        self.assertIsInstance(payload, list)
        return payload

    def fetch_all_rows(self) -> list[tuple]:
        """直接读取库内全部六列内容（按 id 升序），不经产品命令。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

    # -- 三个筛选范围 ------------------------------------------------------

    def test_pending_status_returns_only_ids_1_and_3(self) -> None:
        result = list_records(self.db_path, SUBMITTER_A, "pending")
        records = self.assert_success_array(result)
        self.assertEqual([r["id"] for r in records], [1, 3])
        self.assertEqual(records, [self.expected[1], self.expected[3]])
        # 显式核对排序与完整字段：带说明的保留文字，无说明的没有该字段。
        self.assertEqual(records[0]["attachment_note"], NOTE_TICKET)
        self.assertNotIn("attachment_note", records[1])
        for record in records:
            self.assertEqual(record["status"], "pending")
            self.assertIsInstance(record["amount_minor"], int)

    def test_approved_status_returns_only_id_4(self) -> None:
        result = list_records(self.db_path, SUBMITTER_A, "approved")
        records = self.assert_success_array(result)
        self.assertEqual([r["id"] for r in records], [4])
        self.assertEqual(records, [self.expected[4]])
        self.assertNotIn("attachment_note", records[0])
        self.assertIsInstance(records[0]["amount_minor"], int)
        self.assertEqual(records[0]["amount_minor"], 200)

    def test_omitted_status_returns_ids_1_3_4_ascending(self) -> None:
        result = list_records(self.db_path, SUBMITTER_A)
        records = self.assert_success_array(result)
        ids = [r["id"] for r in records]
        self.assertEqual(ids, [1, 3, 4])
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(
            records,
            [self.expected[1], self.expected[3], self.expected[4]],
        )
        # 演示乙的记录不属于演示甲的任何查询结果。
        rendered = result.stdout
        self.assertNotIn(SUBMITTER_B, rendered)

    # -- 空白、前缀与不存在的提交人 ----------------------------------------

    def test_whitespace_around_submitter_and_status_is_trimmed(self) -> None:
        """提交人与状态首尾带空白，结果与不带空白时逐字节一致。"""
        baseline_pending = list_records(self.db_path, SUBMITTER_A, "pending")
        baseline_approved = list_records(self.db_path, SUBMITTER_A, "approved")
        baseline_all = list_records(self.db_path, SUBMITTER_A)
        # 先确认基线本身成功。
        self.assertEqual(baseline_pending.returncode, 0)
        self.assertEqual(baseline_approved.returncode, 0)
        self.assertEqual(baseline_all.returncode, 0)

        cases = [
            ("  演示甲  ", "  pending  ", baseline_pending),
            ("\t演示甲\t", "approved \t", baseline_approved),
            (" 演示甲 ", "\n pending\n", baseline_pending),
            ("  演示甲  ", _STATUS_UNSET, baseline_all),
        ]
        for submitter, status, baseline in cases:
            with self.subTest(submitter=submitter, status=status):
                result = list_records(self.db_path, submitter, status)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, baseline.stdout)

    def test_prefix_and_unknown_submitter_return_empty_array(self) -> None:
        """前缀名称不做模糊匹配，不存在的提交人同样返回空数组。"""
        for submitter in ("演示", "  演示  ", SUBMITTER_UNKNOWN):
            for status in (_STATUS_UNSET, "pending", "approved"):
                with self.subTest(submitter=submitter, status=status):
                    result = list_records(self.db_path, submitter, status)
                    records = self.assert_success_array(result)
                    self.assertEqual(records, [])
                    # 空数组同样是一行 JSON：恰为 [] 加换行。
                    self.assertEqual(result.stdout, "[]\n")

    # -- 筛选只读、不持久化 ------------------------------------------------

    def test_filtering_neither_modifies_expenses_nor_persists_choice(self) -> None:
        """筛选前后复查两人全部记录：费用不增删不改写，选择不落库。"""
        # 筛选前：两人的完整列表与库内全部六列原始内容。
        before_a = list_records(self.db_path, SUBMITTER_A)
        before_b = list_records(self.db_path, SUBMITTER_B)
        self.assertEqual(before_a.returncode, 0, msg=before_a.stderr)
        self.assertEqual(before_b.returncode, 0, msg=before_b.stderr)
        before_rows = self.fetch_all_rows()
        self.assertEqual(before_rows, EXPECTED_ROWS)
        # 演示乙只有一笔，且为已批准的第 2 笔。
        self.assertEqual(json.loads(before_b.stdout), [self.expected[2]])

        # 依次执行两种状态筛选（各在独立新进程中）。
        pending = list_records(self.db_path, SUBMITTER_A, "pending")
        approved = list_records(self.db_path, SUBMITTER_A, "approved")
        self.assertEqual(
            [r["id"] for r in self.assert_success_array(pending)], [1, 3]
        )
        self.assertEqual(
            [r["id"] for r in self.assert_success_array(approved)], [4]
        )

        # 刚按 pending 筛选后省略 --status：仍返回全部三笔，说明查询选择
        # 没有被保存。
        after_pending_then_all = list_records(self.db_path, SUBMITTER_A)
        self.assertEqual(after_pending_then_all.stdout, before_a.stdout)

        # 筛选后复查：两人的完整列表逐字节不变，库内四笔记录逐字段不变。
        after_a = list_records(self.db_path, SUBMITTER_A)
        after_b = list_records(self.db_path, SUBMITTER_B)
        self.assertEqual(after_a.stdout, before_a.stdout)
        self.assertEqual(after_b.stdout, before_b.stdout)
        self.assertEqual(self.fetch_all_rows(), before_rows)
        self.assertEqual(self.fetch_all_rows(), EXPECTED_ROWS)


class StatusFilterValidationTest(unittest.TestCase):
    """--status 取值与参数缺值的校验边界，以及校验与数据库错误的先后。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_status_err_")
        self.addCleanup(self._tmpdir.cleanup)
        # 初始时数据库文件尚不存在；纯参数校验失败不应创建它。
        self.db_path = Path(self._tmpdir.name) / "validation.sqlite"

    def missing_parent_db(self) -> Path:
        return Path(self._tmpdir.name) / "no_such_dir" / "unreachable.sqlite"

    def test_invalid_status_values_exit_2_without_creating_db(self) -> None:
        """空串、纯空白、Pending、rejected 均退出 2 且提示“状态无效”。"""
        for raw in ("", "   ", "\t \n", "Pending", "rejected"):
            with self.subTest(status=raw):
                result = list_records(self.db_path, SUBMITTER_A, raw)
                self.assertEqual(
                    result.returncode,
                    2,
                    msg=f"状态 {raw!r} 应退出 2，实际 {result.returncode}",
                )
                self.assertEqual(result.stdout, "", msg="状态无效时标准输出应为空")
                self.assertIn("状态无效", result.stderr)
                # 校验先于数据库操作：数据库文件不应被创建。
                self.assertFalse(
                    self.db_path.exists(), msg="状态无效时不应创建数据库文件"
                )

    def test_status_flag_without_value_is_rejected(self) -> None:
        """只给 --status 不给值：退出 2 并指出该参数缺少值。"""
        result = run_cli(
            self.db_path,
            "list",
            "--submitter", SUBMITTER_A,
            "--status",
        )
        self.assertEqual(
            result.returncode, 2, msg=f"应退出 2: {result.stdout!r}"
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("--status", result.stderr)
        self.assertIn("缺少参数值", result.stderr)
        self.assertFalse(self.db_path.exists(), msg="不应创建数据库文件")

    def test_blank_submitter_is_reported_before_invalid_status(self) -> None:
        """提交人为空白且状态无效：先报提交人不能为空。"""
        missing_db = self.missing_parent_db()
        result = list_records(missing_db, "   ", "Pending")
        self.assertEqual(
            result.returncode, 2, msg=f"应退出 2: {result.stderr!r}"
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("提交人", result.stderr)
        self.assertIn("不能为空", result.stderr)
        self.assertNotIn("状态无效", result.stderr)
        self.assertNotIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
        self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")

    def test_invalid_status_is_reported_before_database_error(self) -> None:
        """提交人有效、状态无效且父目录不存在：仍先报状态无效，不建库。"""
        missing_db = self.missing_parent_db()
        self.assertFalse(missing_db.parent.exists())
        for raw in ("Pending", "rejected", ""):
            with self.subTest(status=raw):
                result = list_records(missing_db, SUBMITTER_A, raw)
                self.assertEqual(
                    result.returncode, 2, msg=f"应退出 2: {result.stderr!r}"
                )
                self.assertEqual(result.stdout, "")
                self.assertIn("状态无效", result.stderr)
                self.assertNotIn("数据库操作失败", result.stderr)
                self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
                self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")

    def test_valid_args_with_missing_parent_dir_exit_1(self) -> None:
        """参数有效但数据库父目录不存在：退出 1 且提示数据库操作失败。"""
        missing_db = self.missing_parent_db()
        result = list_records(missing_db, SUBMITTER_A)
        self.assertEqual(
            result.returncode, 1, msg=f"应退出 1: {result.stderr!r}"
        )
        self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
        self.assertIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
        self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")


class LegacyFiveColumnStatusFilterTest(unittest.TestCase):
    """旧五列库上的状态筛选：只返回原有五字段，各值不变，可重复。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_legacy_status_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 直接按五列旧结构建库并写入一条 pending 记录；不经产品命令。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO expenses"
                " (id, submitter, purpose, amount_minor, status)"
                " VALUES (?, ?, ?, ?, ?)",
                LEGACY_ROW,
            )
            conn.commit()
        finally:
            conn.close()
        self.expected_record = {
            "id": LEGACY_ROW[0],
            "submitter": LEGACY_ROW[1],
            "purpose": LEGACY_ROW[2],
            "amount_minor": LEGACY_ROW[3],
            "status": LEGACY_ROW[4],
        }

    def test_pending_filter_returns_five_fields_and_repeats_identically(self) -> None:
        # 新进程按 pending 筛选旧库：记录只含原有五字段，各值不变。
        first = list_records(self.db_path, SUBMITTER_A, "pending")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(first.stderr, "")
        records = json.loads(first.stdout)
        self.assertEqual(records, [self.expected_record])
        self.assertEqual(
            set(records[0]),
            {"id", "submitter", "purpose", "amount_minor", "status"},
        )
        self.assertNotIn("attachment_note", records[0])
        self.assertIsInstance(records[0]["amount_minor"], int)
        self.assertEqual(records[0]["amount_minor"], 500)

        # approved 筛选返回空数组（唯一一笔是 pending）。
        approved = list_records(self.db_path, SUBMITTER_A, "approved")
        self.assertEqual(approved.returncode, 0, msg=approved.stderr)
        self.assertEqual(approved.stderr, "")
        self.assertEqual(approved.stdout, "[]\n")

        # 省略状态与按 pending 筛选的结果一致（库里唯一一笔就是 pending）。
        unfiltered = list_records(self.db_path, SUBMITTER_A)
        self.assertEqual(unfiltered.stdout, first.stdout)

        # 在另一个新进程中重复同一筛选：输出逐字节一致。
        second = list_records(self.db_path, SUBMITTER_A, "pending")
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(second.stderr, "")
        self.assertEqual(second.stdout, first.stdout)

        # 直接核对库内内容：五字段各值不变；结构补齐只允许新增值为 NULL 的
        # 附件说明列，原有数据不被改写。
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0][:5]), LEGACY_ROW)
        self.assertIsNone(rows[0][5])


if __name__ == "__main__":
    unittest.main()
