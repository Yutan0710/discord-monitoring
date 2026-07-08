"""Discord bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import (
    close_latest_online_log,
    create_online_log,
    DailyOnlineReport,
    OnlineInterval,
    get_daily_online_reports,
    get_notification_targets,
    get_user_by_discord_id,
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


def format_display_username(username: str) -> str:
    """Return the display name used in Discord notifications."""
    display_name_overrides = {
        "ゆうたん": "星野拓海",
    }

    return display_name_overrides.get(username, username)


def format_duration_minutes(duration_seconds: int) -> str:
    """Format a duration without seconds for daily report images."""
    hours, remainder = divmod(max(0, duration_seconds), 3600)
    minutes = remainder // 60

    return f"{hours}時間{minutes:02d}分"


def format_report_date(report_date: date) -> str:
    """Format a report date with a Japanese weekday."""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    return report_date.strftime("%Y/%m/%d") + f" ({weekdays[report_date.weekday()]})"


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
    from matplotlib.patches import Circle, FancyBboxPatch

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

    icon = Circle((0.065, 0.895), 0.032, facecolor="#5865f2", edgecolor="none")
    canvas.add_patch(icon)
    canvas.text(
        0.065,
        0.895,
        "D",
        ha="center",
        va="center",
        color="#ffffff",
        fontsize=22,
    )
    canvas.text(
        0.10,
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
        0.10,
        0.83,
        f"日付: {format_report_date(report.report_date)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.32,
        0.83,
        f"合計オンライン時間: {format_duration_minutes(total_seconds)}",
        fontsize=11,
        color="#242832",
        bbox=badge_style,
    )
    canvas.text(
        0.58,
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

    label_rows = [0.31, 0.22]
    for index, interval in enumerate(report.online_intervals):
        start = max(0, min(24, to_day_hour(interval.online_at)))
        end = max(0, min(24, to_day_hour(interval.offline_at)))
        if end <= start:
            continue

        timeline.broken_barh(
            [(start, end - start)],
            (0.40, 0.20),
            facecolors="#5865f2",
            edgecolors="none",
        )
        label_y = label_rows[index % len(label_rows)]
        timeline.text(
            start,
            label_y,
            interval.online_at.astimezone(JST).strftime("%H:%M"),
            ha="center",
            va="top",
            fontsize=9,
            color="#5865f2",
        )
        timeline.text(
            end,
            label_y,
            interval.offline_at.astimezone(JST).strftime("%H:%M"),
            ha="center",
            va="top",
            fontsize=9,
            color="#5865f2",
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
        (0.715, "#d6d9de", "オフライン"),
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
        (0.83, "#d6d9de", "オフライン", format_duration_minutes(offline_seconds), offline_percent),
    ]
    for index, (x_position, color, label, duration, percent) in enumerate(summary_items):
        if index > 0:
            canvas.plot(
                [x_position - 0.11, x_position - 0.11],
                [0.08, 0.175],
                color="#dfe3eb",
                linewidth=1,
            )
        canvas.scatter([x_position - 0.055], [0.155], s=50, color=color)
        canvas.text(
            x_position - 0.04,
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

    @tasks.loop(time=DAILY_REPORT_TIME)
    async def daily_report_task() -> None:
        """Run the daily report job while the bot is active."""
        try:
            await run_daily_report(bot)
        except Exception:
            logger.exception("日次レポートタスクで予期しないエラーが発生しました。")

    @bot.event
    async def on_ready() -> None:
        """Run when the bot has successfully connected to Discord."""
        if bot.user is None:
            logger.warning("Botユーザー情報を取得できませんでした。")
            return

        print("Botが起動しました")
        print(f"Bot名: {bot.user.name}")
        print(f"Bot ID: {bot.user.id}")

        if not daily_report_task.is_running():
            daily_report_task.start()
            logger.info("日次レポートタスクを開始しました。実行時刻=%s", DAILY_REPORT_TIME)

    @bot.command(name="demo_report")
    async def demo_report(context: commands.Context[commands.Bot]) -> None:
        """Send a demo daily report graph to the command author by DM."""
        author = context.author
        report = build_demo_daily_report(author.id, author.display_name)
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

    @bot.event
    async def on_presence_update(
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        """Handle online and offline notifications for the target user."""
        if after.id != target_user_id:
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
                        f"**【Discord】{username} がオンラインになりました**",
                        "",
                        f"ユーザー名: {username}",
                        f"時刻: {current_time}",
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
                    f"**【Discord】{username} がオフラインになりました**",
                    "",
                    f"ユーザー名: {username}",
                    f"時刻: {current_time}",
                    f"オンライン時間: {format_duration(duration_seconds)}",
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

