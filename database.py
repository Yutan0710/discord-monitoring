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


@dataclass(frozen=True, slots=True)
class PeriodOnlineReport:
    """Online summary for a multi-day report period."""

    discord_user_id: int
    username: str
    email: str
    period_start: datetime
    period_end: datetime
    daily_duration_seconds: tuple[int, ...]
    total_duration_seconds: int
    online_count: int
    longest_duration_seconds: int


def to_utc_naive(value: datetime) -> datetime:
    """Convert a datetime to UTC without timezone for database storage."""
    if value.tzinfo is None:
        return value

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_jst_day_range_utc_naive(target_date: date) -> tuple[datetime, datetime]:
    """Return the target JST day range as UTC naive datetimes."""
    jst = get_jst_timezone()
    start_jst = datetime.combine(target_date, time.min, tzinfo=jst)
    end_jst = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=jst)

    return to_utc_naive(start_jst), to_utc_naive(end_jst)


def from_utc_naive_to_jst(value: datetime) -> datetime:
    """Convert a UTC naive database value to a JST aware datetime."""
    return to_utc_naive(value).replace(tzinfo=timezone.utc).astimezone(
        get_jst_timezone()
    )


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


def get_all_monitored_users() -> list[MonitoredUser]:
    """Fetch all monitored Discord users."""
    query = """
        SELECT discord_user_id, username, email
        FROM monitored_users
        ORDER BY discord_user_id
    """

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception("monitored_users の全件取得に失敗しました。")
        return []

    users = [
        MonitoredUser(
            discord_user_id=int(row[0]),
            username=str(row[1]),
            email=str(row[2]),
        )
        for row in rows
    ]
    logger.info("監視対象ユーザー一覧を取得しました。count=%s", len(users))

    return users


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
           AND online_logs.online_at < %s
           AND COALESCE(online_logs.offline_at, %s) > %s
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
          AND online_at < %s
          AND COALESCE(offline_at, %s) > %s
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
        online_jst = from_utc_naive_to_jst(online_at)
        offline_jst = from_utc_naive_to_jst(offline_at)

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
          AND online_at < %s
          AND COALESCE(offline_at, %s) > %s
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
    day_end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=jst)

    for online_at, offline_at in rows:
        online_jst = from_utc_naive_to_jst(online_at)
        offline_source = offline_at or end_utc
        offline_jst = from_utc_naive_to_jst(offline_source)

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


