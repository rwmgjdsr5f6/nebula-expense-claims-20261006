"""list 可选状态筛选（--status）的回归测试。

从项目根目录执行：

    python -m unittest test_expense_status -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / approve / list
公开命令：提交与批准在独立进程中完成并退出后，再由新进程对同一 SQLite
数据库执行带状态筛选的 list，验证筛选结果跨进程准确、且只作用于当次查询。
每个用例使用独立的临时 SQLite 文件与虚构人员，结束后自动清理演示数据；
仅使用 Python 标准库，不访问网络或第三方包。

本文件只覆盖状态筛选与状态校验边界；金额规则、附件说明、审批本身、
summary 与 export 的行为由各自既有测试负责，不在此重复。
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

# 固定样例（按提交顺序）：编号 1 至 4。
# 演示甲：交通费 12.30 元（带说明“演示车票”）、餐费 0.01 元、材料费 2 元；
# 演示乙：办公费 5 元。随后批准第 2 笔与第 4 笔。
SAMPLE = [
    # (提交人, 用途, 金额（元）, 说明, 提交后状态)
    (SUBMITTER_A, "交通费", "12.30", "演示车票", "pending"),
    (SUBMITTER_B, "办公费", "5", None, "approved"),
    (SUBMITTER_A, "餐费", "0.01", None, "pending"),
    (SUBMITTER_A, "材料费", "2", None, "approved"),
]

# 各笔的整数分金额，与 SAMPLE 顺序一一对应。
AMOUNTS_MINOR = [1230, 500, 1, 200]

# 旧库（五列结构）预置记录：id 5、演示甲、办公费、500 分、pending。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""
LEGACY_RECORD = {
    "id": 5,
    "submitter": SUBMITTER_A,
    "purpose": "办公费",
    "amount_minor": 500,
    "status": "pending",
}


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令并捕获退出码与输出。"""
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


def approve(db_path: Path, expense_id: int) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "approve", "--id", str(expense_id))


def list_records(
    db_path: Path, submitter: str, status: str | None = None
) -> subprocess.CompletedProcess[str]:
    argv = ["list", "--submitter", submitter]
    if status is not None:
        argv += ["--status", status]
    return run_cli(db_path, *argv)


