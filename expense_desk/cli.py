"""命令行接口：提交报销单、按提交人查询与汇总、按编号批准。

入口：``python -m expense_desk --db <SQLite 文件> <submit|list|summary|export|approve> ...``

仅依赖 Python 3 标准库，金额以整数分（人民币）存储，不经过浮点运算。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from typing import Sequence

#: SQLite INTEGER 为 64 位有符号整数，金额（分）不得超过其上限。
MAX_AMOUNT_MINOR = 2**63 - 1

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"

#: 报销单编号存入 SQLite INTEGER 主键，不得超过 64 位有符号整数上限。
MAX_EXPENSE_ID = 2**63 - 1

#: CSV 导出的固定表头，与查询返回的字段顺序一致。
CSV_HEADER = ("id", "submitter", "purpose", "amount_minor", "status")

# 整数部分为一或多个 ASCII 数字；小数部分要么没有，要么为小数点加一至两位。
# 显式 ASCII 锚定，拒绝全角数字、科学计数法、正负号、千分位等写法。
_AMOUNT_RE = re.compile(r"\A([0-9]+)(?:\.([0-9]{1,2}))?\Z", re.ASCII)

# 编号仅接受一或多个 ASCII 数字，拒绝正负号、小数点、全角数字等写法。
_ID_RE = re.compile(r"\A[0-9]+\Z", re.ASCII)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL,
    attachment_note TEXT
)
"""

#: 可选附件文字说明列；旧库（五列结构）经 ALTER TABLE 补齐此列。
ATTACHMENT_NOTE_COLUMN = "attachment_note"


class ValidationError(ValueError):
    """参数校验失败（退出码 2）。"""


class _ExpenseArgumentParser(argparse.ArgumentParser):
    """把附件说明缺少参数值的报错改为明确的中文提示。"""

    def error(self, message: str) -> None:
        if "attachment-note" in message and "expected one argument" in message:
            message = "参数 --attachment-note 缺少参数值"
        super().error(message)


def parse_amount(raw: str) -> int:
    """把人民币元金额字符串转换为整数分。

    接受去除首尾空白后形如 ``12``、``12.3``、``12.30`` 的字符串，
    分别得到 1200、1230、1230。纯字符串十进制运算，不受浮点舍入影响。

    拒绝空串、零、负数、科学计数法、超过两位小数及其他格式。
    """
    text = raw.strip()
    if not text:
        raise ValidationError("金额去除首尾空白后不能为空")
    match = _AMOUNT_RE.fullmatch(text)
    if match is None:
        raise ValidationError(
            "金额格式无效，仅接受 ASCII 数字整数，或整数部分加小数点及一至两位小数"
        )
    whole, fraction = match.group(1), match.group(2)
    # 通过字符串拼接补到两位小数，避免任何浮点舍入。
    frac2 = (fraction + "00")[:2] if fraction is not None else "00"
    # 先去掉整数部分的前导零：前导零不改变数值，也不应计入长度判断。
    whole_significant = whole.lstrip("0")
    if not whole_significant:
        # 整数部分全为零，数值完全由两位小数决定（0 到 99 分）。
        minor = int(frac2)
        if minor == 0:
            raise ValidationError("金额必须大于零")
        return minor
    digits = whole_significant + frac2
    # 用十进制字符串按长度再按字典序与上限比较，避免把超长数字串交给
    # int()：Python 默认的整数转换长度限制会对超长输入抛出 ValueError，
    # 该异常不属于参数校验错误，会越过统一的错误处理。
    max_digits = str(MAX_AMOUNT_MINOR)
    if len(digits) > len(max_digits) or (
        len(digits) == len(max_digits) and digits > max_digits
    ):
        raise ValidationError(
            "金额换算为分后超过 SQLite 64 位有符号整数上限 "
            f"({MAX_AMOUNT_MINOR})"
        )
    # 至此数字串长度不超过上限的位数，int() 转换必然成功。
    return int(digits)


def parse_budget(raw: str) -> int:
    """把一次性预算金额（人民币元）转换为整数分。

    预算仅供 summary 当次参考，格式与上限与提交金额完全一致，直接复用
    :func:`parse_amount` 的换算与拒绝规则；错误信息改述为预算无效，
    以便与提交金额的参数错误区分。
    """
    try:
        return parse_amount(raw)
    except ValidationError as exc:
        raise ValidationError(f"预算无效：{exc}") from None


