"""旧库（五列结构）首次直接 export 的回归测试。

从项目根目录执行：

    python -m unittest test_expense_legacy_export -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 export 公开命令：先直接
用 SQLite 连接按已有旧库兼容测试的五列结构建库并写入虚构样例（此步不属于产品
命令，不会提前触发结构补齐），再由新进程对该库执行 ``export --submitter``。
首次导出进程退出后、执行任何其他产品命令之前，另开一个新的 SQLite 连接检查
附件说明列的补齐结果是否已经持久落盘，随后再在新进程中重复导出。

约束：
- 首次导出之前绝不调用 submit / list / summary，以免提前改变待验证的旧库；
- 每个用例使用独立的临时 SQLite 文件，结束后自动清理；
- 人员与费用均为虚构数据，仅使用 Python 标准库，不访问网络或第三方包；
- 金额一律按整数分（整数比较）校验，不按人民币元或浮点数解释。
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

# 导出 CSV 的固定表头，与命令行实现保持一致：固定五列，不含附件说明列。
CSV_HEADER = ["id", "submitter", "purpose", "amount_minor", "status"]

# 旧库（五列结构）建表语句，沿用已有旧库兼容测试（test_expense_attachment）：
# 没有 attachment_note 列。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

# 固定样例：三笔 pending 旧记录。金额以整数分存储。
# id 7  演示甲 交通费 1230 分
# id 11 演示乙 办公费  500 分
# id 13 演示甲 餐费      1 分
SEED_RECORDS = [
    (7, "演示甲", "交通费", 1230, "pending"),
    (11, "演示乙", "办公费", 500, "pending"),
    (13, "演示甲", "餐费", 1, "pending"),
]

# 附件说明列名；旧库经首次产品命令触发 ALTER TABLE 补齐。
ATTACHMENT_NOTE_COLUMN = "attachment_note"


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


def parse_csv(stdout: str) -> list[list[str]]:
    """按标准 CSV 规则解析导出输出，返回含表头在内的各行。"""
    return list(csv.reader(io.StringIO(stdout)))


def create_legacy_database(db_path: Path) -> None:
    """用五列旧结构直接建库并写入三笔样例；不经任何产品命令。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO expenses"
            " (id, submitter, purpose, amount_minor, status)"
            " VALUES (?, ?, ?, ?, ?)",
            SEED_RECORDS,
        )
        conn.commit()
    finally:
        conn.close()


class LegacyDatabaseExportTestBase(unittest.TestCase):
    """两个用例共用的旧库搭建、结构校验与 CSV 断言辅助。"""

    def setUp(self) -> None:
        # 每个用例独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="expense_desk_legacy_export_"
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 直接按旧库五列结构建库并写入样例；此前不调用任何产品命令。
        create_legacy_database(self.db_path)

    def assert_export_succeeded(
        self, result: subprocess.CompletedProcess[str]
    ) -> None:
        """导出进程：退出码 0，标准错误为空。"""
        self.assertEqual(
            result.returncode, 0, msg=f"export 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "", msg="export 成功时标准错误应为空")

    def assert_migration_persisted(self) -> None:
        """首次导出进程退出后，用全新连接检查结构补齐已持久落盘。

        - attachment_note 列存在且只出现一次；
        - 原有五列名称不变；
        - 仍为三笔记录，五字段内容与样例完全一致；
        - 三笔旧记录的 attachment_note 均为 NULL。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            column_names = [
                row[1]
                for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
            ]
            note_occurrences = [
                name for name in column_names if name == ATTACHMENT_NOTE_COLUMN
            ]
            self.assertEqual(
                len(note_occurrences),
                1,
                msg=f"attachment_note 应只出现一次，实际列: {column_names}",
            )
            for original_column in ("id", "submitter", "purpose", "amount_minor", "status"):
                self.assertIn(original_column, column_names)

            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        # 记录数量不变，原有五字段内容逐笔与样例一致，附件说明均为 NULL。
        self.assertEqual(len(rows), len(SEED_RECORDS))
        for row, seed in zip(rows, SEED_RECORDS):
            self.assertEqual(tuple(row[:5]), seed)
            self.assertIsNone(row[5])

    def assert_records_still_unchanged(self) -> None:
        """重复导出后再次核对：不新增费用、不改变状态、内容不变。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), len(SEED_RECORDS))
        for row, seed in zip(rows, SEED_RECORDS):
            self.assertEqual(tuple(row[:5]), seed)
            self.assertEqual(row[4], "pending")
            self.assertIsNone(row[5])


