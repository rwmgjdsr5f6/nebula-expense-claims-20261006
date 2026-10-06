"""金额前导零与超长数字输入的回归测试。

从项目根目录执行：

    python -m unittest test_expense_amount_boundaries -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / list 公开命令，
验证金额文本的长度（数千个前导零或数千位数字）不改变既有换算与拒绝规则：
前导零不参与数值与上限判断，超长输入走统一的参数校验错误（退出码 2），
不触发 Python 默认整数转换长度限制导致的异常堆栈。每次运行使用独立的
临时 SQLite 文件与虚构人员；仅使用 Python 标准库，不访问公网，也不调整
``sys.set_int_max_str_digits`` 的默认限制。
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
SUBMITTER = "边界测试·虚构甲"
PURPOSE = "边界测试·虚构用途"

# 固定前导零前缀：5000 个 ASCII 字符 0。
ZERO_PREFIX = "0" * 5000

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


class AmountBoundaryTest(unittest.TestCase):
    """前导零与超长数字：成功换算、统一拒绝且不影响已有记录。"""

    def setUp(self) -> None:
        # 每次测试使用独立临时目录（父目录存在）与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_boundary_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "boundary.sqlite"

    # -- 辅助 ------------------------------------------------------------

    def assert_submit_ok(self, amount: str) -> dict:
        """提交应成功：退出码 0，标准错误为空，标准输出为一行 JSON 对象。"""
        result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
        self.assertEqual(
            result.returncode, 0,
            msg=f"submit 金额长度 {len(amount)} 应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="成功提交的标准错误应为空")
        # 标准输出为单行 JSON（仅末尾一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        self.assertIsInstance(record["id"], int)
        self.assertGreater(record["id"], 0, msg="id 应为正整数")
        self.assertEqual(record["submitter"], SUBMITTER)
        self.assertEqual(record["purpose"], PURPOSE)
        self.assertEqual(record["status"], "pending")
        self.assertIsInstance(record["amount_minor"], int)
        return record

    def assert_list(self) -> list:
        """在新进程中查询：退出码 0，标准输出为按 id 升序的 JSON 数组。"""
        result = list_records(self.db_path, SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids), msg="查询结果应按 id 升序排列")
        return records

    def assert_submit_rejected(self, amount: str, reason: str) -> None:
        """无效金额：退出码 2，标准输出为空，标准错误含指定原因且无异常堆栈。"""
        label = f"金额长度 {len(amount)}，期望原因 {reason!r}"
        result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
        self.assertEqual(
            result.returncode, 2,
            msg=f"{label} 应退出码 2: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "", msg=f"{label} 时标准输出应为空")
        self.assertIn(
            reason, result.stderr,
            msg=f"{label} 时标准错误应包含 {reason!r}: {result.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", result.stderr,
            msg=f"{label} 时不应出现异常堆栈: {result.stderr!r}",
        )

    # -- 成功场景 ----------------------------------------------------------

    def test_leading_zeros_amounts_roundtrip_across_processes(self) -> None:
        """5000 个前导零不改变换算结果，入库后由新进程读取仍一致。"""
        cases = [
            (ZERO_PREFIX + "12.30", 1230),
            (ZERO_PREFIX + "0.01", 1),
            (ZERO_PREFIX + MAX_YUAN, MAX_MINOR),
        ]
        submitted = []
        for amount, expected_minor in cases:
            with self.subTest(expected_minor=expected_minor):
                record = self.assert_submit_ok(amount)
                self.assertEqual(record["amount_minor"], expected_minor)
                submitted.append(record)

        # 提交进程已全部退出；在新进程中读取同一数据库。
        records = self.assert_list()
        self.assertEqual(len(records), len(cases))
        for record, (_, expected_minor), original in zip(records, cases, submitted):
            # 金额仍只用整数分表示，其余字段与提交输出一致。
            self.assertIsInstance(record["amount_minor"], int)
            self.assertEqual(record["amount_minor"], expected_minor)
            self.assertEqual(record, original)

    def test_whitespace_around_zero_padded_amounts(self) -> None:
        """前两个样例的金额首尾添加空白，换算结果不变。"""
        cases = [
            (f"  {ZERO_PREFIX}12.30\t", 1230),
            (f"\n{ZERO_PREFIX}0.01  ", 1),
        ]
        for amount, expected_minor in cases:
            with self.subTest(expected_minor=expected_minor):
                record = self.assert_submit_ok(amount)
                self.assertEqual(record["amount_minor"], expected_minor)

        records = self.assert_list()
        self.assertEqual(
            [record["amount_minor"] for record in records],
            [expected_minor for _, expected_minor in cases],
        )

    def test_duplicate_zero_padded_amounts_create_separate_records(self) -> None:
        """相同金额重复提交仍生成不同 id 的独立记录。"""
        amount = ZERO_PREFIX + "12.30"
        first = self.assert_submit_ok(amount)
        second = self.assert_submit_ok(amount)
        self.assertNotEqual(first["id"], second["id"])

        records = self.assert_list()
        self.assertEqual(len(records), 2)
        self.assertEqual(records, [first, second])
        self.assertEqual([r["amount_minor"] for r in records], [1230, 1230])

    # -- 失败场景 ----------------------------------------------------------

    def test_long_invalid_amounts_rejected_and_records_unchanged(self) -> None:
        """超长非法金额统一拒绝，每次失败前后已有记录数量与内容均不变。"""
        ok_first = self.assert_submit_ok(ZERO_PREFIX + "12.30")
        ok_second = self.assert_submit_ok(ZERO_PREFIX + "0.01")
        baseline = self.assert_list()
        self.assertEqual(baseline, [ok_first, ok_second])

        invalid_cases = [
            ("5000 个 0 的整数", ZERO_PREFIX, "金额必须大于零"),
            ("5000 个 0 加 .00", ZERO_PREFIX + ".00", "金额必须大于零"),
            ("5000 个 9 的整数", "9" * 5000, "超过"),
            ("零前缀接上界加一分", ZERO_PREFIX + OVER_MAX_YUAN, "超过"),
            ("零前缀接三位小数", ZERO_PREFIX + "12.300", "金额格式无效"),
        ]
        for label, amount, reason in invalid_cases:
            with self.subTest(case=label):
                # 失败前已有记录与基线一致。
                self.assertEqual(self.assert_list(), baseline)
                self.assert_submit_rejected(amount, reason)
                # 失败后在新进程中复查：数量与内容均不变。
                self.assertEqual(self.assert_list(), baseline)

    def test_over_limit_rejections_mention_the_limit(self) -> None:
        """超过整数分上限的拒绝应说明上限，且不影响已有记录。"""
        ok = self.assert_submit_ok(ZERO_PREFIX + "12.30")
        baseline = self.assert_list()
        self.assertEqual(baseline, [ok])

        for label, amount in [
            ("5000 个 9 的整数", "9" * 5000),
            ("零前缀接上界加一分", ZERO_PREFIX + OVER_MAX_YUAN),
        ]:
            with self.subTest(case=label):
                result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("超过", result.stderr)
                self.assertIn("上限", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.assert_list(), baseline)


if __name__ == "__main__":
    unittest.main()
