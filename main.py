"""Discord bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import (
    close_latest_online_log,
    count_old_online_logs,
    create_online_log,
    DailyOnlineReport,
    delete_old_online_logs,
    get_all_monitored_users,
    get_daily_online_reports,
    get_notification_targets,
    get_period_online_reports,
    get_user_by_discord_id,
    OnlineInterval,
    PeriodOnlineReport,
)


# ログ設定を行い、起動時やエラー発生時の状況を確認しやすくします。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

try:
    JST = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    JST = timezone(timedelta(hours=9), "JST")

DAILY_REPORT_TIME = time(hour=0, minute=5, tzinfo=JST)
WEEKLY_REPORT_TIME = time(hour=0, minute=10, tzinfo=JST)
MONTHLY_REPORT_TIME = time(hour=0, minute=15, tzinfo=JST)
CLEANUP_TIME = time(hour=0, minute=30, tzinfo=JST)


def format_display_username(username: str) -> str:
    """Return the fixed display name used in Discord notifications."""
    return unicodedata.normalize("NFC", "星野拓海")


def format_duration_minutes(duration_seconds: int) -> str:
    """Format a duration without seconds for daily report images."""
    hours, remainder = divmod(max(0, duration_seconds), 3600)
    minutes = remainder // 60

    return f"{hours}時間{minutes:02d}分"


def format_report_date(report_date: date) -> str:
    """Format a report date with a Japanese weekday."""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    return report_date.strftime("%Y/%m/%d") + f" ({weekdays[report_date.weekday()]})"




def format_report_period(period_start: datetime, period_end: datetime) -> str:
    """Format a half-open report period as JST dates."""
    start_date = period_start.astimezone(JST).date()
    end_date = (period_end.astimezone(JST) - timedelta(days=1)).date()

    return f"{start_date:%Y/%m/%d} - {end_date:%Y/%m/%d}"


def get_previous_week_range(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the previous Monday-to-Monday JST range."""
    current = reference.astimezone(JST) if reference else datetime.now(JST)
    this_monday = current.date() - timedelta(days=current.weekday())
    period_end = datetime.combine(this_monday, time.min, tzinfo=JST)
    period_start = period_end - timedelta(days=7)

    return period_start, period_end


