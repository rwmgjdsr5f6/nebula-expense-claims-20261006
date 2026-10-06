"""按提交人导出 CSV 流程的回归测试。

从项目根目录执行：

    python -m unittest test_expense_export -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中提交虚构费用，待提交进程退出后，再由另一个新进程对同一 SQLite
数据库执行 ``export --submitter``，把 CSV 写到标准输出并校验其内容。每个
用例使用独立的临时 SQLite 文件，结束后自动清理，重复执行不依赖任何已有
数据；仅使用 Python 标准库，不访问公网。
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 导出 CSV 的固定表头，与命令行实现保持一致。
CSV_HEADER = ["id", "submitter", "purpose", "amount_minor", "status"]

# 固定样例：演示甲(交通费 12.30)、演示乙(办公费 5)、演示甲(餐费 0.01)。
# 金额以整数分存储，故分别为 1230、500、1 分。
SEED_EXPENSES = [
    ("演示甲", "交通费", "12.30"),
    ("演示乙", "办公费", "5"),
    ("演示甲", "餐费", "0.01"),
]

# 特殊用途用例的虚构提交人。
SPECIAL_SUBMITTER = "导出测试·虚构特殊用途用户"
# 用途同时包含中文、ASCII 逗号、双引号与内嵌换行；金额为 1.20 元（120 分）。
SPECIAL_PURPOSE = '特殊用途：中文，混有,ASCII逗号与"双引号"\n还有内嵌换行第二行'
SPECIAL_AMOUNT = "1.20"
SPECIAL_AMOUNT_MINOR = 120


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（不带 --db，用于缺少 --db 的失败用例）。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", *args],
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


def export(db_path: Path, submitter: str):
    return run_cli(db_path, "export", "--submitter", submitter)


def export_bytes(db_path: Path, submitter: str) -> subprocess.CompletedProcess[bytes]:
    """在新进程中导出并以原始字节捕获（不做通用换行转换），用于校验 CRLF。"""
    return subprocess.run(
        [
            sys.executable, "-m", "expense_desk",
            "--db", str(db_path), "export", "--submitter", submitter,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
    )


def parse_csv(stdout: str) -> list[list[str]]:
    """按标准 CSV 规则解析导出输出，返回含表头在内的各行。"""
    return list(csv.reader(io.StringIO(stdout)))


def rows_to_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    """把表头之后的数据行转换为以表头为键的字典列表。"""
    return [dict(zip(rows[0], row)) for row in rows[1:]]


class ExportBySubmitterTest(unittest.TestCase):
    """按提交人导出：固定样例 CSV 内容、名称匹配、特殊字符与重复导出稳定性。

    每个用例先按演示甲、演示乙、演示甲的顺序提交三笔固定样例费用，
    提交进程全部退出后，再由新进程对同一数据库执行 export 导出。
    """

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_export_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "export_regression.sqlite"
        self.submitted = [self._submit_ok(*seed) for seed in SEED_EXPENSES]
        self.seed_jia = [self.submitted[0], self.submitted[2]]  # 演示甲的两笔
        self.seed_yi = [self.submitted[1]]                      # 演示乙的一笔

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_export(self, submitter: str, db_path: Path | None = None):
        """在新进程中导出：退出码 0、标准错误为空，返回 (进程结果, CSV 各行)。"""
        result = export(db_path or self.db_path, submitter)
        self.assertEqual(
            result.returncode, 0,
            msg=f"export --submitter {submitter!r} 退出码应为 0: {result.stderr}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"export --submitter {submitter!r} 的标准错误应为空",
        )
        rows = parse_csv(result.stdout)
        return result, rows

    def assert_list(self, submitter: str) -> list:
        """在新进程中查询：退出码 0、标准错误为空，返回解析后的记录列表。"""
        result = list_records(self.db_path, submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 固定样例 ----------------------------------------------------------

    def test_export_fixed_sample_csv(self) -> None:
        """导出演示甲：仅其两笔，按 id 升序，金额 1230/1，状态 pending。"""
        result, rows = self.assert_export("演示甲")

        # 原始字节以固定表头行起始，行结束符为 CRLF。文本模式的子进程捕获
        # 会做通用换行转换（\r\n -> \n），故原始字节需另行二进制校验。
        raw = export_bytes(self.db_path, "演示甲")
        self.assertEqual(raw.returncode, 0, msg=f"export 失败: {raw.stderr!r}")
        self.assertTrue(
            raw.stdout.startswith((",".join(CSV_HEADER) + "\r\n").encode("utf-8")),
            msg=f"CSV 原始字节应以固定表头加 CRLF 起始: {raw.stdout!r}",
        )
        # 解码后的文本以表头行起始。
        self.assertTrue(
            result.stdout.startswith(",".join(CSV_HEADER) + "\n"),
            msg=f"CSV 应以固定表头行起始: {result.stdout!r}",
        )
        self.assertEqual(rows[0], CSV_HEADER, msg="表头应与约定字段一致")

        data = rows_to_dicts(rows)
        self.assertEqual(len(data), 2, msg="CSV 数据记录应只包含演示甲的两笔")

        expected_ids = [record["id"] for record in self.seed_jia]
        actual_ids = [int(row["id"]) for row in data]
        self.assertEqual(actual_ids, expected_ids, msg="应与提交返回的 id 一致")
        self.assertEqual(actual_ids, sorted(actual_ids), msg="应按 id 升序排列")
        self.assertEqual(
            [row["amount_minor"] for row in data], ["1230", "1"],
            msg="金额（分）应为 1230 与 1",
        )
        self.assertTrue(
            all(row["submitter"] == "演示甲" for row in data),
            msg="CSV 中不应混入其他提交人的记录",
        )
        self.assertTrue(
            all(row["status"] == "pending" for row in data),
            msg="两笔记录状态均应为 pending",
        )

        # 演示乙只导出其一笔办公费 500 分。
        _, rows_yi = self.assert_export("演示乙")
        data_yi = rows_to_dicts(rows_yi)
        self.assertEqual(len(data_yi), 1)
        self.assertEqual(data_yi[0]["id"], str(self.seed_yi[0]["id"]))
        self.assertEqual(data_yi[0]["submitter"], "演示乙")
        self.assertEqual(data_yi[0]["purpose"], "办公费")
        self.assertEqual(data_yi[0]["amount_minor"], "500")
        self.assertEqual(data_yi[0]["status"], "pending")

    def test_export_fields_match_list(self) -> None:
        """CSV 各字段内容与同一提交人的 list 结果逐字段一致。"""
        listed = self.assert_list("演示甲")
        _, rows = self.assert_export("演示甲")
        data = rows_to_dicts(rows)
        self.assertEqual(len(data), len(listed))
        for csv_row, record in zip(data, listed):
            self.assertEqual(csv_row["id"], str(record["id"]))
            self.assertEqual(csv_row["submitter"], record["submitter"])
            self.assertEqual(csv_row["purpose"], record["purpose"])
            self.assertEqual(csv_row["amount_minor"], str(record["amount_minor"]))
            self.assertEqual(csv_row["status"], record["status"])

    # -- 名称匹配规则 ------------------------------------------------------

    def test_query_name_is_stripped_before_matching(self) -> None:
        """提交人带首尾空白时命中清理后的演示甲，记录完全相同。"""
        _, rows_plain = self.assert_export("演示甲")
        _, rows_padded = self.assert_export("  演示甲\t\n ")
        self.assertEqual(rows_padded, rows_plain)
        data = rows_to_dicts(rows_padded)
        self.assertEqual(
            [int(row["id"]) for row in data],
            [record["id"] for record in self.seed_jia],
        )
        self.assertTrue(all(row["submitter"] == "演示甲" for row in data))

    def test_prefix_unknown_and_fresh_db_return_header_only(self) -> None:
        """前缀名称、未提交过的名称、父目录存在的全新数据库都只返回表头。"""
        fresh_db = Path(self._tmpdir.name) / "fresh_export.sqlite"
        self.assertFalse(fresh_db.exists())

        cases = {
            "前缀名称": ("演示", self.db_path),
            "未提交过的名称": ("从未提交·虚构丙", self.db_path),
            "全新数据库": ("演示甲", fresh_db),
        }
        for label, (submitter, db_path) in cases.items():
            with self.subTest(case=label):
                result, rows = self.assert_export(submitter, db_path=db_path)
                self.assertEqual(
                    rows, [CSV_HEADER],
                    msg=f"{label} 应只有表头一行，没有费用数据行",
                )
                # 解码后的文本中表头之后不得再有任何字符（连空白行也没有）；
                # 文本模式捕获已把 CRLF 转换为 \n。
                self.assertEqual(
                    result.stdout,
                    ",".join(CSV_HEADER) + "\n",
                    msg=f"{label} 的解码输出应仅为表头一行",
                )
                # 原始字节层面行结束符仍为 CRLF，且表头之后没有任何字节。
                raw = export_bytes(db_path, submitter)
                self.assertEqual(raw.returncode, 0)
                self.assertEqual(
                    raw.stdout,
                    (",".join(CSV_HEADER) + "\r\n").encode("utf-8"),
                    msg=f"{label} 的原始字节应仅为表头加 CRLF",
                )

    # -- 特殊用途 ----------------------------------------------------------

    def test_special_purpose_roundtrips_through_csv(self) -> None:
        """含中文、逗号、双引号与内嵌换行的用途经 CSV 解析后完整还原。"""
        record = self._submit_ok(
            SPECIAL_SUBMITTER, SPECIAL_PURPOSE, SPECIAL_AMOUNT
        )
        self.assertEqual(record["amount_minor"], SPECIAL_AMOUNT_MINOR)

        result, rows = self.assert_export(SPECIAL_SUBMITTER)
        # 内嵌换行位于引号字段内，不得拆成额外记录：表头 + 一笔数据。
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(len(rows), 2, msg=f"内嵌换行不应拆出额外记录: {rows!r}")
        data = rows_to_dicts(rows)
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["id"], str(record["id"]))
        self.assertEqual(row["submitter"], SPECIAL_SUBMITTER)
        # 用途完整还原，逗号、双引号与内嵌换行一个字符都不能丢失。
        self.assertEqual(row["purpose"], SPECIAL_PURPOSE)
        self.assertIn("\n", row["purpose"], msg="用途中的内嵌换行应保留")
        self.assertIn('"', row["purpose"], msg="用途中的双引号应保留")
        self.assertEqual(row["amount_minor"], str(SPECIAL_AMOUNT_MINOR))
        self.assertEqual(row["status"], "pending")

        # 与同一提交人的 list 结果交叉核对。
        listed = self.assert_list(SPECIAL_SUBMITTER)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["purpose"], SPECIAL_PURPOSE)
        self.assertEqual(listed[0]["amount_minor"], SPECIAL_AMOUNT_MINOR)

        # 原始输出中该字段必须被双引号包裹（含逗号/换行的最小引用规则）。
        self.assertIn(
            '"' + SPECIAL_PURPOSE.replace('"', '""') + '"',
            result.stdout,
            msg="含逗号、双引号与换行的字段应按 CSV 规则加引号转义",
        )

    # -- 稳定性 ------------------------------------------------------------

    def test_repeated_exports_stable_and_list_unchanged(self) -> None:
        """重复导出内容一致，导出前后的 list 结果保持不变。"""
        before_jia = self.assert_list("演示甲")
        before_yi = self.assert_list("演示乙")

        first_jia, _ = self.assert_export("演示甲")
        second_jia, _ = self.assert_export("演示甲")
        first_yi, _ = self.assert_export("演示乙")
        second_yi, _ = self.assert_export("演示乙")
        self.assertEqual(second_jia.stdout, first_jia.stdout, msg="重复导出演示甲应一致")
        self.assertEqual(second_yi.stdout, first_yi.stdout, msg="重复导出演示乙应一致")

        # 导出前后记录数量与内容完全一致，导出只读不写。
        after_jia = self.assert_list("演示甲")
        after_yi = self.assert_list("演示乙")
        self.assertEqual(after_jia, before_jia)
        self.assertEqual(after_yi, before_yi)
        self.assertEqual(
            [r["id"] for r in after_jia],
            [r["id"] for r in self.seed_jia],
        )
        self.assertTrue(all(r["status"] == "pending" for r in after_jia + after_yi))


class ExportFailureTest(unittest.TestCase):
    """导出命令的参数错误与数据库错误：退出码、空标准输出与标准错误说明。

    所有失败用例结束后都复查固定样例记录的数量与内容保持不变。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_export_fail_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "failure.sqlite"
        # 预置固定样例，便于复查失败不影响已有数据。
        for submitter, purpose, amount in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    # -- 辅助 ------------------------------------------------------------

    def assert_records_unchanged(self) -> None:
        """失败之后已有记录的数量和内容与预置样例一致。"""
        for submitter, expected in [("演示甲", None), ("演示乙", None)]:
            result = list_records(self.db_path, submitter)
            self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
            records = json.loads(result.stdout)
            if submitter == "演示甲":
                self.assertEqual(len(records), 2)
                self.assertEqual(
                    [(r["purpose"], r["amount_minor"], r["status"]) for r in records],
                    [("交通费", 1230, "pending"), ("餐费", 1, "pending")],
                )
                self.assertEqual(
                    [r["id"] for r in records],
                    sorted(r["id"] for r in records),
                )
            else:
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    (records[0]["submitter"], records[0]["purpose"],
                     records[0]["amount_minor"], records[0]["status"]),
                    ("演示乙", "办公费", 500, "pending"),
                )

    # -- 参数错误（退出码 2） ----------------------------------------------

    def test_missing_db_exits_2(self) -> None:
        """缺少 --db：退出码 2，标准输出为空，标准错误给出参数原因。"""
        result = run_module("export", "--submitter", "演示甲")
        self.assertEqual(
            result.returncode, 2,
            msg=f"缺少 --db 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="参数错误时标准输出应为空")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空参数原因")
        self.assertIn("--db", result.stderr)
        self.assert_records_unchanged()

    def test_missing_submitter_exits_2(self) -> None:
        """缺少 --submitter：退出码 2，标准输出为空，标准错误给出参数原因。"""
        result = run_cli(self.db_path, "export")
        self.assertEqual(
            result.returncode, 2,
            msg=f"缺少 --submitter 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="参数错误时标准输出应为空")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空参数原因")
        self.assertIn("--submitter", result.stderr)
        self.assertNotIn(",".join(CSV_HEADER), result.stdout, msg="失败时连表头也不能出现")
        self.assert_records_unchanged()

    def test_blank_submitter_exits_2(self) -> None:
        """提交人只有空白：退出码 2，标准输出为空，标准错误给出参数原因。"""
        result = run_cli(self.db_path, "export", "--submitter", "  \t\n ")
        self.assertEqual(
            result.returncode, 2,
            msg=f"空白提交人应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="参数错误时标准输出应为空")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空参数原因")
        self.assertIn("参数错误", result.stderr)
        self.assertNotIn(",".join(CSV_HEADER), result.stdout, msg="失败时连表头也不能出现")
        self.assert_records_unchanged()

    # -- 数据库错误（退出码 1） --------------------------------------------

    def test_missing_database_parent_exits_1(self) -> None:
        """数据库父目录不存在：退出码 1，标准输出为空，标准错误说明数据库原因。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        self.assertFalse(missing_db.parent.exists())
        result = export(missing_db, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"数据库父目录缺失应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
        self.assertNotIn(",".join(CSV_HEADER), result.stdout, msg="连表头也不能出现")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库操作失败", result.stderr)
        # 失败后不应遗留数据库文件或目录。
        self.assertFalse(missing_db.exists())
        self.assert_records_unchanged()

    def test_database_path_is_directory_exits_1(self) -> None:
        """数据库路径指向现有目录：退出码 1，标准输出为空，标准错误说明原因。"""
        directory_db = Path(self._tmpdir.name)
        self.assertTrue(directory_db.is_dir())
        result = export(directory_db, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"路径指向目录应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
        self.assertNotIn(",".join(CSV_HEADER), result.stdout, msg="连表头也不能出现")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库操作失败", result.stderr)
        self.assert_records_unchanged()


if __name__ == "__main__":
    unittest.main()
