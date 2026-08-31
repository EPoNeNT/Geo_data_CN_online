#!/usr/bin/env python3
"""Send administrator and applicant notifications for series change requests."""

import argparse
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


def ensure_decision_notification_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE series_change_requests
            ADD COLUMN IF NOT EXISTS decision_notified_at TIMESTAMPTZ NULL
            """
        )
    conn.commit()


def get_pending_decision_notification(conn, request_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, contact_email, target_series, target_city,
                   proposed_series, proposed_city, review_note
            FROM series_change_requests
            WHERE id = %s
              AND status IN ('applied', 'rejected')
              AND NULLIF(TRIM(contact_email), '') IS NOT NULL
              AND decision_notified_at IS NULL
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def mark_decision_notified(conn, request_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE series_change_requests
            SET decision_notified_at = NOW()
            WHERE id = %s
              AND decision_notified_at IS NULL
            """,
            (request_id,),
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


def build_decision_email_body(request: dict) -> str:
    decision = "已通过" if request["status"] == "applied" else "未通过"
    series_name = request["proposed_series"] or request["target_series"]
    city = request["proposed_city"] or request["target_city"]
    lines = [
        "您好：",
        "",
        f"您提交的系列申请（编号 #{request['id']}）已审核{decision}。",
        f"系列：{series_name}（{city}）",
    ]
    if request["status"] == "rejected" and request["review_note"]:
        lines.append(f"审核说明：{request['review_note']}")
    lines.extend(["", "此邮件由 Geodataing 系列审核后台自动发送，请勿直接回复。"])
    return "\n".join(lines)


def send_decision_email(request: dict) -> None:
    decision = "已通过" if request["status"] == "applied" else "未通过"
    message = EmailMessage()
    message.set_content(build_decision_email_body(request))
    message["Subject"] = f"[Geodataing] 系列申请{decision}"
    message["From"] = SENDER
    message["To"] = request["contact_email"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SENDER, PASSWORD)
        smtp.send_message(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision-request-id", type=int)
    return parser.parse_args()


def main():
    import time
    args = parse_args()
    if args.decision_request_id is not None and args.decision_request_id < 1:
        raise ValueError("decision request id must be positive")
    last_error = None
    for attempt in range(3):
        try:
            conn = connect_postgres(DATABASE_URL, connect_timeout=10)
            break
        except Exception as e:
            last_error = e
            print(f"连接失败 (尝试 {attempt+1}/3): {e}")
            time.sleep(5)
    else:
        raise last_error
    try:
        if args.decision_request_id is not None:
            ensure_decision_notification_schema(conn)
            request = get_pending_decision_notification(conn, args.decision_request_id)
            if request is None:
                print(f"No decision notification to send for request {args.decision_request_id}")
                return
            send_decision_email(request)
            mark_decision_notified(conn, request["id"])
            print(f"Decision email sent for request {request['id']}")
            return

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
