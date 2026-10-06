"""summary 子命令一次性预算参考（--budget）的回归测试。

从项目根目录执行：

    python -m unittest test_expense_budget -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中提交固定样例费用，再由新进程对同一 SQLite 数据库执行
``summary --submitter --budget``，验证预算字段、余额正零负三态、参数错误
（退出码 2）与数据库错误（退出码 1）的优先级，以及预算不落库、不带预算时
结果仍是原有三个字段。每个用例使用独立的临时 SQLite 文件，结束后自动清理；
仅使用 Python 标准库，不访问公网。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 固定样例：演示甲(交通费 12.30)、演示乙(办公费 5)、演示甲(餐费 0.01)。
SEED_EXPENSES = [
    ("演示甲", "交通费", "12.30"),
    ("演示乙", "办公费", "5"),
    ("演示甲", "餐费", "0.01"),
]

# SQLite 64 位有符号整数上限对应的元金额与分值；预算允许达到该上限。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
OVER_MAX_YUAN = "92233720368547758.08"
# 两笔上限金额的合计与 100 元预算下的余额（均超过 64 位有符号整数范围）。
TWO_MAX_TOTAL = 2 * MAX_MINOR
REMAINING_UNDER_100 = 10000 - TWO_MAX_TOTAL  # -18446744073709541614

OVERFLOW_SUBMITTER = "预算测试·虚构极限用户"


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


def summarize(db_path: Path, submitter: str, budget: str | None = None):
    args = ["summary", "--submitter", submitter]
    if budget is not None:
        args += ["--budget", budget]
    return run_cli(db_path, *args)


class BudgetSummaryTest(unittest.TestCase):
    """提供 --budget 时的五字段结果：正余额、零余额、负余额与无匹配零值。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "budget.sqlite"
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def assert_budget_summary(
        self, submitter: str, budget: str, expected: dict
    ) -> dict:
        result = summarize(self.db_path, submitter, budget)
        self.assertEqual(
            result.returncode, 0,
            msg=f"summary --budget {budget!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertNotIn("\n", result.stdout[:-1])
        data = json.loads(result.stdout)
        self.assertEqual(
            set(data.keys()),
            {
                "submitter", "count", "total_amount_minor",
                "budget_amount_minor", "remaining_amount_minor",
            },
        )
        self.assertEqual(data, expected)
        for key in (
            "count", "total_amount_minor",
            "budget_amount_minor", "remaining_amount_minor",
        ):
            self.assertIsInstance(data[key], int, msg=f"{key} 应为整数")
            self.assertNotIsInstance(data[key], bool)
        return data

    def test_fixed_sample_budget_15_leaves_269(self) -> None:
        """预算 15 元：演示甲 2 笔、合计 1231、预算 1500、余额 269 分。"""
        self.assert_budget_summary(
            "演示甲", "15",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1500, "remaining_amount_minor": 269,
            },
        )

    def test_fixed_sample_budget_12_overspent_31(self) -> None:
        """预算改成 12 元：统计值不变，余额为 -31 分（已经超额）。"""
        self.assert_budget_summary(
            "演示甲", "12",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1200, "remaining_amount_minor": -31,
            },
        )

    def test_budget_equal_total_leaves_zero(self) -> None:
        """预算恰好等于合计 12.31 元：余额为 0（刚好用完）。"""
        self.assert_budget_summary(
            "演示甲", "12.31",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1231, "remaining_amount_minor": 0,
            },
        )

    def test_budget_accepts_leading_zeros_and_surrounding_whitespace(self) -> None:
        """前导零与首尾空白不改变预算换算。"""
        for raw in ["00015", " 15 ", "\t0015.00\n"]:
            with self.subTest(budget=raw):
                data = self.assert_budget_summary(
                    "演示甲", raw,
                    {
                        "submitter": "演示甲", "count": 2,
                        "total_amount_minor": 1231,
                        "budget_amount_minor": 1500,
                        "remaining_amount_minor": 269,
                    },
                )
                self.assertGreater(data["remaining_amount_minor"], 0)

    def test_one_and_two_decimal_budgets(self) -> None:
        """一位与两位小数预算分别按 10 分、1 分精度换算。"""
        self.assert_budget_summary(
            "演示甲", "12.4",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1240, "remaining_amount_minor": 9,
            },
        )
        self.assert_budget_summary(
            "演示甲", "12.32",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1232, "remaining_amount_minor": 1,
            },
        )

    def test_no_match_and_fresh_database_budget_equals_remaining(self) -> None:
        """无匹配记录或父目录存在的新库：笔数合计为零，余额等于预算。"""
        # 前缀名称“演示”不命中演示甲/演示乙。
        self.assert_budget_summary(
            "演示", "15",
            {
                "submitter": "演示", "count": 0, "total_amount_minor": 0,
                "budget_amount_minor": 1500, "remaining_amount_minor": 1500,
            },
        )
        fresh_db = Path(self._tmpdir.name) / "fresh_budget.sqlite"
        self.assertFalse(fresh_db.exists())
        result = summarize(fresh_db, "演示丙", "15")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "submitter": "演示丙", "count": 0, "total_amount_minor": 0,
                "budget_amount_minor": 1500, "remaining_amount_minor": 1500,
            },
        )

    def test_submitter_still_stripped_and_exact_with_budget(self) -> None:
        """带预算时提交人仍先去除首尾空白再精确匹配。"""
        self.assert_budget_summary(
            "  演示甲\t\n ", "15",
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1500, "remaining_amount_minor": 269,
            },
        )

    def test_max_valid_budget(self) -> None:
        """预算上限 92233720368547758.07 元 = 2^63−1 分，可正常汇总。"""
        data = self.assert_budget_summary(
            "演示甲", MAX_YUAN,
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": MAX_MINOR,
                "remaining_amount_minor": MAX_MINOR - 1231,
            },
        )
        self.assertEqual(data["budget_amount_minor"], 9223372036854775807)


