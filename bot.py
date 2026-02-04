import asyncio
import os
import time
import random
import datetime
from datetime import timezone, timedelta

import re

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception as _e:
    genai = None
    GEMINI_AVAILABLE = False
    print(f"[WARN] Gemini 라이브러리 로드 실패: {_e} — pip install google-generativeai 실행 후 봇을 다시 켜 주세요.")

# KST (한국 표준시) - 다음날 00시 초기화용
KST = timezone(timedelta(hours=9))

# ======================= 설정 ==========================
# 인텐트 설정: 멤버/보이스 이벤트 받으려면 필요
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- 음성 채널 ID들 (네가 준 거 그대로) ----
CHANNELS = {
    "STUDY_1H": 1466068226315387137,
    "STUDY_1_5H": 1466068279406628995,
    "STUDY_2H": 1466072897331396777,
    "STUDY_2_5H": 1466072931535683689,
    "STUDY_3H": 1466072954260684863,
    "STUDY_3H_PLUS": 1466074628412674150,
    "STUDY_UNLIMITED_MUTE": 1466074907552125000,
    "REST": 1466045072955932766,       # 쉼터(음소거해제)
    "TEST_2M": 1466414888107638949,    # 테스트용 2분
    "FREEDOM": 1466413655708008785,    # 해방 (할당량 채운 사람만 자유)
}

# 각 공부방별 제한 시간 (분 단위)
ROOM_LIMIT_MINUTES = {
    CHANNELS["STUDY_1H"]: 60,
    CHANNELS["STUDY_1_5H"]: 90,
    CHANNELS["STUDY_2H"]: 120,
    CHANNELS["STUDY_2_5H"]: 150,
    CHANNELS["STUDY_3H"]: 180,
    CHANNELS["STUDY_3H_PLUS"]: 9999,          # 3시간 이상 방: 사실상 무제한
    CHANNELS["STUDY_UNLIMITED_MUTE"]: 9999,   # 시간무제한 음소거 공부방
    CHANNELS["TEST_2M"]: 2,                    # 테스트용 2분
}

# 안내 멘트 보낼 텍스트 채널 ID
# 👉 네가 준 채팅 로그 채널 ID
NOTICE_TEXT_CHANNEL_ID = 1466081510221287578
# AI 대화 + 공부시간 답변 채널 (여기서만 제미나이/공부 멘트)
AI_CHAT_CHANNEL_ID = 1468249844073107597
# 제미나이 API 키 (Gemini AI 대화용). .env의 GEMINI_API_KEY 사용
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 유저별 공부 상태 저장 (메모리)
# {
#   user_id: {
#       "in_study": bool,
#       "current_channel_id": int | None,
#       "last_join_at": float | None,  # timestamp (초)
#       "total_study_sec": float,
#   }
# }
study_state = {}

# 체크 주기 (초)
CHECK_INTERVAL_SECONDS = 30

# 쉼터에 들어온 시각 (user_id -> timestamp)
rest_entered_at = {}
# 쉼터 핀잔 이미 보낸 분 (user_id -> {5, 10})
rest_pinch_sent = {}
# 오늘 할당량 채운 사람 (해방 입장 허용, 재입장 시 음소거 안 걸림). 다음날 00시 초기화.
completed_quota_today = set()
# 마지막으로 00시 초기화한 날 (KST "YYYY-MM-DD")
last_reset_date = None
# 할당량 안 채운 사람 오늘 채팅 횟수 (user_id -> int). 다음날 00시 초기화.
message_count_today = {}
# 할당량 안 채운 사람 채팅 제한 (이 횟수 초과하면 핀잔 + 역할로 채팅 불가)
CHAT_LIMIT_FOR_NON_QUOTA = 5
# 6회 넘긴 사람한테 부여할 역할 ID. 이 역할에 "메시지 보내기" 거부해두면 6회 이후엔 채팅 자체가 안 됨.
# 설정법: 서버 설정 → 역할 → 새 역할(예: "채팅제한") 생성 → 채널별로 그 역할 "메시지 보내기" 끄기 → 아래에 역할 ID 넣기.
# 비우면 6회 이후에도 메시지만 삭제되고 핀잔만 뜸(계속 치면 계속 삭제).
CHAT_RESTRICTED_ROLE_ID = None  # 예: 123456789012345678
# 채팅 제한 역할 부여한 유저 (자정·할당량 채우면 역할 해제)
restricted_chat_user_ids = set()

# AI 채널 사용 횟수: 오늘 사용한 횟수 (user_id -> int). 자정 초기화. 기회 = 1 + floor(순공시간/3600) - 이 값
ai_usage_count_today = {}
# 1시간 충전 시 "1회 충전되었어요" 안내한 마지막 시간 (user_id -> int). 자정 초기화.
ai_charged_hour_announced = {}


