"""命令行接口：提交、批准报销单、按提交人查询、汇总与导出。

入口：``python -m expense_desk --db <SQLite 文件> <submit|approve|list|summary|export> ...``

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

#: 报销单编号上限：SQLite INTEGER（64 位有符号整数主键）的最大值。
MAX_EXPENSE_ID = 2**63 - 1

#: CSV 导出的固定表头，与查询返回的字段顺序一致。
CSV_HEADER = ("id", "submitter", "purpose", "amount_minor", "status")

# 整数部分为一或多个 ASCII 数字；小数部分要么没有，要么为小数点加一至两位。
# 显式 ASCII 锚定，拒绝全角数字、科学计数法、正负号、千分位等写法。
_AMOUNT_RE = re.compile(r"\A([0-9]+)(?:\.([0-9]{1,2}))?\Z", re.ASCII)

#: 编号去除首尾空白后必须只由 ASCII 数字组成（允许前导零），数值范围再单独判断。
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
    """把缺少参数/参数值的报错改为明确指出参数名的中文提示。"""

    def error(self, message: str) -> None:
        if "attachment-note" in message and "expected one argument" in message:
            message = "参数 --attachment-note 缺少参数值"
        elif "--status" in message and "expected one argument" in message:
            message = "参数 --status 缺少参数值"
        elif "--id" in message and "expected one argument" in message:
            message = "参数 --id 缺少参数值"
        elif "required" in message:
            # 缺少必需参数：argparse 的信息里带有具体参数名（如 --db、--id）。
            if "--id" in message:
                message = "缺少必需参数 --id（报销单编号）"
            elif "--db" in message:
                message = "缺少必需参数 --db（SQLite 数据库文件路径）"
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
    """把 ``--id`` 的原始字符串解析为报销单编号正整数。

    去除首尾空白后只接受 ASCII 数字组成的字符串（允许前导零），数值须在
    1 至 :data:`MAX_EXPENSE_ID`（SQLite 64 位有符号整数上限）之间。
    空串、零、负数、小数、带正号、非 ASCII 数字（如全角数字）与越界值
    一律抛出 :class:`ValidationError`。

    超长数字串先按十进制字符串与上限比较，不直接交给 ``int()``：Python 的
    整数转换长度限制可能对超长输入抛出与编号校验无关的 ValueError。
    """
    text = raw.strip()
    if not text or _ID_RE.fullmatch(text) is None:
        raise ValidationError(
            "编号无效：去除首尾空白后只能由 ASCII 数字组成，且不能带正负号或小数点"
        )
    significant = text.lstrip("0")
    if not significant:
        # 全为零（含 "0" 与任意位数的前导零）：编号必须为正整数。
        raise ValidationError("编号无效：编号必须是不小于 1 的正整数")
    max_digits = str(MAX_EXPENSE_ID)
    if len(significant) > len(max_digits) or (
        len(significant) == len(max_digits) and significant > max_digits
    ):
        raise ValidationError(
            f"编号无效：编号不得超过 64 位有符号整数上限 ({MAX_EXPENSE_ID})"
        )
    return int(significant)


def _clean_name(field_label: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_label}去除首尾空白后不能为空")
    return cleaned


def parse_status(raw: str) -> str:
    """校验可选状态筛选值（list 与 summary 共用）。

    去除首尾空白后只接受区分大小写的 ``pending`` 与 ``approved``；空串、
    纯空白、大小写不同或其他取值一律抛出 :class:`ValidationError`。
    """
    text = raw.strip()
    if text not in (STATUS_PENDING, STATUS_APPROVED):
        raise ValidationError("状态无效：仅接受区分大小写的 pending 或 approved")
    return text


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
    listing.add_argument(
        "--status",
        help="可选状态筛选：仅接受区分大小写的 pending 或 approved",
    )

    approve = subparsers.add_parser("approve", help="按编号批准单张报销单")
    approve.add_argument("--id", required=True, help="报销单编号（1 至 64 位有符号整数上限的正整数）")

    summary = subparsers.add_parser("summary", help="按提交人汇总笔数与金额")
    summary.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")
    summary.add_argument(
        "--budget",
        help="可选的一次性预算参考（人民币元）；提供时额外返回预算分与余额分",
    )
    summary.add_argument(
        "--status",
        help="可选状态筛选：仅接受区分大小写的 pending 或 approved",
    )

    export = subparsers.add_parser("export", help="按提交人导出报销单 CSV 到标准输出")
    export.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")
    export.add_argument(
        "--status",
        help="可选状态筛选：仅接受区分大小写的 pending 或 approved",
    )

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


def _record_from_row(row: sqlite3.Row | tuple) -> dict[str, object]:
    """把一行六列查询结果组装为对外的记录字典。

    字段顺序与 list 单条记录一致：id、提交人、用途、整数分金额、状态；
    附件说明仅在非 NULL（带说明的新记录）时追加，旧记录保持五个字段。
    """
    record: dict[str, object] = {
        "id": row[0],
        "submitter": row[1],
        "purpose": row[2],
        "amount_minor": row[3],
        "status": row[4],
    }
    if row[5] is not None:
        record["attachment_note"] = row[5]
    return record


_EXPENSE_COLUMNS = (
    "id, submitter, purpose, amount_minor, status, attachment_note"
)


def _approve_expense(db_path: str, expense_id: int) -> dict[str, object]:
    """按编号把单张报销单由 pending 批准为 approved 并返回该记录。

    - 编号不存在（含父目录存在的新库）：抛出 :class:`ValidationError`
      （“报销单不存在”，退出码 2），不新增任何记录；
    - 当前状态为 approved：幂等返回同一记录，不执行写入、不改变记录；
    - 当前状态既不是 pending 也不是 approved：抛出 :class:`ValidationError`
      （“当前状态不能批准”，退出码 2），不改变记录；
    - pending：更新为 approved 并随事务提交，进程退出后新进程仍可读到。

    旧库（五列结构）先经 :func:`_ensure_schema` 补齐附件说明列，读取与
    返回均保留编号、提交人、用途、整数分金额与附件文本。
    """
    conn = _connect(db_path)
    try:
        with conn:  # 异常时自动回滚，保证失败批准不改任何记录
            _ensure_schema(conn)
            row = conn.execute(
                f"SELECT {_EXPENSE_COLUMNS} FROM expenses WHERE id = ?",
                (expense_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("报销单不存在")
            status = row[4]
            if status == STATUS_APPROVED:
                # 已批准：幂等成功，不更新、不改写任何字段。
                return _record_from_row(row)
            if status != STATUS_PENDING:
                raise ValidationError("当前状态不能批准")
            conn.execute(
                "UPDATE expenses SET status = ? WHERE id = ?",
                (STATUS_APPROVED, expense_id),
            )
            approved_row = (
                row[0], row[1], row[2], row[3], STATUS_APPROVED, row[5]
            )
            record = _record_from_row(approved_row)
    finally:
        conn.close()
    return record


def _query_expenses(
    db_path: str, submitter: str, status: str | None = None
) -> list[dict[str, object]]:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        if status is None:
            rows = conn.execute(
                f"SELECT {_EXPENSE_COLUMNS}"
                " FROM expenses WHERE submitter = ? ORDER BY id ASC",
                (submitter,),
            ).fetchall()
        else:
            # 状态筛选只作用于本次查询：记录须同时匹配提交人与状态。
            rows = conn.execute(
                f"SELECT {_EXPENSE_COLUMNS}"
                " FROM expenses WHERE submitter = ? AND status = ? ORDER BY id ASC",
                (submitter, status),
            ).fetchall()
    finally:
        conn.close()
    return [_record_from_row(row) for row in rows]


def _summarize_expenses(
    db_path: str,
    submitter: str,
    budget_minor: int | None = None,
    status: str | None = None,
) -> dict[str, object]:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        if status is None:
            # 省略状态筛选：沿用既有范围，只按提交人完整名称精确匹配。
            rows = conn.execute(
                "SELECT amount_minor FROM expenses WHERE submitter = ?",
                (submitter,),
            ).fetchall()
        else:
            # 状态筛选只作用于本次汇总：记录须同时匹配提交人与状态。
            rows = conn.execute(
                "SELECT amount_minor FROM expenses"
                " WHERE submitter = ? AND status = ?",
                (submitter, status),
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
        elif args.command == "approve":
            # 编号校验先于一切数据库操作：编号与数据库路径同时无效时优先报告
            # 编号错误（退出码 2），且不创建数据库文件。
            expense_id = parse_expense_id(args.id)
            record = _approve_expense(args.db, expense_id)
        elif args.command == "list":
            # 提交人校验先于状态校验，两者均在数据库操作前完成：提交人有效
            # 而状态与数据库路径同时无效时，优先报告状态错误，不创建数据库文件。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            status = (
                parse_status(args.status) if args.status is not None else None
            )
            records = _query_expenses(args.db, submitter, status)
            print(json.dumps(records, ensure_ascii=False))
            return 0
        elif args.command == "export":
            # 提交人校验先于状态校验，两者均在数据库操作前完成：提交人有效
            # 而状态与数据库路径同时无效时，优先报告状态错误，不创建数据库文件。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            status = (
                parse_status(args.status) if args.status is not None else None
            )
            # 先取回全部记录再写 CSV：数据库失败时不输出任何内容（含表头）。
            records = _query_expenses(args.db, submitter, status)
            _write_csv(records)
            return 0
        else:  # summary
            # 先完成全部参数校验，再触碰数据库：校验顺序为提交人、预算、
            # 状态。预算与状态同时无效时报告预算错误；其余参数有效而状态与
            # 数据库路径同时无效时报告状态错误，且失败时不创建数据库文件。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            budget_minor = (
                parse_budget(args.budget) if args.budget is not None else None
            )
            status = (
                parse_status(args.status) if args.status is not None else None
            )
            result = _summarize_expenses(
                args.db, submitter, budget_minor, status
            )
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
