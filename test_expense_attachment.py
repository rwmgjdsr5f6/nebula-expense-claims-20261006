"""可选附件文字说明（--attachment-note）的提交、读取与兼容回归测试。

从项目根目录执行：

    python -m unittest test_expense_attachment -v

全部断言通过时退出码为 0，否则为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的 submit / list 公开命令：
提交在独立进程中完成并退出后，再由另一个新进程对同一 SQLite 数据库执行
list，验证附件说明在进程关闭后仍能准确读取。每个用例使用独立的临时
SQLite 文件与虚构人员，结束后自动清理演示数据；仅使用 Python 标准库，
不访问网络或第三方包。

覆盖范围仅限附件说明的提交、读取与兼容边界：金额规则、提交人精确匹配、
summary 预算参考与 export 固定五列输出等既有行为不在本文件重复验证。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 虚构人员与用途，避免与任何真实数据冲突。
SUBMITTER = "演示甲"
PURPOSE_OFFICE = "办公"
PURPOSE_TRAVEL = "交通"

# 旧库（五列结构，无 attachment_note 列）的建表语句，与迁移前的既有结构一致。
_LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令并捕获退出码与输出。"""
    return subprocess.run(
        [sys.executable, "-m", "expense_desk", "--db", str(db_path), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def submit(
    db_path: Path,
    submitter: str,
    purpose: str,
    amount: str,
    attachment_note: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "submit",
        "--submitter", submitter,
        "--purpose", purpose,
        "--amount", amount,
    ]
    if attachment_note is not None:
        argv += ["--attachment-note", attachment_note]
    return run_cli(db_path, *argv)


def list_records(db_path: Path, submitter: str) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "list", "--submitter", submitter)


class AttachmentNoteTest(unittest.TestCase):
    """附件说明随单提交、跨进程读取，并与无说明记录共存。"""

    def setUp(self) -> None:
        # 每次测试使用独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_note_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "attachment.sqlite"

    # -- 辅助 ------------------------------------------------------------

    def assert_submit_ok(
        self, purpose: str, amount: str, attachment_note: str | None = None
    ) -> dict:
        """提交应成功：退出码 0，标准错误为空，标准输出为一条 JSON 对象。"""
        result = submit(self.db_path, SUBMITTER, purpose, amount, attachment_note)
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        self.assertEqual(result.stderr, "", msg="submit 成功时标准错误应为空")
        record = json.loads(result.stdout)  # 非法 JSON 会在此抛出
        self.assertIsInstance(record, dict)
        return record

    def assert_list(self, submitter: str = SUBMITTER) -> list:
        """在新进程中查询：退出码 0，标准错误为空，标准输出为 JSON 数组。"""
        result = list_records(self.db_path, submitter)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "", msg="list 成功时标准错误应为空")
        records = json.loads(result.stdout)
        self.assertIsInstance(records, list)
        return records

    # -- 成功场景 ----------------------------------------------------------

    def test_note_roundtrip_and_noteless_record_coexist(self) -> None:
        """无说明记录保持五字段，带说明记录跨进程读回说明。"""
        # 先提交不带说明的记录，再提交说明为 "  演示车票  " 的记录。
        plain = self.assert_submit_ok(PURPOSE_OFFICE, "5")
        self.assertEqual(plain["amount_minor"], 500)
        self.assertIsInstance(plain["amount_minor"], int)
        self.assertEqual(plain["status"], "pending")
        # 未提供说明的提交返回原有五个字段，不含 attachment_note。
        self.assertEqual(
            set(plain), {"id", "submitter", "purpose", "amount_minor", "status"}
        )

        noted = self.assert_submit_ok(PURPOSE_TRAVEL, "12.30", "  演示车票  ")
        self.assertEqual(noted["amount_minor"], 1230)
        self.assertIsInstance(noted["amount_minor"], int)
        self.assertEqual(noted["status"], "pending")
        # 说明仅去除首尾空白。
        self.assertEqual(noted["attachment_note"], "演示车票")

        # 提交进程已全部退出；在新进程中按提交人查询同一数据库。
        records = self.assert_list()
        self.assertEqual(len(records), 2)
        # 结果按 id 升序，且 id 与提交输出一致。
        ids = [record["id"] for record in records]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [plain["id"], noted["id"]])

        first, second = records
        # 前一条保持原有五字段，缺少 attachment_note。
        self.assertEqual(
            set(first), {"id", "submitter", "purpose", "amount_minor", "status"}
        )
        self.assertNotIn("attachment_note", first)
        self.assertEqual(first["submitter"], SUBMITTER)
        self.assertEqual(first["purpose"], PURPOSE_OFFICE)
        self.assertEqual(first["amount_minor"], 500)
        self.assertEqual(first["status"], "pending")
        # 后一条的说明恰为 "演示车票"。
        self.assertEqual(second["attachment_note"], "演示车票")
        self.assertEqual(second["purpose"], PURPOSE_TRAVEL)
        self.assertEqual(second["amount_minor"], 1230)
        self.assertEqual(second["status"], "pending")

    def test_note_preserves_inner_text_exactly(self) -> None:
        """说明只去除首尾空白，内部中文、逗号、双引号、连续空格与换行逐字符保留。"""
        inner = '中文, English "双引号"  连续两个空格\n内部换行'
        raw_note = f" \t{inner}\n "
        record = self.assert_submit_ok(PURPOSE_TRAVEL, "12.30", raw_note)
        self.assertEqual(record["attachment_note"], inner)

        # 进程退出后重新读取，内部文本仍逐字符一致。
        records = self.assert_list()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["attachment_note"], inner)
        self.assertEqual(records[0]["id"], record["id"])