async def maybe_reset_midnight() -> None:
    """다음날 00시(KST) 넘기면 모든 시간·쉼터·해방 기록 초기화"""
    global last_reset_date
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    if last_reset_date is not None and today != last_reset_date:
        study_state.clear()
        completed_quota_today.clear()
        rest_entered_at.clear()
        rest_pinch_sent.clear()
        message_count_today.clear()
        ai_usage_count_today.clear()
        ai_charged_hour_announced.clear()
        # 채팅 제한 역할 해제
        if CHAT_RESTRICTED_ROLE_ID is not None:
            for guild in bot.guilds:
                role = guild.get_role(CHAT_RESTRICTED_ROLE_ID)
                if role is None:
                    continue
                for uid in list(restricted_chat_user_ids):
                    member = guild.get_member(uid)
                    if member and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
        restricted_chat_user_ids.clear()
    last_reset_date = today


# ======================= 유틸 함수들 (킹받는 말투 랜덤) ==========================
def snarky_prefix() -> str:
    """살짝 띠꺼운 말투 앞부분"""
    return random.choice([
        "또 왔네요, ", "아직도 버티는 중이네요, ", "이 정도로 해서 되겠어요, ",
        "에휴 참… ", "공부하는 척은 아주 열심히네요, ", "와 진짜… ",
        "어이 어이, ", "자네 또 왔군, ", "참나… ", "킹받게 하지 마라, ",
    ])


def snarky_done_message(member_mention: str) -> str:
    """시간 다 됐을 때 멘트 (해방 이동 시 이거만 뜨게)"""
    return random.choice([
        f"{member_mention} 그래서 공부 다 하신 거 맞죠? ㅎ 안 끝났으면… 뭐 알아서 하시구요.",
        f"{member_mention} 할당량 채웠다고? ㅎ 이제 해방 가서 놀아.",
        f"{member_mention} 공부 다 했다고? 잘했어~ 이제 해방으로 가.",
        f"{member_mention} 시간 다 됐다. 공부 끝. 해방 가.",
        f"{member_mention} ㅋ 그래서 진짜 다 한 거 맞지? ㅎ 해방 가.",
    ])


def rest_entry_message(member_mention: str) -> str:
    """쉼터 입장 시"""
    return random.choice([
        f"{snarky_prefix()}{member_mention} 또 쉬러 왔네요? 이번엔 얼마나 누워있을 건데요.",
        f"{snarky_prefix()}{member_mention} 쉬러 오셨군요. 금방 돌아가세요.",
        f"{snarky_prefix()}{member_mention} 휴식 타임이지? 오래 있으면 끌고 간다.",
        f"{snarky_prefix()}{member_mention} 쉬는 거 15분 넘기면 공부방으로 강제 이동이에요.",
        f"{snarky_prefix()}{member_mention} 또 놀러 왔네 ㅋㅋ 얼마나 쉴 거야.",
    ])


def freedom_taunt_message(member_mention: str) -> str:
    """해방에 할당량 안 채우고 들어왔을 때"""
    return random.choice([
        f"{member_mention} ㅋㅋㅋㅋ 공부도 다 안 했으면서 벌써 놀려고 하고 있네 ㅋㅋㅋ 넌 글렀다",
        f"{member_mention} 야 임마 공부 다 하고 와. 여긴 할당량 채운 사람만 오는 데다.",
        f"{member_mention} ㅋㅋ 넌 아직 해방 올 자격 없어. 공부부터 해.",
        f"{member_mention} 공부 안 하고 해방이? ㅋㅋ 넌 글렀다 진짜.",
        f"{member_mention} 할당량 채우고 와. 지금 뭐 하는 거야 ㅋㅋ",
    ])


def study_room_entry_finite(used_str: str, remain_str: str) -> str:
    """유한 공부방 입장 시 (지금까지 X분, 앞으로 Y분)"""
    return random.choice([
        f"지금까지 {used_str} 공부했네. 앞으로 {remain_str} 남았는데 고작 그거 가지고 공부가 되겠어?",
        f"누적 {used_str}, 남은 거 {remain_str}. 그거로 뭘 해 ㅋ",
        f"아직 {remain_str} 남았다. {used_str} 한 거로 만족해?",
        f"앞으로 {remain_str} 남았어. 지금까지 {used_str}밖에 안 했네. 더 해.",
        f"{used_str} 썼고 {remain_str} 남음. 고작 그걸로 공부했다고?",
    ])


def study_room_entry_zero_extra() -> str:
    """유한 공부방인데 남은 시간 0분일 때 추가 멘트"""
    return random.choice([
        " 근데 남은 시간이 0분이네요? 곧 끌려나가도 놀라지 말아요.",
        " 0분 남았다. 곧 해방(아니면 공부방)으로 끌고 간다.",
        " 시간 다 됐다. 곧 이동시킨다.",
        " 남은 거 0분. 빨리 마무리해.",
    ])


def study_unlimited_mute_message() -> str:
    """시간무제한 음소거 공부방 입장 시"""
    return random.choice([
        "와.... 여기까지 올 정도면 어지간히 놀았나 보네요? 이제 진짜 좀 하겠다는 거죠?",
        "시간무제한 방까지 왔네 ㅋㅋ 진짜 하려는 거 맞지?",
        "여기 오면 놀면 안 된다. 진짜 공부하는 거다.",
        "무제한 방이니까 이제 제대로 해라.",
    ])


