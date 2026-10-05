"""命令行接口：提交报销单、按提交人查询。

入口：``python -m expense_desk --db <SQLite 文件> <submit|list> ...``

仅依赖 Python 3 标准库，金额以整数分（人民币）存储，不经过浮点运算。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from typing import Sequence

#: SQLite INTEGER 为 64 位有符号整数，金额（分）不得超过其上限。
MAX_AMOUNT_MINOR = 2**63 - 1

STATUS_PENDING = "pending"

# 整数部分为一或多个 ASCII 数字；小数部分要么没有，要么为小数点加一至两位。
# 显式 ASCII 锚定，拒绝全角数字、科学计数法、正负号、千分位等写法。
_AMOUNT_RE = re.compile(r"\A([0-9]+)(?:\.([0-9]{1,2}))?\Z", re.ASCII)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL
)
"""


class ValidationError(ValueError):
    """参数校验失败（退出码 2）。"""


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
    # 通过字符串拼接补到两位小数后整体转整数，避免任何浮点舍入。
    minor = int(whole + ((fraction + "00")[:2] if fraction is not None else "00"))
    if minor <= 0:
        raise ValidationError("金额必须大于零")
    if minor > MAX_AMOUNT_MINOR:
        raise ValidationError(
            "金额换算为分后超过 SQLite 64 位有符号整数上限 "
            f"({MAX_AMOUNT_MINOR})"
        )
    return minor


def _clean_name(field_label: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_label}去除首尾空白后不能为空")
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m expense_desk",
        description="本地费用报销台：提交报销单或按提交人查询。",
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

    listing = subparsers.add_parser("list", help="按提交人查询报销单")
    listing.add_argument("--submitter", required=True, help="提交人（完整名称精确匹配）")

    return parser


def _connect(db_path: str) -> sqlite3.Connection:
    # 相对路径按当前工作目录解释，由 sqlite3 直接处理。
    return sqlite3.connect(db_path)


def _insert_expense(
    db_path: str, submitter: str, purpose: str, amount_minor: int
) -> int:
    conn = _connect(db_path)
    try:
        with conn:  # 提交失败时自动回滚，保证失败提交不留记录
            conn.execute(_SCHEMA)
            cursor = conn.execute(
                "INSERT INTO expenses (submitter, purpose, amount_minor, status)"
                " VALUES (?, ?, ?, ?)",
                (submitter, purpose, amount_minor, STATUS_PENDING),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _query_expenses(db_path: str, submitter: str) -> list[dict[str, object]]:
    conn = _connect(db_path)
    try:
        conn.execute(_SCHEMA)
        rows = conn.execute(
            "SELECT id, submitter, purpose, amount_minor, status"
            " FROM expenses WHERE submitter = ? ORDER BY id ASC",
            (submitter,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row[0],
            "submitter": row[1],
            "purpose": row[2],
            "amount_minor": row[3],
            "status": row[4],
        }
        for row in rows
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "submit":
            # 先完成全部校验，再触碰数据库，失败提交不会新增任何记录。
            submitter = _clean_name("提交人(submitter)", args.submitter)
            purpose = _clean_name("用途(purpose)", args.purpose)
            amount_minor = parse_amount(args.amount)
            record_id = _insert_expense(
                args.db, submitter, purpose, amount_minor
            )
            record = {
                "id": record_id,
                "submitter": submitter,
                "purpose": purpose,
                "amount_minor": amount_minor,
                "status": STATUS_PENDING,
            }
        else:  # list
            submitter = _clean_name("提交人(submitter)", args.submitter)
            records = _query_expenses(args.db, submitter)
            print(json.dumps(records, ensure_ascii=False))
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
