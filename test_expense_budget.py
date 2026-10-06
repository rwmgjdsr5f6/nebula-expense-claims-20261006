"""summary 一次性预算参考（--budget）的回归测试。

从项目根目录执行：

    python -m unittest test_expense_budget -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中提交固定样例费用，再由新进程对同一 SQLite 数据库执行
``summary --submitter --budget``，验证预算的解析、余额计算、失败优先级与
“不落库”性质。每个用例使用独立的临时 SQLite 文件，结束后自动清理；
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
# 两笔上限金额的合计与对 1 分预算的差额，均超过 64 位有符号整数范围。
TWO_MAX_TOTAL = 2 * MAX_MINOR

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


class SummaryBudgetTest(unittest.TestCase):
    """固定样例上的预算余额：尚有剩余、刚好用完、已经超额及无预算原行为。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "budget_regression.sqlite"
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def assert_budget_summary_ok(self, submitter: str, budget: str) -> dict:
        """带预算汇总成功：退出码 0、stderr 空、stdout 为恰好五个键的一行 JSON。"""
        result = summarize(self.db_path, submitter, budget)
        self.assertEqual(
            result.returncode, 0,
            msg=f"summary --budget {budget!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        data = json.loads(result.stdout)
        self.assertEqual(
            set(data.keys()),
            {
                "submitter",
                "count",
                "total_amount_minor",
                "budget_amount_minor",
                "remaining_amount_minor",
            },
        )
        for key in (
            "count",
            "total_amount_minor",
            "budget_amount_minor",
            "remaining_amount_minor",
        ):
            self.assertIsInstance(data[key], int, msg=f"{key} 应为整数")
            self.assertNotIsInstance(data[key], bool)
        return data

    def test_budget_15_leaves_269(self) -> None:
        """--budget 15：演示甲 2 笔、合计 1231、预算 1500、余额 269。"""
        data = self.assert_budget_summary_ok("演示甲", "15")
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
        self.assertGreater(data["remaining_amount_minor"], 0, msg="应有剩余")

    def test_budget_12_overruns_31(self) -> None:
        """预算改成 12：统计值不变，预算 1200、余额 -31。"""
        data = self.assert_budget_summary_ok("演示甲", "12")
        self.assertEqual(
            data,
            {
                "submitter": "演示甲",
                "count": 2,
                "total_amount_minor": 1231,
                "budget_amount_minor": 1200,
                "remaining_amount_minor": -31,
            },
        )
        self.assertLess(data["remaining_amount_minor"], 0, msg="应已超额")

    def test_budget_equal_total_is_zero_balance(self) -> None:
        """预算 12.31 元恰为合计 1231 分，余额为 0（刚好用完）。"""
        data = self.assert_budget_summary_ok("演示甲", "12.31")
        self.assertEqual(data["budget_amount_minor"], 1231)
        self.assertEqual(data["remaining_amount_minor"], 0)

    def test_decimal_and_padded_budget_parsed_like_amount(self) -> None:
        """一至两位小数、前导零、首尾空白均按提交金额的规则解析。"""
        cases = [
            ("15.0", 1500, 269),
            ("15.00", 1500, 269),
            ("  00015  \t", 1500, 269),
            ("0.01", 1, -1230),
        ]
        for budget, expected_budget, expected_remaining in cases:
            with self.subTest(budget=budget):
                data = self.assert_budget_summary_ok("演示甲", budget)
                self.assertEqual(data["budget_amount_minor"], expected_budget)
                self.assertEqual(data["remaining_amount_minor"], expected_remaining)

    def test_no_budget_keeps_original_three_fields(self) -> None:
        """不带 --budget 时只有原三个字段，含义与类型不变。"""
        result = summarize(self.db_path, "演示甲")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertEqual(
            data,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

    def test_no_match_and_fresh_db_leave_balance_equal_to_budget(self) -> None:
        """无匹配记录或父目录存在的新库：笔数合计为零，余额等于预算。"""
        # 前缀名称不命中。
        data = self.assert_budget_summary_ok("演示", "15")
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
        # 父目录存在的全新数据库。
        fresh_db = Path(self._tmpdir.name) / "fresh_budget.sqlite"
        self.assertFalse(fresh_db.exists())
        result = summarize(fresh_db, "演示甲", "15")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "submitter": "演示甲",
                "count": 0,
                "total_amount_minor": 0,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 1500,
            },
        )

    def test_budget_is_not_persisted(self) -> None:
        """预算仅供当次参考：带预算汇总后，后续无预算汇总与 list 均不受影响。"""
        before = run_cli(self.db_path, "list", "--submitter", "演示甲")
        self.assertEqual(before.returncode, 0, msg=before.stderr)

        first = summarize(self.db_path, "演示甲", "15")
        second = summarize(self.db_path, "演示甲", "12")
        self.assertNotEqual(first.stdout, second.stdout, msg="两次预算结果应不同")

        # 不带预算的后续汇总仍是原三个字段，且结果不受之前预算影响。
        after_no_budget = summarize(self.db_path, "演示甲")
        self.assertEqual(
            json.loads(after_no_budget.stdout),
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )
        # 预算未留下任何记录：list 结果逐字节不变。
        after = run_cli(self.db_path, "list", "--submitter", "演示甲")
        self.assertEqual(after.stdout, before.stdout)

    def test_submitter_still_stripped_and_exact_with_budget(self) -> None:
        """带预算时提交人仍先去首尾空白再精确匹配，前缀不命中。"""
        data = self.assert_budget_summary_ok("  演示甲\t\n ", "15")
        self.assertEqual(data["submitter"], "演示甲")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["remaining_amount_minor"], 269)

    def test_budget_scoped_to_summary_only(self) -> None:
        """submit/list/export 不接受 --budget。"""
        for args in [
            ("submit", "--submitter", "x", "--purpose", "y", "--amount", "1",
             "--budget", "2"),
            ("list", "--submitter", "演示甲", "--budget", "15"),
            ("export", "--submitter", "演示甲", "--budget", "15"),
        ]:
            with self.subTest(command=args[0]):
                result = run_cli(self.db_path, *args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("--budget", result.stderr)


class SummaryBudgetLargeTotalTest(unittest.TestCase):
    """预算差额与合计超过单笔范围时，仍以完整十进制 JSON 整数精确输出。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_big_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "large_total_budget.sqlite"
        for _ in range(2):
            result = submit(self.db_path, OVERFLOW_SUBMITTER, "大额费用", MAX_YUAN)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def test_remaining_negative_overflow_full_precision(self) -> None:
        """两笔上限金额对 1 分预算：合计与余额均完整精确，非科学计数法。"""
        result = summarize(self.db_path, OVERFLOW_SUBMITTER, "0.01")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertEqual(data["total_amount_minor"], TWO_MAX_TOTAL)
        self.assertEqual(data["budget_amount_minor"], 1)
        self.assertEqual(data["remaining_amount_minor"], 1 - TWO_MAX_TOTAL)
        self.assertEqual(
            data["remaining_amount_minor"], -18446744073709551613
        )

        for field, token in [
            ("total_amount_minor", "18446744073709551614"),
            ("remaining_amount_minor", "-18446744073709551613"),
        ]:
            matched = re.search(
                rf'"{field}":\s*([^,}}]+)', result.stdout
            ).group(1).strip()
            self.assertEqual(matched, token, msg=f"{field} 应为完整十进制整数")
            self.assertNotIn("e", matched.lower())

    def test_budget_at_single_entry_limit_accepted(self) -> None:
        """预算恰好为 2^63−1 分（92233720368547758.07 元）可以接受。"""
        data_result = summarize(self.db_path, OVERFLOW_SUBMITTER, MAX_YUAN)
        self.assertEqual(data_result.returncode, 0, msg=data_result.stderr)
        data = json.loads(data_result.stdout)
        self.assertEqual(data["budget_amount_minor"], MAX_MINOR)
        # 合计两笔上限，余额为负且精确。
        self.assertEqual(data["remaining_amount_minor"], MAX_MINOR - TWO_MAX_TOTAL)


class SummaryBudgetFailureTest(unittest.TestCase):
    """预算参数错误、缺失值及与数据库错误的优先级。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_budget_fail_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "failure.sqlite"
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def assert_budget_rejected(self, budget: str, reason: str) -> None:
        """无效预算：退出码 2、stdout 空、stderr 指出预算原因且无异常堆栈。"""
        result = summarize(self.db_path, "演示甲", budget)
        self.assertEqual(
            result.returncode, 2,
            msg=f"预算 {budget!r} 应退出码 2: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr, msg="标准错误应指出预算字段")
        self.assertIn(reason, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_budgets_exit_2(self) -> None:
        """空预算、零、负数、科学计数法、超两位小数、超限均为退出码 2。"""
        cases = [
            ("   ", "不能为空"),
            ("0", "必须大于零"),
            ("0.00", "必须大于零"),
            ("-5", "格式无效"),
            ("1e2", "格式无效"),
            ("12.345", "格式无效"),
            ("abc", "格式无效"),
            (OVER_MAX_YUAN, "超过"),
            ("9" * 5000, "超过"),
            ("0" * 5000, "必须大于零"),
        ]
        for budget, reason in cases:
            with self.subTest(budget=budget[:20]):
                self.assert_budget_rejected(budget, reason)

    def test_budget_without_value_exits_2(self) -> None:
        """--budget 后没有值：退出码 2，stdout 空，stderr 说明参数原因。"""
        result = run_cli(
            self.db_path, "summary", "--submitter", "演示甲", "--budget"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--budget", result.stderr)

    def test_blank_submitter_with_budget_exits_2(self) -> None:
        """提交人仅为空白（即使预算有效）：退出码 2。"""
        result = summarize(self.db_path, "  \t ", "15")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())

    def test_invalid_budget_takes_priority_over_bad_db(self) -> None:
        """预算无效与数据库路径无效同时发生：优先预算错误（2），不创建数据库文件。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        self.assertFalse(missing_db.parent.exists())
        result = summarize(missing_db, "演示甲", "abc")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr)
        self.assertFalse(missing_db.exists(), msg="预算失败时不应创建数据库文件")

    def test_valid_budget_with_bad_db_exits_1(self) -> None:
        """预算有效而数据库无法创建：退出码 1，stdout 空，原因写入 stderr。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        result = summarize(missing_db, "演示甲", "15")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("数据库", result.stderr)
        self.assertFalse(missing_db.exists())

    def test_budget_failure_leaves_data_and_later_summaries_intact(self) -> None:
        """多次被拒的预算不改变数据，之后有效汇总结果不变。"""
        for bad in ("0", "abc", OVER_MAX_YUAN):
            result = summarize(self.db_path, "演示甲", bad)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
        ok = summarize(self.db_path, "演示甲", "15")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(
            json.loads(ok.stdout),
            {
                "submitter": "演示甲",
                "count": 2,
                "total_amount_minor": 1231,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 269,
            },
        )


if __name__ == "__main__":
    unittest.main()
