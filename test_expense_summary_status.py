"""summary 可选 --status 状态筛选的回归测试。

从项目根目录执行：

    python -m unittest test_expense_summary_status -v

全部断言通过时退出码为 0，任一断言失败时为非 0。

测试通过子进程实际调用 ``python -m expense_desk`` 的公开命令：先用三个独立
提交进程建立固定样例（演示甲两笔、演示乙一笔），再由新进程批准第二、第三笔，
随后每一次 summary 都在全新进程中进行，验证筛选与预算余额在进程退出后仍可
重复读取。状态校验边界（空串、纯空白、大小写不同、其他取值、缺少参数值、
校验顺序与数据库父目录缺失）同样经由公开命令验证；旧库兼容部分先直接用
SQLite 按五列结构建库并写入不同状态的记录，再让产品命令在其上汇总。

约束：
- 每个用例使用各自独立的临时 SQLite 文件与虚构人员，结束后自动清理，
  重复执行不依赖任何已有演示数据；
- 仅使用 Python 标准库，不访问网络或第三方包；
- 金额一律按整数分（整数）核对，不按人民币元或浮点数解释；
- 不修改产品代码、README 或其他入口，本文件只验证本次新增行为与既有行为。
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（本文件所在目录），子进程以此为工作目录运行 python -m expense_desk。
PROJECT_ROOT = Path(__file__).resolve().parent

# 虚构人员，避免与任何真实数据冲突。
SUBMITTER_A = "演示甲"
SUBMITTER_B = "演示乙"
SUBMITTER_UNKNOWN = "演示丙"

# 第一笔交通费随单保存的附件说明。
NOTE_TICKET = "演示车票"

# 固定样例的提交顺序（提交人、用途、命令行金额、整数分金额、附件说明）。
# 1 演示甲 交通费 12.30 元（1230 分，带说明“演示车票”），保持 pending
# 2 演示甲 办公费    5 元（ 500 分，无说明），随后批准为 approved
# 3 演示乙 材料费    2 元（ 200 分，无说明），随后批准为 approved
FIXED_SUBMISSIONS = [
    (SUBMITTER_A, "交通费", "12.30", 1230, NOTE_TICKET),
    (SUBMITTER_A, "办公费", "5", 500, None),
    (SUBMITTER_B, "材料费", "2", 200, None),
]

# 第二、第三笔随后被批准。下列六元组为批准落盘后数据库内的最终内容
# （id、提交人、用途、整数分金额、状态、附件说明），按 id 升序。
EXPECTED_ROWS = [
    (1, SUBMITTER_A, "交通费", 1230, "pending", NOTE_TICKET),
    (2, SUBMITTER_A, "办公费", 500, "approved", None),
    (3, SUBMITTER_B, "材料费", 200, "approved", None),
]

# 无状态筛选时汇总结果恰好包含的三个字段；带预算时仅追加两个预算字段。
SUMMARY_FIELDS = {"submitter", "count", "total_amount_minor"}
BUDGET_EXTRA_FIELDS = {"budget_amount_minor", "remaining_amount_minor"}

# SQLite 64 位有符号整数上限对应的元金额与分值。
MAX_YUAN = "92233720368547758.07"
MAX_MINOR = 2**63 - 1
# 两笔上限金额的合计：2 * (2**63 - 1)，超过 64 位有符号整数上限。
TWO_MAX_TOTAL = 2 * MAX_MINOR

# 大额合计用例的虚构提交人，避免与任何真实数据冲突。
OVERFLOW_SUBMITTER = "汇总状态测试·虚构极限用户"

# 旧库（五列结构）建表语句：没有 attachment_note 列。
LEGACY_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""

# 旧库预置记录：演示甲两笔（pending/approved 各一），演示乙一笔 approved。
LEGACY_ROWS = [
    (7, SUBMITTER_A, "交通费", 1230, "pending"),
    (11, SUBMITTER_A, "办公费", 500, "approved"),
    (13, SUBMITTER_B, "材料费", 200, "approved"),
]


def run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在新进程中执行公开命令（带 --db）并捕获退出码与输出。"""
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
    note: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "submit",
        "--submitter", submitter,
        "--purpose", purpose,
        "--amount", amount,
    ]
    if note is not None:
        argv += ["--attachment-note", note]
    return run_cli(db_path, *argv)


