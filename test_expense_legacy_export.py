"""旧库首次直接 export 的回归测试。

从项目根目录执行：

    python -m unittest test_expense_legacy_export -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 export 公开命令：先用
直接操作 SQLite 的方式按旧库兼容测试的五列表结构（不含 attachment_note）
预置三笔虚构 pending 记录；首次产品命令就是 export，之前不调用 submit、
list 或 summary，以免提前改变待验证的旧库。导出进程退出后，再以新的
SQLite 连接检查附件说明列的补齐是否已经落盘，随后在新进程中重复同一导出。

每个用例使用独立的临时 SQLite 文件，结束后自动清理；人员与费用均为虚构，
重复执行不依赖任何已有数据库、外部服务或第三方包，仅使用 Python 标准库。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 导出 CSV 的固定表头，与命令行实现保持一致；附件说明不在导出列中。
CSV_HEADER = ["id", "submitter", "purpose", "amount_minor", "status"]

# 旧库（五列结构，不含 attachment_note），沿用既有旧库兼容测试的表结构。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

# 固定样例：三笔旧库 pending 记录，金额一律为整数分。
# id 7  演示甲 交通费 1230 分
# id 11 演示乙 办公费 500 分
# id 13 演示甲 餐费   1 分
SEED_ROWS = [
    (7, "演示甲", "交通费", 1230, "pending"),
    (11, "演示乙", "办公费", 500, "pending"),
    (13, "演示甲", "餐费", 1, "pending"),
]

# 演示甲的两笔（按 id 升序）及其整数分金额，用于逐字段核对。
JIA_IDS = [7, 13]
JIA_PURPOSES = ["交通费", "餐费"]
JIA_AMOUNTS_MINOR = ["1230", "1"]


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def export(db_path: Path, submitter: str) -> subprocess.CompletedProcess[str]:
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


class LegacyDatabaseFirstExportTest(unittest.TestCase):
    """五列旧库的首次产品命令即 export：输出正确且结构补齐随进程退出落盘。"""

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_legacy_export_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 直接按旧库五列结构建表并预置三笔记录；此过程不经过任何产品命令。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO expenses"
                " (id, submitter, purpose, amount_minor, status)"
                " VALUES (?, ?, ?, ?, ?)",
                SEED_ROWS,
            )
            conn.commit()
        finally:
            conn.close()

    # -- 辅助 ------------------------------------------------------------

    def assert_export_ok(self, submitter: str):
        """新进程中导出：退出码 0、标准错误为空，返回 (进程结果, CSV 各行)。"""
        result = export(self.db_path, submitter)
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

    def assert_migration_persisted(self) -> None:
        """首次导出进程退出后，用全新 SQLite 连接核对结构补齐已落盘。

        - attachment_note 列存在且只出现一次，位于原有五列之后；
        - 三笔旧记录该列均为 NULL；
        - 原有五字段内容与记录数量完全不变。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            column_names = [
                row[1]
                for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
            ]
            self.assertEqual(
                column_names[:5],
                ["id", "submitter", "purpose", "amount_minor", "status"],
                msg="原有五字段及顺序应保持不变",
            )
            self.assertEqual(
                column_names.count("attachment_note"), 1,
                msg="附件说明列应存在且只出现一次",
            )
            self.assertEqual(
                len(column_names), 6, msg="补齐后应恰为六列"
            )

            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
            # 记录数量不变。
            self.assertEqual(len(rows), len(SEED_ROWS))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],
                len(SEED_ROWS),
            )
            for actual, expected in zip(rows, SEED_ROWS):
                # 原有五字段内容逐字段一致（金额为整数分，不做元/浮点换算）。
                self.assertEqual(tuple(actual[:5]), tuple(expected))
                # 旧记录补齐列均为 NULL。
                self.assertIsNone(
                    actual[5],
                    msg=f"id={expected[0]} 的附件说明列应为 NULL",
                )
                # 状态仍为 pending。
                self.assertEqual(actual[4], "pending")
        finally:
            conn.close()

    # -- 用例 --------------------------------------------------------------

    def test_first_export_exact_name_returns_only_jia_records(self) -> None:
        """首次产品命令直接 export 带空白的演示甲：仅 id 7、13 两笔，五列。"""
        # 注意：此前未调用任何 submit/list/summary，首个产品命令就是 export。
        first, rows = self.assert_export_ok("  演示甲  ")

        # 表头遵循现有五列约定，且不含附件说明列。
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertNotIn("attachment_note", rows[0])
        self.assertEqual(len(rows[0]), 5)

        data = rows_to_dicts(rows)
        # 只有 id 7 与 13，且按此顺序（id 升序）。
        self.assertEqual(
            [int(row["id"]) for row in data], JIA_IDS,
        )
        self.assertEqual(
            [row["id"] for row in data], [str(i) for i in JIA_IDS],
        )
        # 用途与样例一致。
        self.assertEqual([row["purpose"] for row in data], JIA_PURPOSES)
        # 金额按整数分文本逐字节比较，不换算为人民币元、不经浮点。
        self.assertEqual(
            [row["amount_minor"] for row in data], JIA_AMOUNTS_MINOR,
        )
        for row, expected_minor in zip(data, (1230, 1)):
            self.assertTrue(
                row["amount_minor"].isdigit(),
                msg="金额字段应为纯十进制整数字符串",
            )
            self.assertEqual(int(row["amount_minor"]), expected_minor)
        # 提交人均为演示甲，不混入演示乙。
        self.assertTrue(all(row["submitter"] == "演示甲" for row in data))
        self.assertFalse(any(row["submitter"] == "演示乙" for row in data))
        # 两笔均为 pending。
        self.assertTrue(all(row["status"] == "pending" for row in data))
        # 表头加两行数据，没有因空白产生的额外短行。
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(row) == 5 for row in rows))

        # 首次导出进程已退出；在执行其他产品命令前用新连接核对补齐已持久化。
        self.assert_migration_persisted()

        # 在新进程中重复同一导出：结果与首次一致。
        second, second_rows = self.assert_export_ok("  演示甲  ")
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(second_rows, rows)

        # 重复导出不新增费用、不改变状态，旧记录补齐列仍为 NULL。
        self.assert_migration_persisted()

    def test_first_export_prefix_name_returns_header_only(self) -> None:
        """从相同旧表与样例开始，首次以名称前缀“演示”导出：只有表头。"""
        # 导出按完整名称精确匹配，“演示”不命中任何记录。
        first, rows = self.assert_export_ok("演示")
        self.assertEqual(
            rows, [CSV_HEADER],
            msg="前缀名称应只返回表头一行，没有费用数据行",
        )
        self.assertNotIn("attachment_note", rows[0])

        # 表头之后不得再有任何字符（连额外空白行也没有）；文本模式捕获已把
        # CRLF 转换为 \n。
        self.assertEqual(
            first.stdout,
            ",".join(CSV_HEADER) + "\n",
            msg=f"前缀名称的解码输出应仅为表头一行: {first.stdout!r}",
        )
        # 原始字节层面行结束符为 CRLF，且表头之后没有任何字节。
        raw = export_bytes(self.db_path, "演示")
        self.assertEqual(raw.returncode, 0, msg=f"export 失败: {raw.stderr!r}")
        self.assertEqual(raw.stderr, b"")
        self.assertEqual(
            raw.stdout,
            (",".join(CSV_HEADER) + "\r\n").encode("utf-8"),
            msg="前缀名称的原始字节应仅为表头加 CRLF，无额外空白行",
        )

        # 首次导出同样完成旧库结构补齐并已落盘，三笔旧记录原样保留。
        self.assert_migration_persisted()

        # 新进程重复同一前缀导出：仍只有表头，且内容逐字节一致。
        second, second_rows = self.assert_export_ok("演示")
        self.assertEqual(second_rows, [CSV_HEADER])
        self.assertEqual(second.stdout, first.stdout)

        # 不新增费用、不改变状态。
        self.assert_migration_persisted()


if __name__ == "__main__":
    unittest.main()
