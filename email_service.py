"""Email notification helpers."""

from __future__ import annotations

import base64
import logging
import os
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


logger = logging.getLogger(__name__)

RESEND_EMAILS_URL = "https://api.resend.com/emails"


def get_resend_config() -> tuple[str, str]:
    """Load Resend API settings from environment variables."""
    load_dotenv()

    resend_api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("FROM_EMAIL")

    if not resend_api_key or not from_email:
        raise RuntimeError(
            "RESEND_API_KEY と FROM_EMAIL を .env に設定してください。"
        )

    return resend_api_key, from_email


def send_email_via_resend(
    to_email: str,
    subject: str,
    body: str,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    """Send an email using the Resend HTTP API."""
    resend_api_key, from_email = get_resend_config()
    payload: dict[str, object] = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }

    if attachments:
        payload["attachments"] = attachments

    response = requests.post(
        RESEND_EMAILS_URL,
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            "Resend APIでメール送信に失敗しました。"
            f"status_code={response.status_code} response={response.text}"
        )


def send_online_notification(
    to_email: str,
    username: str,
    online_time: str,
) -> None:
    """Send an online notification email via Resend API."""
    try:
        subject = f"【Discord】{username} がオンラインになりました"
        body = (
            "\n".join(
                [
                    "Discordユーザーがオンラインになりました。",
                    "",
                    f"ユーザー名: {username}",
                    f"時刻: {online_time}",
                ]
            )
        )
        send_email_via_resend(to_email, subject, body)

        logger.info("オンライン通知メールを送信しました。to_email=%s", to_email)

    except (RuntimeError, requests.RequestException):
        logger.exception(
            "オンライン通知メールの送信に失敗しました。to_email=%s username=%s",
            to_email,
            username,
        )


def format_duration(duration_seconds: int | None) -> str:
    """Format online duration for notification emails."""
    if duration_seconds is None:
        return "時間不明"

    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}時間{minutes}分{seconds}秒"

    if minutes > 0:
        return f"{minutes}分{seconds}秒"

    return f"{seconds}秒"


def send_offline_notification(
    to_email: str,
    username: str,
    offline_time: str,
    duration_seconds: int | None,
) -> None:
    """Send an offline notification email via Resend API."""
    try:
        duration_text = format_duration(duration_seconds)
        subject = f"【Discord】{username} がオフラインになりました"
        body = (
            "\n".join(
                [
                    "Discordユーザーがオフラインになりました。",
                    "",
                    f"ユーザー名: {username}",
                    f"時刻: {offline_time}",
                    f"オンライン時間: {duration_text}",
                ]
            )
        )
        send_email_via_resend(to_email, subject, body)

        logger.info("オフライン通知メールを送信しました。to_email=%s", to_email)

    except (RuntimeError, requests.RequestException):
        logger.exception(
            "オフライン通知メールの送信に失敗しました。to_email=%s username=%s",
            to_email,
            username,
        )


def send_daily_report(
    to_email: str,
    username: str,
    report_date: date,
    total_duration_seconds: int,
    graph_path: Path,
) -> None:
    """Send a daily online report email with a PNG graph attachment."""
    try:
        duration_text = format_duration(total_duration_seconds)
        subject = f"【Discord】日次オンラインレポート {report_date}"
        body = (
            "\n".join(
                [
                    "Discordオンライン日次レポートです。",
                    "",
                    f"ユーザー名: {username}",
                    f"対象日: {report_date}",
                    f"合計オンライン時間: {duration_text}",
                ]
            )
        )

        with graph_path.open("rb") as graph_file:
            graph_content = base64.b64encode(graph_file.read()).decode("ascii")

        send_email_via_resend(
            to_email=to_email,
            subject=subject,
            body=body,
            attachments=[
                {
                    "filename": graph_path.name,
                    "content": graph_content,
                }
            ],
        )

        logger.info(
            "日次レポートメールを送信しました。to_email=%s report_date=%s",
            to_email,
            report_date,
        )

    except (RuntimeError, requests.RequestException, OSError):
        logger.exception(
            "日次レポートメールの送信に失敗しました。to_email=%s username=%s",
            to_email,
            username,
        )


def get_test_email() -> str:
    """Load the test email recipient from the environment."""
    load_dotenv()

    test_email = os.getenv("TEST_EMAIL")
    if not test_email:
        raise RuntimeError("TEST_EMAIL が設定されていません。.env に追加してください。")

    return test_email


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        send_online_notification(
            to_email=get_test_email(),
            username="テストユーザー",
            online_time=datetime.now().astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
        )
    except RuntimeError:
        logger.exception("テストメール送信の設定読み込みに失敗しました。")