def parse_expense_id(raw: str) -> int:
    """把报销单编号字符串转换为整数。

    接受去除首尾空白后由 ASCII 数字组成、数值在 1 到
    ``MAX_EXPENSE_ID`` 之间的字符串，允许前导零（``"007"`` 即 7）。
    拒绝空串、零、负数、小数、带正号、非 ASCII 数字及越界值。
    """
    text = raw.strip()
    if not text:
        raise ValidationError("编号无效：编号去除首尾空白后不能为空")
    if _ID_RE.fullmatch(text) is None:
        raise ValidationError("编号无效：仅接受 ASCII 数字组成的正整数")
    # 前导零不改变数值，先去掉再做长度与数值比较。
    digits = text.lstrip("0")
    if not digits:
        raise ValidationError("编号无效：编号必须大于零")
    # 与金额相同的十进制字符串比较法：避免把超长数字串交给 int()，
    # 防止触发 Python 的整数转换长度限制而抛出非校验类异常。
    max_digits = str(MAX_EXPENSE_ID)
    if len(digits) > len(max_digits) or (
        len(digits) == len(max_digits) and digits > max_digits
    ):
        raise ValidationError(
            f"编号无效：超过 SQLite 64 位有符号整数上限 ({MAX_EXPENSE_ID})"
        )
    return int(digits)


def _clean_name(field_label: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_label}去除首尾空白后不能为空")
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = _ExpenseArgumentParser(
        prog="python -m expense_desk",
        description="本地费用报销台：提交报销单、按提交人查询或汇总。",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="SQLite 数据库文件路径；父目录存在时自动创建新库",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="提交一张报销单")
    submit.add_argument("--submitter", required=True, help="提交人")
    submit.add_argument("--purpose", required=True, help="费用用途")
    submit.add_argument("--amount", required=True, help="人民币金额（元）")
    submit.add_argument(
        "--attachment-note",
        help=(
            "可选的附件文字说明：仅作为普通文本随单保存，不读取文件或访问网络；"
            "去除首尾空白，内部空格、中文、逗号、双引号与换行原样保留"
        ),
    )

    listing = subparsers.add_parser("list", help="按提交人查询报销单")
    listing.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")

    summary = subparsers.add_parser("summary", help="按提交人汇总笔数与金额")
    summary.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")
    summary.add_argument(
        "--budget",
        help="可选的一次性预算参考（人民币元）；提供时额外返回预算分与余额分",
    )

    export = subparsers.add_parser("export", help="按提交人导出报销单 CSV 到标准输出")
    export.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")

    approve = subparsers.add_parser("approve", help="按编号批准一张报销单")
    approve.add_argument("--id", required=True, help="报销单编号（正整数）")

    return parser


