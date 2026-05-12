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

## 7. Work Mode (DLP-안전 자문)

회사에서 일하는 동안 텔레그램으로 Friday에게 자문 받을 때 켜는 모드.

**한 줄 원칙**: Friday는 코드 안 봐요. 사장님이 머리로 추상화한 묘사만 받아요.

### 골든 패스

```
회사 PC 모니터  →  머리에서 추상화
    ↓
개인 폰 → Telegram → Friday (집 Mac)
    ↓ 한국어 설명 + 영문 Copilot 프롬프트 코드블록
개인 폰에서 읽음
    ↓ 본인이 회사 PC에 직접 입력 (또는 패턴만 머리로 가져감)
회사 PC → IDE Copilot 자동완성 / 입력
```

회사 코드/데이터가 텔레그램을 통과하는 채널이 0이라 DLP 안전.

### 절대 금지 행동

- 회사 PC에서 텔레그램 열기 (클립보드/네트워크 감시 위험)
- 코드 캡처/복붙해서 폰으로 전송
- 내부 식별자(회사명/제품명/팀명/내부 도메인 단어) 그대로 입력 → `/abstract` 사용

### 명령어

```
/work          → 현재 상태 확인
/work on       → Work Mode 켜기
/work off      → 끄기
/abstract <텍스트>  → 내부 용어 섞인 문장을 일반 산업 용어 + 영문 Copilot 프롬프트로 변환
```

### 사용 예

```
사장님: /work on
봇: ✓ Work Mode ON. 코드 입력 X, 묘사만.

사장님: 결제 환불에서 멱등성 처리 어떻게 해?
봇: 3rd-party 결제 환불의 멱등성은 보통 3 레이어로:
    1) request_id 기반 dedupe table
    2) DB-level unique constraint
    3) 외부 API 응답 재해석

    Copilot에 넣어보세요:
    ```
    Add idempotency to refund endpoint:
    - idempotency_keys table (key, status, response_hash, created_at)
    - Check key before processing; return stored response on retry
    - Compose unique constraint on (key, created_at)
    ```
```

추상화 도움이 필요할 때:

```
사장님: /abstract HANSubPayService 환불 시 KAKAO_PAY 응답 처리
봇: 1. 추상화: "3rd-party 결제 게이트웨이의 환불 응답 처리"
    2. ```text
       Handle refund response from third-party payment gateway,
       parsing idempotency markers from response headers
       ```
```


## 8. 보안 노트

봇은 다음 가드레일을 코드 레벨에서 강제합니다:

- **화이트리스트** — `FRIDAY_TELEGRAM_ALLOWED_IDS` 에 없는 user_id 의 텍스트
  메시지는 조용히 무시 (로그만 남김). `/id` 는 예외(자기 ID 확인용).
- **Rate limit** — 사용자당 60초당 최대 20 메시지. 초과 시 대기 안내.
  (`bot.py` 상단의 `RATE_LIMIT_MSGS` / `RATE_LIMIT_WINDOW` 조정 가능)
- **위험 명령 차단** — Bash 호출 시 `can_use_tool` 콜백이 다음 패턴을 deny:
  `sudo`, `su -`, `rm -rf` (대소문자 무관), `mkfs`, `dd of=/dev/...`,
  `>/dev/sda` 류, `shutdown`/`reboot`/`halt`/`poweroff`,
  `curl|sh` 류 파이프-실행, `chmod 777`, `chown root`, 포크 폭탄,
  `.env` / SSH 키 / AWS 자격증명 읽기. 정당한 작업이 막히면 로컬 REPL에서
  실행할 것.
- **`permission_mode="bypassPermissions"` + `can_use_tool` 게이트** — 봇은
  대화형 승인 채널이 없으므로 콜백이 유일한 정책 지점. 콜백을 통과한 도구만
  실행됨.

추가로 사용자가 해야 하는 것:

- **텔레그램 2FA 켜기** — Settings → Privacy → Two-Step Verification.
  계정 탈취 = 봇 권한 탈취이므로 필수.
- **Anthropic 콘솔에서 월 spend limit 설정** — 만에 하나 봇이 폭주해도
  요금 폭탄 방어선.
- **`.env` 파일 권한** — `chmod 600 ~/phython/.env` 로 본인만 읽도록.
- **봇 토큰 유출 시** — BotFather에서 `/revoke` 로 즉시 회수, 새 토큰 발급.
- **민감 정보 직접 입력 금지** — 모든 메시지는 Anthropic API로 전송됨.

알려진 한계:

- `dispatch_to_repo` 로 띄운 서브 에이전트는 `can_use_tool` 을 상속하지 않음.
  서브 에이전트는 해당 repo 컨텍스트에서 `permission_mode="acceptEdits"` 로
  동작하므로 파일 편집은 자동 승인, Bash는 별도 정책에 따름.
- 정규식 기반 차단은 우회 시도에 약함 (`s​udo` 같은 zero-width 삽입 등).
  완벽한 방어가 아니라 실수 방지용. 화이트리스트가 1차 방어선.

## 9. 동작 방식

- REPL과 동일한 `ClaudeSDKClient` 한 개를 봇 수명 내내 공유.
- 한 번에 한 메시지만 처리(asyncio.Lock). 처리 중에 새 메시지가 와도
  텔레그램 큐에 쌓였다 순차 처리됨.
- 응답 텍스트는 4096자 제한에 맞춰 자동 분할.
- 백그라운드 잡 완료, PR 감시 알림 등은 5초마다 폴링해서 푸시.
- Mac 슬립 들어가도 텔레그램 서버가 메시지 보관 → 깨어나면 받아 처리.

## 10. 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `error: FRIDAY_TELEGRAM_BOT_TOKEN not set` | `.env` 누락 또는 잘못된 디렉토리에서 실행 |
| 봇이 응답 안함 | 화이트리스트에 user_id 없음. `/id` 로 확인 후 `.env` 갱신 → 재시작 |
| `Conflict: terminated by other getUpdates` | 같은 토큰으로 다른 인스턴스가 이미 돌고 있음. 하나만 켤 것 |
| 응답이 잘림 | 4000자 청크. 정상 동작이며 자동으로 여러 메시지로 나뉨 |