class LegacyDatabaseNoteTest(unittest.TestCase):
    """原有五列结构的旧库：旧记录不受影响，新记录可携带说明。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_legacy_test_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 预置旧库：五列结构，id 为 7 的演示甲 pending 记录（办公，500 分）。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(_LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO expenses (id, submitter, purpose, amount_minor, status)"
                " VALUES (7, ?, ?, 500, 'pending')",
                (SUBMITTER, PURPOSE_OFFICE),
            )
            conn.commit()
        finally:
            conn.close()

    def _list(self) -> list:
        result = list_records(self.db_path, SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_legacy_record_untouched_and_new_record_carries_note(self) -> None:
        """旧记录各值不变且无说明字段，新记录 id 大于 7 并准确返回说明。"""
        # 先查询：旧库记录原样可读，且没有说明字段。
        before = self._list()
        self.assertEqual(len(before), 1)
        self.assertEqual(
            before[0],
            {
                "id": 7,
                "submitter": SUBMITTER,
                "purpose": PURPOSE_OFFICE,
                "amount_minor": 500,
                "status": "pending",
            },
        )

        # 再提交一条带说明的新记录。
        result = submit(
            self.db_path, SUBMITTER, PURPOSE_TRAVEL, "12.30", "演示车票"
        )
        self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")
        self.assertEqual(result.stderr, "")
        new_record = json.loads(result.stdout)
        self.assertGreater(new_record["id"], 7)
        self.assertEqual(new_record["attachment_note"], "演示车票")

        # 重新查询：旧记录各值不变且没有说明字段，新记录说明准确。
        after = self._list()
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0], before[0])
        self.assertNotIn("attachment_note", after[0])
        self.assertEqual(after[1]["id"], new_record["id"])
        self.assertEqual(after[1]["attachment_note"], "演示车票")
        self.assertEqual(after[1]["amount_minor"], 1230)
        self.assertEqual(after[1]["status"], "pending")


class EmptyNoteRejectionTest(unittest.TestCase):
    """显式空串与仅含空白的说明被拒绝（退出码 2），且不触碰数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_note_reject_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "reject.sqlite"

    def assert_note_rejected(self, result: subprocess.CompletedProcess[str]) -> None:
        """说明无效：退出码 2，标准输出为空，标准错误包含“附件说明不能为空”。"""
        self.assertEqual(
            result.returncode, 2, msg=f"应拒绝却成功: {result.stdout!r}"
        )
        self.assertEqual(result.stdout, "", msg="说明无效时标准输出应为空")
        self.assertIn("附件说明不能为空", result.stderr)

    def test_empty_and_blank_notes_are_rejected_without_creating_db(self) -> None:
        """其他字段有效时，空串与纯空白说明均退出 2，且未创建数据库文件。"""
        self.assertFalse(self.db_path.exists())
        for note in ["", "   ", " \t\n "]:
            with self.subTest(note=note):
                result = submit(
                    self.db_path, SUBMITTER, PURPOSE_OFFICE, "5", note
                )
                self.assert_note_rejected(result)
                # 校验失败先于数据库操作：数据库文件仍不存在。
                self.assertFalse(
                    self.db_path.exists(), msg="说明无效时不应创建数据库文件"
                )

    def test_rejected_note_leaves_existing_records_unchanged(self) -> None:
        """已有库中先提交成功记录，空说明提交被拒后查询结果保持不变。"""
        ok = submit(self.db_path, SUBMITTER, PURPOSE_OFFICE, "5", "演示车票")
        self.assertEqual(ok.returncode, 0, msg=f"submit 失败: {ok.stderr}")
        baseline = self.assert_list_records()

        for note in ["", "  \t "]:
            with self.subTest(note=note):
                result = submit(
                    self.db_path, SUBMITTER, PURPOSE_TRAVEL, "12.30", note
                )
                self.assert_note_rejected(result)
                # 失败提交不影响已有记录。
                self.assertEqual(self.assert_list_records(), baseline)

    def assert_list_records(self) -> list:
        result = list_records(self.db_path, SUBMITTER)
        self.assertEqual(result.returncode, 0, msg=f"list 失败: {result.stderr}")
        return json.loads(result.stdout)

    def test_missing_note_value_is_rejected(self) -> None:
        """只给出 --attachment-note 而不给参数值：退出 2，标准错误指出缺少值。"""
        result = run_cli(
            self.db_path,
            "submit",
            "--submitter", SUBMITTER,
            "--purpose", PURPOSE_OFFICE,
            "--amount", "5",
            "--attachment-note",
        )
        self.assertEqual(result.returncode, 2, msg=f"应退出 2: {result.stdout!r}")
        self.assertEqual(result.stdout, "", msg="缺少参数值时标准输出应为空")
        self.assertIn("缺少参数值", result.stderr)
        self.assertIn("--attachment-note", result.stderr)
        # 参数解析失败同样不应创建数据库文件。
        self.assertFalse(self.db_path.exists())

    def test_empty_note_error_precedes_missing_parent_directory_error(self) -> None:
        """说明为空且数据库父目录不存在时，优先得到说明错误而非数据库错误。"""
        missing_dir_db = (
            Path(self._tmpdir.name) / "no_such_dir" / "unreachable.sqlite"
        )
        self.assertFalse(missing_dir_db.parent.exists())
        result = submit(
            missing_dir_db, SUBMITTER, PURPOSE_OFFICE, "5", "   "
        )
        self.assert_note_rejected(result)
        self.assertNotIn("数据库操作失败", result.stderr)
        # 说明校验优先：父目录仍不存在，数据库文件也未创建。
        self.assertFalse(missing_dir_db.parent.exists())


if __name__ == "__main__":
    unittest.main()