def approve(db_path: Path, expense_id: str) -> subprocess.CompletedProcess[str]:
    return run_cli(db_path, "approve", "--id", expense_id)


# 省略 --status / --budget 的哨兵：区别于显式传入空串（空串是待校验的非法值）。
_STATUS_UNSET = object()
_BUDGET_UNSET = object()


def summarize(
    db_path: Path,
    submitter: str,
    status: object = _STATUS_UNSET,
    budget: object = _BUDGET_UNSET,
) -> subprocess.CompletedProcess[str]:
    """在新进程中执行 summary；参数为哨兵时省略对应选项。"""
    argv = ["summary", "--submitter", submitter]
    if status is not _STATUS_UNSET:
        argv += ["--status", status]
    if budget is not _BUDGET_UNSET:
        argv += ["--budget", budget]
    return run_cli(db_path, *argv)


class SummaryStatusFixedSampleTest(unittest.TestCase):
    """固定样例上的状态筛选：approved/pending/省略状态与预算余额核对。"""

    def setUp(self) -> None:
        # 每个用例独立临时目录与 SQLite 文件，结束后自动清理。
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_status_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "summary_status.sqlite"

        # 依次在三个独立进程中提交固定样例，并逐笔核对提交输出。
        for expected_id, (submitter, purpose, amount, amount_minor, note) in enumerate(
            FIXED_SUBMISSIONS, start=1
        ):
            result = submit(self.db_path, submitter, purpose, amount, note)
            self.assertEqual(
                result.returncode, 0, msg=f"第 {expected_id} 笔提交失败: {result.stderr}"
            )
            self.assertEqual(result.stderr, "")
            record = json.loads(result.stdout)
            self.assertEqual(record["id"], expected_id)
            self.assertEqual(record["amount_minor"], amount_minor)
            self.assertEqual(record["status"], "pending")
            if note is None:
                self.assertNotIn("attachment_note", record)
            else:
                self.assertEqual(record["attachment_note"], note)

        # 在新进程中批准第二、第三笔。
        for approved_id in (2, 3):
            result = approve(self.db_path, str(approved_id))
            self.assertEqual(
                result.returncode,
                0,
                msg=f"批准第 {approved_id} 笔失败: {result.stderr}",
            )
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["status"], "approved")

    # -- 辅助 ------------------------------------------------------------

    def assert_summary_success(
        self, result: subprocess.CompletedProcess[str]
    ) -> dict:
        """成功汇总：退出 0、标准错误为空、标准输出恰为一行 JSON 对象。"""
        self.assertEqual(
            result.returncode, 0, msg=f"summary 失败: {result.stderr}"
        )
        self.assertEqual(result.stderr, "", msg="summary 成功时标准错误应为空")
        self.assertTrue(
            result.stdout.endswith("\n"),
            msg=f"输出应以换行结束: {result.stdout!r}",
        )
        self.assertNotIn("\n", result.stdout[:-1], msg="标准输出应只有一行 JSON")
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        return data

    def fetch_all_rows(self) -> list[tuple]:
        """直接读取库内全部六列内容（按 id 升序），不经产品命令。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

    # -- 需求给定的核对样例 ------------------------------------------------

    def test_approved_with_budget_15_matches_required_example(self) -> None:
        """--status approved --budget 15：1 笔、500 分、预算 1500、余额 1000。"""
        result = summarize(
            self.db_path, SUBMITTER_A, status="approved", budget="15"
        )
        data = self.assert_summary_success(result)
        self.assertEqual(
            data,
            {
                "submitter": SUBMITTER_A,
                "count": 1,
                "total_amount_minor": 500,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 1000,
            },
        )
        # 提供状态不增加状态字段，仍是原有三个字段加两个预算字段。
        self.assertEqual(set(data), SUMMARY_FIELDS | BUDGET_EXTRA_FIELDS)
        self.assertNotIn("status", data)
        for key in (
            "count", "total_amount_minor",
            "budget_amount_minor", "remaining_amount_minor",
        ):
            self.assertIs(type(data[key]), int, msg=f"{key} 应为整数")

    def test_omitted_status_with_budget_15_counts_all_records(self) -> None:
        """省略 --status：2 笔、1730 分、余额 -230，与既有统计范围一致。"""
        result = summarize(self.db_path, SUBMITTER_A, budget="15")
        data = self.assert_summary_success(result)
        self.assertEqual(
            data,
            {
                "submitter": SUBMITTER_A,
                "count": 2,
                "total_amount_minor": 1730,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": -230,
            },
        )

    # -- 三种筛选范围与字段 ------------------------------------------------

    def test_pending_filter_without_budget_keeps_three_fields(self) -> None:
        """--status pending 不带预算：只统计 pending 一笔，输出仍为三字段。"""
        result = summarize(self.db_path, SUBMITTER_A, status="pending")
        data = self.assert_summary_success(result)
        self.assertEqual(
            data,
            {"submitter": SUBMITTER_A, "count": 1, "total_amount_minor": 1230},
        )
        self.assertEqual(set(data), SUMMARY_FIELDS)
        self.assertNotIn("status", data)

    def test_approved_filter_without_budget_keeps_three_fields(self) -> None:
        """--status approved 不带预算：1 笔 500 分，输出仍为三字段。"""
        result = summarize(self.db_path, SUBMITTER_A, status="approved")
        data = self.assert_summary_success(result)
        self.assertEqual(
            data,
            {"submitter": SUBMITTER_A, "count": 1, "total_amount_minor": 500},
        )
        self.assertEqual(set(data), SUMMARY_FIELDS)

    def test_omitted_status_matches_existing_totals(self) -> None:
        """省略 --status 的输出与新增功能前逐字节一致（2 笔 1730 分）。"""
        with_status = summarize(self.db_path, SUBMITTER_A, status="pending")
        omitted = summarize(self.db_path, SUBMITTER_A)
        data = self.assert_summary_success(omitted)
        self.assertEqual(
            data,
            {"submitter": SUBMITTER_A, "count": 2, "total_amount_minor": 1730},
        )
        self.assertEqual(set(data), SUMMARY_FIELDS)
        # 两种筛选范围的合计之和等于全部合计（1230 + 500 = 1730）。
        pending_data = self.assert_summary_success(with_status)
        approved_data = self.assert_summary_success(
            summarize(self.db_path, SUBMITTER_A, status="approved")
        )
        self.assertEqual(
            pending_data["total_amount_minor"] + approved_data["total_amount_minor"],
            data["total_amount_minor"],
        )

    def test_other_submitter_and_other_status_not_mixed_in(self) -> None:
        """演示乙的 approved 费用不混入演示甲；状态不匹配时不命中。"""
        # 演示乙只有一笔 approved 的 200 分。
        yi = self.assert_summary_success(
            summarize(self.db_path, SUBMITTER_B, status="approved")
        )
        self.assertEqual(
            yi,
            {"submitter": SUBMITTER_B, "count": 1, "total_amount_minor": 200},
        )
        # 演示乙没有 pending 记录。
        yi_pending = self.assert_summary_success(
            summarize(self.db_path, SUBMITTER_B, status="pending")
        )
        self.assertEqual(
            yi_pending,
            {"submitter": SUBMITTER_B, "count": 0, "total_amount_minor": 0},
        )
        # 演示甲的 approved 汇总结果中不出现演示乙。
        rendered = summarize(
            self.db_path, SUBMITTER_A, status="approved"
        ).stdout
        self.assertNotIn(SUBMITTER_B, rendered)

    # -- 空白、前缀与未命中的零值 ------------------------------------------

    def test_whitespace_around_submitter_and_status_is_trimmed(self) -> None:
        """提交人与状态首尾带空白，结果与不带空白时逐字节一致。"""
        baseline_pending = summarize(self.db_path, SUBMITTER_A, status="pending")
        baseline_approved = summarize(
            self.db_path, SUBMITTER_A, status="approved", budget="15"
        )
        self.assertEqual(baseline_pending.returncode, 0)
        self.assertEqual(baseline_approved.returncode, 0)

        cases = [
            ("  演示甲  ", "  pending  ", _BUDGET_UNSET, baseline_pending),
            ("\t演示甲\t", "approved \t", "15", baseline_approved),
            (" 演示甲 ", "\n approved\n", " 15 ", baseline_approved),
        ]
        for submitter, status, budget, baseline in cases:
            with self.subTest(submitter=submitter, status=status, budget=budget):
                result = summarize(
                    self.db_path, submitter, status=status, budget=budget
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, baseline.stdout)

    def test_unknown_and_prefix_return_zeroes(self) -> None:
        """前缀与未提交名称：笔数合计为整数零；带预算时余额等于预算。"""
        for name in ("演示", "  演示  ", SUBMITTER_UNKNOWN):
            for status in (_STATUS_UNSET, "pending", "approved"):
                with self.subTest(name=name, status=status):
                    result = summarize(
                        self.db_path, name, status=status, budget="15"
                    )
                    data = self.assert_summary_success(result)
                    self.assertEqual(data["submitter"], name.strip())
                    self.assertIs(type(data["count"]), int)
                    self.assertIs(type(data["total_amount_minor"]), int)
                    self.assertEqual(data["count"], 0)
                    self.assertEqual(data["total_amount_minor"], 0)
                    self.assertEqual(data["budget_amount_minor"], 1500)
                    self.assertEqual(data["remaining_amount_minor"], 1500)

    def test_fresh_database_status_filter_returns_zeroes(self) -> None:
        """父目录存在的新库上带状态汇总：退出 0 并返回零值。"""
        fresh_db = Path(self._tmpdir.name) / "fresh_summary_status.sqlite"
        self.assertFalse(fresh_db.exists())
        result = summarize(
            fresh_db, SUBMITTER_A, status="approved", budget="15"
        )
        data = self.assert_summary_success(result)
        self.assertEqual(
            data,
            {
                "submitter": SUBMITTER_A,
                "count": 0,
                "total_amount_minor": 0,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 1500,
            },
        )

    # -- 筛选只读、不持久化，重启后读取已保存状态 ---------------------------

    def test_filtering_neither_modifies_expenses_nor_persists_choice(self) -> None:
        """带状态/预算汇总前后，库内三笔记录逐字段不变，选择不落库。"""
        before_rows = self.fetch_all_rows()
        self.assertEqual(before_rows, EXPECTED_ROWS)
        before_all = summarize(self.db_path, SUBMITTER_A)

        # 依次执行不同筛选（各在独立新进程中）。
        for args in (
            dict(status="pending"),
            dict(status="approved"),
            dict(status="approved", budget="15"),
            dict(budget="15"),
        ):
            result = summarize(self.db_path, SUBMITTER_A, **args)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

        # 刚按 pending 筛选后省略 --status：仍返回全部两笔，选择未被保存。
        after = summarize(self.db_path, SUBMITTER_A)
        self.assertEqual(after.stdout, before_all.stdout)
        self.assert_summary_success(after)

        # 库内三笔记录（含附件说明与实际状态）逐字段不变。
        self.assertEqual(self.fetch_all_rows(), before_rows)
        self.assertEqual(self.fetch_all_rows(), EXPECTED_ROWS)

    def test_status_reread_from_disk_in_new_process_after_approval(self) -> None:
        """批准落盘后，新进程的状态汇总读到已保存的实际状态。"""
        # 当前第二笔已是 approved；再在新进程中幂等批准，不改变任何内容。
        again = approve(self.db_path, "2")
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        self.assertEqual(json.loads(again.stdout)["status"], "approved")

        pending = self.assert_summary_success(
            summarize(self.db_path, SUBMITTER_A, status="pending")
        )
        approved = self.assert_summary_success(
            summarize(self.db_path, SUBMITTER_A, status="approved")
        )
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["total_amount_minor"], 1230)
        self.assertEqual(approved["count"], 1)
        self.assertEqual(approved["total_amount_minor"], 500)


class SummaryStatusValidationTest(unittest.TestCase):
    """summary --status 取值、缺值与校验顺序边界。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_status_err_")
        self.addCleanup(self._tmpdir.cleanup)
        # 初始时数据库文件尚不存在；纯参数校验失败不应创建它。
        self.db_path = Path(self._tmpdir.name) / "validation.sqlite"

    def missing_parent_db(self) -> Path:
        return Path(self._tmpdir.name) / "no_such_dir" / "unreachable.sqlite"

    def test_invalid_status_values_exit_2_without_creating_db(self) -> None:
        """空串、纯空白、Pending、其他值均退出 2 且提示“状态无效”。"""
        for raw in ("", "   ", "\t \n", "Pending", "APPROVED", "rejected"):
            with self.subTest(status=raw):
                result = summarize(
                    self.db_path, SUBMITTER_A, status=raw, budget="15"
                )
                self.assertEqual(
                    result.returncode,
                    2,
                    msg=f"状态 {raw!r} 应退出 2，实际 {result.returncode}",
                )
                self.assertEqual(result.stdout, "", msg="状态无效时标准输出应为空")
                self.assertIn("状态无效", result.stderr)
                # 校验先于数据库操作：数据库文件不应被创建。
                self.assertFalse(
                    self.db_path.exists(), msg="状态无效时不应创建数据库文件"
                )

    def test_status_flag_without_value_is_rejected(self) -> None:
        """只给 --status 不给值：退出 2 并指出该参数缺少值。"""
        result = run_cli(
            self.db_path,
            "summary",
            "--submitter", SUBMITTER_A,
            "--status",
        )
        self.assertEqual(
            result.returncode, 2, msg=f"应退出 2: {result.stdout!r}"
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("--status", result.stderr)
        self.assertIn("缺少参数值", result.stderr)
        self.assertFalse(self.db_path.exists(), msg="不应创建数据库文件")

    def test_blank_submitter_is_reported_before_invalid_status(self) -> None:
        """提交人为空白且状态、数据库路径均无效：先报提交人不能为空。"""
        missing_db = self.missing_parent_db()
        result = summarize(missing_db, "   ", status="Pending", budget="15")
        self.assertEqual(
            result.returncode, 2, msg=f"应退出 2: {result.stderr!r}"
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("提交人", result.stderr)
        self.assertIn("不能为空", result.stderr)
        self.assertNotIn("状态无效", result.stderr)
        self.assertNotIn("预算无效", result.stderr)
        self.assertNotIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
        self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")

    def test_invalid_budget_is_reported_before_invalid_status(self) -> None:
        """预算与状态同时无效：优先报告预算错误，不创建数据库文件。"""
        missing_db = self.missing_parent_db()
        result = summarize(missing_db, SUBMITTER_A, status="Pending", budget="0")
        self.assertEqual(
            result.returncode, 2, msg=f"应退出 2: {result.stderr!r}"
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("预算", result.stderr)
        self.assertNotIn("状态无效", result.stderr)
        self.assertNotIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
        self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")

    def test_invalid_status_is_reported_before_database_error(self) -> None:
        """其余参数有效、状态无效且父目录不存在：仍先报状态无效，不建库。"""
        missing_db = self.missing_parent_db()
        self.assertFalse(missing_db.parent.exists())
        for raw in ("Pending", "rejected", ""):
            with self.subTest(status=raw):
                result = summarize(
                    missing_db, SUBMITTER_A, status=raw, budget="15"
                )
                self.assertEqual(
                    result.returncode, 2, msg=f"应退出 2: {result.stderr!r}"
                )
                self.assertEqual(result.stdout, "")
                self.assertIn("状态无效", result.stderr)
                self.assertNotIn("数据库操作失败", result.stderr)
                self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
                self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")

    def test_valid_args_with_missing_parent_dir_exit_1(self) -> None:
        """参数有效但数据库父目录不存在：退出 1、空标准输出、提示数据库原因。"""
        missing_db = self.missing_parent_db()
        result = summarize(
            missing_db, SUBMITTER_A, status="approved", budget="15"
        )
        self.assertEqual(
            result.returncode, 1, msg=f"应退出 1: {result.stderr!r}"
        )
        self.assertEqual(result.stdout, "", msg="数据库失败时标准输出应为空")
        self.assertIn("数据库操作失败", result.stderr)
        self.assertFalse(missing_db.exists(), msg="不应创建数据库文件")
        self.assertFalse(missing_db.parent.exists(), msg="不应创建父目录")


class LegacyFiveColumnSummaryStatusTest(unittest.TestCase):
    """旧五列库上的状态汇总：统计值正确，旧记录五列值不变，可重复。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_legacy_summary_status_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        # 直接按五列旧结构建库并写入样例；不经产品命令。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO expenses"
                " (id, submitter, purpose, amount_minor, status)"
                " VALUES (?, ?, ?, ?, ?)",
                LEGACY_ROWS,
            )
            conn.commit()
        finally:
            conn.close()

    def test_status_filter_on_legacy_database(self) -> None:
        # 首次产品操作即带状态与预算汇总：演示甲 approved 1 笔 500 分。
        first = summarize(
            self.db_path, SUBMITTER_A, status="approved", budget="15"
        )
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(first.stderr, "")
        data = json.loads(first.stdout)
        self.assertEqual(
            data,
            {
                "submitter": SUBMITTER_A,
                "count": 1,
                "total_amount_minor": 500,
                "budget_amount_minor": 1500,
                "remaining_amount_minor": 1000,
            },
        )
        self.assertNotIn("attachment_note", first.stdout)

        # pending 筛选：演示甲仅一笔 1230 分。
        pending = summarize(self.db_path, SUBMITTER_A, status="pending")
        self.assertEqual(pending.returncode, 0, msg=pending.stderr)
        self.assertEqual(
            json.loads(pending.stdout),
            {"submitter": SUBMITTER_A, "count": 1, "total_amount_minor": 1230},
        )

        # 省略状态：演示甲两笔合计 1730 分。
        omitted = summarize(self.db_path, SUBMITTER_A)
        self.assertEqual(omitted.returncode, 0, msg=omitted.stderr)
        self.assertEqual(
            json.loads(omitted.stdout),
            {"submitter": SUBMITTER_A, "count": 2, "total_amount_minor": 1730},
        )

        # 演示乙的 approved 一笔不混入演示甲。
        yi = summarize(self.db_path, SUBMITTER_B, status="approved")
        self.assertEqual(
            json.loads(yi.stdout),
            {"submitter": SUBMITTER_B, "count": 1, "total_amount_minor": 200},
        )

        # 另一新进程重复同一筛选：输出逐字节一致。
        second = summarize(
            self.db_path, SUBMITTER_A, status="approved", budget="15"
        )
        self.assertEqual(second.stdout, first.stdout)

        # 直接核对库内内容：五字段各值不变；结构补齐只允许新增值为 NULL 的
        # 附件说明列，原有数据不被改写。
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), len(LEGACY_ROWS))
        for row, seed in zip(rows, LEGACY_ROWS):
            self.assertEqual(tuple(row[:5]), seed)
            self.assertIsNone(row[5])


class SummaryStatusLargeTotalTest(unittest.TestCase):
    """带状态汇总时合计仍可超过单笔上限；批准后新进程读到实际状态。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="expense_desk_summary_status_big_")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "large.sqlite"
        for _ in range(2):
            result = submit(self.db_path, OVERFLOW_SUBMITTER, "大额费用", MAX_YUAN)
            self.assertEqual(result.returncode, 0, msg=f"submit 失败: {result.stderr}")

    def test_filtered_total_exceeds_limit_and_status_reread_after_approve(self) -> None:
        # 两笔均 pending：带状态合计精确为两笔上限之和。
        pending = summarize(self.db_path, OVERFLOW_SUBMITTER, status="pending")
        self.assertEqual(pending.returncode, 0, msg=pending.stderr)
        self.assertEqual(pending.stderr, "")
        data = json.loads(pending.stdout)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total_amount_minor"], TWO_MAX_TOTAL)
        token = re.search(
            r'"total_amount_minor":\s*([^,}]+)', pending.stdout
        ).group(1).strip()
        self.assertEqual(token, "18446744073709551614")
        self.assertNotIn(".", token)
        self.assertNotIn("e", token.lower())

        # 在新进程中批准第一笔并落盘。
        approval = approve(self.db_path, "1")
        self.assertEqual(approval.returncode, 0, msg=approval.stderr)
        self.assertEqual(json.loads(approval.stdout)["status"], "approved")

        # 重新启动后的汇总读取已保存的实际状态：各状态均为一笔上限金额。
        pending_after = json.loads(
            summarize(self.db_path, OVERFLOW_SUBMITTER, status="pending").stdout
        )
        approved_after = json.loads(
            summarize(self.db_path, OVERFLOW_SUBMITTER, status="approved").stdout
        )
        all_after = json.loads(
            summarize(self.db_path, OVERFLOW_SUBMITTER).stdout
        )
        self.assertEqual(pending_after["count"], 1)
        self.assertEqual(pending_after["total_amount_minor"], MAX_MINOR)
        self.assertEqual(approved_after["count"], 1)
        self.assertEqual(approved_after["total_amount_minor"], MAX_MINOR)
        self.assertEqual(all_after["count"], 2)
        self.assertEqual(all_after["total_amount_minor"], TWO_MAX_TOTAL)


if __name__ == "__main__":
    unittest.main()
