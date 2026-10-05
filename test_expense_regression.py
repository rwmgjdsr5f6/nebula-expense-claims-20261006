"""金额入库后再次读取的回归测试。

从项目根目录执行：

    python -m unittest test_expense_regression -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / list 公开命令：
提交在独立进程中完成并退出后，再由另一个新进程对同一 SQLite 数据库执行
list，验证进程关闭后数据仍可读取。每次运行使用独立的临时 SQLite 文件与
虚构人员，重复执行不依赖已有数据；仅使用 Python 标准库，不访问公网。
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

# 虚构人员与用途，避免与任何真实数据冲突。
SUBMITTER = "回归测试·虚构甲"
PURPOSE = "回归测试·虚构用途"

# 按提交人查询的固定正常样例：演示甲、演示乙、演示甲各一笔。
SEED_EXPENSES = [
    ("演示甲", "演示交通", "12.30"),
    ("演示乙", "演示办公", "5"),
    ("演示甲", "演示餐费", "0.01"),
]

# SQLite 64 位有符号整数上限对应的元与分。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
OVER_MAX_YUAN = "92233720368547758.08"


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


class ExpenseRegressionTest(unittest.TestCase):
    """提交成功后再由新进程读取，以及失败提交不影响已有记录。"""

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "regression.sqlite"

    # -- 辅助 ------------------------------------------------------------

    def assert_submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        """提交应成功：退出码 0，标准输出为一条 JSON 对象。"""
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        return record

    def assert_list(self, submitter: str) -> list:
        """在新进程中查询：退出码 0，标准输出为 JSON 数组。"""
        result = list_records(self.db_path, submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    def assert_submit_rejected(self, submitter: str, purpose: str, amount: str) -> None:
        """无效提交：退出码 2，标准输出为空，标准错误含非空说明。"""
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 2, msg=f"应拒绝却成功: {result.stdout}")
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的输入错误说明")

    # -- 成功场景 ----------------------------------------------------------

    def test_amounts_roundtrip_across_processes(self) -> None:
        """金额入库后由另一进程读取，分值为预期整数。"""
        cases = [
            ("12", 1200),
            ("12.3", 1230),
            ("12.30", 1230),
            ("0.01", 1),
            (MAX_YUAN, MAX_MINOR),
        ]
        submitted = []
        for amount, expected_minor in cases:
            with self.subTest(amount=amount):
                record = self.assert_submit_ok(SUBMITTER, PURPOSE, amount)
                self.assertEqual(record["amount_minor"], expected_minor)
                self.assertIsInstance(record["amount_minor"], int)
                self.assertEqual(record["submitter"], SUBMITTER)
                self.assertEqual(record["purpose"], PURPOSE)
                self.assertEqual(record["status"], "pending")
                submitted.append(record)

        # 提交进程已全部退出；在新进程中读取同一数据库。
        records = self.assert_list(SUBMITTER)
        self.assertEqual(len(records), len(cases))
        for record, (_, expected_minor), original in zip(records, cases, submitted):
            self.assertEqual(record["amount_minor"], expected_minor)
            self.assertIsInstance(record["amount_minor"], int)
            self.assertEqual(record["submitter"], SUBMITTER)
            self.assertEqual(record["purpose"], PURPOSE)
            self.assertEqual(record["status"], "pending")
            # id 与提交时相同，且整体按 id 升序。
            self.assertEqual(record["id"], original["id"])
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids))

    def test_submitter_and_purpose_are_stripped(self) -> None:
        """提交人与用途按去除首尾空白后的值保存与查询。"""
        record = self.assert_submit_ok(
            f"  {SUBMITTER}\t", f"\n{PURPOSE}  ", "12"
        )
        self.assertEqual(record["submitter"], SUBMITTER)
        self.assertEqual(record["purpose"], PURPOSE)

        records = self.assert_list(f"  {SUBMITTER}  ")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["submitter"], SUBMITTER)
        self.assertEqual(records[0]["purpose"], PURPOSE)
        self.assertEqual(records[0]["id"], record["id"])

    def test_equivalent_amounts_keep_separate_records(self) -> None:
        """同一提交人和用途提交 12.3 与 12.30，查询保留两条独立记录。"""
        first = self.assert_submit_ok(SUBMITTER, PURPOSE, "12.3")
        second = self.assert_submit_ok(SUBMITTER, PURPOSE, "12.30")
        self.assertNotEqual(first["id"], second["id"])

        records = self.assert_list(SUBMITTER)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["id"] for record in records], [first["id"], second["id"]]
        )
        self.assertEqual([record["amount_minor"] for record in records], [1230, 1230])
        self.assertTrue(all(record["status"] == "pending" for record in records))

    # -- 失败场景 ----------------------------------------------------------

    def test_invalid_submissions_do_not_change_existing_records(self) -> None:
        """先建立成功记录，再逐项尝试无效输入并复查结果不变。"""
        ok_first = self.assert_submit_ok(SUBMITTER, PURPOSE, "12.3")
        ok_second = self.assert_submit_ok(SUBMITTER, PURPOSE, "12.30")
        baseline = self.assert_list(SUBMITTER)
        self.assertEqual([r["id"] for r in baseline], [ok_first["id"], ok_second["id"]])

        invalid_cases = [
            ("amount 零", SUBMITTER, PURPOSE, "0"),
            ("amount 负数", SUBMITTER, PURPOSE, "-1"),
            ("amount 三位小数", SUBMITTER, PURPOSE, "12.300"),
            ("amount 科学计数法", SUBMITTER, PURPOSE, "1e2"),
            ("amount 空白", SUBMITTER, PURPOSE, "   "),
            ("amount 超上限", SUBMITTER, PURPOSE, OVER_MAX_YUAN),
            ("submitter 空白", "   ", PURPOSE, "12.30"),
            ("purpose 空白", SUBMITTER, "  \t ", "12.30"),
        ]
        for label, submitter, purpose, amount in invalid_cases:
            with self.subTest(case=label):
                self.assert_submit_rejected(submitter, purpose, amount)
                # 每次失败后在新的进程中复查：内容与数量均保持不变。
                records = self.assert_list(SUBMITTER)
                self.assertEqual(records, baseline)


class ListBySubmitterTest(unittest.TestCase):
    """按提交人查询：同一库内不同提交人相互隔离，完整名称精确匹配。

    每个用例先按演示甲、演示乙、演示甲的顺序提交三笔固定样例费用，
    提交进程退出后再由新进程对同一数据库执行 list 查询。
    """

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_list_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "list_regression.sqlite"
        # 固定样例：演示甲(交通 12.30)、演示乙(办公 5)、演示甲(餐费 0.01)。
        self.submitted = [self._submit_ok(*seed) for seed in SEED_EXPENSES]
        self.seed_jia = [self.submitted[0], self.submitted[2]]  # 演示甲的两笔
        self.seed_yi = [self.submitted[1]]                      # 演示乙的一笔

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_query(self, submitter: str, db_path: Path | None = None):
        """在新进程中查询：退出码 0，标准错误为空，标准输出为一行 JSON 数组。

        返回 (CompletedProcess, 解析后的记录列表)，便于比较原始输出。
        """
        result = list_records(db_path or self.db_path, submitter)
        self.assertEqual(
            result.returncode, 0,
            msg=f"list --submitter {submitter!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"list --submitter {submitter!r} 的标准错误应为空",
        )
        # 标准输出为单行 JSON（仅末尾一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return result, records

    def assert_records_match(self, records: list, expected: list) -> None:
        """查询结果应与提交时返回的记录完全一致（含 id 升序）。"""
        self.assertEqual(records, expected)
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids), msg="查询结果应按 id 升序排列")
        for record in records:
            self.assertEqual(record["status"], "pending")

    # -- 正常样例 ----------------------------------------------------------

    def test_submitters_are_isolated_and_exact(self) -> None:
        """演示甲只命中第一、三笔，演示乙只命中第二笔，字段完整保留。"""
        _, records_jia = self.assert_query("演示甲")
        self.assert_records_match(records_jia, self.seed_jia)
        self.assertEqual(
            [r["amount_minor"] for r in records_jia], [1230, 1],
            msg="演示甲的金额（分）应为 1230 与 1",
        )

        _, records_yi = self.assert_query("演示乙")
        self.assert_records_match(records_yi, self.seed_yi)
        self.assertEqual(
            [r["amount_minor"] for r in records_yi], [500],
            msg="演示乙的金额（分）应为 500",
        )

        # 两人的记录互不包含对方内容。
        self.assertTrue(all(r["submitter"] == "演示甲" for r in records_jia))
        self.assertTrue(all(r["submitter"] == "演示乙" for r in records_yi))

    def test_repeated_queries_are_stable(self) -> None:
        """重复查询两人结果逐字节相同，原记录不增不改。"""
        first_jia, records_jia = self.assert_query("演示甲")
        first_yi, records_yi = self.assert_query("演示乙")
        second_jia, _ = self.assert_query("演示甲")
        second_yi, _ = self.assert_query("演示乙")

        self.assertEqual(second_jia.stdout, first_jia.stdout, msg="重复查询演示甲应一致")
        self.assertEqual(second_yi.stdout, first_yi.stdout, msg="重复查询演示乙应一致")
        # 三笔原始记录仍完整存在，未被增删或改写。
        self.assert_records_match(records_jia, self.seed_jia)
        self.assert_records_match(records_yi, self.seed_yi)
        all_ids = sorted(r["id"] for r in records_jia + records_yi)
        self.assertEqual(all_ids, sorted(r["id"] for r in self.submitted))

    # -- 名称匹配规则 ------------------------------------------------------

    def test_query_name_is_stripped_before_matching(self) -> None:
        """查询名称两端带空白时命中清理后的同一提交人。"""
        _, records = self.assert_query("  演示甲\t\n ")
        self.assert_records_match(records, self.seed_jia)

    def test_prefix_substring_and_unknown_names_return_empty(self) -> None:
        """前缀、子串及未提交过费用的名称均返回空数组。"""
        for name in ["演示", "示甲", "演", "演示甲某", "从未提交·虚构丙"]:
            with self.subTest(submitter=name):
                _, records = self.assert_query(name)
                self.assertEqual(records, [], msg=f"查询 {name!r} 应返回空数组")

    def test_inner_space_and_case_must_match_exactly(self) -> None:
        """内部空格与大小写保持精确匹配：Demo A 命中，DemoA / demo a 不命中。"""
        demo = self._submit_ok("Demo A", "演示杂项", "1")
        _, records = self.assert_query("Demo A")
        self.assert_records_match(records, [demo])

        for name in ["DemoA", "demo a"]:
            with self.subTest(submitter=name):
                _, records = self.assert_query(name)
                self.assertEqual(records, [], msg=f"查询 {name!r} 应返回空数组")

    def test_fresh_database_returns_empty_list(self) -> None:
        """对父目录存在的全新数据库首次查询，退出码 0 并返回空数组。"""
        fresh_db = Path(self._tmpdir.name) / "fresh.sqlite"
        self.assertFalse(fresh_db.exists())
        _, records = self.assert_query("演示甲", db_path=fresh_db)
        self.assertEqual(records, [], msg="全新数据库首次查询应返回空数组")

    # -- 失败场景 ----------------------------------------------------------

    def test_invalid_list_queries_are_rejected(self) -> None:
        """空白提交人或缺少 --submitter 退出码为 2，且不影响已有记录。"""
        cases = {
            "提交人仅含空白": ["list", "--submitter", "  \t "],
            "缺少 --submitter": ["list"],
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

        # 失败查询之后，已有两人的正常查询结果保持不变。
        _, records_jia = self.assert_query("演示甲")
        self.assert_records_match(records_jia, self.seed_jia)
        _, records_yi = self.assert_query("演示乙")
        self.assert_records_match(records_yi, self.seed_yi)


if __name__ == "__main__":
    unittest.main()
