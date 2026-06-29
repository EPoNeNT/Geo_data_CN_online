#!/usr/bin/env python3
"""Check for unnotified series change requests and send email notification."""

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent

from runtime_utils import connect_postgres


SENDER = os.environ["NOTIFY_SENDER"]
PASSWORD = os.environ["NOTIFY_PASSWORD"]
RECEIVER = os.environ["NOTIFY_RECEIVER"]
DATABASE_URL = os.environ["DATABASE_URL"]


def get_unnotified_requests(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.status, r.actor, r.target_series, r.target_city,
                   r.proposed_series, r.proposed_city, r.note,
                   COUNT(i.id) AS item_count
            FROM series_change_requests r
            LEFT JOIN series_change_request_items i ON i.request_id = r.id
            WHERE r.notified = FALSE
            GROUP BY r.id
            ORDER BY r.id
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_notified(conn, ids: list[int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE series_change_requests SET notified = TRUE WHERE id = ANY(%s)",
            (ids,),
        )
    conn.commit()


def build_email_body(requests: list[dict]) -> str:
    if not requests:
        return ""
    lines = [f"共 {len(requests)} 条新的系列修改请求:\n"]
    for r in requests:
        lines.append(f"--- 请求 #{r['id']} ---")
        lines.append(f"  提交者: {r['actor']}")
        lines.append(f"  状态:   {r['status']}")
        lines.append(f"  目标:   {r['target_series']} ({r['target_city']})")
        if r['proposed_series']:
            lines.append(f"  提议:   {r['proposed_series']} ({r['proposed_city']})")
        if r['note']:
            lines.append(f"  备注:   {r['note']}")
        lines.append(f"  涉及:   {r['item_count']} 个缓存")
        lines.append("")
    return "\n".join(lines)


def main():
    conn = connect_postgres(DATABASE_URL, connect_timeout=10)
    try:
        requests = get_unnotified_requests(conn)
        if not requests:
            print("No new requests to notify")
            return

        body = build_email_body(requests)

        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"] = f"[Geo-data] {len(requests)} 条新的系列修改请求"
        msg["From"] = SENDER
        msg["To"] = RECEIVER

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SENDER, PASSWORD)
            s.send_message(msg)

        print(f"Email sent for {len(requests)} requests")
        mark_notified(conn, [r["id"] for r in requests])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
