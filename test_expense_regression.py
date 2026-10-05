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


if __name__ == "__main__":
    unittest.main()