def _connect(db_path: str) -> sqlite3.Connection:
    # 相对路径按当前工作目录解释，由 sqlite3 直接处理。
    return sqlite3.connect(db_path)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表（新库直接含附件说明列），并为旧库补齐可选的附件说明列。

    旧库的 expenses 表只有五个字段；ALTER TABLE ... ADD COLUMN 对旧记录
    填入 NULL，旧记录读取时因此不输出 attachment_note 字段，其 id、金额、
    用途与状态均保持原样。
    """
    conn.execute(_SCHEMA)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(expenses)").fetchall()
    }
    if ATTACHMENT_NOTE_COLUMN not in columns:
        conn.execute(
            f"ALTER TABLE expenses ADD COLUMN {ATTACHMENT_NOTE_COLUMN} TEXT"
        )
        # 显式提交：查询/汇总路径不在事务块中调用本函数，连接关闭时未提交
        # 的变更会被回滚，旧库迁移将无法落盘。
        conn.commit()


def _insert_expense(
    db_path: str,
    submitter: str,
    purpose: str,
    amount_minor: int,
    attachment_note: str | None = None,
) -> int:
    conn = _connect(db_path)
    try:
        with conn:  # 提交失败时自动回滚，保证失败提交不留记录
            _ensure_schema(conn)
            cursor = conn.execute(
                "INSERT INTO expenses"
                " (submitter, purpose, amount_minor, status, attachment_note)"
                " VALUES (?, ?, ?, ?, ?)",
                (submitter, purpose, amount_minor, STATUS_PENDING, attachment_note),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _query_expenses(db_path: str, submitter: str) -> list[dict[str, object]]:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, submitter, purpose, amount_minor, status, attachment_note"
            " FROM expenses WHERE submitter = ? ORDER BY id ASC",
            (submitter,),
        ).fetchall()
    finally:
        conn.close()
    records: list[dict[str, object]] = []
    for row in rows:
        record: dict[str, object] = {
            "id": row[0],
            "submitter": row[1],
            "purpose": row[2],
            "amount_minor": row[3],
            "status": row[4],
        }
        # 仅带说明的新记录追加该字段；旧记录（NULL）保持原有五个字段。
        if row[5] is not None:
            record["attachment_note"] = row[5]
        records.append(record)
    return records


def _summarize_expenses(
    db_path: str, submitter: str, budget_minor: int | None = None
) -> dict[str, object]:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT amount_minor FROM expenses WHERE submitter = ?",
            (submitter,),
        ).fetchall()
    finally:
        conn.close()
    # 在 Python 中按任意精度整数求和：SQLite 的 SUM() 在合计超过
    # 64 位有符号整数上限时会报整数溢出，这里逐行累加不受该限制。
    total = 0
    for row in rows:
        total += int(row[0])
    result: dict[str, object] = {
        "submitter": submitter,
        "count": len(rows),
        "total_amount_minor": total,
    }
    if budget_minor is not None:
        # 预算只供当次参考、不落库；余额为预算减合计的任意精度整数差，
        # 合计超过单笔上限时仍可得到精确的（可能为负的）完整十进制值。
        result["budget_amount_minor"] = budget_minor
        result["remaining_amount_minor"] = budget_minor - total
    return result


def _approve_expense(db_path: str, expense_id: int) -> dict[str, object]:
    """把指定编号的报销单从 pending 改为 approved，并返回该记录。

    编号不存在、或记录状态既不是 pending 也不是 approved 时抛出
    :class:`ValidationError`（退出码 2），不改动任何记录；记录已是
    approved 时直接原样返回，不重复写入。
    """
    conn = _connect(db_path)
    try:
        with conn:  # 更新失败时自动回滚，保证失败批准不改记录
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT id, submitter, purpose, amount_minor, status,"
                " attachment_note FROM expenses WHERE id = ?",
                (expense_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"报销单不存在：编号 {expense_id}")
            status = row[4]
            if status == STATUS_PENDING:
                conn.execute(
                    "UPDATE expenses SET status = ? WHERE id = ?",
                    (STATUS_APPROVED, expense_id),
                )
                status = STATUS_APPROVED
            elif status != STATUS_APPROVED:
                raise ValidationError(
                    f"当前状态不能批准：编号 {expense_id} 的状态为 {status}"
                )
    finally:
        conn.close()
    record: dict[str, object] = {
        "id": row[0],
        "submitter": row[1],
        "purpose": row[2],
        "amount_minor": row[3],
        "status": status,
    }
    # 与查询一致：仅带说明的记录追加该字段，无说明保持原有五个字段。
    if row[5] is not None:
        record["attachment_note"] = row[5]
    return record


def _write_csv(records: list[dict[str, object]]) -> None:
    """把报销单记录按 CSV 写入标准输出。

    使用标准库 csv 模块的最小引用规则：含逗号、双引号或换行的字段
    自动加双引号，字段内双引号写成两个双引号；每条记录以 CRLF 结束。
    金额（分）按完整十进制整数输出，状态与文本字段原样保留。
    """
    writer = csv.writer(sys.stdout, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for record in records:
        writer.writerow([record[field] for field in CSV_HEADER])


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "submit":
            # 先完成全部校验，再触碰数据库，失败提交不会新增任何记录。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            purpose = _clean_name("用途(purpose)", args.purpose)
            amount_minor = parse_amount(args.amount)
            # 附件说明校验排在提交人、用途、金额之后、数据库操作之前：
            # 显式空串或仅含空白时拒绝（退出码 2），且不创建数据库文件。
            attachment_note = None
            if args.attachment_note is not None:
                attachment_note = args.attachment_note.strip()
                if not attachment_note:
                    raise ValidationError("附件说明不能为空")
            record_id = _insert_expense(
                args.db, submitter, purpose, amount_minor, attachment_note
            )
            record: dict[str, object] = {
                "id": record_id,
                "submitter": submitter,
                "purpose": purpose,
                "amount_minor": amount_minor,
                "status": STATUS_PENDING,
            }
            # 仅在提供说明时追加字段；未提供的记录保持原有五个字段。
            if attachment_note is not None:
                record["attachment_note"] = attachment_note
        elif args.command == "list":
            submitter = _clean_name("提交人(submitter)", args.submitter)
            records = _query_expenses(args.db, submitter)
            print(json.dumps(records, ensure_ascii=False))
            return 0
        elif args.command == "export":
            submitter = _clean_name("提交人(submitter)", args.submitter)
            # 先取回全部记录再写 CSV：数据库失败时不输出任何内容（含表头）。
            records = _query_expenses(args.db, submitter)
            _write_csv(records)
            return 0
        elif args.command == "approve":
            # 编号校验先于数据库操作：编号无效时不创建数据库文件，
            # 编号与数据库路径同时无效时优先报告编号错误。
            expense_id = parse_expense_id(args.id)
            record = _approve_expense(args.db, expense_id)
        else:  # summary
            # 先完成全部参数校验（含预算），再触碰数据库：预算无效优先于
            # 数据库路径无效，且失败时不创建数据库文件。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            budget_minor = (
                parse_budget(args.budget) if args.budget is not None else None
            )
            result = _summarize_expenses(args.db, submitter, budget_minor)
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except ValidationError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"数据库操作失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
