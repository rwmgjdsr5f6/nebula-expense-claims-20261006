"""旧库（五列结构）首次直接 summary 的回归测试。

从项目根目录执行：

    python -m unittest test_expense_legacy_summary -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 summary 公开命令：先直接
用 SQLite 连接按已有旧库兼容测试的五列结构建库并写入虚构样例（此步不属于产品
命令，不会提前触发结构补齐），再由新进程对该库执行 ``summary --submitter``
（可选 ``--budget``）。首次汇总进程退出后，另开一个新的 SQLite 连接核对三条
旧记录的原五列值完全保留，随后再在新进程中重复同一汇总命令。

约束：
- 首次汇总之前绝不调用 submit / list / export，以免提前改变待验证的旧库；
- 每个用例使用独立的临时 SQLite 文件，结束后自动清理；
- 人员与费用均为虚构数据，仅使用 Python 标准库，不访问网络或第三方包；
- 金额一律按整数分（整数比较）校验，不按人民币元或浮点数解释。
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

# 旧库（五列结构）建表语句，沿用已有旧库兼容测试（test_expense_attachment /
# test_expense_legacy_export）：没有 attachment_note 列。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

# 固定样例：三笔 pending 旧记录。金额以整数分存储。
# id 7  演示甲 交通费 1230 分
# id 11 演示乙 办公费  500 分
# id 13 演示甲 餐费      1 分
SEED_RECORDS = [
    (7, "演示甲", "交通费", 1230, "pending"),
    (11, "演示乙", "办公费", 500, "pending"),
    (13, "演示甲", "餐费", 1, "pending"),
]

# 附件说明列名；旧库经首次产品命令触发 ALTER TABLE 补齐。
ATTACHMENT_NOTE_COLUMN = "attachment_note"

# 不带预算时的原有三个汇总字段。
SUMMARY_KEYS = {"submitter", "count", "total_amount_minor"}
# 带预算时仅追加的两个字段。
BUDGET_EXTRA_KEYS = {"budget_amount_minor", "remaining_amount_minor"}


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def summarize(
    db_path: Path, submitter: str, budget: str | None = None
) -> subprocess.CompletedProcess[str]:
    args = ["summary", "--submitter", submitter]
    if budget is not None:
        args += ["--budget", budget]
    return run_cli(db_path, *args)


def create_legacy_database(db_path: Path) -> None:
    """用五列旧结构直接建库并写入三笔样例；不经任何产品命令。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO expenses"
            " (id, submitter, purpose, amount_minor, status)"
            " VALUES (?, ?, ?, ?, ?)",
            SEED_RECORDS,
        )
        conn.commit()
    finally:
        conn.close()


class LegacyDatabaseSummaryTestBase(unittest.TestCase):
    """各用例共用的旧库搭建与汇总/数据库断言辅助。"""

    def setUp(self) -> None:
        # 每个用例独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="expense_desk_legacy_summary_"
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 直接按旧库五列结构建库并写入样例；此前不调用任何产品命令。
        create_legacy_database(self.db_path)

    def assert_summary_succeeded(
        self, result: subprocess.CompletedProcess[str]
    ) -> dict:
        """汇总进程：退出码 0，标准错误为空，标准输出恰为一行 JSON 对象。"""
        self.assertEqual(
            result.returncode, 0, msg=f"summary 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "", msg="summary 成功时标准错误应为空")
        self.assertTrue(result.stdout.endswith("\n"))
        # 除结尾换行外不含其他换行：输出只有一行。
        self.assertNotIn("\n", result.stdout[:-1])
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        # 汇总输出不含附件说明字段。
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, data)
        return data

    def assert_amounts_are_ints(self, data: dict, keys: tuple[str, ...]) -> None:
        """指定的数值字段均为 JSON 整数（不是布尔、不是浮点）。"""
        for key in keys:
            self.assertIsInstance(data[key], int, msg=f"{key} 应为整数")
            self.assertNotIsInstance(data[key], bool)

    def assert_legacy_rows_preserved(self) -> None:
        """重新打开数据库：三条旧记录的原五列值完全保留，金额仍为整数。

        首次汇总触发的结构补齐只会追加 attachment_note 列（旧记录为 NULL），
        原五列的名称与内容不得改变。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            column_names = [
                row[1]
                for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
            ]
            for original_column in (
                "id", "submitter", "purpose", "amount_minor", "status",
            ):
                self.assertIn(original_column, column_names)
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status"
                " FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), len(SEED_RECORDS))
        for row, seed in zip(rows, SEED_RECORDS):
            self.assertEqual(tuple(row), seed)
            # 金额落库后仍是整数分。
            self.assertIsInstance(row[3], int)
            self.assertNotIsInstance(row[3], bool)


class LegacySummaryWithoutBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary（不带预算）：原有三个字段，随后重复结果一致。"""

    def test_first_summary_without_budget_then_repeat(self) -> None:
        # 首次汇总：提交人带首尾空白，产品侧去除空白后按完整名称精确匹配。
        # 此前未调用 submit / list / export，旧库保持五列结构未被触碰。
        first = summarize(self.db_path, "  演示甲  ")
        data = self.assert_summary_succeeded(first)

        # 只有原有三个汇总字段，不含预算与附件说明字段。
        self.assertEqual(set(data.keys()), SUMMARY_KEYS)
        self.assertEqual(
            data,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )
        self.assert_amounts_are_ints(data, ("count", "total_amount_minor"))
        # 不混入演示乙的记录。
        self.assertNotIn("演示乙", first.stdout)

        # 首次汇总进程已退出：重新打开数据库，三条旧记录原五列值完全保留。
        self.assert_legacy_rows_preserved()

        # 在新进程中重复同一命令：输出与首次逐字节一致。
        second = summarize(self.db_path, "  演示甲  ")
        second_data = self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(second_data, data)

        # 重复汇总后旧记录仍完全保留。
        self.assert_legacy_rows_preserved()


class LegacySummaryWithBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary --budget 15：仅追加预算与余额两个字段。"""

    def test_first_summary_with_budget_then_repeat_and_plain(self) -> None:
        # 首次汇总即带预算：此前未调用任何其他产品命令。
        first = summarize(self.db_path, "  演示甲  ", "15")
        data = self.assert_summary_succeeded(first)

        # 原有三个字段之外仅追加预算 1500 分与余额 269 分。
        self.assertEqual(set(data.keys()), SUMMARY_KEYS | BUDGET_EXTRA_KEYS)
        self.assertEqual(
            data,
            {
                "submitter": "演示甲",
                "count": 2,
                "total_amount_minor": 1231,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 269,
            },
        )
        self.assert_amounts_are_ints(
            data,
            (
                "count",
                "total_amount_minor",
                "budget_amount_minor",
                "remaining_amount_minor",
            ),
        )
        self.assertNotIn("演示乙", first.stdout)

        # 首次汇总进程已退出：重新打开数据库，三条旧记录原五列值完全保留。
        self.assert_legacy_rows_preserved()

        # 在新进程中重复同一命令：输出与首次逐字节一致。
        second = summarize(self.db_path, "  演示甲  ", "15")
        self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)

        # 带预算汇总之后再执行无预算汇总：仍只有原有三个字段，
        # 证明预算仅供当次参考、不影响后续结果。
        later = summarize(self.db_path, "  演示甲  ")
        later_data = self.assert_summary_succeeded(later)
        self.assertEqual(set(later_data.keys()), SUMMARY_KEYS)
        self.assertEqual(
            later_data,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

        # 全部汇总结束后旧记录仍完全保留。
        self.assert_legacy_rows_preserved()


class LegacySummaryByPrefixNameTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary：名称前缀“演示”不做模糊匹配，零笔零分。"""

    def test_first_summary_with_prefix_name_and_budget(self) -> None:
        # 独立旧库，首次产品操作即带预算汇总；前缀不命中演示甲/演示乙。
        first = summarize(self.db_path, "演示", "15")
        data = self.assert_summary_succeeded(first)

        self.assertEqual(set(data.keys()), SUMMARY_KEYS | BUDGET_EXTRA_KEYS)
        self.assertEqual(
            data,
            {
                "submitter": "演示",
                "count": 0,
                "total_amount_minor": 0,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 1500,
            },
        )
        self.assert_amounts_are_ints(
            data,
            (
                "count",
                "total_amount_minor",
                "budget_amount_minor",
                "remaining_amount_minor",
            ),
        )
        # 不混入两人的记录，输出中也不含附件说明。
        self.assertNotIn("演示甲", first.stdout)
        self.assertNotIn("演示乙", first.stdout)
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, first.stdout)

        # 首次汇总进程已退出：三条旧记录原五列值完全保留。
        self.assert_legacy_rows_preserved()

        # 在新进程中重复同一命令：输出与首次逐字节一致。
        second = summarize(self.db_path, "演示", "15")
        self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)


class LegacySummaryInvalidBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary --budget 0：退出码 2，旧库结构与原记录均不变。"""

    def test_first_summary_with_zero_budget_rejected(self) -> None:
        # 另一份尚未使用过的旧库：首次产品操作即传入无效预算。
        result = summarize(self.db_path, "演示甲", "0")
        self.assertEqual(
            result.returncode, 2,
            msg=f"预算 0 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # 参数校验先于数据库操作：旧库仍是原五列结构，没有补齐附件说明列。
        conn = sqlite3.connect(self.db_path)
        try:
            column_names = [
                row[1]
                for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
            ]
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status"
                " FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            column_names,
            ["id", "submitter", "purpose", "amount_minor", "status"],
        )
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, column_names)
        # 三条旧记录完全保留，金额仍为整数。
        self.assertEqual([tuple(row) for row in rows], SEED_RECORDS)
        for row in rows:
            self.assertIsInstance(row[3], int)
            self.assertNotIsInstance(row[3], bool)


if __name__ == "__main__":
    unittest.main()
