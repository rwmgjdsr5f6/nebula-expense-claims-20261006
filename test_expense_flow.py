"""金额入库后再次读取的端到端回归测试。

从项目根目录执行：

    python -m unittest test_expense_flow -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程调用公开命令 ``python -m expense_desk``，每次运行使用
独立临时目录中的 SQLite 文件与虚构人员，重复执行不依赖已有数据；
结束后自动清理临时文件，不访问公网，不引入第三方包。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录，
# 保证 ``python -m expense_desk`` 能解析到被测包。
PROJECT_ROOT = Path(__file__).resolve().parent

# 虚构人员与用途，不对应任何真实数据。
SUBMITTER = "林小满"
PURPOSE = "测试交通费"

# (命令行金额, 期望的整数分)
SUCCESS_CASES = [
    ("12", 1200),
    ("12.3", 1230),
    ("12.30", 1230),
    ("0.01", 1),
    ("92233720368547758.07", 9223372036854775807),
]

# 无效金额：零、负数、超过两位小数、科学计数法、纯空白、超出 64 位上限。
INVALID_AMOUNTS = ["0", "-1", "12.300", "1e2", "   ", "92233720368547758.08"]


class ExpenseFlowTest(unittest.TestCase):
    """先建立成功记录，再逐项尝试无效输入并复查结果。"""

    def setUp(self) -> None:
        # 每次运行使用独立临时目录与全新的 SQLite 文件。
        self._tmp = tempfile.TemporaryDirectory(prefix="expense_desk_test_")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "regression.sqlite")

    # -- 子进程辅助 ---------------------------------------------------------

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """在新进程中执行公开命令并捕获输出。"""
        return subprocess.run(
            [sys.executable, "-m", "expense_desk", "--db", self.db_path, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

    def submit(self, submitter: str, purpose: str, amount: str) -> dict:
        """提交一条报销单并断言成功，返回解析后的 JSON 记录。"""
        result = self.run_cli(
            "submit",
            "--submitter", submitter,
            "--purpose", purpose,
            "--amount", amount,
        )
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        return record

    def list_records(self, submitter: str) -> list:
        """在新进程中按提交人查询，返回解析后的 JSON 数组。"""
        result = self.run_cli("list", "--submitter", submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 断言辅助 -----------------------------------------------------------

    def assert_records_match(self, records: list, expected: list) -> None:
        """按 JSON 值逐条比对（不依赖键顺序），并校验字段类型与排序。"""
        self.assertEqual(len(records), len(expected))
        previous_id = 0
        for record, want in zip(records, expected):
            self.assertIsInstance(record["id"], int)
            self.assertIsInstance(record["amount_minor"], int)
            self.assertNotIsInstance(record["amount_minor"], bool)
            self.assertEqual(record["status"], "pending")
            self.assertGreater(record["id"], previous_id)  # id 升序
            previous_id = record["id"]
            # 整体按值比较：id 与提交时相同，提交人/用途为去空白后的输入。
            self.assertEqual(record, want)

    # -- 主流程 -------------------------------------------------------------

    def test_submit_then_list_roundtrip(self) -> None:
        # 提交人与用途故意带首尾空白，验证入库与查询均为去空白后的值。
        padded_submitter = f"  {SUBMITTER}  "
        padded_purpose = f"\t{PURPOSE}  "

        # 1) 成功场景：通过公开命令提交全部有效金额。
        expected = []
        for raw_amount, expected_minor in SUCCESS_CASES:
            record = self.submit(padded_submitter, padded_purpose, raw_amount)
            self.assertEqual(
                record,
                {
                    "id": record["id"],
                    "submitter": SUBMITTER,
                    "purpose": PURPOSE,
                    "amount_minor": expected_minor,
                    "status": "pending",
                },
            )
            expected.append(record)

        # 同一提交人与用途提交 12.3 与 12.30，应保留两条独立记录。
        same_value_records = [r for r in expected if r["amount_minor"] == 1230]
        self.assertEqual(len(same_value_records), 2)
        self.assertNotEqual(same_value_records[0]["id"], same_value_records[1]["id"])

        # 2) 在另一个进程中对同一数据库执行 list，验证关闭进程后仍可读取。
        self.assert_records_match(self.list_records(SUBMITTER), expected)

        # 3) 失败场景：无效金额、空白提交人、空白用途。
        invalid_submissions = [
            (SUBMITTER, PURPOSE, amount) for amount in INVALID_AMOUNTS
        ]
        invalid_submissions.append(("   ", PURPOSE, "12.30"))  # 空白提交人
        invalid_submissions.append((SUBMITTER, "  \t ", "12.30"))  # 空白用途

        for submitter, purpose, amount in invalid_submissions:
            with self.subTest(submitter=submitter, purpose=purpose, amount=amount):
                result = self.run_cli(
                    "submit",
                    "--submitter", submitter,
                    "--purpose", purpose,
                    "--amount", amount,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip(), "标准错误应包含输入错误说明")

                # 每次失败后在新进程中复查：已有成功记录内容与数量不变。
                self.assert_records_match(self.list_records(SUBMITTER), expected)


if __name__ == "__main__":
    unittest.main()
