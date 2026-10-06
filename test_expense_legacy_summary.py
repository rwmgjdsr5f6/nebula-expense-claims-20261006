"""旧库（五列结构）首次直接 summary 的回归测试。

从项目根目录执行：

    python -m unittest test_expense_legacy_summary -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 summary 公开命令：先直接
用 SQLite 连接按已有旧库兼容测试的五列结构建库并写入虚构样例（此步不属于产品
命令，不会提前触发结构补齐），再由新进程对该库执行 ``summary --submitter``
（可选 ``--budget``）。首次汇总进程退出后，另开一个新的 SQLite 连接核对三条
旧记录的原五列值完全保留，随后再在新进程中重复汇总。

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

# 旧库（五列结构）建表语句，沿用已有旧库兼容测试（test_expense_attachment）：
# 没有 attachment_note 列。
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

# 附件说明列名；汇总输出中绝不应出现。
ATTACHMENT_NOTE_COLUMN = "attachment_note"

# 无预算汇总恰好包含的三个字段；带预算时仅追加两个预算字段。
SUMMARY_FIELDS = {"submitter", "count", "total_amount_minor"}
BUDGET_EXTRA_FIELDS = {"budget_amount_minor", "remaining_amount_minor"}


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def summary(
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
    ) -> None:
        """汇总进程：退出码 0，标准错误为空。"""
        self.assertEqual(
            result.returncode, 0, msg=f"summary 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "", msg="summary 成功时标准错误应为空")

    def parse_summary_json(self, result: subprocess.CompletedProcess[str]) -> dict:
        """标准输出恰为一行 JSON 对象；解析并返回该对象。"""
        stdout = result.stdout
        # 恰为一行：单个换行结尾，且换行前不含其他换行。
        self.assertTrue(stdout.endswith("\n"), msg=f"输出应以单个换行结尾: {stdout!r}")
        self.assertNotIn("\n", stdout[:-1], msg=f"输出应只有一行: {stdout!r}")
        payload = json.loads(stdout)
        self.assertIsInstance(payload, dict, msg="汇总输出应为 JSON 对象")
        # 输出中不包含附件说明列。
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, payload)
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, stdout)
        return payload

    def assert_int_values(self, payload: dict, *fields: str) -> None:
        """指定字段的值均为 JSON 整数（非浮点、非布尔、非字符串）。"""
        for field in fields:
            value = payload[field]
            self.assertIs(
                type(value), int, msg=f"{field} 应为整数，实际: {value!r}"
            )

    def assert_legacy_rows_preserved(self) -> None:
        """重新打开数据库核对：三条旧记录的原五列值完全保留，金额仍为整数。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " typeof(amount_minor) FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), len(SEED_RECORDS))
        for row, seed in zip(rows, SEED_RECORDS):
            self.assertEqual(tuple(row[:5]), seed)
            # SQLite 层面金额仍为 INTEGER 存储类。
            self.assertEqual(row[5], "integer")

    def assert_legacy_schema_untouched(self) -> None:
        """旧库仍保持原五列结构：未补齐附件说明列，记录内容不变。"""
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
        self.assertEqual([tuple(row) for row in rows], SEED_RECORDS)


class LegacySummaryWithoutBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary（不带预算）：演示甲两笔合计 1231 分，仅三个字段。"""

    def test_first_summary_without_budget_on_legacy_database(self) -> None:
        # 首次汇总：提交人带首尾空白，产品侧去除空白后按完整名称精确匹配。
        # 此前未调用 submit / list / export，旧库保持五列结构未被触碰。
        first = summary(self.db_path, "  演示甲  ")
        self.assert_summary_succeeded(first)

        payload = self.parse_summary_json(first)
        # 只有原有三个汇总字段，无预算字段。
        self.assertEqual(set(payload), SUMMARY_FIELDS)
        # 返回清理后的名称、2 笔、合计 1230 + 1 = 1231 分，均为整数。
        self.assertEqual(payload["submitter"], "演示甲")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total_amount_minor"], 1231)
        self.assert_int_values(payload, "count", "total_amount_minor")
        # 不混入演示乙的记录。
        self.assertNotIn("演示乙", first.stdout)

        # 首次汇总进程退出后，新进程重复同一命令得到相同输出。
        second = summary(self.db_path, "  演示甲  ")
        self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)

        # 重新打开数据库核对三条旧记录的原五列值完全保留，金额仍为整数。
        self.assert_legacy_rows_preserved()


class LegacySummaryWithBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary（带 --budget 15）：仅追加预算与余额两个字段。"""

    def test_first_summary_with_budget_on_legacy_database(self) -> None:
        # 独立旧库上的首次产品操作即带预算汇总。
        first = summary(self.db_path, "  演示甲  ", budget="15")
        self.assert_summary_succeeded(first)

        payload = self.parse_summary_json(first)
        # 原有三个字段之外仅追加预算分与余额分。
        self.assertEqual(set(payload), SUMMARY_FIELDS | BUDGET_EXTRA_FIELDS)
        self.assertEqual(payload["submitter"], "演示甲")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total_amount_minor"], 1231)
        # 预算 15 元 = 1500 分，余额 1500 - 1231 = 269 分，均为整数。
        self.assertEqual(payload["budget_amount_minor"], 1500)
        self.assertEqual(payload["remaining_amount_minor"], 269)
        self.assert_int_values(
            payload,
            "count",
            "total_amount_minor",
            "budget_amount_minor",
            "remaining_amount_minor",
        )

        # 新进程重复同一命令得到相同输出。
        second = summary(self.db_path, "  演示甲  ", budget="15")
        self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)

        # 带预算汇总之后再执行无预算汇总：结果仍只有三个字段，
        # 证明预算不影响后续结果。
        plain = summary(self.db_path, "  演示甲  ")
        self.assert_summary_succeeded(plain)
        plain_payload = self.parse_summary_json(plain)
        self.assertEqual(set(plain_payload), SUMMARY_FIELDS)
        self.assertEqual(plain_payload["submitter"], "演示甲")
        self.assertEqual(plain_payload["count"], 2)
        self.assertEqual(plain_payload["total_amount_minor"], 1231)

        # 三条旧记录的原五列值完全保留，金额仍为整数。
        self.assert_legacy_rows_preserved()


class LegacySummaryByPrefixNameTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary：名称前缀“演示”不匹配任何记录，零笔零分。"""

    def test_first_summary_with_prefix_name_and_budget(self) -> None:
        # 独立旧库，首次产品操作即以名称前缀执行带预算汇总。
        first = summary(self.db_path, "演示", budget="15")
        self.assert_summary_succeeded(first)

        payload = self.parse_summary_json(first)
        self.assertEqual(set(payload), SUMMARY_FIELDS | BUDGET_EXTRA_FIELDS)
        # 零笔、合计零分、余额为全部预算 1500 分，均为整数。
        self.assertEqual(payload["submitter"], "演示")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["total_amount_minor"], 0)
        self.assertEqual(payload["budget_amount_minor"], 1500)
        self.assertEqual(payload["remaining_amount_minor"], 1500)
        self.assert_int_values(
            payload,
            "count",
            "total_amount_minor",
            "budget_amount_minor",
            "remaining_amount_minor",
        )
        # 不混入演示甲、演示乙的任何记录信息。
        self.assertNotIn("演示甲", first.stdout)
        self.assertNotIn("演示乙", first.stdout)

        # 新进程重复同一命令得到相同输出。
        second = summary(self.db_path, "演示", budget="15")
        self.assert_summary_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)

        # 三条旧记录的原五列值完全保留，金额仍为整数。
        self.assert_legacy_rows_preserved()


class LegacySummaryInvalidBudgetTest(LegacyDatabaseSummaryTestBase):
    """旧库首次直接 summary：--budget 0 为无效预算，退出码 2 且旧库不变。"""

    def test_first_summary_with_zero_budget_is_rejected(self) -> None:
        # 另一份尚未使用的旧库：首次产品操作即传入 --budget 0。
        result = summary(self.db_path, "演示甲", budget="0")
        # 退出码 2，标准输出为空，标准错误指出预算无效。
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("预算无效", result.stderr)

        # 参数校验先于数据库操作：旧记录和原五列结构均不改变。
        self.assert_legacy_schema_untouched()


if __name__ == "__main__":
    unittest.main()