class BudgetLargeRemainingTest(unittest.TestCase):
    """合计超过单笔范围时，余额仍以完整十进制 JSON 整数精确输出。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_big_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "large.sqlite"
        for _ in range(2):
            result = submit(self.db_path, OVERFLOW_SUBMITTER, "大额费用", MAX_YUAN)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def test_negative_remaining_output_as_full_decimal_integer(self) -> None:
        result = summarize(self.db_path, OVERFLOW_SUBMITTER, "100")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total_amount_minor"], TWO_MAX_TOTAL)
        self.assertEqual(data["budget_amount_minor"], 10000)
        self.assertEqual(data["remaining_amount_minor"], REMAINING_UNDER_100)
        self.assertEqual(data["remaining_amount_minor"], -18446744073709541614)
        self.assertLess(data["remaining_amount_minor"], -MAX_MINOR)
        # 原始输出中余额即完整十进制整数，不允许小数点或科学计数法。
        token = re.search(
            r'"remaining_amount_minor":\s*([^,}]+)', result.stdout
        ).group(1).strip()
        self.assertEqual(token, "-18446744073709541614")
        self.assertNotIn(".", token)
        self.assertNotIn("e", token.lower())


class BudgetValidationTest(unittest.TestCase):
    """无效预算与缺失参数：退出码 2、空标准输出、标准错误说明原因。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_invalid_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "failure.sqlite"
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def assert_budget_rejected(self, budget: str) -> None:
        result = summarize(self.db_path, "演示甲", budget)
        self.assertEqual(
            result.returncode, 2,
            msg=f"预算 {budget!r} 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_budgets_exit_2_without_touching_data(self) -> None:
        """空、零、负、科学计数法、三位小数、超限及超长数字一律拒绝。"""
        invalid_budgets = [
            "", "   ", "\t\n",
            "0", "0.00", "000",
            "-5", "+5",
            "1e2", "15E0", ".5", "15.",
            "12.300", "12.3.0", "abc", "１５",
            OVER_MAX_YUAN, "9" * 5000,
        ]
        for budget in invalid_budgets:
            with self.subTest(budget=budget):
                self.assert_budget_rejected(budget)

        # 全部拒绝后，带有效预算与不带预算的汇总都保持应有结果。
        ok = summarize(self.db_path, "演示甲", "15")
        self.assertEqual(ok.returncode, 0, msg=ok.stderr)
        self.assertEqual(
            json.loads(ok.stdout),
            {
                "submitter": "演示甲", "count": 2, "total_amount_minor": 1231,
                "budget_amount_minor": 1500, "remaining_amount_minor": 269,
            },
        )

    def test_budget_without_value_exits_2(self) -> None:
        """--budget 后没有值：argparse 拦截，退出码 2 且标准输出为空。"""
        result = run_cli(self.db_path, "summary", "--submitter", "演示甲", "--budget")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--budget", result.stderr)

    def test_missing_submitter_with_budget_exits_2(self) -> None:
        """即使提供 --budget，缺少 --submitter 仍退出码 2。"""
        result = run_cli(self.db_path, "summary", "--budget", "15")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())

    def test_blank_submitter_with_budget_exits_2(self) -> None:
        """提交人仅为空白：退出码 2 并说明提交人原因。"""
        result = summarize(self.db_path, "  \t ", "15")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("提交人", result.stderr)

    def test_invalid_budget_takes_priority_over_bad_database_path(self) -> None:
        """预算无效与数据库路径无效同时出现：优先预算错误，不建库文件。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        self.assertFalse(missing_db.parent.exists())
        result = summarize(missing_db, "演示甲", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr)
        self.assertNotIn("数据库", result.stderr)
        self.assertFalse(missing_db.exists())
        self.assertFalse(missing_db.parent.exists())

    def test_valid_budget_with_bad_database_path_exits_1(self) -> None:
        """预算有效而数据库无法创建：退出码 1、空标准输出、说明数据库原因。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        result = summarize(missing_db, "演示甲", "15")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("数据库", result.stderr)
        self.assertFalse(missing_db.exists())


