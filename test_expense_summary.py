"""按提交人汇总报销笔数与金额的回归测试。

从项目根目录执行：

    python -m unittest test_expense_summary -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中提交虚构费用，待提交进程退出后，再由另一个新进程对同一 SQLite
数据库执行 ``summary --submitter``，验证汇总的既有行为。每个用例使用独立的
临时 SQLite 文件，结束后自动清理，重复执行不依赖任何已有数据；仅使用 Python
标准库，不访问公网。
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
# 金额以整数分存储，故分别为 1230、500、1 分。
SEED_EXPENSES = [
    ("演示甲", "交通费", "12.30", 1230),
    ("演示乙", "办公费", "5", 500),
    ("演示甲", "餐费", "0.01", 1),
]

# SQLite 64 位有符号整数上限对应的元金额与分值；单笔金额允许达到该上限。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
# 两笔上限金额的合计：2 * (2**63 - 1)，超过 64 位有符号整数上限，
# 汇总合计仍应精确得到该值，不得舍入或溢出。
TWO_MAX_TOTAL = 2 * MAX_MINOR  # 18446744073709551614

# 大额合计用例的虚构提交人，避免与任何真实数据冲突。
OVERFLOW_SUBMITTER = "汇总测试·虚构极限用户"


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
    """按提交人汇总：固定样例的笔数与合计，以及空白、前缀、新库等边界。

    每个用例先按演示甲、演示乙、演示甲的顺序提交三笔固定样例费用，
    提交进程全部退出后，再由新进程对同一数据库执行 summary 查询。
    """

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "summary_regression.sqlite"
        self.submitted = [self._submit_ok(*seed[:3]) for seed in SEED_EXPENSES]
        self.seed_jia = [self.submitted[0], self.submitted[2]]  # 演示甲的两笔
        self.seed_yi = [self.submitted[1]]                      # 演示乙的一笔

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_summary(self, submitter: str, db_path: Path | None = None):
        """在新进程中汇总：退出码 0、标准错误为空、标准输出为一行 JSON 对象。

        返回 (CompletedProcess, 解析后的汇总字典)。
        """
        result = summarize(db_path or self.db_path, submitter)
        self.assertEqual(
            result.returncode, 0,
            msg=f"summary --submitter {submitter!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"summary --submitter {submitter!r} 的标准错误应为空",
        )
        # 标准输出为单行 JSON（仅末尾一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        self.assertEqual(
            set(data.keys()),
            {"submitter", "count", "total_amount_minor"},
        )
        # 提交人是去除首尾空白后的名称；笔数与合计均为整数（排除布尔值）。
        self.assertEqual(data["submitter"], submitter.strip())
        self.assertIsInstance(data["count"], int)
        self.assertNotIsInstance(data["count"], bool)
        self.assertIsInstance(data["total_amount_minor"], int)
        self.assertNotIsInstance(data["total_amount_minor"], bool)
        self.assertGreaterEqual(data["count"], 0)
        self.assertGreaterEqual(data["total_amount_minor"], 0)
        return result, data

    def assert_list(self, submitter: str):
        """在新进程中查询：退出码 0、标准错误为空，返回解析后的记录列表。"""
        result = list_records(self.db_path, submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 固定样例 ----------------------------------------------------------

    def test_fixed_samples_counts_and_totals(self) -> None:
        """演示甲 2 笔合计 1231 分，演示乙 1 笔合计 500 分，互不混入。"""
        _, summary_jia = self.assert_summary("演示甲")
        self.assertEqual(
            summary_jia,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

        _, summary_yi = self.assert_summary("演示乙")
        self.assertEqual(
            summary_yi,
            {"submitter": "演示乙", "count": 1, "total_amount_minor": 500},
        )

        # 与新进程 list 出的逐笔金额交叉核对：合计等于该提交人各笔之和，
        # 演示甲(1230 + 1)中不含演示乙的 500，演示乙也不含演示甲的金额。
        records_jia = self.assert_list("演示甲")
        records_yi = self.assert_list("演示乙")
        amounts_jia = [record["amount_minor"] for record in records_jia]
        amounts_yi = [record["amount_minor"] for record in records_yi]
        self.assertEqual(amounts_jia, [1230, 1])
        self.assertEqual(amounts_yi, [500])
        self.assertEqual(sum(amounts_jia), summary_jia["total_amount_minor"])
        self.assertEqual(sum(amounts_yi), summary_yi["total_amount_minor"])
        self.assertTrue(all(r["submitter"] == "演示甲" for r in records_jia))
        self.assertTrue(all(r["submitter"] == "演示乙" for r in records_yi))

    def test_query_name_is_stripped_before_matching(self) -> None:
        """汇总查询名称两端带空白时，仍命中清理后的演示甲。"""
        _, summary_jia = self.assert_summary("  演示甲\t\n ")
        self.assertEqual(
            summary_jia,
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

    def test_prefix_and_unknown_names_return_zeroes(self) -> None:
        """查询前缀“演示”或未提交过的“演示丙”，返回该名称及两个零值。"""
        for name in ["演示", "演示丙"]:
            with self.subTest(submitter=name):
                _, data = self.assert_summary(name)
                self.assertEqual(
                    data,
                    {"submitter": name, "count": 0, "total_amount_minor": 0},
                )

    def test_fresh_database_returns_zeroes(self) -> None:
        """对父目录存在的全新数据库首次汇总，退出码 0 并返回两个零值。"""
        fresh_db = Path(self._tmpdir.name) / "fresh_summary.sqlite"
        self.assertFalse(fresh_db.exists())
        _, data = self.assert_summary("演示甲", db_path=fresh_db)
        self.assertEqual(
            data,
            {"submitter": "演示甲", "count": 0, "total_amount_minor": 0},
        )

    def test_repeated_summaries_stable_and_list_unchanged(self) -> None:
        """连续两次汇总结果相同，且汇总前后 list 结果保持一致。"""
        before_jia = self.assert_list("演示甲")
        before_yi = self.assert_list("演示乙")

        first_jia, summary_jia = self.assert_summary("演示甲")
        second_jia, _ = self.assert_summary("演示甲")
        first_yi, summary_yi = self.assert_summary("演示乙")
        second_yi, _ = self.assert_summary("演示乙")
        self.assertEqual(second_jia.stdout, first_jia.stdout, msg="重复汇总演示甲应一致")
        self.assertEqual(second_yi.stdout, first_yi.stdout, msg="重复汇总演示乙应一致")

        # 汇总前后记录数量、id、用途、金额与 pending 状态完全一致。
        after_jia = self.assert_list("演示甲")
        after_yi = self.assert_list("演示乙")
        self.assertEqual(after_jia, before_jia)
        self.assertEqual(after_yi, before_yi)

        for records, expected, expected_total in [
            (after_jia, self.seed_jia, summary_jia["total_amount_minor"]),
            (after_yi, self.seed_yi, summary_yi["total_amount_minor"]),
        ]:
            self.assertEqual(len(records), len(expected))
            self.assertEqual([r["id"] for r in records], [r["id"] for r in expected])
            self.assertEqual(
                [r["purpose"] for r in records], [r["purpose"] for r in expected]
            )
            self.assertEqual(
                [r["amount_minor"] for r in records],
                [r["amount_minor"] for r in expected],
            )
            self.assertTrue(all(r["status"] == "pending" for r in records))
            self.assertEqual(
                sum(r["amount_minor"] for r in records), expected_total
            )


class SummaryLargeTotalTest(unittest.TestCase):
    """汇总合计可超过单笔金额上限：在 Python 端按任意精度整数求和。"""

    def setUp(self) -> None:
        # 使用与固定样例相互独立的临时数据库。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_total_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "large_total.sqlite"

    def test_total_can_exceed_single_entry_limit(self) -> None:
        """两笔 92233720368547758.07 的合计精确为 18446744073709551614。"""
        records = []
        for _ in range(2):
            result = submit(self.db_path, OVERFLOW_SUBMITTER, "大额费用", MAX_YUAN)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
            record = json.loads(result.stdout)
            # 单笔金额精确为 64 位有符号整数上限，且为整数而非浮点。
            self.assertEqual(record["amount_minor"], MAX_MINOR)
            self.assertIsInstance(record["amount_minor"], int)
            self.assertEqual(record["status"], "pending")
            records.append(record)
        self.assertEqual(len({r["id"] for r in records}), 2, msg="两笔应生成独立 id")

        # 提交进程已退出；在新进程中汇总同一数据库。
        result = summarize(self.db_path, OVERFLOW_SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"summary 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertEqual(data["submitter"], OVERFLOW_SUBMITTER)
        self.assertEqual(data["count"], 2)
        # 合计超过 64 位有符号整数上限，仍须精确、无舍入、无溢出。
        self.assertEqual(data["total_amount_minor"], TWO_MAX_TOTAL)
        self.assertEqual(data["total_amount_minor"], 18446744073709551614)
        self.assertIsInstance(data["total_amount_minor"], int)
        self.assertGreater(data["total_amount_minor"], MAX_MINOR)
        # 原始输出中合计即完整十进制整数，不允许小数点或科学计数法。
        total_token = re.search(
            r'"total_amount_minor":\s*([^,}]+)', result.stdout
        ).group(1).strip()
        self.assertEqual(total_token, "18446744073709551614")
        self.assertNotIn(".", total_token)
        self.assertNotIn("e", total_token.lower())

        # 跨进程复查：两笔原始记录仍是 pending 的上限金额，汇总未改动数据。
        listed = json.loads(list_records(self.db_path, OVERFLOW_SUBMITTER).stdout)
        self.assertEqual(len(listed), 2)
        self.assertEqual([r["amount_minor"] for r in listed], [MAX_MINOR, MAX_MINOR])
        self.assertTrue(all(r["status"] == "pending" for r in listed))


class SummaryFailureTest(unittest.TestCase):
    """汇总命令的参数错误与数据库错误：退出码、空标准输出与标准错误说明。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_fail_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "failure.sqlite"
        # 预置固定样例，便于复查参数错误不影响已有数据。
        for submitter, purpose, amount, _ in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def test_missing_submitter_exits_2(self) -> None:
        """缺少 --submitter：退出码 2，标准输出为空，标准错误含非空原因。"""
        result = run_cli(self.db_path, "summary")
        self.assertEqual(
            result.returncode, 2,
            msg=f"缺少 --submitter 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空原因")

    def test_blank_submitter_exits_2(self) -> None:
        """提交人仅为空白：退出码 2，标准输出为空，标准错误含非空原因。"""
        result = run_cli(self.db_path, "summary", "--submitter", "  \t ")
        self.assertEqual(
            result.returncode, 2,
            msg=f"空白提交人应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空原因")

        # 参数错误之后，已有提交人的汇总结果保持不变。
        result = summarize(self.db_path, "演示甲")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout),
            {"submitter": "演示甲", "count": 2, "total_amount_minor": 1231},
        )

    def test_missing_database_parent_exits_1(self) -> None:
        """数据库父目录不存在：退出码 1，标准输出为空，标准错误说明数据库原因。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        self.assertFalse(missing_db.parent.exists())
        result = summarize(missing_db, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"数据库父目录缺失应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库", result.stderr)
        # 失败后不应遗留数据库文件或目录。
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
