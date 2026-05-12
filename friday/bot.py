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
import re
import sys
import time
from collections import defaultdict, deque
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
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
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

# Retry policy for send_message when the network drops (Mac sleep, Wi-Fi
# hiccup, etc.). Polling itself recovers automatically inside PTB, but
# outbound sends do not — without retry they vanish silently.
SEND_MAX_RETRIES = 6
SEND_BACKOFF_BASE = 1.5  # seconds; exponential: 1.5, 3, 6, 12, 24, capped 60

# Rate limit: per-user sliding window
RATE_LIMIT_MSGS = 20
RATE_LIMIT_WINDOW = 60.0  # seconds


# Appended to the shared SYSTEM_PROMPT only when running as the Telegram bot.
# The REPL keeps its original prompt unchanged.
TELEGRAM_PROMPT_SUFFIX = """

Chat-style interaction (Telegram):
- You are talking to the user via Telegram. There is NO UI for structured
  prompts — AskUserQuestion and ExitPlanMode are disabled in this context
  and would return empty.
- When you need clarification, ask in plain Korean (or English if the user
  writes English) and STOP. The user's next message is their reply.
- When a request is ambiguous, ALWAYS ask one or two short questions before
  dispatching long-running jobs. Don't guess and run — the user can't see
  what you're about to do until you've already done it.
- Keep replies tight: bullets over paragraphs, no decorative section headers.
- The user can't approve tool calls interactively. If a Bash command is
  blocked by the safety policy, tell them so and suggest running it from the
  local REPL instead — don't loop trying variations.
"""


# Prepended to a user's message when Work Mode is ON for that chat. Tells the
# model the user is at a workplace with DLP active and must not be asked to
# share code, and that outputs should be optimized for paste into GitHub
# Copilot at work.
WORK_MODE_TAG = """[WORK MODE — DLP active]
사장님은 회사 환경에서 작업 중. 회사 코드/데이터는 텔레그램으로 절대
오지 않음. 다음 원칙으로 응답:

- "코드 보여달라" 요청 금지. 묘사만으로 추론.
- 회사 코드/제품/팀/내부 도메인 식별자가 메시지에 우연히 섞이면 즉시
  "그 식별자는 DLP 위험. 일반 용어로 다시 부탁드려요" 안내.
- 응답은 두 부분:
  (a) 한국어 간결 설명 (3-5줄) — 설계 원칙 또는 단계
  (b) 영문 Copilot 프롬프트 코드블록 — 회사 PC Copilot에 그대로 또는
      살짝 변형해 입력 가능한 형태. 명령형, 짧고 구체적.
- Shell/git 명령어가 답이라면 copilot_suggest 도구로 한 번 사전 검증한
  후 보내라 (없으면 그냥 추천).

사장님 메시지:
"""


log = logging.getLogger("friday.bot")


# --- safety: dangerous Bash command patterns ------------------------------
#
# These run as a deny-list on every Bash tool invocation. Designed to block
# the most catastrophic actions (filesystem wipe, privilege escalation, raw
# disk writes, pipe-to-shell, exfiltration of credentials). False positives
# are preferred over false negatives — if a legit command is blocked, run it
# from the local REPL where the user can review it interactively.

_DANGEROUS_BASH: list[tuple[str, str]] = [
    (r"\bsudo\b", "sudo invocation"),
    (r"\bsu\s+-", "su elevation"),
    (
        r"\brm\s+[^|;&\n]*(-[a-zA-Z]*[rR][a-zA-Z]*[fF]|-[a-zA-Z]*[fF][a-zA-Z]*[rR])\b",
        "rm with recursive+force flags",
    ),
    (r"\bmkfs\b", "filesystem format"),
    (r"\bdd\b[^|;&\n]*\bof=/dev/", "raw disk write via dd"),
    (r">\s*/dev/(sd[a-z]|nvme\w*|disk\w*)", "redirect to raw disk"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "system power command"),
    (
        r"(curl|wget|fetch)\s+[^|;\n]*\|\s*(bash|sh|zsh|fish|python|python3|node)\b",
        "pipe download into interpreter",
    ),
    (r"\bchmod\s+(-R\s+)?[0-7]?777\b", "world-writable chmod"),
    (r"\bchown\s+(-R\s+)?root\b", "chown to root"),
    (r":\(\)\s*\{\s*:\s*\|\s*:&\s*\};:", "fork bomb"),
    # credential exfil
    (r"\bcat\b[^|;&\n]*\.env(\s|$|;|&|\||>)", "read .env file"),
    (
        r"\b(cat|less|more|head|tail)\b[^|;&\n]*id_(rsa|ed25519|ecdsa|dsa)\b",
        "read SSH private key",
    ),
    (
        r"\b(cat|less|more|head|tail)\b[^|;&\n]*\.aws/credentials",
        "read AWS credentials",
    ),
]
_DANGEROUS_BASH_COMPILED = [(re.compile(p), reason) for p, reason in _DANGEROUS_BASH]