def get_period_online_reports(
    period_start: datetime,
    period_end: datetime,
) -> list[PeriodOnlineReport]:
    """Fetch multi-day online summaries for all monitored users."""
    query = """
        SELECT
            monitored_users.discord_user_id,
            monitored_users.username,
            monitored_users.email,
            online_logs.online_at,
            online_logs.offline_at
        FROM monitored_users
        LEFT JOIN online_logs
            ON online_logs.discord_user_id = monitored_users.discord_user_id
           AND online_logs.online_at < %s
           AND COALESCE(online_logs.offline_at, %s) > %s
        ORDER BY monitored_users.discord_user_id, online_logs.online_at
    """

    if period_end <= period_start:
        raise ValueError("period_end must be after period_start.")

    jst = get_jst_timezone()
    period_start_jst = period_start.astimezone(jst)
    period_end_jst = period_end.astimezone(jst)
    period_start_utc = to_utc_naive(period_start_jst)
    period_end_utc = to_utc_naive(period_end_jst)
    open_log_end_utc = min(
        period_end_utc,
        datetime.now(timezone.utc).replace(tzinfo=None),
    )
    day_count = (period_end_jst.date() - period_start_jst.date()).days

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (period_end_utc, period_end_utc, period_start_utc),
                )
                rows = cursor.fetchall()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception(
            "期間オンラインレポートの取得に失敗しました。"
            "period_start=%s period_end=%s",
            period_start_jst,
            period_end_jst,
        )
        return []

    report_map: dict[int, dict[str, object]] = {}
    for row in rows:
        discord_user_id = int(row[0])
        if discord_user_id not in report_map:
            report_map[discord_user_id] = {
                "username": str(row[1]),
                "email": str(row[2]),
                "daily_seconds": [0 for _ in range(day_count)],
                "online_count": 0,
                "longest_seconds": 0,
            }

        online_at = row[3]
        if online_at is None:
            continue

        offline_at = row[4] or open_log_end_utc
        online_jst = from_utc_naive_to_jst(online_at)
        offline_jst = from_utc_naive_to_jst(offline_at)
        interval_start = max(online_jst, period_start_jst)
        interval_end = min(offline_jst, period_end_jst)
        if interval_end <= interval_start:
            continue

        report_data = report_map[discord_user_id]
        daily_seconds = report_data["daily_seconds"]
        if not isinstance(daily_seconds, list):
            continue

        clipped_seconds = int((interval_end - interval_start).total_seconds())
        report_data["online_count"] = int(report_data["online_count"]) + 1
        report_data["longest_seconds"] = max(
            int(report_data["longest_seconds"]),
            clipped_seconds,
        )

        for day_index in range(day_count):
            day_start = period_start_jst + timedelta(days=day_index)
            day_end = day_start + timedelta(days=1)
            overlap_start = max(interval_start, day_start)
            overlap_end = min(interval_end, day_end)
            if overlap_end > overlap_start:
                daily_seconds[day_index] += int(
                    (overlap_end - overlap_start).total_seconds()
                )

    reports: list[PeriodOnlineReport] = []
    for discord_user_id, report_data in report_map.items():
        daily_seconds = report_data["daily_seconds"]
        if not isinstance(daily_seconds, list):
            daily_seconds = []

        total_seconds = sum(int(value) for value in daily_seconds)
        reports.append(
            PeriodOnlineReport(
                discord_user_id=discord_user_id,
                username=str(report_data["username"]),
                email=str(report_data["email"]),
                period_start=period_start_jst,
                period_end=period_end_jst,
                daily_duration_seconds=tuple(int(value) for value in daily_seconds),
                total_duration_seconds=total_seconds,
                online_count=int(report_data["online_count"]),
                longest_duration_seconds=int(report_data["longest_seconds"]),
            )
        )

    logger.info(
        "期間オンラインレポートを取得しました。period_start=%s period_end=%s count=%s",
        period_start_jst,
        period_end_jst,
        len(reports),
    )

    return reports


def get_online_logs_timestamp_type() -> str | None:
    """Return the database type of online_logs.online_at."""
    query = """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'online_logs'
          AND column_name = 'online_at'
        LIMIT 1
    """

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                row = cursor.fetchone()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception("online_logs.online_at の型確認に失敗しました。")
        return None

    return str(row[0]) if row else None


def count_old_online_logs() -> int:
    """Count finished online logs older than six months."""
    timestamp_type = get_online_logs_timestamp_type()
    if timestamp_type == "timestamp with time zone":
        query = """
            SELECT COUNT(*)
            FROM online_logs
            WHERE offline_at IS NOT NULL
              AND online_at < NOW() - INTERVAL '6 months'
        """
    else:
        query = """
            SELECT COUNT(*)
            FROM online_logs
            WHERE offline_at IS NOT NULL
              AND online_at < (
                  (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '6 months'
              )
        """

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                row = cursor.fetchone()

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception("削除対象オンラインログ件数の取得に失敗しました。")
        return 0

    return int(row[0]) if row else 0


def delete_old_online_logs() -> int:
    """Delete finished online logs older than six months and return count."""
    timestamp_type = get_online_logs_timestamp_type()
    if timestamp_type == "timestamp with time zone":
        query = """
            DELETE FROM online_logs
            WHERE offline_at IS NOT NULL
              AND online_at < NOW() - INTERVAL '6 months'
        """
    else:
        query = """
            DELETE FROM online_logs
            WHERE offline_at IS NOT NULL
              AND online_at < (
                  (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '6 months'
              )
        """

    try:
        database_url = get_database_url()

        with psycopg2.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                deleted_count = cursor.rowcount

    except RuntimeError:
        logger.exception("データベース設定の読み込みに失敗しました。")
        raise
    except psycopg2.Error:
        logger.exception("古いオンラインログの削除に失敗しました。")
        return 0

    logger.info("古いオンラインログを削除しました。deleted_count=%s", deleted_count)

    return deleted_count