def study_3h_plus_message(used_str: str) -> str:
    """3시간 이상 등 무제한 계열 입장 시"""
    return random.choice([
        f"여긴 사실상 무제한인데, 그 와중에 지금까지 {used_str}밖에 안 했네요? 더 할 수는 있는 거죠?",
        f"무제한 방인데 {used_str}밖에 안 했어? ㅋ 더 해.",
        f"지금까지 {used_str}. 여기선 더 하라는 거다.",
    ])


def rest_pinch_5min(member_mention: str) -> str:
    """쉼터 5분 경과"""
    return random.choice([
        f"{member_mention} 지금 휴식 5분째인데 언제까지 쉴려고…? 그걸 지금 공부라 하는 거야…? 15분 넘기면 3시간 공부방으로 끌고 간다.",
        f"{member_mention} 5분 됐다. 더 쉬면 3시간 방으로 보낸다. 새낀 더 많이 공부해라.",
        f"{member_mention} 5분째 쉬는 중이네. 10분 되면 또 말하고 15분 되면 3시간 공부방으로 끌고 간다.",
    ])


def rest_pinch_10min(member_mention: str) -> str:
    """쉼터 10분 경과"""
    return random.choice([
        f"{member_mention} 지금 휴식 10분째인데 언제까지 쉴려고…? 그걸 지금 공부라 하는 거야…? 5분 더 있으면 3시간 방으로 강제 이동이다.",
        f"{member_mention} 10분이다. 5분 더 있으면 3시간 공부방으로 보낸다. 길게 공부하란 뜻이다.",
        f"{member_mention} 10분째 놀고 있네. 이게 공부야? 15분 되면 3시간 공부방으로 끌고 간다. 더 해라.",
    ])


def sunong_time_reply(member_mention: str, study_minutes: int) -> str:
    """!순공시간 명령 시 꼽주기 (오늘 누적 공부 시간 알려주기)"""
    used_str = format_minutes(study_minutes)
    return random.choice([
        f"{member_mention} {used_str} 공부했는데, 그거 고작 공부했다고 지금 물어본 거야?",
        f"{member_mention} 오늘 {used_str}. 원래 공부 잘하는 애들은 시간 안 물어보던데....",
        f"{member_mention} {used_str}다. 그걸로 만족해? 더 해라.",
        f"{member_mention} 지금까지 {used_str}. 시간 세는 거 말고 공부나 더 해.",
        f"{member_mention} {used_str} 공부했네. 그거 가지고 물어보기나 하네 ㅋ",
        f"{member_mention} 오늘 순공 {used_str}. 적으면 부끄러우니까 더 하고 물어봐.",
        f"{member_mention} {used_str}밖에 안 했어. 시간 체크할 시간에 책 펴라.",
    ])


def chat_limit_pinchan(member_mention: str) -> str:
    """할당량 안 채운 사람이 채팅 5회 초과 시 핀잔"""
    return random.choice([
        f"{member_mention} 야 공부도 안 한 놈이 집중 안 해? 채팅 그만 해.",
        f"{member_mention} 공부 할당량 안 채웠으면 채팅부터 줄여. 집중해.",
        f"{member_mention} 공부도 안 했으면서 채팅만 미친 듯이 치네? 집중 안 해?",
        f"{member_mention} 야. 공부 안 한 놈이 채팅만 하지 말라.",
        f"{member_mention} 할당량 채우고 와. 채팅 그만.",
    ])


def rest_force_move_15min(member_mention: str) -> str:
    """쉼터 15분 → 3시간 공부방 강제 이동 시 (더 길게 공부하란 뜻)"""
    return random.choice([
        f"{member_mention} 어휴 니새끼 공부 안 하니까 내가 강제로라도 시켜야지 원. 3시간 방으로 보낸다. 너 새낀 더 많이 공부해.",
        f"{member_mention} 15분 넘겼다. 이제 3시간 공부방 가. 강제다. 쉬기만 하면 안 되니까 길게 공부해라.",
        f"{member_mention} 쉬는 거 끝. 공부하러 가. 3시간 채워라. 더 오래 해.",
        f"{member_mention} 놀기만 하지 말고 공부해. 3시간 방으로 끌고 간다. 새낀 더 많이 해라.",
        f"{member_mention} 쉬는 데 15분 넘겼으면 이제 공부하는 데 3시간은 해라. 강제로 보낸다.",
        f"{member_mention} 니 새낀 더 많이 공부해야지. 3시간 공부방 가. 거기서 제대로 해.",
        f"{member_mention} 공부 안 하고 쉬기만 하니까 3시간짜리 방으로 보낸다. 길게 해라.",
        f"{member_mention} 어휴… 쉬기만 하네. 3시간 공부방 가서 제대로 길게 공부해라.",
    ])


