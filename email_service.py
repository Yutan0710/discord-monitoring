"""Email notification helpers."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv


logger = logging.getLogger(__name__)


def get_smtp_config() -> tuple[str, int, str, str]:
    """Load SMTP settings from environment variables."""
    load_dotenv()

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not all((smtp_host, smtp_port, smtp_user, smtp_password)):
        raise RuntimeError(
            "SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD を .env に設定してください。"
        )

    try:
        port = int(smtp_port)
    except ValueError as error:
        raise RuntimeError("SMTP_PORT は数値で設定してください。") from error

    return smtp_host, port, smtp_user, smtp_password


def send_online_notification(
    to_email: str,
    username: str,
    online_time: str,
) -> None:
    """Send an online notification email via Gmail SMTP."""
    try:
        smtp_host, smtp_port, smtp_user, smtp_password = get_smtp_config()

        message = EmailMessage()
        message["From"] = smtp_user
        message["To"] = to_email
        message["Subject"] = f"【Discord】{username} がオンラインになりました"
        message.set_content(
            "\n".join(
                [
                    "Discordユーザーがオンラインになりました。",
                    "",
                    f"ユーザー名: {username}",
                    f"時刻: {online_time}",
                ]
            )
        )

        # Gmail SMTPではTLSを有効化してからログインします。
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)

        logger.info("オンライン通知メールを送信しました。to_email=%s", to_email)

    except (RuntimeError, smtplib.SMTPException, OSError):
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
    """Send an offline notification email via Gmail SMTP."""
    try:
        smtp_host, smtp_port, smtp_user, smtp_password = get_smtp_config()
        duration_text = format_duration(duration_seconds)

        message = EmailMessage()
        message["From"] = smtp_user
        message["To"] = to_email
        message["Subject"] = f"【Discord】{username} がオフラインになりました"
        message.set_content(
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

        # Gmail SMTPではTLSを有効化してからログインします。
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)

        logger.info("オフライン通知メールを送信しました。to_email=%s", to_email)

    except (RuntimeError, smtplib.SMTPException, OSError):
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
        smtp_host, smtp_port, smtp_user, smtp_password = get_smtp_config()
        duration_text = format_duration(total_duration_seconds)

        message = EmailMessage()
        message["From"] = smtp_user
        message["To"] = to_email
        message["Subject"] = f"【Discord】日次オンラインレポート {report_date}"
        message.set_content(
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
            message.add_attachment(
                graph_file.read(),
                maintype="image",
                subtype="png",
                filename=graph_path.name,
            )

        # Gmail SMTPではTLSを有効化してからログインします。
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)

        logger.info(
            "日次レポートメールを送信しました。to_email=%s report_date=%s",
            to_email,
            report_date,
        )

    except (RuntimeError, smtplib.SMTPException, OSError):
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