class StatusFilterTest(unittest.TestCase):
    """按提交人与状态共同查询：结果准确、按编号升序，且不改变任何记录。"""

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_status_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "status.sqlite"
        self._build_sample()

    # -- 辅助 ------------------------------------------------------------

    def _build_sample(self) -> None:
        """通过公开入口建立固定样例：四笔提交，批准第 2 与第 4 笔。"""
        for index, (submitter, purpose, amount, note, _status) in enumerate(
            SAMPLE, start=1
        ):
            result = submit(self.db_path, submitter, purpose, amount, note)
            self.assertEqual(
                result.returncode, 0, msg=f"第 {index} 笔提交失败: {result.stderr}"
            )
            self.assertEqual(result.stderr, "")
            record = json.loads(result.stdout)
            # 全新数据库：编号从 1 开始按提交顺序递增。
            self.assertEqual(record["id"], index)
        for expense_id in (2, 4):
            result = approve(self.db_path, expense_id)
            self.assertEqual(
                result.returncode, 0, msg=f"批准编号 {expense_id} 失败: {result.stderr}"
            )
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["status"], "approved")

    def expected_record(self, index: int) -> dict:
        """第 index 笔（1 起）应有的完整记录。"""
        submitter, purpose, _amount, note, status = SAMPLE[index - 1]
        record: dict[str, object] = {
            "id": index,
            "submitter": submitter,
            "purpose": purpose,
            "amount_minor": AMOUNTS_MINOR[index - 1],
            "status": status,
        }
        if note is not None:
            record["attachment_note"] = note
        return record

    def assert_list_ok(
        self, submitter: str, status: str | None = None
    ) -> tuple[subprocess.CompletedProcess[str], list]:
        """查询应成功：退出码 0，标准错误为空，标准输出为一行 JSON 数组。"""
        result = list_records(self.db_path, submitter, status)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "", msg="list 成功时标准错误应为空")
        # 标准输出恰为单行 JSON（末尾仅一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertNotIn("\n", result.stdout.rstrip("\n"))
        records = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(records, list)
        return result, records

    def assert_full_records(self, records: list, indexes: list[int]) -> None:
        """核对完整记录：字段、整数分金额、说明有无均与样例一致，按编号升序。"""
        self.assertEqual(records, [self.expected_record(i) for i in indexes])
        self.assertEqual([r["id"] for r in records], indexes)
        for record in records:
            self.assertIsInstance(record["amount_minor"], int)
            if "attachment_note" in record:
                self.assertEqual(record["attachment_note"], "演示车票")

    # -- 筛选结果 ----------------------------------------------------------

    def test_pending_filter_returns_ids_1_and_3(self) -> None:
        """演示甲的 pending 只返回编号 1、3，含完整记录与说明文字。"""
        _result, records = self.assert_list_ok(SUBMITTER_A, "pending")
        self.assert_full_records(records, [1, 3])
        # 带说明的记录保留文字，无说明的记录不含 attachment_note。
        self.assertEqual(records[0]["attachment_note"], "演示车票")
        self.assertNotIn("attachment_note", records[1])

    def test_approved_filter_returns_id_4(self) -> None:
        """演示甲的 approved 只返回编号 4。"""
        _result, records = self.assert_list_ok(SUBMITTER_A, "approved")
        self.assert_full_records(records, [4])
        self.assertNotIn("attachment_note", records[0])

    def test_no_status_returns_ids_1_3_4(self) -> None:
        """省略状态时返回演示甲的全部三笔（编号 1、3、4），按编号升序。"""
        _result, records = self.assert_list_ok(SUBMITTER_A)
        self.assert_full_records(records, [1, 3, 4])

    def test_whitespace_around_submitter_and_status(self) -> None:
        """提交人与状态首尾带空白时，筛选结果与无空白完全一致。"""
        _plain_result, plain = self.assert_list_ok(SUBMITTER_A, "pending")
        _padded_result, padded = self.assert_list_ok("  演示甲  ", "  pending  ")
        self.assertEqual(padded, plain)
        self.assert_full_records(padded, [1, 3])

    def test_prefix_and_unknown_submitter_return_empty(self) -> None:
        """前缀名称或不存在的提交人返回空数组（完整名称精确匹配）。"""
        for label, submitter in [("前缀名称", "演示"), ("不存在的提交人", "演示丙")]:
            with self.subTest(case=label):
                _result, records = self.assert_list_ok(submitter, "pending")
                self.assertEqual(records, [])
                _result_all, records_all = self.assert_list_ok(submitter)
                self.assertEqual(records_all, [])

    def test_filtering_does_not_change_any_records(self) -> None:
        """筛选前后两人的全部记录逐字节一致：筛选未增删改写，也未保存查询选择。"""
        _ra, before_a = self.assert_list_ok(SUBMITTER_A)
        _rb, before_b = self.assert_list_ok(SUBMITTER_B)
        self.assert_full_records(before_a, [1, 3, 4])
        self.assert_full_records(before_b, [2])

        # 执行各种筛选（含带空白的形式）。
        self.assert_list_ok(SUBMITTER_A, "pending")
        self.assert_list_ok(SUBMITTER_A, "approved")
        self.assert_list_ok(SUBMITTER_B, "approved")
        self.assert_list_ok("  演示甲  ", "  pending  ")

        # 筛选后复查：输出与筛选前逐字节一致，无状态查询仍返回全部记录。
        after_a = list_records(self.db_path, SUBMITTER_A)
        after_b = list_records(self.db_path, SUBMITTER_B)
        self.assertEqual(after_a.returncode, 0, msg=f"list 失败: {after_a.stderr}")
        self.assertEqual(after_b.returncode, 0, msg=f"list 失败: {after_b.stderr}")
        self.assertEqual(after_a.stdout, _ra.stdout)
        self.assertEqual(after_b.stdout, _rb.stdout)
        self.assertEqual(json.loads(after_a.stdout), before_a)
        self.assertEqual(json.loads(after_b.stdout), before_b)