def _check_bash_command(cmd: str) -> Optional[str]:
    """Return the reason a Bash command is blocked, or None if allowed."""
    for pattern, reason in _DANGEROUS_BASH_COMPILED:
        if pattern.search(cmd):
            return reason
    return None


async def _can_use_tool(
    tool_name: str,
    tool_input: dict,
    context,
) -> PermissionResultAllow | PermissionResultDeny:
    """Gatekeeper called by the SDK before every tool invocation."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        reason = _check_bash_command(cmd)
        if reason:
            log.warning(
                f"BLOCKED Bash command (reason: {reason}): {cmd[:200]}"
            )
            return PermissionResultDeny(
                message=(
                    f"Bot safety policy denied this command ({reason}). "
                    "If it is legitimate, run it from the local REPL where "
                    "you can confirm interactively."
                ),
                interrupt=False,
            )
    return PermissionResultAllow()


# --- rate limiting --------------------------------------------------------

_rate_buckets: dict[int, deque] = defaultdict(deque)


def _rate_limit_check(user_id: int) -> tuple[bool, float]:
    """Sliding-window per-user rate limit.

    Returns (allowed, retry_in_seconds). On allow, the current timestamp is
    recorded in the user's bucket.
    """
    now = time.time()
    bucket = _rate_buckets[user_id]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MSGS:
        retry_in = RATE_LIMIT_WINDOW - (now - bucket[0])
        return False, retry_in
    bucket.append(now)
    return True, 0.0


# --- resilient sending ----------------------------------------------------


async def _safe_send(bot, chat_id: int, text: str) -> bool:
    """Send a Telegram message, retrying on transient network failures.

    Returns True if delivered, False if all retries were exhausted.
    """
    delay = SEND_BACKOFF_BASE
    last_exc: Optional[Exception] = None
    for attempt in range(1, SEND_MAX_RETRIES + 1):
        try:
            await bot.send_message(chat_id, text)
            if attempt > 1:
                log.info(f"send_message recovered on attempt {attempt}")
            return True
        except (NetworkError, TimedOut) as exc:
            last_exc = exc
            if attempt == SEND_MAX_RETRIES:
                break
            log.warning(
                f"send_message network error (attempt {attempt}/"
                f"{SEND_MAX_RETRIES}): {exc}; retrying in {delay:.1f}s"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, 60.0)
        except Exception as exc:  # noqa: BLE001
            # non-network error: don't retry, but don't crash the handler
            log.warning(f"send_message failed (non-network): {exc}")
            return False
    log.error(
        f"send_message gave up after {SEND_MAX_RETRIES} retries: {last_exc}"
    )
    return False


class BotState:
    def __init__(self) -> None:
        self.client: Optional[ClaudeSDKClient] = None
        self.lock = asyncio.Lock()
        self.allowed_ids: set[int] = set()
        self.notify_chats: set[int] = set()
        # chat_ids where Work Mode (DLP-safe consulting) is currently active
        self.work_mode_chats: set[int] = set()


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
    chat_id = update.effective_chat.id
    state.notify_chats.add(chat_id)
    await _safe_send(
        context.bot,
        chat_id,
        "Friday 가동 중.\n"
        "메시지를 보내면 작업을 진행합니다.\n\n"
        "/status — 프로젝트/잡 현황\n"
        "/work — DLP-안전 모드 토글\n"
        "/abstract — 묘사를 DLP-안전 표현으로 변환\n"
        "/id — 내 텔레그램 ID 확인\n"
        "/help — 사용법",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_allowed(user.id):
        return
    await _safe_send(
        context.bot,
        update.effective_chat.id,
        "사용법\n"
        "• 자연어로 지시하면 Friday가 처리합니다.\n"
        "• 백그라운드 잡 완료, PR 코멘트 등은 알림으로 푸시됩니다.\n"
        "• 응답이 길면 여러 메시지로 분할됩니다.\n"
        "• 한 번에 한 메시지만 처리 (다음 메시지는 큐에서 대기).\n\n"
        "회사 작업용:\n"
        "• /work on/off — DLP-안전 모드. 코드 입력 X, 묘사만. 답변에 영문 "
        "Copilot 프롬프트 포함.\n"
        "• /abstract <텍스트> — 내부 용어 섞인 문장을 일반 산업 용어로 변환.",
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # accessible to anyone — needed for initial whitelist setup
    if update.effective_user is None:
        return
    await _safe_send(
        context.bot,
        update.effective_chat.id,
        f"your telegram id: {update.effective_user.id}",
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
    await _safe_send(context.bot, update.effective_chat.id, "\n".join(lines))


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle DLP-safe Work Mode for this chat."""
    user = update.effective_user
    if user is None or update.message is None or not _is_allowed(user.id):
        return
    chat_id = update.effective_chat.id
    arg = ((context.args[0].lower() if context.args else "")).strip()

    if arg in ("on", "1", "true"):
        state.work_mode_chats.add(chat_id)
        await _safe_send(
            context.bot,
            chat_id,
            "✓ Work Mode ON (DLP 적용).\n"
            "• 코드/내부 식별자 입력 X — 일반 묘사만\n"
            "• 답변은 한국어 설명 + 영문 Copilot 프롬프트 코드블록\n"
            "• /work off 로 해제",
        )
        return
    if arg in ("off", "0", "false"):
        state.work_mode_chats.discard(chat_id)
        await _safe_send(context.bot, chat_id, "✓ Work Mode OFF. 평소 모드.")
        return

    status = "ON" if chat_id in state.work_mode_chats else "OFF"
    await _safe_send(
        context.bot,
        chat_id,
        f"Work Mode: 현재 {status}\n사용법: /work on | /work off",
    )


