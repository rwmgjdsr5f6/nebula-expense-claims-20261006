"""expense_desk 命令行入口。

用法：
    python -m expense_desk --db <SQLite 文件> submit --submitter <提交人> --purpose <用途> --amount <金额>
    python -m expense_desk --db <SQLite 文件> list --submitter <提交人>

退出码：0 成功；2 参数缺失或字段校验失败；1 数据库操作失败。
"""

import argparse
import json
import re
import sqlite3
import sys

STATUS_PENDING = "pending"

# SQLite 64 位有符号整数上限
MAX_INT64 = 9223372036854775807

# 金额：ASCII 数字整数，或整数部分加小数点及一至两位小数
_AMOUNT_RE = re.compile(r"^([0-9]+)(?:\.([0-9]{1,2}))?$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter TEXT NOT NULL,
    purpose TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    status TEXT NOT NULL
);
"""


def fail_validation(field, reason):
    """字段校验失败：标准错误说明字段及原因，退出码 2。"""
    print(f"错误：参数 {field} 无效：{reason}", file=sys.stderr)
    sys.exit(2)


def fail_database(operation, exc):
    """数据库操作失败：标准错误说明失败，退出码 1。"""
    print(f"错误：数据库操作失败（{operation}）：{exc}", file=sys.stderr)
    sys.exit(1)


def parse_amount(raw):
    """把金额字符串解析为整数分。

    只接受 ASCII 数字整数，或整数部分加小数点及一至两位小数；
    数值必须大于零且整数分不超过 64 位有符号整数上限。
    """
    text = raw.strip()
    match = _AMOUNT_RE.match(text)
    if not match:
        fail_validation(
            "--amount",
            f"{raw!r} 不是有效金额，只接受非负整数或最多两位小数的十进制数（如 12 或 12.30）",
        )
    yuan = int(match.group(1))
    fraction = match.group(2)
    cents = int(fraction.ljust(2, "0")) if fraction else 0
    amount_minor = yuan * 100 + cents
    if amount_minor <= 0:
        fail_validation("--amount", "金额必须大于零")
    if amount_minor > MAX_INT64:
        fail_validation("--amount", "金额超出可表示范围（整数分不得超过 9223372036854775807）")
    return amount_minor


def parse_submitter(raw):
    """提交人去除首尾空白后不能为空。"""
    submitter = raw.strip()
    if not submitter:
        fail_validation("--submitter", "提交人去除首尾空白后不能为空")
    return submitter


def parse_purpose(raw):
    """用途去除首尾空白后不能为空。"""
    purpose = raw.strip()
    if not purpose:
        fail_validation("--purpose", "用途去除首尾空白后不能为空")
    return purpose


def open_database(path):
    """打开（必要时创建）数据库并确保表结构存在。"""
    try:
        conn = sqlite3.connect(path)
        conn.execute(_SCHEMA)
        conn.commit()
    except sqlite3.Error as exc:
        fail_database("无法创建或打开数据库", exc)
    return conn


def cmd_submit(db_path, args):
    submitter = parse_submitter(args.submitter)
    purpose = parse_purpose(args.purpose)
    amount_minor = parse_amount(args.amount)

    conn = open_database(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO expenses (submitter, purpose, amount_minor, status) VALUES (?, ?, ?, ?)",
            (submitter, purpose, amount_minor, STATUS_PENDING),
        )
        conn.commit()
        record_id = cursor.lastrowid
    except sqlite3.Error as exc:
        conn.rollback()
        fail_database("写入报销单失败", exc)
    finally:
        conn.close()

    record = {
        "id": record_id,
        "submitter": submitter,
        "purpose": purpose,
        "amount_minor": amount_minor,
        "status": STATUS_PENDING,
    }
    print(json.dumps(record, ensure_ascii=False))


def cmd_list(db_path, args):
    submitter = parse_submitter(args.submitter)

    conn = open_database(db_path)
    try:
        rows = conn.execute(
            "SELECT id, submitter, purpose, amount_minor, status FROM expenses"
            " WHERE submitter = ? ORDER BY id ASC",
            (submitter,),
        ).fetchall()
    except sqlite3.Error as exc:
        fail_database("读取报销单失败", exc)
    finally:
        conn.close()

    records = [
        {
            "id": row[0],
            "submitter": row[1],
            "purpose": row[2],
            "amount_minor": row[3],
            "status": row[4],
        }
        for row in rows
    ]
    print(json.dumps(records, ensure_ascii=False))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="expense_desk",
        description="本地费用报销台：提交报销单并按提交人查询",
    )
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径（相对路径按当前工作目录解释）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("submit", help="提交一条报销单")
    submit_parser.add_argument("--submitter", required=True, help="提交人姓名")
    submit_parser.add_argument("--purpose", required=True, help="费用用途")
    submit_parser.add_argument("--amount", required=True, help="金额（人民币元，整数或最多两位小数）")

    list_parser = subparsers.add_parser("list", help="按提交人查询报销单")
    list_parser.add_argument("--submitter", required=True, help="提交人姓名（完整名称精确匹配）")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "submit":
        cmd_submit(args.db, args)
    else:
        cmd_list(args.db, args)


if __name__ == "__main__":
    main()
