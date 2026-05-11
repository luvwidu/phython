"""Telegram bridge for Friday.

Run with: ``python -m friday.bot`` or, after install, ``friday-bot``.

A long-lived ClaudeSDKClient is shared across all turns (same pattern as
``friday.main.repl``). Each incoming Telegram message is queried against that
client, and the streamed response is sent back as one or more Telegram
messages. Background notifications (from completed jobs, watcher hits, etc.)
are pushed to every chat that has talked to the bot in this run.

Required env vars:
  FRIDAY_TELEGRAM_BOT_TOKEN  — token from @BotFather
  FRIDAY_TELEGRAM_ALLOWED_IDS — comma-separated Telegram user ids permitted
                                to use the bot. Get yours from @userinfobot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agents import REGISTRY
from .config import load_mcp_config
from .prompts import SYSTEM_PROMPT
from . import sync
from .tasks import store
from .tools import ALLOWED_TOOLS, build_server
from .watcher import resume_all as resume_watchers


MAX_MESSAGE_LEN = 3900  # Telegram hard limit is 4096; leave headroom
TYPING_REFRESH_SEC = 4
NOTIFICATIONS_POLL_SEC = 5


log = logging.getLogger("friday.bot")


class BotState:
    def __init__(self) -> None:
        self.client: Optional[ClaudeSDKClient] = None
        self.lock = asyncio.Lock()
        self.allowed_ids: set[int] = set()
        self.notify_chats: set[int] = set()


state = BotState()


# --- helpers --------------------------------------------------------------


def _is_allowed(user_id: int) -> bool:
    return user_id in state.allowed_ids


def _render_message(msg) -> Optional[str]:
    if isinstance(msg, AssistantMessage):
        parts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                parts.append(f"· {block.name}")
            elif isinstance(block, ThinkingBlock):
                pass
        return "\n".join(parts) if parts else None
    if isinstance(msg, ResultMessage):
        if msg.is_error:
            return f"[error] {msg.result}"
    return None


def _split_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_len:
            if cur:
                chunks.append(cur)
                cur = ""
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def _keep_typing(bot, chat_id: int) -> None:
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception as exc:  # noqa: BLE001
                log.debug(f"typing indicator failed: {exc}")
            await asyncio.sleep(TYPING_REFRESH_SEC)
    except asyncio.CancelledError:
        return


# --- handlers -------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_allowed(user.id):
        return
    state.notify_chats.add(update.effective_chat.id)
    await update.message.reply_text(
        "Friday 가동 중.\n"
        "메시지를 보내면 작업을 진행합니다.\n\n"
        "/status — 프로젝트/잡 현황\n"
        "/id — 내 텔레그램 ID 확인\n"
        "/help — 사용법"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_allowed(user.id):
        return
    await update.message.reply_text(
        "사용법\n"
        "• 자연어로 지시하면 Friday가 처리합니다.\n"
        "• 백그라운드 잡 완료, PR 코멘트 등은 알림으로 푸시됩니다.\n"
        "• 응답이 길면 여러 메시지로 분할됩니다.\n"
        "• 한 번에 한 메시지만 처리 (다음 메시지는 큐에서 대기)."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # accessible to anyone — needed for initial whitelist setup
    if update.effective_user is None:
        return
    await update.message.reply_text(
        f"your telegram id: {update.effective_user.id}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_allowed(user.id):
        return
    projects = store.list_projects()
    running_jobs = [j for j in store.list_jobs() if j.status == "running"]
    open_tasks = [
        t for t in store.list_tasks()
        if t.status in ("pending", "in_progress", "blocked")
    ]
    lines = [
        f"projects: {len(projects)}",
        f"open tasks: {len(open_tasks)}",
        f"running jobs: {len(running_jobs)}",
    ]
    if running_jobs:
        lines.append("")
        for j in running_jobs[:5]:
            lines.append(f"  · {j.id} {j.repo_path}")
    await update.message.reply_text("\n".join(lines))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not _is_allowed(user.id):
        log.info(f"ignored message from unallowed user {user.id}")
        return
    chat_id = update.effective_chat.id
    state.notify_chats.add(chat_id)
    text = update.message.text or ""
    if not text.strip():
        return

    log.info(f"[{user.id}] → {text[:120]}")

    if state.client is None:
        await context.bot.send_message(chat_id, "[bot error] client not ready")
        return

    async with state.lock:
        typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
        response: str
        try:
            await state.client.query(text)
            parts: list[str] = []
            async for msg in state.client.receive_response():
                fragment = _render_message(msg)
                if fragment:
                    parts.append(fragment)
            response = "\n".join(parts).strip() or "(no response)"
        except Exception as exc:  # noqa: BLE001
            log.exception("error processing message")
            response = f"[bot error] {exc}"
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        for chunk in _split_message(response):
            try:
                await context.bot.send_message(chat_id, chunk)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"failed to send chunk: {exc}")


# --- background notifications --------------------------------------------


async def notifications_loop(app: Application) -> None:
    while True:
        try:
            await asyncio.sleep(NOTIFICATIONS_POLL_SEC)
            notifs = store.pop_notifications()
            if not notifs:
                continue
            lines = []
            for n in notifs:
                ts = time.strftime("%H:%M:%S", time.localtime(n.get("ts", time.time())))
                lines.append(f"[{ts}] {n.get('text', '')}")
            text = "🔔 알림\n" + "\n".join(lines)
            for chat_id in list(state.notify_chats):
                try:
                    await app.bot.send_message(chat_id, text)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"notification send failed for {chat_id}: {exc}")
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(f"notifications loop error: {exc}")


# --- entrypoint -----------------------------------------------------------


async def _main() -> None:
    token = os.environ.get("FRIDAY_TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: FRIDAY_TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    raw_allowed = os.environ.get("FRIDAY_TELEGRAM_ALLOWED_IDS", "").strip()
    if not raw_allowed:
        print(
            "error: FRIDAY_TELEGRAM_ALLOWED_IDS not set (comma-separated user ids).\n"
            "       send /id to your bot from a non-whitelisted account to learn your id,\n"
            "       or use @userinfobot.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        state.allowed_ids = {int(x.strip()) for x in raw_allowed.split(",") if x.strip()}
    except ValueError:
        print(
            "error: FRIDAY_TELEGRAM_ALLOWED_IDS must be comma-separated integers",
            file=sys.stderr,
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )
    # quiet down chatty telegram libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    log.info(f"allowed user ids: {sorted(state.allowed_ids)}")

    server = build_server()
    extra_servers, extra_allowed, source = load_mcp_config()
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"friday": server, **extra_servers},
        allowed_tools=ALLOWED_TOOLS + extra_allowed,
        agents=REGISTRY,
        model="claude-opus-4-7",
        permission_mode="default",
        setting_sources=["user"],
    )

    log.info(sync.init())
    if source:
        log.info(f"loaded {len(extra_servers)} extra MCP server(s) from {source}")
    resume_watchers()
    sync.schedule_sync()

    state.client = ClaudeSDKClient(options=options)
    await state.client.__aenter__()
    log.info("claude sdk client ready")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("telegram polling started")

    notif_task = asyncio.create_task(notifications_loop(app))

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        notif_task.cancel()
        try:
            await notif_task
        except asyncio.CancelledError:
            pass
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass
        if state.client is not None:
            try:
                await state.client.__aexit__(None, None, None)
            except Exception:
                pass


def run() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY not set; the SDK will fall back to your "
            "Claude Code login if available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