class BudgetNotPersistedTest(unittest.TestCase):
    """预算仅供当次参考：不落库，不影响后续无预算汇总与 list/export。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_ephemeral_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "ephemeral.sqlite"
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def test_later_summary_without_budget_keeps_three_fields(self) -> None:
        first = summarize(self.db_path, "演示甲", "15")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        later = summarize(self.db_path, "演示甲")
        self.assertEqual(later.returncode, 0, msg=later.stderr)
        self.assertEqual(later.stderr, "")
        data = json.loads(later.stdout)
        self.assertEqual(
            set(data.keys()),
            {"submitter", "count", "total_amount_minor"},
        )
        self.assertEqual(
            data,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

    def test_budget_summary_does_not_change_records(self) -> None:
        """带预算汇总前后，list 结果逐字节一致且仍是 pending 的原金额。"""
        before = run_cli(self.db_path, "list", "--submitter", "演示甲")
        self.assertEqual(before.returncode, 0, msg=before.stderr)
        for budget in ["15", "12", "0.01", MAX_YUAN]:
            result = summarize(self.db_path, "演示甲", budget)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
        after = run_cli(self.db_path, "list", "--submitter", "演示甲")
        self.assertEqual(after.stdout, before.stdout)
        records = json.loads(after.stdout)
        self.assertEqual(
            [(r["id"], r["purpose"], r["amount_minor"], r["status"]) for r in records],
            [(1, "交通费", 1230, "pending"), (3, "餐费", 1, "pending")],
        )

        # 演示乙的记录同样不受影响。
        yi = json.loads(
            run_cli(self.db_path, "list", "--submitter", "演示乙").stdout
        )
        self.assertEqual(
            [(r["id"], r["purpose"], r["amount_minor"], r["status"]) for r in yi],
            [(2, "办公费", 500, "pending")],
        )


if __name__ == "__main__":
    unittest.main()