def get_previous_month_range(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the previous calendar month JST range."""
    current = reference.astimezone(JST) if reference else datetime.now(JST)
    period_end = datetime.combine(
        current.date().replace(day=1),
        time.min,
        tzinfo=JST,
    )
    previous_month_last_day = period_end.date() - timedelta(days=1)
    period_start = datetime.combine(
        previous_month_last_day.replace(day=1),
        time.min,
        tzinfo=JST,
    )

    return period_start, period_end


def to_day_hour(value: datetime) -> float:
    """Convert a datetime to an hour position in its JST day."""
    value_jst = value.astimezone(JST)

    return (
        value_jst.hour
        + value_jst.minute / 60
        + value_jst.second / 3600
    )


def format_duration(duration_seconds: int | None) -> str:
    """Format online duration for Discord notifications."""
    if duration_seconds is None:
        return "時間不明"

    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}時間{minutes}分{seconds}秒"

    if minutes > 0:
        return f"{minutes}分{seconds}秒"

    return f"{seconds}秒"


def generate_daily_report_graph(
    report: DailyOnlineReport,
    output_dir: Path,
) -> Path:
    """Generate a timeline-style daily online report as a PNG file."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    matplotlib_config_dir = output_dir / ".matplotlib"
    matplotlib_config_dir.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_config_dir))

    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import FancyBboxPatch

    import importlib.util

    japanese_font = None
    package_spec = importlib.util.find_spec("japanize_matplotlib")
    if package_spec and package_spec.submodule_search_locations:
        package_dir = Path(package_spec.submodule_search_locations[0])
        font_path = package_dir / "fonts" / "ipaexg.ttf"
        if font_path.exists():
            font_manager.fontManager.addfont(str(font_path))
            japanese_font = "IPAexGothic"

    if japanese_font is None:
        font_candidates = [
            "Yu Gothic",
            "Meiryo",
            "MS Gothic",
            "Noto Sans JP",
            "BIZ UDGothic",
            "sans-serif",
        ]
        available_fonts = {font.name for font in font_manager.fontManager.ttflist}
        japanese_font = next(
            (font for font in font_candidates if font in available_fonts),
            "sans-serif",
        )

    plt.rcParams["font.family"] = [japanese_font]
    plt.rcParams["font.sans-serif"] = [japanese_font]

    plt.rcParams["axes.unicode_minus"] = False

    graph_path = output_dir / f"daily_report_{report.discord_user_id}.png"
    display_username = format_display_username(report.username)
    total_seconds = max(0, report.total_duration_seconds)
    offline_seconds = max(0, 24 * 3600 - total_seconds)
    total_percent = total_seconds / (24 * 3600) * 100
    offline_percent = offline_seconds / (24 * 3600) * 100

    figure = plt.figure(figsize=(12.8, 6.3), dpi=100, facecolor="#f8fafc")
    canvas = figure.add_axes([0, 0, 1, 1])
    canvas.set_xlim(0, 1)
    canvas.set_ylim(0, 1)
    canvas.axis("off")

    card = FancyBboxPatch(
        (0.02, 0.03),
        0.96,
        0.94,
        boxstyle="round,pad=0.008,rounding_size=0.018",
        linewidth=1.2,
        edgecolor="#d9dee7",
        facecolor="#ffffff",
    )
    canvas.add_patch(card)

    canvas.text(
        0.055,
        0.91,
        f"{display_username} のオンライン履歴",
        ha="left",
        va="center",
        fontsize=23,
        color="#17181c",
    )

    badge_style = {
        "boxstyle": "round,pad=0.38,rounding_size=0.10",
        "facecolor": "#ffffff",
        "edgecolor": "#dfe3eb",
        "linewidth": 1,
    }
    canvas.text(
        0.055,
        0.83,
        f"日付: {format_report_date(report.report_date)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.285,
        0.83,
        f"合計オンライン時間: {format_duration_minutes(total_seconds)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.545,
        0.83,
        f"オンライン回数: {len(report.online_intervals)}回",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )

    timeline_card = FancyBboxPatch(
        (0.04, 0.36),
        0.92,
        0.38,
        boxstyle="round,pad=0.008,rounding_size=0.012",
        linewidth=1,
        edgecolor="#dfe3eb",
        facecolor="#ffffff",
    )
    canvas.add_patch(timeline_card)
    canvas.text(
        0.065,
        0.53,
        display_username,
        ha="center",
        va="center",
        fontsize=13,
        color="#242832",
    )

    timeline = figure.add_axes([0.095, 0.43, 0.84, 0.22])
    timeline.set_xlim(0, 24)
    timeline.set_ylim(0, 1)
    timeline.axis("off")

    for hour in range(0, 25, 2):
        timeline.axvline(
            hour,
            color="#e4e7ee",
            linestyle=(0, (2, 4)),
            linewidth=1,
        )
        timeline.text(
            hour,
            0.96,
            f"{hour:02d}:00",
            ha="center",
            va="bottom",
            fontsize=10,
            color="#5e6470",
        )

    timeline.broken_barh(
        [(0, 24)],
        (0.40, 0.20),
        facecolors="#edf0f3",
        edgecolors="none",
    )
    day_start = datetime.combine(report.report_date, time.min, tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    for interval in report.online_intervals:
        interval_start = max(interval.online_at.astimezone(JST), day_start)
        interval_end = min(interval.offline_at.astimezone(JST), day_end)
        if interval_end <= interval_start:
            continue

        start = max(
            0,
            min(24, (interval_start - day_start).total_seconds() / 3600),
        )
        end = max(
            0,
            min(24, (interval_end - day_start).total_seconds() / 3600),
        )
        if end <= start:
            continue

        timeline.broken_barh(
            [(start, end - start)],
            (0.40, 0.20),
            facecolors="#5865f2",
            edgecolors="none",
        )

    legend_card = FancyBboxPatch(
        (0.24, 0.275),
        0.52,
        0.055,
        boxstyle="round,pad=0.008,rounding_size=0.01",
        linewidth=1,
        edgecolor="#dfe3eb",
        facecolor="#ffffff",
    )
    canvas.add_patch(legend_card)

    legend_items = [
        (0.285, "#5865f2", "オンライン"),
        (0.415, "#f6bd3b", "退席中 (idle)"),
        (0.555, "#f04747", "取り込み中 (dnd)"),
        (0.695, "#d6d9de", "オフライン"),
    ]
    for x_position, color, label in legend_items:
        canvas.scatter([x_position], [0.302], s=110, marker="s", color=color)
        canvas.text(
            x_position + 0.018,
            0.302,
            label,
            ha="left",
            va="center",
            fontsize=9,
            color="#3b404a",
        )

    summary_card = FancyBboxPatch(
        (0.04, 0.06),
        0.92,
        0.15,
        boxstyle="round,pad=0.008,rounding_size=0.012",
        linewidth=1,
        edgecolor="#dfe3eb",
        facecolor="#ffffff",
    )
    canvas.add_patch(summary_card)
    canvas.text(
        0.06,
        0.19,
        "ステータス別合計時間",
        ha="left",
        va="center",
        fontsize=10,
        color="#242832",
    )

    summary_items = [
        (0.17, "#5865f2", "オンライン", format_duration_minutes(total_seconds), total_percent),
        (0.39, "#f6bd3b", "退席中 (idle)", "0時間00分", 0.0),
        (0.61, "#f04747", "取り込み中 (dnd)", "0時間00分", 0.0),
        (0.81, "#d6d9de", "オフライン", format_duration_minutes(offline_seconds), offline_percent),
    ]
    for index, (x_position, color, label, duration, percent) in enumerate(summary_items):
        if index > 0:
            canvas.plot(
                [x_position - 0.11, x_position - 0.11],
                [0.08, 0.175],
                color="#dfe3eb",
                linewidth=1,
            )
        marker_x = x_position - 0.075
        label_x = x_position - 0.055
        canvas.scatter([marker_x], [0.155], s=50, color=color)
        canvas.text(
            label_x,
            0.155,
            label,
            ha="left",
            va="center",
            fontsize=9,
            color="#3b404a",
        )
        canvas.text(
            x_position,
            0.11,
            duration,
            ha="center",
            va="center",
            fontsize=16,
            color="#242832",
        )
        canvas.text(
            x_position,
            0.08,
            f"({percent:.1f}%)",
            ha="center",
            va="center",
            fontsize=9,
            color="#5e6470",
        )

    figure.savefig(graph_path, format="png", bbox_inches="tight", pad_inches=0)
    plt.close(figure)

    return graph_path




def generate_period_report_graph(
    report: PeriodOnlineReport,
    output_dir: Path,
    report_label: str,
) -> Path:
    """Generate a multi-day online report PNG using the daily card style."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    matplotlib_config_dir = output_dir / ".matplotlib"
    matplotlib_config_dir.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_config_dir))

    import importlib.util

    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import FancyBboxPatch

    japanese_font = None
    package_spec = importlib.util.find_spec("japanize_matplotlib")
    if package_spec and package_spec.submodule_search_locations:
        package_dir = Path(package_spec.submodule_search_locations[0])
        font_path = package_dir / "fonts" / "ipaexg.ttf"
        if font_path.exists():
            font_manager.fontManager.addfont(str(font_path))
            japanese_font = "IPAexGothic"

    if japanese_font is None:
        font_candidates = [
            "Yu Gothic",
            "Meiryo",
            "MS Gothic",
            "Noto Sans JP",
            "BIZ UDGothic",
            "sans-serif",
        ]
        available_fonts = {font.name for font in font_manager.fontManager.ttflist}
        japanese_font = next(
            (font for font in font_candidates if font in available_fonts),
            "sans-serif",
        )

    plt.rcParams["font.family"] = [japanese_font]
    plt.rcParams["font.sans-serif"] = [japanese_font]
    plt.rcParams["axes.unicode_minus"] = False

    graph_path = output_dir / f"{report_label}_report_{report.discord_user_id}.png"
    display_username = format_display_username(report.username)
    day_count = len(report.daily_duration_seconds)
    labels = [
        (report.period_start + timedelta(days=index)).strftime("%m/%d")
        for index in range(day_count)
    ]
    hours = [seconds / 3600 for seconds in report.daily_duration_seconds]
    max_hours = max(hours, default=0)
    y_limit = max(1.0, max_hours * 1.25)
    average_seconds = (
        report.total_duration_seconds // day_count
        if day_count > 0
        else 0
    )

    figure = plt.figure(figsize=(12.8, 6.3), dpi=100, facecolor="#f8fafc")
    canvas = figure.add_axes([0, 0, 1, 1])
    canvas.set_xlim(0, 1)
    canvas.set_ylim(0, 1)
    canvas.axis("off")

    card = FancyBboxPatch(
        (0.02, 0.03),
        0.96,
        0.94,
        boxstyle="round,pad=0.008,rounding_size=0.018",
        linewidth=1.2,
        edgecolor="#d9dee7",
        facecolor="#ffffff",
    )
    canvas.add_patch(card)

    title = f"{display_username} の{report_label}オンライン履歴"
    canvas.text(0.055, 0.91, title, ha="left", va="center", fontsize=23, color="#17181c")

    badge_style = {
        "boxstyle": "round,pad=0.38,rounding_size=0.10",
        "facecolor": "#ffffff",
        "edgecolor": "#dfe3eb",
        "linewidth": 1,
    }
    canvas.text(
        0.055,
        0.83,
        f"期間: {format_report_period(report.period_start, report.period_end)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.355,
        0.83,
        f"合計オンライン時間: {format_duration_minutes(report.total_duration_seconds)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.635,
        0.83,
        f"オンライン回数: {report.online_count}回",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )

    chart_card = FancyBboxPatch(
        (0.04, 0.28),
        0.92,
        0.46,
        boxstyle="round,pad=0.008,rounding_size=0.012",
        linewidth=1,
        edgecolor="#dfe3eb",
        facecolor="#ffffff",
    )
    canvas.add_patch(chart_card)

    chart = figure.add_axes([0.095, 0.37, 0.83, 0.28])
    bar_positions = list(range(day_count))
    chart.bar(bar_positions, hours, color="#5865f2", width=0.62)
    chart.set_ylim(0, y_limit)
    chart.set_ylabel("オンライン時間", color="#3b404a", fontsize=10)
    chart.grid(axis="y", color="#e4e7ee", linestyle=(0, (2, 4)), linewidth=1)
    chart.set_axisbelow(True)
    chart.spines["top"].set_visible(False)
    chart.spines["right"].set_visible(False)
    chart.spines["left"].set_color("#dfe3eb")
    chart.spines["bottom"].set_color("#dfe3eb")
    chart.tick_params(colors="#5e6470", labelsize=9)

    if day_count <= 10:
        tick_positions = bar_positions
    else:
        tick_step = max(1, day_count // 10)
        tick_positions = [index for index in bar_positions if index % tick_step == 0]
        if bar_positions and bar_positions[-1] not in tick_positions:
            tick_positions.append(bar_positions[-1])

    chart.set_xticks(tick_positions)
    chart.set_xticklabels(
        [labels[index] for index in tick_positions],
        rotation=35 if day_count > 10 else 0,
    )

    if report.total_duration_seconds == 0:
        chart.text(
            0.5,
            0.5,
            "対象期間のオンラインログはありません",
            transform=chart.transAxes,
            ha="center",
            va="center",
            fontsize=14,
            color="#5e6470",
        )

    summary_card = FancyBboxPatch(
        (0.04, 0.06),
        0.92,
        0.15,
        boxstyle="round,pad=0.008,rounding_size=0.012",
        linewidth=1,
        edgecolor="#dfe3eb",
        facecolor="#ffffff",
    )
    canvas.add_patch(summary_card)
    canvas.text(0.06, 0.19, "期間サマリー", ha="left", va="center", fontsize=10, color="#242832")

    summary_items = [
        (0.17, "合計", format_duration_minutes(report.total_duration_seconds)),
        (0.39, "オンライン回数", f"{report.online_count}回"),
        (0.61, "1日平均", format_duration_minutes(average_seconds)),
        (0.83, "最長オンライン時間", format_duration_minutes(report.longest_duration_seconds)),
    ]
    for index, (x_position, label, value) in enumerate(summary_items):
        if index > 0:
            canvas.plot(
                [x_position - 0.11, x_position - 0.11],
                [0.08, 0.175],
                color="#dfe3eb",
                linewidth=1,
            )
        canvas.text(x_position, 0.155, label, ha="center", va="center", fontsize=9, color="#3b404a")
        canvas.text(x_position, 0.11, value, ha="center", va="center", fontsize=15, color="#242832")

    figure.savefig(graph_path, format="png", bbox_inches="tight", pad_inches=0)
    plt.close(figure)

    return graph_path


def build_demo_daily_report(
    discord_user_id: int,
    username: str,
) -> DailyOnlineReport:
    """Build fixed demo data for manual DM verification."""
    hourly_minutes = [
        0,
        0,
        0,
        0,
        0,
        15,
        45,
        60,
        35,
        0,
        20,
        50,
        60,
        40,
        0,
        0,
        25,
        60,
        60,
        30,
        10,
        0,
        0,
        0,
    ]
    hourly_seconds = tuple(minutes * 60 for minutes in hourly_minutes)
    report_date = datetime.now(JST).date() - timedelta(days=1)
    online_intervals = (
        OnlineInterval(
            online_at=datetime.combine(report_date, time(6, 16), tzinfo=JST),
            offline_at=datetime.combine(report_date, time(17, 1), tzinfo=JST),
        ),
        OnlineInterval(
            online_at=datetime.combine(report_date, time(18, 15), tzinfo=JST),
            offline_at=datetime.combine(report_date, time(18, 31), tzinfo=JST),
        ),
        OnlineInterval(
            online_at=datetime.combine(report_date, time(19, 0), tzinfo=JST),
            offline_at=datetime.combine(report_date, time(23, 0), tzinfo=JST),
        ),
    )
    total_duration_seconds = sum(
        int((interval.offline_at - interval.online_at).total_seconds())
        for interval in online_intervals
    )

    return DailyOnlineReport(
        discord_user_id=discord_user_id,
        username=username,
        email="",
        report_date=report_date,
        total_duration_seconds=total_duration_seconds,
        hourly_duration_seconds=hourly_seconds,
        online_intervals=online_intervals,
    )


async def send_dm_to_notification_targets(
    bot: commands.Bot,
    monitored_discord_user_id: int,
    message: str,
    file_path: Path | None = None,
) -> None:
    """Send a DM to every active notification target for a monitored user."""
    try:
        target_user_ids = await asyncio.to_thread(
            get_notification_targets,
            monitored_discord_user_id,
        )
    except Exception:
        logger.exception(
            "通知先ユーザーの取得中に予期しないエラーが発生しました。"
            "monitored_discord_user_id=%s",
            monitored_discord_user_id,
        )
        return

    if not target_user_ids:
        logger.info(
            "有効なDM通知先がありません。monitored_discord_user_id=%s",
            monitored_discord_user_id,
        )
        return

    for target_user_id in target_user_ids:
        try:
            target_user = await bot.fetch_user(target_user_id)
            if file_path is None:
                await target_user.send(message)
            else:
                await target_user.send(
                    message,
                    file=discord.File(file_path),
                )

            logger.info(
                "DM通知を送信しました。monitored_discord_user_id=%s target_user_id=%s",
                monitored_discord_user_id,
                target_user_id,
            )
        except Exception:
            logger.exception(
                "DM通知の送信に失敗しました。"
                "monitored_discord_user_id=%s target_user_id=%s",
                monitored_discord_user_id,
                target_user_id,
            )


async def run_daily_report(
    bot: commands.Bot,
    target_date: date | None = None,
) -> None:
    """Create and send daily reports for all monitored users."""
    report_date = target_date or (
        datetime.now(JST).date() - timedelta(days=1)
    )

    try:
        reports = await asyncio.to_thread(get_daily_online_reports, report_date)
    except Exception:
        logger.exception("日次レポート集計に失敗しました。report_date=%s", report_date)
        return

    if not reports:
        logger.info("送信対象の日次レポートがありません。report_date=%s", report_date)
        return

    with tempfile.TemporaryDirectory() as temporary_dir:
        output_dir = Path(temporary_dir)

        for report in reports:
            try:
                graph_path = await asyncio.to_thread(
                    generate_daily_report_graph,
                    report,
                    output_dir,
                )
                message = "\n".join(
                    [
                        f"**【Discord】日次オンラインレポート {report.report_date}**",
                        "",
                        "Discordオンライン日次レポートです。",
                        "",
                        f"ユーザー名: {format_display_username(report.username)}",
                        f"対象日: {report.report_date}",
                        "合計オンライン時間: "
                        f"{format_duration(report.total_duration_seconds)}",
                    ]
                )
                await send_dm_to_notification_targets(
                    bot=bot,
                    monitored_discord_user_id=report.discord_user_id,
                    message=message,
                    file_path=graph_path,
                )
                logger.info(
                    "日次レポートを送信しました。discord_user_id=%s total=%s",
                    report.discord_user_id,
                    format_duration(report.total_duration_seconds),
                )
            except Exception:
                logger.exception(
                    "日次レポート送信中に予期しないエラーが発生しました。"
                    "discord_user_id=%s",
                    report.discord_user_id,
                )


async def run_period_report(
    bot: commands.Bot,
    report_label: str,
    period_start: datetime,
    period_end: datetime,
) -> None:
    """Create and send weekly or monthly reports for all monitored users."""
    try:
        reports = await asyncio.to_thread(
            get_period_online_reports,
            period_start,
            period_end,
        )
    except Exception:
        logger.exception(
            "%sレポート集計に失敗しました。period_start=%s period_end=%s",
            report_label,
            period_start,
            period_end,
        )
        return

    if not reports:
        logger.info(
            "送信対象の%sレポートがありません。period_start=%s period_end=%s",
            report_label,
            period_start,
            period_end,
        )
        return

    with tempfile.TemporaryDirectory() as temporary_dir:
        output_dir = Path(temporary_dir)

        for report in reports:
            logger.info(
                "%sレポート生成開始: user_id=%s period_start=%s period_end=%s",
                report_label,
                report.discord_user_id,
                period_start,
                period_end,
            )
            try:
                graph_path = await asyncio.to_thread(
                    generate_period_report_graph,
                    report,
                    output_dir,
                    report_label,
                )
                day_count = len(report.daily_duration_seconds)
                average_seconds = (
                    report.total_duration_seconds // day_count
                    if day_count > 0
                    else 0
                )
                no_logs_message = (
                    "対象期間のオンラインログはありません。"
                    if report.total_duration_seconds == 0
                    else ""
                )
                message_parts = [
                    f"**【Discord】{report_label}オンラインレポート**",
                    "",
                    f"Discordオンライン{report_label}オンラインレポートです。",
                    "",
                    f"ユーザー名: {format_display_username(report.username)}",
                    f"対象期間: {format_report_period(report.period_start, report.period_end)}",
                    f"合計オンライン時間: {format_duration(report.total_duration_seconds)}",
                    f"オンライン回数: {report.online_count}回",
                    f"1日平均: {format_duration(average_seconds)}",
                    f"最長オンライン時間: {format_duration(report.longest_duration_seconds)}",
                ]
                if no_logs_message:
                    message_parts.extend(["", no_logs_message])

                await send_dm_to_notification_targets(
                    bot=bot,
                    monitored_discord_user_id=report.discord_user_id,
                    message="\n".join(message_parts),
                    file_path=graph_path,
                )
                logger.info(
                    "%sレポートDM送信完了: user_id=%s total=%s",
                    report_label,
                    report.discord_user_id,
                    format_duration(report.total_duration_seconds),
                )
            except Exception:
                logger.exception(
                    "%sレポート送信中に予期しないエラーが発生しました。discord_user_id=%s",
                    report_label,
                    report.discord_user_id,
                )


async def run_weekly_report(
    bot: commands.Bot,
    reference: datetime | None = None,
) -> None:
    """Create and send the previous weekly report."""
    period_start, period_end = get_previous_week_range(reference)
    await run_period_report(bot, "週次", period_start, period_end)


async def run_monthly_report(
    bot: commands.Bot,
    reference: datetime | None = None,
) -> None:
    """Create and send the previous monthly report."""
    period_start, period_end = get_previous_month_range(reference)
    await run_period_report(bot, "月次", period_start, period_end)


async def run_cleanup_old_logs() -> int:
    """Delete old online logs without stopping the bot on failure."""
    try:
        deleted_count = await asyncio.to_thread(delete_old_online_logs)
    except Exception:
        logger.exception("古いオンラインログ削除中に予期しないエラーが発生しました。")
        return 0

    logger.info("古いオンラインログを削除しました。deleted_count=%s", deleted_count)

    return deleted_count

def get_discord_token() -> str:
    """Load the Discord bot token from the environment."""
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。.env に DISCORD_TOKEN を追加してください。"
        )

    return token


def get_target_user_id() -> int:
    """Load the target Discord user ID from the environment."""
    load_dotenv()

    target_user_id = os.getenv("TARGET_USER_ID")
    if not target_user_id:
        raise RuntimeError(
            "TARGET_USER_ID が設定されていません。.env に TARGET_USER_ID を追加してください。"
        )

    try:
        return int(target_user_id)
    except ValueError as error:
        raise RuntimeError("TARGET_USER_ID は数値で設定してください。") from error



def create_bot(target_user_id: int) -> commands.Bot:
    """Create and configure the Discord bot."""
    intents = discord.Intents.default()

    # commands.Bot でプレフィックスコマンドを扱う場合に必要です。
    # Discord Developer Portal 側でも Message Content Intent を有効化してください。
    intents.message_content = True

    # プレゼンス監視には Presence Intent が必要です。
    # Discord Developer Portal 側でも Presence Intent を有効化してください。
    intents.presences = True

    # on_presence_update の対象ユーザー情報を安定して扱うために有効化します。
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    monitored_user_ids: set[int] = {target_user_id}

    async def load_monitored_user_ids() -> None:
        """Load monitored user IDs from DB, falling back to TARGET_USER_ID."""
        nonlocal monitored_user_ids

        try:
            monitored_users = await asyncio.to_thread(get_all_monitored_users)
        except Exception:
            logger.exception(
                "監視対象ユーザー一覧の取得に失敗しました。"
                "TARGET_USER_IDのみを監視します。target_user_id=%s",
                target_user_id,
            )
            monitored_user_ids = {target_user_id}
            return

        if monitored_users:
            monitored_user_ids = {
                user.discord_user_id for user in monitored_users
            }
        else:
            monitored_user_ids = {target_user_id}
            logger.warning(
                "DBに監視対象ユーザーが存在しません。"
                "TARGET_USER_IDのみを監視します。target_user_id=%s",
                target_user_id,
            )

        logger.info(
            "監視対象ユーザーIDを読み込みました。ids=%s",
            sorted(monitored_user_ids),
        )

    async def log_monitored_member_statuses() -> None:
        """Log whether each monitored user can be resolved in each guild."""
        for guild in bot.guilds:
            logger.info(
                "Guild確認: guild_id=%s guild_name=%s member_count=%s",
                guild.id,
                guild.name,
                guild.member_count,
            )

            for monitored_user_id in sorted(monitored_user_ids):
                member = guild.get_member(monitored_user_id)
                if member is not None:
                    logger.info(
                        "監視対象member取得成功(get_member): "
                        "guild_id=%s user_id=%s name=%s status=%s",
                        guild.id,
                        monitored_user_id,
                        member.name,
                        member.status,
                    )
                    continue

                logger.info(
                    "監視対象member取得失敗(get_member=None): "
                    "guild_id=%s user_id=%s",
                    guild.id,
                    monitored_user_id,
                )

                try:
                    fetched_member = await guild.fetch_member(monitored_user_id)
                except discord.NotFound:
                    logger.info(
                        "監視対象member取得失敗(fetch_member NotFound): "
                        "guild_id=%s user_id=%s",
                        guild.id,
                        monitored_user_id,
                    )
                except discord.Forbidden:
                    logger.exception(
                        "監視対象member取得失敗(fetch_member Forbidden): "
                        "guild_id=%s user_id=%s",
                        guild.id,
                        monitored_user_id,
                    )
                except discord.DiscordException:
                    logger.exception(
                        "監視対象member取得失敗(fetch_member DiscordException): "
                        "guild_id=%s user_id=%s",
                        guild.id,
                        monitored_user_id,
                    )
                else:
                    logger.info(
                        "監視対象member取得成功(fetch_member): "
                        "guild_id=%s user_id=%s name=%s status=%s",
                        guild.id,
                        monitored_user_id,
                        fetched_member.name,
                        fetched_member.status,
                    )

    @tasks.loop(time=DAILY_REPORT_TIME)
    async def daily_report_task() -> None:
        """Run the daily report job while the bot is active."""
        try:
            await run_daily_report(bot)
        except Exception:
            logger.exception("日次レポートタスクで予期しないエラーが発生しました。")

    @tasks.loop(time=WEEKLY_REPORT_TIME)
    async def weekly_report_task() -> None:
        """Run the weekly report job every Monday while the bot is active."""
        if datetime.now(JST).weekday() != 0:
            return

        try:
            await run_weekly_report(bot)
        except Exception:
            logger.exception(
                "\u9031\u6b21\u30ec\u30dd\u30fc\u30c8\u30bf\u30b9\u30af\u3067"
                "\u4e88\u671f\u3057\u306a\u3044\u30a8\u30e9\u30fc\u304c"
                "\u767a\u751f\u3057\u307e\u3057\u305f\u3002"
            )

    @tasks.loop(time=MONTHLY_REPORT_TIME)
    async def monthly_report_task() -> None:
        """Run the monthly report job on the first day of each month."""
        if datetime.now(JST).day != 1:
            return

        try:
            await run_monthly_report(bot)
        except Exception:
            logger.exception(
                "\u6708\u6b21\u30ec\u30dd\u30fc\u30c8\u30bf\u30b9\u30af\u3067"
                "\u4e88\u671f\u3057\u306a\u3044\u30a8\u30e9\u30fc\u304c"
                "\u767a\u751f\u3057\u307e\u3057\u305f\u3002"
            )

    @tasks.loop(time=CLEANUP_TIME)
    async def cleanup_old_logs_task() -> None:
        """Delete old finished online logs once per day."""
        await run_cleanup_old_logs()

    @bot.event
    async def on_ready() -> None:
        """Run when the bot has successfully connected to Discord."""
        if bot.user is None:
            logger.warning("Botユーザー情報を取得できませんでした。")
            return

        print("Botが起動しました")
        print(f"Bot名: {bot.user.name}")
        print(f"Bot ID: {bot.user.id}")

        await load_monitored_user_ids()
        await log_monitored_member_statuses()

        if not daily_report_task.is_running():
            daily_report_task.start()
            logger.info("日次レポートタスクを開始しました。実行時刻=%s", DAILY_REPORT_TIME)

        await run_cleanup_old_logs()

        if not weekly_report_task.is_running():
            weekly_report_task.start()
            logger.info(
                "\u9031\u6b21\u30ec\u30dd\u30fc\u30c8\u30bf\u30b9\u30af\u3092"
                "\u958b\u59cb\u3057\u307e\u3057\u305f\u3002"
                "\u5b9f\u884c\u6642\u523b=%s",
                WEEKLY_REPORT_TIME,
            )

        if not monthly_report_task.is_running():
            monthly_report_task.start()
            logger.info(
                "\u6708\u6b21\u30ec\u30dd\u30fc\u30c8\u30bf\u30b9\u30af\u3092"
                "\u958b\u59cb\u3057\u307e\u3057\u305f\u3002"
                "\u5b9f\u884c\u6642\u523b=%s",
                MONTHLY_REPORT_TIME,
            )

        if not cleanup_old_logs_task.is_running():
            cleanup_old_logs_task.start()
            logger.info(
                "\u30ed\u30b0\u524a\u9664\u30bf\u30b9\u30af\u3092"
                "\u958b\u59cb\u3057\u307e\u3057\u305f\u3002"
                "\u5b9f\u884c\u6642\u523b=%s",
                CLEANUP_TIME,
            )

    @bot.command(name="demo_report")
    async def demo_report(context: commands.Context[commands.Bot]) -> None:
        """Send a demo daily report graph to the command author by DM."""
        author = context.author
        report = build_demo_daily_report(
            author.id,
            format_display_username(author.display_name),
        )
        message = "\n".join(
            [
                f"**【Discord】日次オンラインレポート {report.report_date}**",
                "",
                "Discordオンライン日次レポートです。",
                "",
                f"ユーザー名: {format_display_username(report.username)}",
                f"対象日: {report.report_date}",
                "合計オンライン時間: "
                f"{format_duration(report.total_duration_seconds)}",
            ]
        )

        try:
            with tempfile.TemporaryDirectory() as temporary_dir:
                output_dir = Path(temporary_dir)
                graph_path = await asyncio.to_thread(
                    generate_daily_report_graph,
                    report,
                    output_dir,
                )
                await author.send(
                    message,
                    file=discord.File(graph_path),
                )

            await context.send("デモ日次レポートをDMに送信しました。")
            logger.info("デモ日次レポートを送信しました。user_id=%s", author.id)
        except Exception:
            logger.exception(
                "デモ日次レポートのDM送信に失敗しました。user_id=%s",
                author.id,
            )
            await context.send(
                "デモ日次レポートのDM送信に失敗しました。DM設定を確認してください。"
            )


    @bot.command(name="weekly_report")
    async def weekly_report(context: commands.Context[commands.Bot]) -> None:
        """Manually send the previous weekly report for verification."""
        await run_weekly_report(bot)
        await context.send("前週レポートをDM通知先へ送信しました。")

    @bot.command(name="monthly_report")
    async def monthly_report(context: commands.Context[commands.Bot]) -> None:
        """Manually send the previous monthly report for verification."""
        await run_monthly_report(bot)
        await context.send("前月レポートをDM通知先へ送信しました。")

    @bot.command(name="cleanup_preview")
    async def cleanup_preview(context: commands.Context[commands.Bot]) -> None:
        """Show how many old logs would be deleted."""
        try:
            target_count = await asyncio.to_thread(count_old_online_logs)
        except Exception:
            logger.exception("削除対象オンラインログ件数の確認に失敗しました。")
            await context.send("削除対象件数の確認に失敗しました。")
            return

        await context.send(f"削除対象のオンラインログ: {target_count}回")

    @bot.command(name="cleanup_logs")
    async def cleanup_logs(context: commands.Context[commands.Bot]) -> None:
        """Manually delete old finished online logs."""
        deleted_count = await run_cleanup_old_logs()
        await context.send(f"古いオンラインログを削除しました。: {deleted_count}回")

    @bot.event
    async def on_presence_update(
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        """Handle online and offline notifications for monitored users."""
        logger.info(
            "Presence Update受信: user_id=%s before_status=%s after_status=%s",
            after.id,
            before.status,
            after.status,
        )

        if after.id not in monitored_user_ids:
            logger.info(
                "監視対象外のPresence Updateをスキップします。"
                "user_id=%s monitored_user_ids=%s",
                after.id,
                sorted(monitored_user_ids),
            )
            return

        # idleやdndもDiscord上はオンライン扱いなので、offline以外をオンラインとして扱います。
        is_online = (
            before.status == discord.Status.offline
            and after.status != discord.Status.offline
        )
        is_offline = (
            before.status != discord.Status.offline
            and after.status == discord.Status.offline
        )

        if not is_online and not is_offline:
            return

        # Railway上でも日本時間で通知できるようにJST固定にします。
        current_datetime = datetime.now(JST)
        current_time = current_datetime.strftime("%Y-%m-%d %H:%M:%S %Z")

        try:
            # DBから監視対象ユーザー情報を取得し、登録がなければDM通知は行いません。
            monitored_user = await asyncio.to_thread(
                get_user_by_discord_id,
                after.id,
            )
        except Exception:
            logger.exception(
                "監視対象ユーザー情報の取得に失敗しました。discord_user_id=%s",
                after.id,
            )
            return

        if monitored_user is None:
            logger.info(
                "DBに登録されていないためDM通知をスキップしました。discord_user_id=%s",
                after.id,
            )
            return

        username = format_display_username(
            monitored_user.username or after.display_name,
        )

        if is_online:
            try:
                await asyncio.to_thread(
                    create_online_log,
                    monitored_user.discord_user_id,
                    current_datetime,
                )
            except Exception:
                logger.exception(
                    "オンラインログ作成中に予期しないエラーが発生しました。"
                )

            print("==================")
            print("オンライン通知")
            print(f"ユーザー名: {username}")
            print(f"時刻: {current_time}")
            print("==================")

            try:
                message = "\n".join(
                    [
                        "━━━━━━━━━━━━",
                        "**🟢オンライン通知**",
                        f"ユーザー名: {username}",
                        f"時刻: {current_time}",
                        "━━━━━━━━━━━━",
                    ]
                )
                await send_dm_to_notification_targets(
                    bot=bot,
                    monitored_discord_user_id=monitored_user.discord_user_id,
                    message=message,
                )
                logger.info(
                    "オンラインDM通知処理が完了しました。discord_user_id=%s",
                    monitored_user.discord_user_id,
                )
            except Exception:
                logger.exception(
                    "オンラインDM通知処理中に予期しないエラーが発生しました。"
                )

            return

        duration_seconds: int | None = None
        try:
            duration_seconds = await asyncio.to_thread(
                close_latest_online_log,
                monitored_user.discord_user_id,
                current_datetime,
            )
        except Exception:
            logger.exception(
                "オンラインログのクローズ中に予期しないエラーが発生しました。"
            )

        try:
            message = "\n".join(
                [
                    "━━━━━━━━━━━━",
                    "**🔴オフライン通知**",
                    f"ユーザー名: {username}",
                    f"時刻: {current_time}",
                    f"オンライン時間: {format_duration(duration_seconds)}",
                    "━━━━━━━━━━━━",
                ]
            )
            await send_dm_to_notification_targets(
                bot=bot,
                monitored_discord_user_id=monitored_user.discord_user_id,
                message=message,
            )
            logger.info(
                "オフラインDM通知処理が完了しました。discord_user_id=%s",
                monitored_user.discord_user_id,
            )
        except Exception:
            logger.exception(
                "オフラインDM通知処理中に予期しないエラーが発生しました。"
            )

    @bot.event
    async def on_command_error(
        context: commands.Context[commands.Bot],
        error: commands.CommandError,
    ) -> None:
        """Handle command errors consistently."""
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await context.send("必要な引数が不足しています。")
            logger.warning("Missing required argument: %s", error)
            return

        if isinstance(error, commands.BadArgument):
            await context.send("引数の形式が正しくありません。")
            logger.warning("Bad argument: %s", error)
            return

        await context.send("コマンド実行中にエラーが発生しました。")
        logger.exception("Unhandled command error", exc_info=error)

    @bot.event
    async def on_error(event_method: str, *args: object, **kwargs: object) -> None:
        """Log unexpected event errors without exposing sensitive details."""
        logger.exception("Unhandled Discord event error: %s", event_method)

    return bot


def main() -> int:
    """Start the Discord bot."""
    try:
        token = get_discord_token()
        target_user_id = get_target_user_id()
        bot = create_bot(target_user_id)
        bot.run(token)
    except RuntimeError as error:
        logger.error("%s", error)
        return 1
    except discord.LoginFailure:
        logger.error("Discordへのログインに失敗しました。トークンを確認してください。")
        return 1
    except discord.DiscordException:
        logger.exception("Discordクライアントでエラーが発生しました。")
        return 1
    except KeyboardInterrupt:
        logger.info("Botを停止しました。")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

