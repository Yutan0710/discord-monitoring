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
    """Generate a daily online duration bar chart as a PNG file."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    matplotlib_config_dir = output_dir / ".matplotlib"
    matplotlib_config_dir.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_config_dir))
    from matplotlib import font_manager
    import matplotlib.pyplot as plt

    font_candidates = [
        "Yu Gothic",
        "Meiryo",
        "MS Gothic",
        "Noto Sans JP",
        "BIZ UDGothic",
    ]
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    japanese_font = next(
        (font for font in font_candidates if font in available_fonts),
        "sans-serif",
    )
    plt.rcParams["font.family"] = [japanese_font]
    plt.rcParams["axes.unicode_minus"] = False

    graph_path = output_dir / f"daily_report_{report.discord_user_id}.png"
    hours = list(range(24))
    minutes = [seconds / 60 for seconds in report.hourly_duration_seconds]

    figure, axis = plt.subplots(figsize=(10, 4))
    axis.bar(hours, minutes, color="#4f8fd9")
    axis.set_title(f"日次オンラインレポート - {report.report_date}")
    axis.set_xlabel("時間帯（Asia/Tokyo）")
    axis.set_ylabel("オンライン時間（分）")
    axis.set_xticks(hours)
    axis.set_xlim(-0.5, 23.5)
    axis.grid(axis="y", alpha=0.3)
    figure.tight_layout()
    figure.savefig(graph_path, format="png")
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

    return DailyOnlineReport(
        discord_user_id=discord_user_id,
        username=username,
        email="",
        report_date=datetime.now(JST).date() - timedelta(days=1),
        total_duration_seconds=sum(hourly_seconds),
        hourly_duration_seconds=hourly_seconds,
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
                        f"ユーザー名: {report.username}",
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
                f"ユーザー名: {report.username}",
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

        is_online = (
            before.status == discord.Status.offline
            and after.status == discord.Status.online
        )
        is_offline = (
            before.status != discord.Status.offline
            and after.status == discord.Status.offline
        )

        if not is_online and not is_offline:
            return

        # ローカルタイムゾーンの現在時刻を通知に表示します。
        current_datetime = datetime.now().astimezone()
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

        username = monitored_user.username or after.display_name

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
                        "Discordユーザーがオンラインになりました。",
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
                    "Discordユーザーがオフラインになりました。",
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