def is_study_channel(channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    return channel_id in ROOM_LIMIT_MINUTES


def is_rest_channel(channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    return channel_id == CHANNELS["REST"]


def is_freedom_channel(channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    return channel_id == CHANNELS["FREEDOM"]


def get_user_state(user_id: int) -> dict:
    if user_id not in study_state:
        study_state[user_id] = {
            "in_study": False,
            "current_channel_id": None,
            "last_join_at": None,
            "total_study_sec": 0.0,
        }
    return study_state[user_id]


def update_user_study_time(user_id: int) -> None:
    """현재 시간 기준으로 직전 입장 시각부터 누적 공부 시간 추가"""
    import time

    state = get_user_state(user_id)
    if not state["in_study"] or state["last_join_at"] is None:
        return

    now = time.time()
    diff = now - state["last_join_at"]
    if diff > 0:
        state["total_study_sec"] += diff
        state["last_join_at"] = now


def get_remaining_minutes(user_id: int, room_channel_id: int) -> int:
    """해당 공부방 기준으로 남은 시간(분) 계산"""
    state = get_user_state(user_id)
    limit = ROOM_LIMIT_MINUTES.get(room_channel_id, 9999)
    total_minutes = int(state["total_study_sec"] // 60)
    return limit - total_minutes


def format_minutes(mins: int) -> str:
    if mins <= 0:
        return "0분"
    h = mins // 60
    m = mins % 60
    if h > 0 and m > 0:
        return f"{h}시간 {m}분"
    if h > 0:
        return f"{h}시간"
    return f"{m}분"


# ---------- AI 채널: 공부시간 입력 파싱 / 제미나이 대화 ----------
def parse_study_minutes_from_message(text: str) -> int | None:
    """메시지에서 'N시간', 'N분', 'N시간 M분' 추출해서 총 분 단위로 반환. 없으면 None."""
    text = text.strip()
    total_min = 0
    # N시간
    m = re.search(r"(\d+)\s*시간", text)
    if m:
        total_min += int(m.group(1)) * 60
    # N분
    m = re.search(r"(\d+)\s*분", text)
    if m:
        total_min += int(m.group(1))
    if total_min > 0:
        return total_min
    return None


def reply_for_study_input(minutes: int, mention: str) -> str:
    """'N시간/분 공부했어' 입력했을 때 꼽주기 멘트."""
    s = format_minutes(minutes)
    return random.choice([
        f"{mention} {s} 했는데 그거 고작이야? 더 해라.",
        f"{mention} {s}면 시작은 한 거다. 내일은 더 해.",
        f"{mention} {s} 공부했다고? ㅋ 괜찮은데 더 하면 좋겠다.",
        f"{mention} 오늘 {s}네. 그거로 만족하지 말고 더 해라.",
    ])


# AI 채널용 시스템 프롬프트: 츤데레 + 꼼꼼한 공부 조언
AI_CHANNEL_SYSTEM_PROMPT = """너는 공부하는 사람한테 츤데레처럼 말하면서 조언하는 AI다.

[반드시 지켜야 할 것]
1. 핵심을 숨기지 말고 한눈에 보이게 써라. 조언할 때 "① … ② … ③ …" 또는 "· … · …" 같은 번호·불릿을 써서 핵심만 스캔해도 읽히게 해 줘. "정리하면", "핵심만 말하면" 다음에 요약을 넣는 것도 좋아. 긴 말 속에 핵심을 묻어두지 말고 드러나게.
2. 공부 조언은 꼼꼼하고 자세하게 하되, 위처럼 구조를 잡아서 (1) 지금 할 행동 (2) 그 이유 (3) 나중에 점검할 것 같은 걸 구분해서 써라.
3. 말할 때 먼저 살짝 꼽주듯이 한마디 (예: "에휴 그거 가지고?", "겨우 그거?"), 그 다음 진심으로 조언하는 톤으로 이어가라.
4. 말투는 통통 튀게. 존댓말/반말 섞어도 됨. 츤데레 느낌 유지하되, 조언 부분은 확실히 알려주는 느낌으로.
5. 답변 길이는 조언이 들어가면 5~10문장 정도. 한국어."""


def is_study_query(text: str) -> bool:
    """'내 공부시간', '순공', '얼마나 했어' 등 조회 의도인지."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        return False
    if "순공" in t or "공부시간" in t or "공부시간" in text:
        return True
    if "얼마나" in t and ("했" in t or "해" in t):
        return True
    if "내" in t and ("공부" in t or "시간" in t):
        return True
    return False


# 429 시 봇이 보낼 안내 문구 (사용자에게 표시)
GEMINI_QUOTA_MESSAGE = "지금 API 한도가 다 찼어요. 잠시 뒤에 다시 시도하거나, Google AI Studio(https://aistudio.google.com)에서 사용량·한도 확인해 주세요."

# API에서 조회한 사용 가능 모델 목록 캐시 (봇 켜질 때 한 번 조회)
_gemini_models_cache = None

# True면 1.5 Flash만 사용 (API 모델 목록 조회 안 함, 아래 목록만 시도)
GEMINI_USE_ONLY_15_FLASH = True

# 1.5 Flash 전용 모델 목록 (GEMINI_USE_ONLY_15_FLASH=True일 때만 사용)
# 1.5 시도 후 실패하면 2.5 Flash로 폴백
GEMINI_15_FLASH_MODELS = (
    "gemini-flash-latest",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

# 1.5 전용 끄면 쓰는 목록 (모델 목록 조회 실패 시)
GEMINI_MODEL_FALLBACK = (
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


async def _fetch_available_gemini_models() -> list:
    """v1beta/models 로 사용 가능한 모델 목록 조회. generateContent 지원하는 것만, 이름 순."""
    if not (GEMINI_API_KEY and GEMINI_API_KEY.strip()):
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception as e:
        print(f"[WARN] Gemini 모델 목록 조회 실패: {e}")
        return []
    out = []
    for m in (data.get("models") or []):
        name = m.get("name") or ""
        if name.startswith("models/"):
            name = name[7:]
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" in methods and name:
            out.append(name)
    return sorted(out)


async def get_gemini_reply(user_message: str, image_bytes: bytes | None = None, image_mime: str = "image/jpeg") -> tuple[str | None, str | None]:
    """제미나이 v1beta REST API로 직접 generateContent 호출. 반환: (답변 텍스트, 사용한 모델명) 또는 (None, None)."""
    global _gemini_models_cache
    if not (GEMINI_API_KEY and GEMINI_API_KEY.strip()):
        print("[WARN] Gemini: API 키가 비어 있음.")
        return (None, None)
    import base64
    import asyncio

    # 1.5 Flash만 쓸 때는 API 목록 조회 안 하고 고정 목록만 사용
    if GEMINI_USE_ONLY_15_FLASH:
        models_to_try = list(GEMINI_15_FLASH_MODELS)
        print(f"[Gemini] 1.5 Flash 전용 — 시도 순서: {', '.join(models_to_try)}")
    else:
        if _gemini_models_cache is None:
            _gemini_models_cache = await _fetch_available_gemini_models()
            if _gemini_models_cache:
                print(f"[Gemini] 사용 가능 모델: {', '.join(_gemini_models_cache)}")
            else:
                print("[Gemini] 모델 목록 조회 실패 → 기본 목록 사용")
        models_to_try = _gemini_models_cache if _gemini_models_cache else list(GEMINI_MODEL_FALLBACK)

    user_text = (user_message.strip() or "이거 봐줘.")[:4000]
    full_prompt = f"[역할 지시]\n{AI_CHANNEL_SYSTEM_PROMPT}\n\n[사용자 말]\n{user_text}"

    parts = []
    if image_bytes and len(image_bytes) < 20 * 1024 * 1024:
        parts.append({
            "inlineData": {
                "mimeType": image_mime,
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })
    parts.append({"text": full_prompt})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 1500},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    async def _fetch():
        async with aiohttp.ClientSession() as session:
            for model_name in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
                try:
                    async with session.post(url, headers=headers, json=body) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            cands = data.get("candidates") or []
                            if not cands:
                                continue
                            content = cands[0].get("content") or {}
                            part_list = content.get("parts") or []
                            if not part_list:
                                continue
                            text = (part_list[0].get("text") or "").strip()[:2000]
                            if text:
                                print(f"[Gemini] 답변 생성됨 — 사용 모델: {model_name}")
                                return (text, model_name)
                        elif resp.status == 429:
                            print(f"[WARN] Gemini {model_name} 한도 초과(429), 다음 모델 시도")
                            continue
                        elif resp.status == 404:
                            print(f"[WARN] Gemini {model_name} 없음(404), 다음 모델 시도")
                            continue
                        else:
                            text = await resp.text()
                            print(f"[WARN] Gemini REST {model_name} {resp.status}: {text[:300]}")
                            continue
                except Exception as e:
                    print(f"[WARN] Gemini {model_name} 요청 오류: {e}")
                    continue
        return (None, None)

    try:
        return await asyncio.wait_for(_fetch(), timeout=25.0)
    except asyncio.TimeoutError:
        print("[WARN] Gemini 응답 시간 초과(25초)")
        return (None, None)
    except Exception as e:
        print(f"[WARN] Gemini REST 오류: {e}")
        return (None, None)


async def send_notice(guild: discord.Guild, content: str) -> None:
    """안내용 텍스트 채널로 메시지 보내기 (권한 없으면 그냥 무시)"""
    if NOTICE_TEXT_CHANNEL_ID is None:
        return
    channel = guild.get_channel(NOTICE_TEXT_CHANNEL_ID)
    if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(content)
        except discord.Forbidden:
            # 채널 권한 부족하면 봇이 죽지 않게 그냥 패스
            print(f"[WARN] 채널 {channel.id} 에 메시지 보낼 권한이 없습니다.")


# ======================= Koyeb Health Check API ==========================
HEALTH_CHECK_PORT = 8000

async def health_check(request: web.Request) -> web.Response:
    """Koyeb이 봇 상태 확인용으로 호출하는 엔드포인트. 200 OK 반환."""
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Health Check용 HTTP 서버를 백그라운드로 띄움. Koyeb 배포 시 필요."""
    app = web.Application()
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
    await site.start()
    print(f"[Health Check] 서버 시작 — http://0.0.0.0:{HEALTH_CHECK_PORT}/health")

async def ping_self():
    """Koyeb 수면 모드(scale to zero) 방지: 주기적으로 자신의 URL 호출. KOYEB_URL 환경변수 있으면 실행."""
    koyeb_url = os.getenv("KOYEB_URL") or os.getenv("KOYEP_URL")  # 블로그에선 KOYEP 오타로 적힌 경우 감안
    if not koyeb_url:
        return
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with aiohttp.ClientSession() as session:
                url = koyeb_url.rstrip("/") + "/health"
                await session.get(url, timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass
        await asyncio.sleep(180)


# ======================= 이벤트 핸들러 ==========================
@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user} (ID: {bot.user.id})")
    if GEMINI_AVAILABLE and GEMINI_API_KEY:
        print("Gemini AI: 사용 가능 (API 키 설정됨)")
    else:
        print("Gemini AI: 비활성 —", "라이브러리 없음" if not GEMINI_AVAILABLE else "API 키 없음")
    # Koyeb Health Check API 서버 시작 (배포 시 상태 확인용)
    bot.loop.create_task(start_web_server())
    # 수면 모드 방지 (KOYEB_URL 설정 시에만)
    bot.loop.create_task(ping_self())
    if not check_study_time.is_running():
        check_study_time.start()
        print("공부 시간 체크 루프 시작")
    if not check_rest_time.is_running():
        check_rest_time.start()
        print("쉼터 체크 루프 시작")


@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong!")


@bot.command(name="gemini테스트")
async def gemini_test(ctx: commands.Context):
    """AI 채널이 아닌 곳에서도 Gemini 연결 테스트 (관리자 디버그용)"""
    if ctx.channel.id != AI_CHAT_CHANNEL_ID:
        await ctx.send("이 명령은 AI 채널에서만 사용할 수 있어요.", delete_after=10)
        return
    await ctx.send("Gemini 호출 중...")
    reply, model_used = await get_gemini_reply("한 문장으로 인사만 해줘.")
    if reply:
        await ctx.send(f"성공: {reply[:500]}")
    else:
        await ctx.send("실패: 위에 봇이 돌아가는 콘솔/터미널에 오류가 찍혀 있을 거예요. 확인해 주세요.")

@bot.command(name="순공시간")
async def sunong_time(ctx: commands.Context):
    """오늘 누적 공부 시간 알려주기 (꼽주기 멘트)"""
    await maybe_reset_midnight()
    user_id = ctx.author.id
    state = get_user_state(user_id)
    total_minutes = int(state["total_study_sec"] // 60)
    msg = sunong_time_reply(ctx.author.mention, total_minutes)
    await ctx.send(msg)


@bot.command(name="AI횟수")
async def ai_count(ctx: commands.Context):
    """남은 AI 사용 기회 보여주기"""
    await maybe_reset_midnight()
    update_user_study_time(ctx.author.id)
    state = get_user_state(ctx.author.id)
    study_hours = int(state["total_study_sec"] // 3600)
    used = ai_usage_count_today.get(ctx.author.id, 0)
    remaining = max(0, 1 + study_hours - used)
    await ctx.send(
        f"{ctx.author.mention} 남은 AI 사용 기회 **{remaining}번**이에요. "
        f"(오늘 순공 {study_hours}시간 → +{study_hours}회, 사용 {used}회)"
    )


@bot.event
async def on_message(message: discord.Message):
    """할당량 안 채운 사람 채팅 5회 제한, 초과 시 핀잔"""
    if message.author.bot:
        await bot.process_commands(message)
        return

    await maybe_reset_midnight()
    user_id = message.author.id

    if user_id not in completed_quota_today:
        message_count_today[user_id] = message_count_today.get(user_id, 0) + 1
        if message_count_today[user_id] > CHAT_LIMIT_FOR_NON_QUOTA:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
            try:
                await message.channel.send(chat_limit_pinchan(message.author.mention))
            except discord.Forbidden:
                pass
            # 6회 넘기면 채팅 제한 역할 부여 → 진짜 채팅 불가
            if CHAT_RESTRICTED_ROLE_ID is not None:
                role = message.guild.get_role(CHAT_RESTRICTED_ROLE_ID)
                if role and role not in message.author.roles:
                    try:
                        await message.author.add_roles(role)
                        restricted_chat_user_ids.add(user_id)
                    except discord.Forbidden:
                        pass

    # AI 채널: 기회 제한 (1 + 순공 1시간당 1회, 사용 시 1회 차감)
    if message.channel.id == AI_CHAT_CHANNEL_ID and not message.content.strip().startswith("!"):
        content = message.content.strip()
        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        if not content and not has_image:
            await bot.process_commands(message)
            return

        await maybe_reset_midnight()
        update_user_study_time(user_id)
        state = get_user_state(user_id)
        study_hours = int(state["total_study_sec"] // 3600)
        used = ai_usage_count_today.get(user_id, 0)
        remaining = max(0, 1 + study_hours - used)

        if remaining <= 0:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
            try:
                await message.channel.send(
                    f"{message.author.mention} AI 사용 기회가 없어요. 공부 1시간당 1회 충전돼요. `!순공시간`으로 오늘 순공 확인해 보세요."
                )
            except discord.Forbidden:
                pass
            await bot.process_commands(message)
            return

        # 순공 조회는 !순공시간 명령어로만. 그 외 전부 AI로 처리
        image_bytes = None
        image_mime = "image/jpeg"
        for a in message.attachments:
            if a.content_type and a.content_type.startswith("image/"):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(a.url) as resp:
                            if resp.status == 200:
                                image_bytes = await resp.read()
                                image_mime = a.content_type or "image/jpeg"
                except Exception as e:
                    print(f"[WARN] 이미지 다운로드 실패: {e}")
                break
        # 시도 순서 도는 동안 디스코드에 "입력 중..." 표시
        try:
            async with message.channel.typing():
                gemini_reply, model_used = await get_gemini_reply(content or "이거 봐줘.", image_bytes, image_mime)
        except Exception:
            gemini_reply, model_used = None, None
        if gemini_reply and gemini_reply.strip():
            try:
                await message.channel.send(gemini_reply[:2000])
            except discord.Forbidden:
                pass
        else:
            try:
                await message.channel.send(GEMINI_QUOTA_MESSAGE)
            except discord.Forbidden:
                pass

        ai_usage_count_today[user_id] = ai_usage_count_today.get(user_id, 0) + 1
        left = max(0, 1 + study_hours - ai_usage_count_today[user_id])
        try:
            await message.channel.send(f"{message.author.mention} 기회 **{left}번** 남았어요.")
        except discord.Forbidden:
            pass

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """음성 채널 입장/이동/퇴장 감지해서 공부 시간 로직 처리"""
    if member.bot:
        return

    user_id = member.id
    guild = member.guild
    state = get_user_state(user_id)

    old_channel_id = before.channel.id if before.channel else None
    new_channel_id = after.channel.id if after.channel else None

    # 채널이 안 바뀌었는데(예: 자기 음소거/헤드폰만 바꾼 경우) 이벤트 들어오면 무시
    if old_channel_id == new_channel_id:
        return

    # 우선, 직전까지의 공부 시간 정산
    update_user_study_time(user_id)

    # 쉼터에서 나갔으면 쉼터 체크용 기록 삭제
    if old_channel_id is not None and is_rest_channel(old_channel_id):
        rest_entered_at.pop(user_id, None)
        rest_pinch_sent.pop(user_id, None)

    # ===== 1) 완전히 보이스를 나간 경우 =====
    if old_channel_id is not None and new_channel_id is None:
        if is_study_channel(old_channel_id):
            state["in_study"] = False
            state["current_channel_id"] = None
            state["last_join_at"] = None
        return

    # ===== 2) 보이스에 들어오거나 채널 이동한 경우 =====
    if new_channel_id is not None:
        joined_study = is_study_channel(new_channel_id)
        joined_rest = is_rest_channel(new_channel_id)
        joined_freedom = is_freedom_channel(new_channel_id)

        # --- 쉼터 입장 ---
        if joined_rest:
            state["in_study"] = False
            state["current_channel_id"] = None
            state["last_join_at"] = None
            rest_entered_at[user_id] = time.time()
            rest_pinch_sent[user_id] = set()
            try:
                await member.edit(mute=False)
            except discord.Forbidden:
                print(f"[WARN] {member} 서버 음소거 해제 권한 없음")
            await send_notice(guild, rest_entry_message(member.mention))
            return

        # --- 해방 입장 (할당량 안 채우고 들어오면 음소거 + 꼽주기) ---
        if joined_freedom:
            state["in_study"] = False
            state["current_channel_id"] = None
            state["last_join_at"] = None
            if user_id in completed_quota_today:
                # 할당량 채운 사람: 나갔다 들어와도 음소거 안 걸림
                try:
                    await member.edit(mute=False)
                except discord.Forbidden:
                    print(f"[WARN] {member} 서버 음소거 해제 권한 없음")
            else:
                # 할당량 안 채운 사람: 서버 음소거 + 꼽주기
                try:
                    await member.edit(mute=True)
                except discord.Forbidden:
                    print(f"[WARN] {member} 서버 음소거 권한 없음")
                await send_notice(guild, freedom_taunt_message(member.mention))
            return

        # --- 공부방 입장 ---
        if joined_study:
            state["in_study"] = True
            state["current_channel_id"] = new_channel_id
            state["last_join_at"] = time.time()

            try:
                await member.edit(mute=True)
            except discord.Forbidden:
                print(f"[WARN] {member} 서버 음소거 권한 없음 (Mute Members 권한 확인 필요)")

            remaining = get_remaining_minutes(user_id, new_channel_id)
            total_minutes = int(state["total_study_sec"] // 60)
            limit_minutes = ROOM_LIMIT_MINUTES.get(new_channel_id, 9999)

            # 시간무제한 음소거 공부방 전용 멘트
            if new_channel_id == CHANNELS["STUDY_UNLIMITED_MUTE"]:
                core = study_unlimited_mute_message()
            # 일반 유한 공부방: 남은 시간만 + 꼽주기
            elif limit_minutes < 9999:
                used_str = format_minutes(total_minutes)
                remain_str = format_minutes(remaining)
                core = study_room_entry_finite(used_str, remain_str)
            # 그 외 무제한 계열 (3시간 이상 방 등)
            else:
                used_str = format_minutes(total_minutes)
                core = study_3h_plus_message(used_str)

            msg = f"{snarky_prefix()}{member.mention} {core}"
            if remaining <= 0 and limit_minutes < 9999:
                msg += study_room_entry_zero_extra()

            await send_notice(guild, msg)
            return

        # --- 공부/쉼터/해방이 아닌 다른 음성 채널 ---
        state["in_study"] = False
        state["current_channel_id"] = None
        state["last_join_at"] = None
        try:
            await member.edit(mute=False)
        except discord.Forbidden:
            print(f"[WARN] {member} 서버 음소거 해제 권한 없음")


# ======================= 주기적 체크 루프 ==========================
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_study_time():
    """주기적으로 공부 시간 체크해서 다 된 사람 해방으로 이동시키기"""
    await maybe_reset_midnight()

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue

            user_id = member.id
            state = study_state.get(user_id)
            if not state or not state["in_study"]:
                continue

            voice = member.voice
            if voice is None or voice.channel is None:
                continue

            channel_id = voice.channel.id
            if not is_study_channel(channel_id):
                continue

            # 현재까지 공부 시간 갱신
            update_user_study_time(user_id)
            state = get_user_state(user_id)
            study_hours = int(state["total_study_sec"] // 3600)
            last_announced = ai_charged_hour_announced.get(user_id, -1)
            if study_hours > last_announced:
                ai_charged_hour_announced[user_id] = study_hours
                try:
                    await send_notice(guild, f"{member.mention} AI 이용횟수 1회 충전되었어요.")
                except Exception:
                    pass

            remaining = get_remaining_minutes(user_id, channel_id)

            if remaining <= 0:
                # 할당량 채운 걸 먼저 기록 → move_to 하면 on_voice_state_update 에서 해방 입장 시 "공부 안 했으면서" 안 뜸
                completed_quota_today.add(user_id)
                # 채팅 제한 역할 해제 (할당량 채우면 채팅 다시 가능)
                if user_id in restricted_chat_user_ids and CHAT_RESTRICTED_ROLE_ID is not None:
                    role = guild.get_role(CHAT_RESTRICTED_ROLE_ID)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
                    restricted_chat_user_ids.discard(user_id)
                # 해방으로 이동
                freedom_channel = guild.get_channel(CHANNELS["FREEDOM"])
                if isinstance(freedom_channel, discord.VoiceChannel):
                    try:
                        await member.move_to(freedom_channel)
                    except Exception as e:
                        print(f"해방 이동 실패 ({member}): {e}")
                state["in_study"] = False
                state["current_channel_id"] = None
                state["last_join_at"] = None

                try:
                    await member.edit(mute=False)
                except discord.Forbidden:
                    print(f"[WARN] {member} 서버 음소거 해제 권한 없음")

                await send_notice(guild, snarky_done_message(member.mention))


@tasks.loop(seconds=60)
async def check_rest_time():
    """쉼터에 오래 있으면 5/10분 핀잔, 15분 시 공부방으로 강제 이동"""
    await maybe_reset_midnight()

    now = time.time()
    for guild in bot.guilds:
        study_room = guild.get_channel(CHANNELS["STUDY_3H"])
        if not isinstance(study_room, discord.VoiceChannel):
            continue

        for member in guild.members:
            if member.bot:
                continue
            voice = member.voice
            if voice is None or voice.channel is None:
                continue
            if voice.channel.id != CHANNELS["REST"]:
                continue

            user_id = member.id
            entered = rest_entered_at.get(user_id)
            if entered is None:
                continue

            elapsed_min = int((now - entered) / 60)
            sent = rest_pinch_sent.setdefault(user_id, set())

            if elapsed_min >= 15:
                try:
                    await member.move_to(study_room)
                except Exception as e:
                    print(f"쉼터→공부방 이동 실패 ({member}): {e}")
                rest_entered_at.pop(user_id, None)
                rest_pinch_sent.pop(user_id, None)
                await send_notice(guild, rest_force_move_15min(member.mention))
            elif elapsed_min >= 10 and 10 not in sent:
                sent.add(10)
                await send_notice(guild, rest_pinch_10min(member.mention))
            elif elapsed_min >= 5 and 5 not in sent:
                sent.add(5)
                await send_notice(guild, rest_pinch_5min(member.mention))


# ======================= 실행 ==========================
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN이 .env에 없습니다. .env 파일을 만들고 DISCORD_TOKEN=봇토큰 을 넣어 주세요.")
    bot.run(token)
