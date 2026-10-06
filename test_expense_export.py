"""按提交人导出报销单 CSV 的回归测试。

从项目根目录执行：

    python -m unittest test_expense_export -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用 submit
在独立进程中提交虚构费用，待提交进程退出后，再由另一个新进程对同一 SQLite
数据库执行 ``export --submitter``，验证导出的既有行为。每个用例使用独立的
临时 SQLite 文件，结束后自动清理，重复执行不依赖任何已有数据；仅使用 Python
标准库，不访问公网。
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

# 固定样例：演示甲(交通费 12.30)、演示乙(办公费 5)、演示甲(餐费 0.01)。
# 金额以整数分存储，故分别为 1230、500、1 分。
SEED_EXPENSES = [
    ("演示甲", "交通费", "12.30", 1230),
    ("演示乙", "办公费", "5", 500),
    ("演示甲", "餐费", "0.01", 1),
]

# CSV 导出的固定表头，与查询返回的字段顺序一致。
CSV_HEADER = ["id", "submitter", "purpose", "amount_minor", "status"]

# 特殊用途样例：金额 1.20（120 分），用途包含中文、逗号、双引号与内嵌换行，
# 用于验证 CSV 引用规则能完整还原字段而不拆出额外记录。
SPECIAL_SUBMITTER = "导出测试·虚构特殊用途用户"
SPECIAL_PURPOSE = '团建"茶歇", 含税\n第二行备注'
SPECIAL_AMOUNT = "1.20"
SPECIAL_AMOUNT_MINOR = 120


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def run_cli_no_db(*args: str) -> subprocess.CompletedProcess[str]:
    """不提供 --db 参数，在新进程中执行公开命令。"""
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


def parse_csv(text: str) -> list[list[str]]:
    """把导出的 CSV 文本解析为行列表（首行为表头）。"""
    return list(csv.reader(io.StringIO(text)))


class ExportBySubmitterTest(unittest.TestCase):
    """按提交人导出：固定样例的表头、记录内容、顺序与边界名称。

    每个用例先按演示甲、演示乙、演示甲的顺序提交三笔固定样例费用，
    提交进程全部退出后，再由新进程对同一数据库执行 export 导出。
    """

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_export_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "export_regression.sqlite"
        self.submitted = [self._submit_ok(*seed[:3]) for seed in SEED_EXPENSES]
        self.seed_jia = [self.submitted[0], self.submitted[2]]  # 演示甲的两笔
        self.seed_yi = [self.submitted[1]]                      # 演示乙的一笔

    # -- 辅助 ------------------------------------------------------------

    def _submit_ok(self, submitter: str, purpose: str, amount: str) -> dict:
        result = submit(self.db_path, submitter, purpose, amount)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        return json.loads(result.stdout)

    def assert_export(self, submitter: str, db_path: Path | None = None):
        """在新进程中导出：退出码 0、标准错误为空、首行为固定表头。

        返回 (CompletedProcess, 数据行列表)（不含表头）。
        """
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
        self.assertGreaterEqual(len(rows), 1, msg="导出至少应包含表头行")
        self.assertEqual(rows[0], CSV_HEADER, msg="首行应为固定表头")
        return result, rows[1:]

    def assert_list(self, submitter: str):
        """在新进程中查询：退出码 0、标准错误为空，返回解析后的记录列表。"""
        result = list_records(self.db_path, submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 固定样例 ----------------------------------------------------------

    def test_export_jia_contains_only_his_two_records(self) -> None:
        """导出演示甲：仅含其两笔，按 id 升序，金额 1230 与 1，状态 pending。"""
        _, rows = self.assert_export("演示甲")
        self.assertEqual(len(rows), 2, msg="演示甲应恰好导出两笔记录")

        ids = [int(row[0]) for row in rows]
        self.assertEqual(ids, sorted(ids), msg="记录应按 id 升序排列")
        self.assertEqual(ids, [r["id"] for r in self.seed_jia])
        self.assertTrue(all(row[1] == "演示甲" for row in rows))
        self.assertEqual([row[2] for row in rows], ["交通费", "餐费"])
        self.assertEqual([row[3] for row in rows], ["1230", "1"])
        self.assertTrue(all(row[4] == "pending" for row in rows))

        # 不含演示乙的办公费记录。
        self.assertNotIn("演示乙", [row[1] for row in rows])
        self.assertNotIn("500", [row[3] for row in rows])

    def test_export_fields_match_list_records(self) -> None:
        """导出各字段内容与同一提交人的 list 结果逐字段一致。"""
        for name, expected in [("演示甲", self.seed_jia), ("演示乙", self.seed_yi)]:
            with self.subTest(submitter=name):
                _, rows = self.assert_export(name)
                records = self.assert_list(name)
                self.assertEqual(len(rows), len(records))
                self.assertEqual(len(records), len(expected))
                for row, record, seed in zip(rows, records, expected):
                    # CSV 字段为字符串形式，与 list 记录逐字段对应。
                    self.assertEqual(row[0], str(record["id"]))
                    self.assertEqual(row[1], record["submitter"])
                    self.assertEqual(row[2], record["purpose"])
                    self.assertEqual(row[3], str(record["amount_minor"]))
                    self.assertEqual(row[4], record["status"])
                    # 同时与 submit 返回的原始记录一致。
                    self.assertEqual(record, seed)

    def test_query_name_is_stripped_before_matching(self) -> None:
        """导出名称两端带空白时，仍命中清理后的演示甲。"""
        _, rows_plain = self.assert_export("演示甲")
        _, rows_padded = self.assert_export("  演示甲\t\n ")
        self.assertEqual(rows_padded, rows_plain)
        self.assertEqual(len(rows_padded), 2)

    def test_prefix_and_unknown_names_return_header_only(self) -> None:
        """导出前缀“演示”或未提交过的“演示丙”，只有表头，没有费用数据行。"""
        for name in ["演示", "演示丙"]:
            with self.subTest(submitter=name):
                _, rows = self.assert_export(name)
                self.assertEqual(rows, [], msg=f"{name} 不应导出任何数据行")

    def test_fresh_database_returns_header_only(self) -> None:
        """对父目录存在的全新数据库首次导出，退出码 0 且只有表头。"""
        fresh_db = Path(self._tmpdir.name) / "fresh_export.sqlite"
        self.assertFalse(fresh_db.exists())
        _, rows = self.assert_export("演示甲", db_path=fresh_db)
        self.assertEqual(rows, [])

    def test_repeated_exports_stable_and_list_unchanged(self) -> None:
        """连续两次导出内容一致，且导出前后 list 结果保持不变。"""
        before_jia = self.assert_list("演示甲")
        before_yi = self.assert_list("演示乙")

        first_jia, _ = self.assert_export("演示甲")
        second_jia, _ = self.assert_export("演示甲")
        first_yi, _ = self.assert_export("演示乙")
        second_yi, _ = self.assert_export("演示乙")
        self.assertEqual(second_jia.stdout, first_jia.stdout, msg="重复导出演示甲应一致")
        self.assertEqual(second_yi.stdout, first_yi.stdout, msg="重复导出演示乙应一致")

        # 导出前后记录数量与内容完全一致。
        after_jia = self.assert_list("演示甲")
        after_yi = self.assert_list("演示乙")
        self.assertEqual(after_jia, before_jia)
        self.assertEqual(after_yi, before_yi)
        self.assertEqual(after_jia, self.seed_jia)
        self.assertEqual(after_yi, self.seed_yi)


class ExportSpecialPurposeTest(unittest.TestCase):
    """用途含中文、逗号、双引号与内嵌换行时，CSV 解析后完整还原字段。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_export_csv_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "special_purpose.sqlite"
        result = submit(
            self.db_path, SPECIAL_SUBMITTER, SPECIAL_PURPOSE, SPECIAL_AMOUNT
        )
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        self.record = json.loads(result.stdout)

    def test_special_characters_round_trip(self) -> None:
        """导出的 CSV 解析后恰为一条记录，用途逐字符还原为提交时的内容。"""
        result = export(self.db_path, SPECIAL_SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"export 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")

        rows = parse_csv(result.stdout)
        self.assertEqual(rows[0], CSV_HEADER)
        # 内嵌换行不得把一条费用拆成多条记录。
        self.assertEqual(len(rows), 2, msg="应恰好为表头加一条数据记录")
        row = rows[1]
        self.assertEqual(len(row), len(CSV_HEADER), msg="记录应恰好五个字段")
        self.assertEqual(row[0], str(self.record["id"]))
        self.assertEqual(row[1], SPECIAL_SUBMITTER)
        self.assertEqual(row[2], SPECIAL_PURPOSE)
        self.assertEqual(row[3], str(SPECIAL_AMOUNT_MINOR))
        self.assertEqual(row[4], "pending")

        # 与同一提交人的 list 结果逐字段一致。
        listed = json.loads(list_records(self.db_path, SPECIAL_SUBMITTER).stdout)
        self.assertEqual(listed, [self.record])
        self.assertEqual(listed[0]["purpose"], SPECIAL_PURPOSE)