class StatusValidationTest(unittest.TestCase):
    """状态校验边界：无效状态退出 2，校验顺序先于数据库操作。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_status_err_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "validation.sqlite"

    def assert_status_rejected(self, result: subprocess.CompletedProcess[str]) -> None:
        """状态无效：退出码 2，标准输出为空，标准错误指出状态无效。"""
        self.assertEqual(
            result.returncode, 2,
            msg=f"应拒绝却退出 {result.returncode}: {result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="状态无效时标准输出应为空")
        self.assertIn("状态无效", result.stderr)

    def test_invalid_statuses_are_rejected(self) -> None:
        """空串、纯空白、Pending 与 rejected 均退出 2 并报告状态无效。"""
        cases = [
            ("空串", ""),
            ("纯空白", "  \t "),
            ("首字母大写", "Pending"),
            ("其他取值", "rejected"),
        ]
        for label, status in cases:
            with self.subTest(case=label):
                result = list_records(self.db_path, SUBMITTER_A, status)
                self.assert_status_rejected(result)
                # 校验先于数据库操作：数据库文件不应被创建。
                self.assertFalse(
                    self.db_path.exists(), msg=f"{label} 时不应创建数据库文件"
                )

    def test_status_flag_without_value_is_rejected(self) -> None:
        """只给出 --status 而不给参数值：退出 2，标准错误指出该参数缺少值。"""
        result = run_cli(
            self.db_path,
            "list",
            "--submitter", SUBMITTER_A,
            "--status",
        )
        self.assertEqual(result.returncode, 2, msg=f"应退出 2: {result.stdout!r}")
        self.assertEqual(result.stdout, "", msg="缺少参数值时标准输出应为空")
        self.assertIn("缺少参数值", result.stderr)
        self.assertIn("--status", result.stderr)
        self.assertFalse(self.db_path.exists(), msg="不应创建数据库文件")

    def test_blank_submitter_error_precedes_status_error(self) -> None:
        """提交人为空白且状态无效时，先报告提交人不能为空。"""
        result = list_records(self.db_path, "   ", "rejected")
        self.assertEqual(result.returncode, 2, msg=f"应退出 2: {result.stdout!r}")
        self.assertEqual(result.stdout, "")
        self.assertIn("提交人", result.stderr)
        self.assertIn("不能为空", result.stderr)
        self.assertNotIn("状态无效", result.stderr)
        self.assertFalse(self.db_path.exists(), msg="不应创建数据库文件")

    def test_status_error_precedes_database_error(self) -> None:
        """提交人有效、状态无效且数据库父目录不存在时，仍先报状态无效。"""
        missing_dir_db = (
            Path(self._tmpdir.name) / "no_such_dir" / "unreachable.sqlite"
        )
        self.assertFalse(missing_dir_db.parent.exists())
        result = list_records(missing_dir_db, SUBMITTER_A, "Pending")
        self.assertEqual(
            result.returncode, 2,
            msg=f"状态校验应优先于数据库错误（退出码 2）: {result.stderr!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("状态无效", result.stderr)
        self.assertNotIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_dir_db.parent.exists(), msg="不应创建父目录")
        self.assertFalse(missing_dir_db.exists(), msg="不应创建数据库文件")

    def test_missing_parent_dir_is_database_error(self) -> None:
        """参数有效但数据库父目录不存在：退出 1，标准错误报告数据库操作失败。"""
        missing_dir_db = (
            Path(self._tmpdir.name) / "no_such_dir" / "unreachable.sqlite"
        )
        self.assertFalse(missing_dir_db.parent.exists())
        for label, status in [("带状态筛选", "pending"), ("不带状态筛选", None)]:
            with self.subTest(case=label):
                result = list_records(missing_dir_db, SUBMITTER_A, status)
                self.assertEqual(
                    result.returncode, 1,
                    msg=f"应退出 1: {result.stdout!r} {result.stderr!r}",
                )
                self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
                self.assertIn("数据库操作失败", result.stderr)
                self.assertFalse(missing_dir_db.parent.exists(), msg="不应创建父目录")


class LegacyDatabaseStatusFilterTest(unittest.TestCase):
    """旧五列结构库：状态筛选仍返回原有五字段且各值不变，重复查询一致。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_status_legacy_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 用原有五列结构建库，并预置一条演示甲的 pending 记录。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO expenses"
                " (id, submitter, purpose, amount_minor, status)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    LEGACY_RECORD["id"],
                    LEGACY_RECORD["submitter"],
                    LEGACY_RECORD["purpose"],
                    LEGACY_RECORD["amount_minor"],
                    LEGACY_RECORD["status"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_record_filtered_unchanged_and_repeatable(self) -> None:
        """按 pending 筛选返回原记录五字段；重复查询与无筛选查询结果一致。"""
        filtered = list_records(self.db_path, SUBMITTER_A, "pending")
        self.assertEqual(filtered.returncode, 0, msg=f"list 失败: {filtered.stderr}")
        self.assertEqual(filtered.stderr, "")
        records = json.loads(filtered.stdout)
        # 旧记录保持原有五个字段，各值不变，不增加 attachment_note。
        self.assertEqual(records, [LEGACY_RECORD])
        self.assertEqual(
            set(records[0]),
            {"id", "submitter", "purpose", "amount_minor", "status"},
        )
        self.assertNotIn("attachment_note", records[0])

        # 重复筛选结果逐字节一致。
        again = list_records(self.db_path, SUBMITTER_A, "pending")
        self.assertEqual(again.returncode, 0, msg=f"list 失败: {again.stderr}")
        self.assertEqual(again.stdout, filtered.stdout)

        # 筛选不命中时为空数组；不带筛选仍返回原记录（筛选未落库）。
        missed = list_records(self.db_path, SUBMITTER_A, "approved")
        self.assertEqual(missed.returncode, 0, msg=f"list 失败: {missed.stderr}")
        self.assertEqual(json.loads(missed.stdout), [])
        unfiltered = list_records(self.db_path, SUBMITTER_A)
        self.assertEqual(unfiltered.returncode, 0, msg=f"list 失败: {unfiltered.stderr}")
        self.assertEqual(json.loads(unfiltered.stdout), [LEGACY_RECORD])


if __name__ == "__main__":
    unittest.main()
