"""金额前导零与长数字输入的回归测试。

从项目根目录执行：

    python -m unittest test_expense_amount_boundaries -v

全部断言通过时退出码为 0，否则为非 0。

覆盖金额文本远长于常规输入时的公开行为：5000 个前导零不改变既有换算
规则（成功样例的分值与短写法一致），超长纯零仍按“金额必须大于零”拒绝，
超长有效数字仍按整数分上限拒绝，超长输入附带三位小数仍按“金额格式无效”
拒绝。测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / list
公开命令，提交进程退出后由新进程查询验证。每次运行使用父目录存在的独立
临时 SQLite 文件与虚构人员，结束后自动清理；仅使用 Python 标准库，不访问
公网，也不调整 Python 默认的整数转换长度限制。
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
SUBMITTER = "边界测试·虚构丁"
PURPOSE = "边界测试·虚构用途"

# 固定长前缀：5000 个 ASCII 字符 0。
ZERO_PREFIX = "0" * 5000

# SQLite 64 位有符号整数上限对应的元与分。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
OVER_MAX_YUAN = "92233720368547758.08"

# 长输入成功样例：(金额文本, 期望整数分)。
LONG_OK_CASES = [
    (ZERO_PREFIX + "12.30", 1230),
    (ZERO_PREFIX + "0.01", 1),
    (ZERO_PREFIX + MAX_YUAN, MAX_MINOR),
]


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


class LongAmountSubmitTest(unittest.TestCase):
    """长金额文本提交成功：前导零不改变换算结果，进程退出后记录可完整读回。"""

    def setUp(self) -> None:
        # 每次测试使用父目录存在的独立临时 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_boundary_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "boundary.sqlite"

    # -- 辅助 ------------------------------------------------------------

    def assert_submit_ok(self, amount: str, expected_minor: int) -> dict:
        """提交应成功：退出码 0，标准错误为空，标准输出为一行 JSON 对象。"""
        result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
        self.assertEqual(
            result.returncode, 0,
            msg=f"submit 应成功: returncode={result.returncode} stderr={result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="成功提交的标准错误应为空")
        # 标准输出为单行 JSON（仅末尾一个换行符）。
        self.assertTrue(result.stdout.endswith("\n"), msg="标准输出应以换行结尾")
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应为一行 JSON")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        self.assertEqual(record["amount_minor"], expected_minor)
        self.assertIsInstance(record["amount_minor"], int)
        self.assertEqual(record["submitter"], SUBMITTER)
        self.assertEqual(record["purpose"], PURPOSE)
        self.assertEqual(record["status"], "pending")
        self.assertIsInstance(record["id"], int)
        self.assertGreater(record["id"], 0, msg="id 应为正整数")
        return record

    def assert_list(self) -> list:
        """在新进程中查询：退出码 0，标准输出为 JSON 数组。"""
        result = list_records(self.db_path, SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 成功场景 ----------------------------------------------------------

    def test_long_zero_prefix_amounts_roundtrip(self) -> None:
        """5000 个前导零加常规金额，分值与短写法一致，新进程读回完全一致。"""
        submitted = []
        for amount, expected_minor in LONG_OK_CASES:
            label = f"...{amount[-20:]} (长度 {len(amount)})"
            with self.subTest(amount=label):
                submitted.append(self.assert_submit_ok(amount, expected_minor))

        # 提交进程已全部退出；在新进程中读取同一数据库。
        records = self.assert_list()
        self.assertEqual(len(records), len(LONG_OK_CASES))
        # 记录按 id 升序，且与提交输出逐字段一致。
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids), msg="查询结果应按 id 升序排列")
        for record, (_, expected_minor), original in zip(
            records, LONG_OK_CASES, submitted
        ):
            self.assertEqual(record, original)
            self.assertEqual(record["amount_minor"], expected_minor)
            self.assertIsInstance(record["amount_minor"], int)
            self.assertEqual(record["submitter"], SUBMITTER)
            self.assertEqual(record["purpose"], PURPOSE)
            self.assertEqual(record["status"], "pending")

    def test_long_zero_prefix_with_surrounding_whitespace(self) -> None:
        """长零前缀金额首尾添加空白，换算结果不变。"""
        cases = [
            (f"  {ZERO_PREFIX}12.30\t", 1230),
            (f"\n{ZERO_PREFIX}0.01  ", 1),
        ]
        for amount, expected_minor in cases:
            with self.subTest(amount=f"...{amount[-20:]!r}"):
                self.assert_submit_ok(amount, expected_minor)

        records = self.assert_list()
        self.assertEqual(
            [record["amount_minor"] for record in records], [1230, 1]
        )

    def test_same_long_amount_twice_creates_separate_records(self) -> None:
        """相同长金额重复提交，生成不同 id 的独立记录。"""
        amount = ZERO_PREFIX + "12.30"
        first = self.assert_submit_ok(amount, 1230)
        second = self.assert_submit_ok(amount, 1230)
        self.assertNotEqual(first["id"], second["id"])

        records = self.assert_list()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["id"] for record in records], [first["id"], second["id"]]
        )
        self.assertEqual([record["amount_minor"] for record in records], [1230, 1230])
        self.assertTrue(all(record["status"] == "pending" for record in records))


class LongAmountRejectTest(unittest.TestCase):
    """长金额文本被拒绝：退出码 2、标准输出为空、无异常堆栈，已有记录不变。"""

    def setUp(self) -> None:
        # 每次测试使用父目录存在的独立临时 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_boundary_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "boundary_reject.sqlite"
        # 先建立两条成功记录作为基线，验证失败提交不改变已有数据。
        self.baseline = [self._submit_ok(amount) for amount in ("12.30", "0.01")]

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, amount: str) -> dict:
        result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
        self.assertEqual(result.returncode, 0, msg=f"基线 submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_list(self) -> list:
        """在新进程中查询：退出码 0，标准输出为 JSON 数组。"""
        result = list_records(self.db_path, SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    def assert_submit_rejected(self, amount: str, reason: str) -> None:
        """无效提交：退出码 2，标准输出为空，标准错误含指定说明且无堆栈。"""
        result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
        self.assertEqual(
            result.returncode, 2,
            msg=f"应拒绝却返回 {result.returncode}: stdout={result.stdout}",
        )
        self.assertEqual(result.stdout, "", msg="失败提交的标准输出应为空")
        self.assertIn(
            reason, result.stderr,
            msg=f"标准错误应包含 {reason!r}: {result.stderr}",
        )
        self.assertNotIn(
            "Traceback", result.stderr,
            msg=f"标准错误不应包含异常堆栈: {result.stderr}",
        )

    # -- 失败场景 ----------------------------------------------------------

    def test_long_amounts_rejected_and_records_unchanged(self) -> None:
        """超长零、超上限与三位小数均被拒绝，失败前后已有记录数量与内容不变。"""
        before = self.assert_list()
        self.assertEqual([r["id"] for r in before], [r["id"] for r in self.baseline])

        invalid_cases = [
            ("5000 个 0 组成的整数", ZERO_PREFIX, "金额必须大于零"),
            ("5000 个 0 加 .00", ZERO_PREFIX + ".00", "金额必须大于零"),
            ("5000 个 9 组成的整数", "9" * 5000, "上限"),
            ("零前缀接超上限金额", ZERO_PREFIX + OVER_MAX_YUAN, "上限"),
            ("零前缀接三位小数", ZERO_PREFIX + "12.300", "金额格式无效"),
        ]
        for label, amount, reason in invalid_cases:
            with self.subTest(case=label):
                self.assert_submit_rejected(amount, reason)
                # 每次失败后在新的进程中复查：数量与内容均保持不变。
                self.assertEqual(self.assert_list(), before)

    def test_over_limit_message_mentions_minor_cap(self) -> None:
        """超上限拒绝说明同时包含“超过”与“上限”，指向整数分上限。"""
        for label, amount in [
            ("5000 个 9", "9" * 5000),
            ("零前缀接超上限金额", ZERO_PREFIX + OVER_MAX_YUAN),
        ]:
            with self.subTest(case=label):
                result = submit(self.db_path, SUBMITTER, PURPOSE, amount)
                self.assertEqual(result.returncode, 2)
                self.assertIn("超过", result.stderr)
                self.assertIn("上限", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_default_int_conversion_limit_is_kept(self) -> None:
        """测试进程未调整 Python 默认整数转换长度限制（4300 位）。"""
        get_limit = getattr(sys, "get_int_max_str_digits", None)
        if get_limit is None:
            self.skipTest("当前 Python 版本无整数转换长度限制")
        self.assertEqual(get_limit(), 4300)


if __name__ == "__main__":
    unittest.main()
