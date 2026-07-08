"""Database access helpers for monitored Discord users."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg2
from dotenv import load_dotenv


logger = logging.getLogger(__name__)


def get_jst_timezone() -> tzinfo:
    """Return Asia/Tokyo timezone with a Windows-friendly fallback."""
    try:
        return ZoneInfo("Asia/Tokyo")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=9), "JST")


@dataclass(frozen=True, slots=True)
class MonitoredUser:
    """A monitored Discord user and their notification destination."""

    discord_user_id: int
    username: str
    email: str


@dataclass(frozen=True, slots=True)
class OnlineInterval:
    """A clipped online interval in JST for a daily report."""

    online_at: datetime
    offline_at: datetime


@dataclass(frozen=True, slots=True)
class DailyOnlineReport:
    """Daily online summary for a monitored Discord user."""

    discord_user_id: int
    username: str
    email: str
    report_date: date
    total_duration_seconds: int
    hourly_duration_seconds: tuple[int, ...]
    online_intervals: tuple[OnlineInterval, ...]


def to_utc_naive(value: datetime) -> datetime:
    """Convert a datetime to UTC without timezone for database storage."""
    if value.tzinfo is None:
        return value

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_jst_day_range_utc_naive(target_date: date) -> tuple[datetime, datetime]:
    """Return the target JST day range as UTC naive datetimes."""
    jst = get_jst_timezone()
    start_jst = datetime.combine(target_date, time.min, tzinfo=jst)
    end_jst = datetime.combine(target_date, time.max, tzinfo=jst)

    return to_utc_naive(start_jst), to_utc_naive(end_jst)


def get_database_url() -> str:
    """Load the PostgreSQL connection URL from the environment."""
    load_dotenv()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL が設定されていません。.env に DATABASE_URL を追加してください。"
        )

    return database_url


def get_user_by_discord_id(discord_user_id: int) -> MonitoredUser | None:
    """Fetch a monitored user by Discord user ID."""
    query = """
        SELECT discord_user_id, username, email
        FROM monitored_users
        WHERE discord_user_id = %s
        LIMIT 1
    """

    try:
        database_url = get_database_url()

        # with文で接続とカーソルを管理し、正常終了時は自動でクローズします。
        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (discord_user_id,))
                row = cursor.fetchone()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "monitored_users からユーザー情報を取得できませんでした。discord_user_id=%s",
            discord_user_id,
        )
        return None

    if row is None:
        logger.info(
            "監視対象ユーザーが見つかりませんでした。discord_user_id=%s",
            discord_user_id,
        )
        return None

    logger.info(
        "監視対象ユーザーを取得しました。discord_user_id=%s",
        discord_user_id,
    )

    return MonitoredUser(
        discord_user_id=int(row[0]),
        username=str(row[1]),
        email=str(row[2]),
    )


def get_notification_targets(monitored_discord_user_id: int) -> list[int]:
    """Fetch active Discord user IDs that should receive notifications."""
    query = """
        SELECT notify_discord_user_id
        FROM notification_targets
        WHERE monitored_discord_user_id = %s
          AND is_active = true
        ORDER BY notify_discord_user_id
    """

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (monitored_discord_user_id,))
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "通知先ユーザーの取得に失敗しました。monitored_discord_user_id=%s",
            monitored_discord_user_id,
        )
        return []

    target_user_ids = [int(row[0]) for row in rows]
    logger.info(
        "通知先ユーザーを取得しました。monitored_discord_user_id=%s count=%s",
        monitored_discord_user_id,
        len(target_user_ids),
    )

    return target_user_ids


def create_online_log(discord_user_id: int, online_at: datetime) -> None:
    """Create a new online log entry."""
    query = """
        INSERT INTO online_logs (discord_user_id, online_at)
        VALUES (%s, %s)
    """

    try:
        database_url = get_database_url()
        stored_online_at = to_utc_naive(online_at)

        # DB保存用の時刻はUTC naiveに統一し、JSTとの差分混入を防ぎます。
        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (discord_user_id, stored_online_at))

        logger.info(
            "オンラインログを作成しました。discord_user_id=%s online_at=%s",
            discord_user_id,
            stored_online_at,
        )
    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "オンラインログの作成に失敗しました。discord_user_id=%s",
            discord_user_id,
        )


def close_latest_online_log(
    discord_user_id: int,
    offline_at: datetime,
) -> int | None:
    """Close the latest open online log and return duration in seconds."""
    select_query = """
        SELECT id, online_at
        FROM online_logs
        WHERE discord_user_id = %s
          AND offline_at IS NULL
        ORDER BY online_at DESC
        LIMIT 1
        FOR UPDATE
    """
    update_query = """
        UPDATE online_logs
        SET offline_at = %s,
            duration_seconds = %s
        WHERE id = %s
    """

    try:
        database_url = get_database_url()
        stored_offline_at = to_utc_naive(offline_at)

        # 未クローズの最新ログのみを対象にし、重複更新を避けます。
        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(select_query, (discord_user_id,))
                row = cursor.fetchone()

                if row is None:
                    logger.info(
                        "未クローズのオンラインログが見つかりませんでした。"
                        "discord_user_id=%s",
                        discord_user_id,
                    )
                    return None

                online_log_id = int(row[0])
                online_at = row[1]
                stored_online_at = to_utc_naive(online_at)

                duration_seconds = max(
                    0,
                    int((stored_offline_at - stored_online_at).total_seconds()),
                )
                cursor.execute(
                    update_query,
                    (stored_offline_at, duration_seconds, online_log_id),
                )

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "オンラインログのクローズに失敗しました。discord_user_id=%s",
            discord_user_id,
        )
        return None
    logger.info(
        "オンラインログをクローズしました。discord_user_id=%s duration_seconds=%s",
        discord_user_id,
        duration_seconds,
    )

    return duration_seconds


def get_daily_online_reports(target_date: date) -> list[DailyOnlineReport]:
    """Fetch daily online summaries for all monitored users."""
    query = """
        SELECT
            monitored_users.discord_user_id,
            monitored_users.username,
            monitored_users.email,
            COALESCE(SUM(
                EXTRACT(EPOCH FROM (
                    LEAST(COALESCE(online_logs.offline_at, %s), %s)
                    - GREATEST(online_logs.online_at, %s)
                ))
            ), 0)::integer AS total_duration_seconds
        FROM monitored_users
        LEFT JOIN online_logs
            ON online_logs.discord_user_id = monitored_users.discord_user_id
           AND online_logs.online_at <= %s
           AND COALESCE(online_logs.offline_at, %s) >= %s
        GROUP BY
            monitored_users.discord_user_id,
            monitored_users.username,
            monitored_users.email
        ORDER BY monitored_users.discord_user_id
    """

    start_utc, end_utc = get_jst_day_range_utc_naive(target_date)

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (end_utc, end_utc, start_utc, end_utc, end_utc, start_utc),
                )
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception("日次オンラインレポートの取得に失敗しました。")
        return []

    reports: list[DailyOnlineReport] = []
    for row in rows:
        discord_user_id = int(row[0])
        hourly_duration_seconds = get_hourly_online_durations(
            discord_user_id,
            target_date,
        )
        online_intervals = get_daily_online_intervals(
            discord_user_id,
            target_date,
        )
        reports.append(
            DailyOnlineReport(
                discord_user_id=discord_user_id,
                username=str(row[1]),
                email=str(row[2]),
                report_date=target_date,
                total_duration_seconds=max(0, int(row[3])),
                hourly_duration_seconds=hourly_duration_seconds,
                online_intervals=online_intervals,
            )
        )

    logger.info(
        "日次オンラインレポートを取得しました。target_date=%s count=%s",
        target_date,
        len(reports),
    )

    return reports


def get_daily_online_intervals(
    discord_user_id: int,
    target_date: date,
) -> tuple[OnlineInterval, ...]:
    """Fetch online intervals clipped to the target JST date."""
    query = """
        SELECT online_at, COALESCE(offline_at, %s)
        FROM online_logs
        WHERE discord_user_id = %s
          AND online_at <= %s
          AND COALESCE(offline_at, %s) >= %s
        ORDER BY online_at
    """

    start_utc, end_utc = get_jst_day_range_utc_naive(target_date)

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (end_utc, discord_user_id, end_utc, end_utc, start_utc),
                )
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "日次オンライン区間の取得に失敗しました。discord_user_id=%s",
            discord_user_id,
        )
        return ()

    jst = get_jst_timezone()
    day_start = datetime.combine(target_date, time.min, tzinfo=jst)
    day_end = day_start + timedelta(days=1)
    intervals: list[OnlineInterval] = []

    for online_at, offline_at in rows:
        online_jst = to_utc_naive(online_at).replace(
            tzinfo=timezone.utc
        ).astimezone(jst)
        offline_jst = to_utc_naive(offline_at).replace(
            tzinfo=timezone.utc
        ).astimezone(jst)

        interval_start = max(online_jst, day_start)
        interval_end = min(offline_jst, day_end)
        if interval_end <= interval_start:
            continue

        intervals.append(
            OnlineInterval(
                online_at=interval_start,
                offline_at=interval_end,
            )
        )

    return tuple(intervals)
def get_hourly_online_durations(
    discord_user_id: int,
    target_date: date,
) -> tuple[int, ...]:
    """Calculate hourly online durations for a user on a JST date."""
    query = """
        SELECT online_at, offline_at
        FROM online_logs
        WHERE discord_user_id = %s
          AND online_at <= %s
          AND COALESCE(offline_at, %s) >= %s
    """

    start_utc, end_utc = get_jst_day_range_utc_naive(target_date)
    hourly_seconds = [0 for _ in range(24)]

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (discord_user_id, end_utc, end_utc, start_utc),
                )
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "時間別オンライン時間の取得に失敗しました。discord_user_id=%s",
            discord_user_id,
        )
        return tuple(hourly_seconds)

    jst = get_jst_timezone()
    day_start = datetime.combine(target_date, time.min, tzinfo=jst)
    day_end = datetime.combine(target_date, time.max, tzinfo=jst)

    for online_at, offline_at in rows:
        online_jst = to_utc_naive(online_at).replace(
            tzinfo=timezone.utc
        ).astimezone(jst)
        offline_source = offline_at or end_utc
        offline_jst = to_utc_naive(offline_source).replace(
            tzinfo=timezone.utc
        ).astimezone(jst)

        interval_start = max(online_jst, day_start)
        interval_end = min(offline_jst, day_end)
        if interval_end <= interval_start:
            continue

        for hour in range(24):
            hour_start = datetime.combine(
                target_date,
                time(hour=hour),
                tzinfo=jst,
            )
            hour_end = (
                day_end
                if hour == 23
                else datetime.combine(
                    target_date,
                    time(hour=hour + 1),
                    tzinfo=jst,
                )
            )
            overlap_start = max(interval_start, hour_start)
            overlap_end = min(interval_end, hour_end)

            if overlap_end > overlap_start:
                hourly_seconds[hour] += int(
                    (overlap_end - overlap_start).total_seconds()
                )

    return tuple(hourly_seconds)