class ExportFailureTest(unittest.TestCase):
    """导出命令的参数错误与数据库错误：退出码、空标准输出与标准错误说明。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_export_fail_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "failure.sqlite"
        # 预置固定样例，便于复查失败不影响已有数据。
        for submitter, purpose, amount, _ in SEED_EXPENSES:
            result = submit(self.db_path, submitter, purpose, amount)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def assert_records_unchanged(self) -> None:
        """失败之后，已有记录的数量与内容保持不变。"""
        listed = json.loads(list_records(self.db_path, "演示甲").stdout)
        self.assertEqual(len(listed), 2)
        self.assertEqual([r["purpose"] for r in listed], ["交通费", "餐费"])
        self.assertEqual([r["amount_minor"] for r in listed], [1230, 1])
        self.assertTrue(all(r["status"] == "pending" for r in listed))
        listed_yi = json.loads(list_records(self.db_path, "演示乙").stdout)
        self.assertEqual(len(listed_yi), 1)
        self.assertEqual(listed_yi[0]["amount_minor"], 500)

    def test_missing_db_exits_2(self) -> None:
        """缺少 --db：退出码 2，标准输出为空，标准错误含非空原因。"""
        result = run_cli_no_db("export", "--submitter", "演示甲")
        self.assertEqual(
            result.returncode, 2,
            msg=f"缺少 --db 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空原因")
        self.assert_records_unchanged()

    def test_missing_submitter_exits_2(self) -> None:
        """缺少 --submitter：退出码 2，标准输出为空，标准错误含非空原因。"""
        result = run_cli(self.db_path, "export")
        self.assertEqual(
            result.returncode, 2,
            msg=f"缺少 --submitter 应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空原因")
        self.assert_records_unchanged()

    def test_blank_submitter_exits_2(self) -> None:
        """提交人仅为空白：退出码 2，标准输出为空，标准错误含非空原因。"""
        result = run_cli(self.db_path, "export", "--submitter", "  \t ")
        self.assertEqual(
            result.returncode, 2,
            msg=f"空白提交人应退出码 2: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空原因")
        self.assert_records_unchanged()

    def test_missing_database_parent_exits_1(self) -> None:
        """数据库父目录不存在：退出码 1，标准输出为空（连表头也没有）。"""
        missing_db = Path(self._tmpdir.name) / "no_such_dir" / "missing.sqlite"
        self.assertFalse(missing_db.parent.exists())
        result = export(missing_db, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"数据库父目录缺失应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="失败时连表头也不能输出")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库", result.stderr)
        # 失败后不应遗留数据库文件或目录。
        self.assertFalse(missing_db.exists())
        self.assert_records_unchanged()

    def test_db_path_is_directory_exits_1(self) -> None:
        """数据库路径指向现有目录：退出码 1，标准输出为空（连表头也没有）。"""
        dir_path = Path(self._tmpdir.name) / "a_directory"
        dir_path.mkdir()
        result = export(dir_path, "演示甲")
        self.assertEqual(
            result.returncode, 1,
            msg=f"数据库路径为目录应退出码 1: stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="失败时连表头也不能输出")
        self.assertTrue(result.stderr.strip(), msg="标准错误应包含非空的数据库原因")
        self.assertIn("数据库", result.stderr)
        self.assert_records_unchanged()


if __name__ == "__main__":
    unittest.main()
