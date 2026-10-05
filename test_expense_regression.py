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


class SubmitterQueryTest(unittest.TestCase):
    """按提交人查询：同一库内不同提交人隔离、完整名称精确匹配。"""

    # 固定正常样例的虚构人员与用途。
    JIA = "演示甲"
    YI = "演示乙"

    def setUp(self) -> None:
        # 每个用例使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_query_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "query.sqlite"

    # -- 辅助 ------------------------------------------------------------

    def submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        """提交应成功：退出码 0，标准输出为一条 JSON 对象，标准错误为空。"""
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        return record

    def list_raw(self, submitter: str) -> subprocess.CompletedProcess[str]:
        """在新进程中查询并返回原始结果，由调用方断言。"""
        return list_records(self.db_path, submitter)

    def assert_list_ok(self, submitter: str) -> tuple[list, str]:
        """查询应成功：退出码 0，标准错误为空，标准输出为一行 JSON 数组。"""
        result = self.list_raw(submitter)
        self.assertEqual(
            result.returncode, 0,
            msg=f"list --submitter {submitter!r} 失败: {result.stderr}",
        )
        self.assertEqual(result.stderr, "")
        # 标准输出必须恰好是一行（单个换行结尾）的 JSON 数组。
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stdout.count("\n"), 1)
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records, result.stdout

    def assert_list_empty(self, submitter: str) -> None:
        """查询应成功且结果为空数组。"""
        records, _ = self.assert_list_ok(submitter)
        self.assertEqual(records, [])

    def submit_fixed_sample(self) -> dict[str, list[dict]]:
        """按 演示甲、演示乙、演示甲 顺序提交三笔固定样例费用。"""
        first = self.submit_ok(self.JIA, "演示交通", "12.30")
        second = self.submit_ok(self.YI, "演示办公", "5")
        third = self.submit_ok(self.JIA, "演示餐费", "0.01")
        return {self.JIA: [first, third], self.YI: [second]}

    def assert_records_match(self, records: list, expected: list[dict]) -> None:
        """查询结果应与提交返回的记录完全一致（含 id、字段与顺序）。"""
        self.assertEqual(
            len(records), len(expected),
            msg=f"记录数不符: 期望 {len(expected)} 条，实际 {records}",
        )
        for index, (record, original) in enumerate(zip(records, expected)):
            with self.subTest(记录序号=index, 期望id=original["id"]):
                # id 与提交时返回的值一致。
                self.assertEqual(record["id"], original["id"])
                self.assertEqual(record["submitter"], original["submitter"])
                self.assertEqual(record["purpose"], original["purpose"])
                self.assertEqual(record["amount_minor"], original["amount_minor"])
                self.assertIsInstance(record["amount_minor"], int)
                self.assertEqual(record["status"], "pending")
        # 整体按 id 升序。
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids))

    # -- 正常样例 ----------------------------------------------------------

    def test_submitters_are_isolated_within_same_database(self) -> None:
        """同一库内两名提交人各自只能查到自己的记录。"""
        expected = self.submit_fixed_sample()

        # 提交进程已全部退出；在新进程中查询同一数据库。
        jia_records, _ = self.assert_list_ok(self.JIA)
        # 演示甲只含第一笔与第三笔，amount_minor 分别为 1230 与 1。
        self.assertEqual(
            [r["amount_minor"] for r in jia_records], [1230, 1],
            msg="演示甲的金额序列不符",
        )
        self.assert_records_match(jia_records, expected[self.JIA])

        yi_records, _ = self.assert_list_ok(self.YI)
        # 演示乙只含第二笔，amount_minor 为 500。
        self.assertEqual(
            [r["amount_minor"] for r in yi_records], [500],
            msg="演示乙的金额序列不符",
        )
        self.assert_records_match(yi_records, expected[self.YI])

    def test_repeated_queries_return_identical_results(self) -> None:
        """重复查询两人结果相同，查询不增删或改写原记录。"""
        expected = self.submit_fixed_sample()

        for submitter in (self.JIA, self.YI):
            with self.subTest(提交人=submitter):
                first_records, first_stdout = self.assert_list_ok(submitter)
                second_records, second_stdout = self.assert_list_ok(submitter)
                # 原始输出逐字节一致。
                self.assertEqual(first_stdout, second_stdout)
                self.assertEqual(first_records, second_records)
                self.assert_records_match(first_records, expected[submitter])

    # -- 名称匹配 ------------------------------------------------------------

    def test_query_strips_surrounding_whitespace(self) -> None:
        """查询名称两端带空白时命中清理后的同一提交人。"""
        expected = self.submit_fixed_sample()

        records, _ = self.assert_list_ok(f"  {self.JIA}\t ")
        self.assert_records_match(records, expected[self.JIA])
        self.assertTrue(all(r["submitter"] == self.JIA for r in records))

    def test_prefix_substring_and_unknown_names_return_empty(self) -> None:
        """前缀、子串与未提交费用的名称均返回空数组。"""
        self.submit_fixed_sample()

        for name in ("演示", "示甲", "演示丙"):
            with self.subTest(查询名称=name):
                self.assert_list_empty(name)

    def test_internal_space_and_case_must_match_exactly(self) -> None:
        """内部空格与大小写保持精确匹配。"""
        record = self.submit_ok("Demo A", "演示记录", "1")

        records, _ = self.assert_list_ok("Demo A")
        self.assert_records_match(records, [record])

        for name in ("DemoA", "demo a"):
            with self.subTest(查询名称=name):
                self.assert_list_empty(name)

    # -- 边界与失败 ----------------------------------------------------------

    def test_first_query_on_fresh_database_returns_empty(self) -> None:
        """父目录存在的全新数据库首次查询返回空数组。"""
        fresh_db = Path(self._tmpdir.name) / "fresh.sqlite"
        self.assertFalse(fresh_db.exists())
        result = list_records(fresh_db, self.JIA)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), [])

    def test_invalid_list_arguments_exit_2_and_keep_records(self) -> None:
        """空白提交人或缺少 --submitter 退出码为 2，已有记录保持不变。"""
        expected = self.submit_fixed_sample()

        invalid_cases = [
            ("submitter 仅空白", ["list", "--submitter", "  \t "]),
            ("缺少 --submitter", ["list"]),
        ]
        for label, argv in invalid_cases:
            with self.subTest(用例=label):
                result = run_cli(self.db_path, *argv)
                self.assertEqual(
                    result.returncode, 2,
                    msg=f"应拒绝却成功: {result.stdout}",
                )
                self.assertEqual(result.stdout, "")
                self.assertTrue(
                    result.stderr.strip(),
                    msg="标准错误应包含非空的输入错误说明",
                )

        # 失败查询之后，已有两人的结果保持不变。
        for submitter in (self.JIA, self.YI):
            with self.subTest(提交人=submitter):
                records, _ = self.assert_list_ok(submitter)
                self.assert_records_match(records, expected[submitter])


if __name__ == "__main__":
    unittest.main()