async def cmd_abstract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-shot: rewrite a message in DLP-safe abstract form."""
    user = update.effective_user
    if user is None or update.message is None or not _is_allowed(user.id):
        return
    chat_id = update.effective_chat.id
    raw = update.message.text or ""
    args_text = raw.partition(" ")[2].strip()
    if not args_text:
        await _safe_send(
            context.bot,
            chat_id,
            "사용법: /abstract <텍스트>\n"
            "회사/제품/팀/내부 도메인 용어 섞인 문장을 DLP-안전한 일반 표현 + "
            "영문 Copilot 프롬프트로 변환합니다.\n"
            "예: /abstract 한영서비스 결제 환불 멱등성 처리",
        )
        return

    if state.client is None:
        await _safe_send(context.bot, chat_id, "[bot error] client not ready")
        return

    allowed, retry_in = _rate_limit_check(user.id)
    if not allowed:
        await _safe_send(
            context.bot,
            chat_id,
            f"⏱ 분당 메시지 제한 초과. {int(retry_in) + 1}초 후 재시도.",
        )
        return

    log.info(f"[{user.id}] /abstract → {args_text[:120]}")
    state.notify_chats.add(chat_id)

    prompt = (
        "다음 텍스트를 DLP-안전하게 추상화해주세요. 회사/제품/팀/내부 도메인 단어를 "
        "산업 일반 용어로 치환:\n\n"
        f"{args_text}\n\n"
        "출력 형식:\n"
        "1. 추상화된 한국어 설명 (1-2줄)\n"
        "2. 영문 Copilot 프롬프트 (```bash 또는 ```text 코드블록)"
    )

    async with state.lock:
        typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
        try:
            await state.client.query(prompt)
            parts: list[str] = []
            async for msg in state.client.receive_response():
                fragment = _render_message(msg)
                if fragment:
                    parts.append(fragment)
            response = "\n".join(parts).strip() or "(no response)"
        except Exception as exc:  # noqa: BLE001
            log.exception("error processing /abstract")
            response = f"[bot error] {exc}"
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        for chunk in _split_message(response):
            await _safe_send(context.bot, chat_id, chunk)


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

    allowed, retry_in = _rate_limit_check(user.id)
    if not allowed:
        await _safe_send(
            context.bot,
            chat_id,
            f"⏱ 분당 메시지 제한 초과 ({RATE_LIMIT_MSGS}/{int(RATE_LIMIT_WINDOW)}s). "
            f"{int(retry_in) + 1}초 후 다시 시도해주세요.",
        )
        return

    log.info(f"[{user.id}] → {text[:120]}")

    if state.client is None:
        await _safe_send(context.bot, chat_id, "[bot error] client not ready")
        return

    async with state.lock:
        typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
        response: str
        try:
            prepared = (
                WORK_MODE_TAG + text
                if chat_id in state.work_mode_chats
                else text
            )
            await state.client.query(prepared)
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
            await _safe_send(context.bot, chat_id, chunk)


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
                await _safe_send(app.bot, chat_id, text)
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
        system_prompt=SYSTEM_PROMPT + TELEGRAM_PROMPT_SUFFIX,
        mcp_servers={"friday": server, **extra_servers},
        allowed_tools=ALLOWED_TOOLS + extra_allowed,
        # AskUserQuestion + ExitPlanMode return empty in headless contexts;
        # the model would then proceed with assumptions. Force it to ask
        # follow-ups as plain text instead.
        disallowed_tools=["AskUserQuestion", "ExitPlanMode"],
        agents=REGISTRY,
        model="claude-opus-4-7",
        # bot has no interactive approval channel → bypass + gated by
        # can_use_tool. The callback blocks dangerous Bash patterns.
        permission_mode="bypassPermissions",
        can_use_tool=_can_use_tool,
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
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("abstract", cmd_abstract))
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
