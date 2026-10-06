"""按提交人汇总报销笔数与金额的回归测试。

从项目根目录执行：

    python -m unittest test_expense_summary -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中准备虚构费用，提交进程退出后再由另一个新进程对同一 SQLite
数据库执行 summary --submitter，验证汇总的既有行为。每次运行使用独立的
临时 SQLite 文件，结束后自动清理，重复执行不依赖已有数据；仅使用 Python
标准库，不访问公网。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 按提交人汇总的固定正常样例：演示甲、演示乙、演示甲各一笔。
SEED_EXPENSES = [
    ("演示甲", "交通费", "12.30"),
    ("演示乙", "办公费", "5"),
    ("演示甲", "餐费", "0.01"),
]

# 演示甲：1230 + 1 = 1231 分；演示乙：500 分。
EXPECTED_JIA = {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231}
EXPECTED_YI = {"submitter": "演示乙", "count": 1, "total_amount_minor": 500}

# 大金额样例：单笔为 SQLite 64 位有符号整数上限（分），两笔合计为 2^64 - 2。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
TWO_MAX_TOTAL = 2 * MAX_MINOR  # 18446744073709551614
BIG_SUBMITTER = "汇总测试·虚构超大额"


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def submit(db_path: Path, submitter: str, purpose: str, amount: str):
    return run_cli(
        db_path,
        "submit",
        "--submitter", submitter,
        "--purpose", purpose,
        "--amount", amount,
    )


def list_records(db_path: Path, submitter: str):
    return run_cli(db_path, "list", "--submitter", submitter)


def summarize(db_path: Path, submitter: str):
    return run_cli(db_path, "summary", "--submitter", submitter)


class SummaryBySubmitterTest(unittest.TestCase):
    """按提交人汇总：同一库内不同提交人相互隔离，名称精确匹配。

    每个用例先按演示甲(交通费 12.30)、演示乙(办公费 5)、演示甲(餐费 0.01)
    的顺序提交三笔固定样例费用，再由新进程对同一数据库执行 summary。
    """

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "summary_regression.sqlite"
        for submitter_name, purpose, amount in SEED_EXPENSES:
            self._submit_ok(submitter_name, purpose, amount)

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, submitter_name: str, purpose: str, amount: str) -> dict:
        result = submit(self.db_path, submitter_name, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_summary(self, submitter_name: str, db_path: Path | None = None):
        """在新进程中汇总：退出码 0，标准错误为空，标准输出为一行 JSON 对象。

        返回 (CompletedProcess, 解析后的汇总对象)，便于比较原始输出。
        """
        result = summarize(db_path or self.db_path, submitter_name)
        self.assertEqual(
            result.returncode, 0,
            msg=f"summary --submitter {submitter_name!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"summary --submitter {submitter_name!r} 的标准错误应为空",
        )
        # 标准输出为单行 JSON（仅末尾一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        summary_obj = json.loads(result.stdout)
        self.assertIsInstance(summary_obj, dict)
        return result, summary_obj

    def assert_zero_summary(self, submitter_name: str) -> None:
        """未命中任何记录时：回显去空白后的名称，笔数与合计均为整数零。"""
        _, summary_obj = self.assert_summary(submitter_name)
        self.assertEqual(
            summary_obj,
            {"submitter": submitter_name.strip(), "count": 0, "total_amount_minor": 0},
        )
        self.assertIsInstance(summary_obj["count"], int)
        self.assertIsInstance(summary_obj["total_amount_minor"], int)

    def assert_list(self, submitter_name: str) -> list:
        """在新进程中查询：退出码 0，标准输出为 JSON 数组。"""
        result = list_records(self.db_path, submitter_name)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 正常样例 ----------------------------------------------------------

    def test_summary_counts_and_totals_per_submitter(self) -> None:
        """演示甲笔数 2、合计 1231 分；演示乙笔数 1、合计 500 分，互不混入。"""
        _, summary_jia = self.assert_summary("演示甲")
        self.assertEqual(summary_jia, EXPECTED_JIA)
        self.assertIsInstance(summary_jia["count"], int)
        self.assertIsInstance(summary_jia["total_amount_minor"], int)
        self.assertEqual(summary_jia["submitter"], "演示甲")

        _, summary_yi = self.assert_summary("演示乙")
        self.assertEqual(summary_yi, EXPECTED_YI)
        self.assertIsInstance(summary_yi["count"], int)
        self.assertIsInstance(summary_yi["total_amount_minor"], int)

        # 两人的金额不能相互混入：各自合计只包含本人的费用。
        self.assertNotEqual(
            summary_jia["total_amount_minor"], summary_yi["total_amount_minor"]
        )
        self.assertEqual(summary_jia["total_amount_minor"], 1230 + 1)
        self.assertEqual(summary_yi["total_amount_minor"], 500)

    def test_query_name_is_stripped_before_matching(self) -> None:
        """查询名称两端带空白时仍命中去空白后的演示甲，回显清理后的名称。"""
        _, summary_obj = self.assert_summary("  \t演示甲\n ")
        self.assertEqual(summary_obj, EXPECTED_JIA)

    def test_prefix_and_unknown_submitter_return_zeroes(self) -> None:
        """前缀（演示）与未提交过费用的名称（演示丙）均回显名称并返回两个零值。"""
        self.assert_zero_summary("演示")
        self.assert_zero_summary("演示丙")

    def test_fresh_database_returns_zeroes(self) -> None:
        """对父目录存在的全新数据库首次汇总，退出码 0 并返回两个零值。"""
        fresh_db = Path(self._tmpdir.name) / "fresh.sqlite"
        self.assertFalse(fresh_db.exists())
        result = summarize(fresh_db, "演示甲")
        self.assertEqual(result.returncode, 0, msg=f"新库汇总失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        summary_obj = json.loads(result.stdout)
        self.assertEqual(
            summary_obj,
            {"submitter": "演示甲", "count": 0, "total_amount_minor": 0},
        )

    def test_repeated_summaries_are_stable_and_list_unchanged(self) -> None:
        """连续两次汇总结果相同，且汇总前后 list 的记录保持一致。"""
        before_jia = self.assert_list("演示甲")
        before_yi = self.assert_list("演示乙")

        first_jia, _ = self.assert_summary("演示甲")
        first_yi, _ = self.assert_summary("演示乙")
        second_jia, summary_jia = self.assert_summary("演示甲")
        second_yi, summary_yi = self.assert_summary("演示乙")

        # 连续两次汇总的标准输出逐字节相同。
        self.assertEqual(second_jia.stdout, first_jia.stdout, msg="重复汇总演示甲应一致")
        self.assertEqual(second_yi.stdout, first_yi.stdout, msg="重复汇总演示乙应一致")
        self.assertEqual(summary_jia, EXPECTED_JIA)
        self.assertEqual(summary_yi, EXPECTED_YI)

        # 汇总前后 list 结果保持一致：记录数量、id、用途、金额与 pending 状态。
        after_jia = self.assert_list("演示甲")
        after_yi = self.assert_list("演示乙")
        self.assertEqual(after_jia, before_jia, msg="汇总前后演示甲的 list 结果应一致")
        self.assertEqual(after_yi, before_yi, msg="汇总前后演示乙的 list 结果应一致")

        for records, expected_count in [(after_jia, 2), (after_yi, 1)]:
            self.assertEqual(len(records), expected_count)
            self.assertEqual(
                [r["id"] for r in records], sorted(r["id"] for r in records)
            )
            for record in records:
                self.assertIn(record["purpose"], {"交通费", "办公费", "餐费"})
                self.assertIsInstance(record["amount_minor"], int)
                self.assertEqual(record["status"], "pending")

        # 汇总不改变任何记录：三笔样例费用的 id 与字段逐一保留（顺序无关）。
        all_records = after_jia + after_yi
        self.assertEqual(len({r["id"] for r in all_records}), 3)
        self.assertCountEqual(
            [
                (r["submitter"], r["purpose"], r["amount_minor"])
                for r in all_records
            ],
            [
                ("演示甲", "交通费", 1230),
                ("演示甲", "餐费", 1),
                ("演示乙", "办公费", 500),
            ],
        )

    # -- 大金额合计 --------------------------------------------------------

    def test_total_can_exceed_single_entry_limit(self) -> None:
        """两笔各为 64 位上限的费用，合计可超过单笔上限且不舍入、不溢出。"""
        big_dir = tempfile.TemporaryDirectory(prefix="expense_desk_big_summary_")
        self.addCleanup(big_dir.cleanup)
        big_db = Path(big_dir.name) / "big_total.sqlite"

        for _ in range(2):
            result = submit(big_db, BIG_SUBMITTER, "超大额", MAX_YUAN)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
            record = json.loads(result.stdout)
            self.assertEqual(record["amount_minor"], MAX_MINOR)

        result = summarize(big_db, BIG_SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"summary 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        summary_obj = json.loads(result.stdout)
        self.assertEqual(summary_obj["submitter"], BIG_SUBMITTER)
        self.assertEqual(summary_obj["count"], 2)
        # 合计精确为 2^64 - 2，超过单笔 64 位有符号整数上限，不得舍入或溢出。
        self.assertEqual(summary_obj["total_amount_minor"], TWO_MAX_TOTAL)
        self.assertEqual(summary_obj["total_amount_minor"], 18446744073709551614)
        self.assertGreater(summary_obj["total_amount_minor"], MAX_MINOR)
        self.assertIsInstance(summary_obj["total_amount_minor"], int)

    # -- 失败场景 ----------------------------------------------------------

    def test_invalid_summary_queries_are_rejected(self) -> None:
        """缺少 --submitter 或提交人仅为空白：退出码 2，标准输出为空。"""
        cases = {
            "提交人仅含空白": ["summary", "--submitter", "  \t "],
            "缺少 --submitter": ["summary"],
        }
        for label, argv in cases.items():
            with self.subTest(case=label):
                result = run_cli(self.db_path, *argv)
                self.assertEqual(
                    result.returncode, 2,
                    msg=f"{label} 应退出码 2: stdout={result.stdout!r}",
                )
                self.assertEqual(result.stdout, "", msg=f"{label} 时标准输出应为空")
                self.assertTrue(
                    result.stderr.strip(),
                    msg=f"{label} 时标准错误应包含非空原因",
                )

        # 失败查询之后，正常汇总结果保持不变。
        _, summary_jia = self.assert_summary("演示甲")
        self.assertEqual(summary_jia, EXPECTED_JIA)
        _, summary_yi = self.assert_summary("演示乙")
        self.assertEqual(summary_yi, EXPECTED_YI)

    def test_missing_database_parent_is_a_database_error(self) -> None:
        """数据库父目录不存在：退出码 1，标准输出为空，标准错误说明数据库原因。"""
        missing_db = (
            Path(self._tmpdir.name) / "不存在的父目录" / "missing.sqlite"
        )
        self.assertFalse(missing_db.parent.exists())
        result = summarize(missing_db, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"父目录缺失应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库", result.stderr, msg="标准错误应说明是数据库原因")


if __name__ == "__main__":
    unittest.main()
