# Friday Telegram Bot

폰/PC 어디서든 텔레그램으로 Friday와 대화합니다. 같은 `ClaudeSDKClient`를
공유하므로 REPL과 동일한 도구·에이전트·MCP 서버가 그대로 동작합니다.

## 1. 봇 만들기 (BotFather, 5분)

1. 텔레그램에서 `@BotFather` 검색 → 대화 시작
2. `/newbot` → 이름(예: `Friday`) → username(예: `사장님_friday_bot`, 끝이 `_bot`이어야 함)
3. 받은 **HTTP API 토큰** 복사 (`123456:ABC-...` 형태)
4. (선택) `/setprivacy` → `Disable` — 그룹에서도 일반 메시지 받게 하고 싶을 때만
5. (선택) `/setcommands` 로 명령어 목록 등록 — 자동완성용
   ```
   start - 봇 시작
   help - 사용법
   status - 프로젝트/잡 현황
   id - 내 텔레그램 ID 확인
   ```

## 2. 내 텔레그램 user_id 알아내기

- `@userinfobot` 에게 `/start` 보내면 user_id 알려줌, 또는
- 봇을 먼저 띄운 뒤(아래 단계) `/id` 명령 보내기

## 3. 환경변수 설정

`~/phython/.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
FRIDAY_TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
FRIDAY_TELEGRAM_ALLOWED_IDS=11111111            # 여러 명이면 쉼표로 구분
```

`.env`는 gitignore되어 있음.

## 4. 의존성 설치

```bash
cd ~/phython
source .venv/bin/activate    # 또는 본인 환경 활성화
pip install -e .             # python-telegram-bot 포함해서 설치
```

## 5. 실행

```bash
friday-bot
# 또는
python -m friday.bot
```

로그에 `telegram polling started` 가 뜨면 OK.
텔레그램에서 본인 봇 찾아 `/start` → 환영 메시지 받으면 연결 완료.

## 6. 백그라운드 + 자동시작 (launchd, macOS)

세션 끊겨도 동작하게 하려면 launchd 사용:

`~/Library/LaunchAgents/com.사장님.friday-bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.사장님.friday-bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/사장님/phython/.venv/bin/friday-bot</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/사장님/phython</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/사장님/phython/data/bot.log</string>
  <key>StandardErrorPath</key><string>/Users/사장님/phython/data/bot.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

로드:

```bash
launchctl load -w ~/Library/LaunchAgents/com.사장님.friday-bot.plist
# 끄기: launchctl unload -w ~/Library/LaunchAgents/com.사장님.friday-bot.plist
```

> 주의: launchd는 셸 환경을 상속하지 않음. `.env` 의 키들은 봇이 `python-dotenv`
> 로 직접 로드하므로 OK.

## 7. 보안 노트

- 화이트리스트(`FRIDAY_TELEGRAM_ALLOWED_IDS`)에 없는 user_id 의 텍스트 메시지는
  조용히 무시 (로그만 남김). 단 `/id` 는 누구나 호출 가능 (자기 ID 확인용).
- 봇 토큰이 유출되면 즉시 BotFather `/revoke` 로 회수.
- 모든 메시지가 Anthropic API 로 전송됨 — 민감 정보는 절대 직접 입력 금지.

## 8. 동작 방식

- REPL과 동일한 `ClaudeSDKClient` 한 개를 봇 수명 내내 공유.
- 한 번에 한 메시지만 처리(asyncio.Lock). 처리 중에 새 메시지가 와도
  텔레그램 큐에 쌓였다 순차 처리됨.
- 응답 텍스트는 4096자 제한에 맞춰 자동 분할.
- 백그라운드 잡 완료, PR 감시 알림 등은 5초마다 폴링해서 푸시.
- Mac 슬립 들어가도 텔레그램 서버가 메시지 보관 → 깨어나면 받아 처리.

## 9. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `error: FRIDAY_TELEGRAM_BOT_TOKEN not set` | `.env` 누락 또는 잘못된 디렉토리에서 실행 |
| 봇이 응답 안함 | 화이트리스트에 user_id 없음. `/id` 로 확인 후 `.env` 갱신 → 재시작 |
| `Conflict: terminated by other getUpdates` | 같은 토큰으로 다른 인스턴스가 이미 돌고 있음. 하나만 켤 것 |
| 응답이 잘림 | 4000자 청크. 정상 동작이며 자동으로 여러 메시지로 나뉨 |