class LegacyExportByExactNameTest(LegacyDatabaseExportTestBase):
    """旧库首次直接 export：按完整名称（带首尾空白）导出演示甲的两笔费用。"""

    def test_first_export_filters_legacy_rows_and_persists_migration(self) -> None:
        # 首次导出：参数带首尾空白，产品侧去除空白后按完整名称精确匹配。
        # 此前未调用 submit / list / summary，旧库保持五列结构未被触碰。
        first = export(self.db_path, "  演示甲  ")
        self.assert_export_succeeded(first)

        rows = parse_csv(first.stdout)
        # 表头 + 两笔数据，没有演示乙的记录，也没有额外空白行。
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, rows[0])
        self.assertEqual(len(rows[0]), 5)

        # 数据只有 id 7 与 id 13，且按此顺序（id 升序）排列。
        data_rows = rows[1:]
        self.assertEqual(
            [int(cells[0]) for cells in data_rows], [7, 13]
        )

        # id 7：演示甲、交通费、1230 分（按整数字符串与整数分两种方式核对，
        # 绝不按人民币元或浮点数比较）。
        self.assertEqual(data_rows[0], ["7", "演示甲", "交通费", "1230", "pending"])
        self.assertEqual(int(data_rows[0][3]), 1230)
        self.assertIsInstance(int(data_rows[0][3]), int)
        # id 13：演示甲、餐费、1 分。
        self.assertEqual(data_rows[1], ["13", "演示甲", "餐费", "1", "pending"])
        self.assertEqual(int(data_rows[1][3]), 1)
        # 每行固定五列，不含附件说明列数据。
        for cells in data_rows:
            self.assertEqual(len(cells), 5)
        # 不含演示乙（id 11）。
        rendered = first.stdout
        self.assertNotIn("演示乙", rendered)
        self.assertNotIn("11", [cells[0] for cells in data_rows])

        # 首次导出进程已退出、且尚未执行其他产品命令：用新连接检查补齐已落盘。
        self.assert_migration_persisted()

        # 在新进程中重复同一导出：结果与首次完全一致。
        second = export(self.db_path, "  演示甲  ")
        self.assert_export_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(parse_csv(second.stdout), rows)

        # 重复导出不新增费用、不改变状态。
        self.assert_records_still_unchanged()


class LegacyExportByPrefixNameTest(LegacyDatabaseExportTestBase):
    """旧库首次直接 export：名称前缀“演示”不做模糊匹配，仅输出表头。"""

    def test_first_export_with_prefix_name_outputs_header_only(self) -> None:
        # 独立用例，从相同的五列旧表与三笔样例开始；此前不调用任何产品命令。
        first = export(self.db_path, "演示")
        self.assert_export_succeeded(first)

        rows = parse_csv(first.stdout)
        # 只有表头一行：没有数据行，也没有额外空白行（空白行会被 CSV 解析为 []）。
        self.assertEqual(rows, [CSV_HEADER])
        self.assertEqual(len(rows), 1)
        # 文本模式下换行被统一转换为 \n；整个输出恰为表头加单个换行，
        # 从字节层面排除额外空白行。
        self.assertEqual(first.stdout, ",".join(CSV_HEADER) + "\n")
        # 不出现任何样例提交人与附件说明列。
        self.assertNotIn("演示甲", first.stdout)
        self.assertNotIn("演示乙", first.stdout)
        self.assertNotIn(ATTACHMENT_NOTE_COLUMN, first.stdout)

        # 首次导出进程已退出、且尚未执行其他产品命令：用新连接检查补齐已落盘。
        self.assert_migration_persisted()

        # 在新进程中重复同一导出：仍只有表头，结果与首次逐字节一致。
        second = export(self.db_path, "演示")
        self.assert_export_succeeded(second)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(parse_csv(second.stdout), [CSV_HEADER])

        # 重复导出不新增费用、不改变状态。
        self.assert_records_still_unchanged()


if __name__ == "__main__":
    unittest.main()
