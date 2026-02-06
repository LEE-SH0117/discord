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

from dotenv import load_dotenv
load_dotenv()

# KST (한국 표준시) - 다음날 00시 초기화용
KST = timezone(timedelta(hours=9))

# ======================= 설정 ==========================
# 인텐트 설정: 멤버/보이스 이벤트 받으려면 필요
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- 음성 채널 ID들 ----
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
    CHANNELS["STUDY_3H_PLUS"]: 300,          # 3시간 이상 방: 사실상 무제한
    CHANNELS["STUDY_UNLIMITED_MUTE"]: 9999,   # 시간무제한 음소거 공부방
    CHANNELS["TEST_2M"]: 2,                    # 테스트용 2분
}

# 안내 멘트 보낼 텍스트 채널 ID (공부 로그 / 쉼터 로그 등)
NOTICE_TEXT_CHANNEL_ID = 1466081510221287578
# AI 대화 + 공부시간 답변 채널 (여기서만 제미나이/공부 멘트)
AI_CHAT_CHANNEL_ID = 1468249844073107597
# "N시간 공부하겠다" 채팅하는 채널 → 여기서 인식하면 아래 음성방으로 이동
STUDY_PLEDGE_TEXT_CHANNEL_ID = 1468944422703075430
# 스스로 N시간 공부 선언 시 이동하는 음성 공부방
STUDY_PLEDGE_VOICE_CHANNEL_ID = 1468943967663161418
# 제미나이 API 키 (Gemini AI 대화용). .env의 GEMINI_API_KEY 사용
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 유저별 공부 상태 저장 (메모리)
# {
#   user_id: {
#       "in_study": bool,
#       "current_channel_id": int | None,
#       "last_join_at": float | None,  # timestamp (초)
#       "total_study_sec": float,      # 오늘 누적 (할당량용)
#       "session_study_sec": float,   # 재방문 시 현재 세션만 (할당량 이미 채운 뒤 다시 공부할 때)
#       "session_start_total_sec": float,  # 이번 세션 입장 시점의 total_study_sec (퇴장 시 "방금 N분" 계산용)
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

# !AI횟수추가 명령 사용 가능한 사용자 ID (본인만)
ADMIN_USER_ID = 764463640811143169

# AI 채널 사용 횟수: 오늘 사용한 횟수 (user_id -> int). 자정 초기화. 기회 = 1 + floor(순공시간/3600) - 이 값
ai_usage_count_today = {}
# 1시간 충전 시 "1회 충전되었어요" 안내한 마지막 시간 (user_id -> int). 자정 초기화.
ai_charged_hour_announced = {}
# 정신과 시간(무제한)방에서 5시간 됐을 때 "이동 가능하다" 알림 보낸 유저 (자정 초기화)
unlimited_room_5h_notified_today = set()
# 쉼터: 오늘 방문 횟수 / 누적 쉬운 시간(초) (자정 초기화)
rest_visit_count_today = {}
rest_total_seconds_today = {}
# 스스로 N시간 공부 선언: 목표 분 / 누적 완료 분 (나갔다 들어와도 유지) / 해당 음성방 입장 시각 (자정·달성 시 초기화)
pledge_target_minutes: dict[int, int] = {}
pledge_completed_minutes: dict[int, float] = {}  # 선언한 시간 중 이미 채운 분 (선언방+다른 공부방 포함)
pledge_room_entered_at: dict[int, float] = {}


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
        unlimited_room_5h_notified_today.clear()
        rest_visit_count_today.clear()
        rest_total_seconds_today.clear()
        pledge_target_minutes.clear()
        pledge_completed_minutes.clear()
        pledge_room_entered_at.clear()
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
        f"{member_mention} ㅋㅋ 넌 아직 해방에 올 자격 없어. 공부부터 해.",
        f"{member_mention} 공부 안 하고 해방에? ㅋㅋ 넌 글렀다 진짜.",
        f"{member_mention} 지금 스트레스 좀 받을 거야.근데 이런 스트레스도 필요해. 공부 안 하고 해방 들어온 거 지금은 별거 아닌 것 같지?오늘 자기 전에 생각날 거야",
    ])


def study_room_entry_finite(used_str: str, remain_str: str) -> str:
    """시간제한 공부방 입장 시 (지금까지 X분, 앞으로 Y분)"""
    return random.choice([
        f"지금까지 {used_str} 공부했네. 앞으로 {remain_str} 남았는데 고작 그거 가지고 공부가 되겠어?",
        f"누적 {used_str}, 남은 거 {remain_str}. 그거로 뭘 해 ㅋ",
        f"아직 {remain_str} 남았다. {used_str} 한 거로 만족해?",
        f"앞으로 {remain_str} 남았어. 지금까지 {used_str}밖에 안 했네. 더 해.",
        f"{used_str} 썼고 {remain_str} 남음. 고작 그걸로 공부했다고?",
    ])


def study_room_entry_zero_extra() -> str:
    """시간제한 공부방인데 남은 시간 0분일 때 추가 멘트"""
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
        "지금 스트레스 좀 받을 거야.근데 공부 많이 된다.?",
        "여기 오면 놀면 안 된다. 진짜 공부하는 거다.",
        "이모 여기 시간 무제한으로 서비스 넣어드렸어요~",
    ])


def study_3h_plus_message(used_str: str) -> str:
    """5시간 입장 시"""
    return random.choice([
        f"여긴 5시간 공부방인데.... 그 와중에 지금까지 {used_str}밖에 안 했네요? 5시간이 쉬워보여??",
        f"여긴 공부좀 할려고 하는애들만 오는데인데... {used_str}밖에 안 했어? ㅋ 더 해.",
        f"지금까지 {used_str}. 여긴 진짜들만 오는데이다.",
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


def unlimited_room_can_move_message(member_mention: str) -> str:
    """정신과 시간(무제한)방 5시간 됐을 때 해방 이동 가능 알림 (꼽주기)"""
    return random.choice([
        f"{member_mention} 5시간 채웠다. 이제 해방으로 이동 가능하다. 가고 싶으면 가고, 더 하려면 여기서 쭉 해.",
        f"{member_mention} 할당량 다 채웠네. 이동 가능해. 해방 가도 되고 여기서 계속 해도 되고.",
        f"{member_mention} 5시간 됐다. 이동 가능하다. 놀러 가고 싶으면 해방으로, 아니면 그냥 여기서 더 해라.",
        f"{member_mention} 이제 이동 가능해. 해방 가도 되고 여기서 작업 계속해도 된다.",
    ])


def rest_force_move_15min(member_mention: str) -> str:
    """쉼터 15분 → 3시간 공부방 강제 이동 시 (더 길게 공부하란 뜻)"""
    return random.choice([
        f"{member_mention} 어휴 니놈 공부 안 하니까 내가 강제로라도 시켜야지 원. 3시간 방으로 보낸다. 너 새낀 더 많이 공부해.",
        f"{member_mention} 15분 넘겼다. 이제 3시간 공부방 가. 강제다. 쉬기만 하면 안 되니까 길게 공부해라.",
        f"{member_mention} 쉬는 거 끝. 공부하러 가. 3시간 채워라. 더 오래 해.",
        f"{member_mention} 놀기만 하지 말고 공부해. 3시간 방으로 끌고 간다. 새낀 더 많이 해라.",
        f"{member_mention} 쉬는 데 15분 넘겼으면 이제 공부하는 데 3시간은 해라. 강제로 보낸다.",
        f"{member_mention} 넌 더 많이 공부해야지. 3시간 공부방 가. 거기서 제대로 해.",
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


def is_pledge_voice_channel(channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    return channel_id == STUDY_PLEDGE_VOICE_CHANNEL_ID


def is_study_or_pledge_channel(channel_id: int | None) -> bool:
    return is_study_channel(channel_id) or is_pledge_voice_channel(channel_id)


def study_reentry_message(member_mention: str) -> str:
    """할당량 이미 채운 뒤 공부방 재방문 시 (놀람+응원 꼽주기)"""
    return random.choice([
        f"오.... {member_mention} 다시 공부하게....? 겨우 한 번 하고 끝이 아니었구나. 뭐, 하려면 제대로 해.",
        f"와 {member_mention} 할당량 채우고 또 왔네? 놀랐다. 그 결심 함 좀 지켜봐.",
        f"{member_mention} 또 공부하러 왔어? 한 번 하고 끝일 줄 알았는데. 뭐, 해라.",
        f"오.... {member_mention} 다시 들어왔네. 할당량은 이미 채웠잖아. 그래도 더 한다고? 괜찮은데.",
    ])


def freedom_quota_done_taunt(member_mention: str) -> str:
    """할당량 채운 사람이 해방 왔을 때 (음소거 안 걸리지만 꼽주기)"""
    return random.choice([
        f"그래... 뭐 {member_mention} 넌 오늘 공부 할당량 했으니까.... 그래도 뭔가 좀 한다 싶어서 놀랐는데 역시나....",
        f"{member_mention} 할당량은 채웠네. 그래도 해방 오면 역시 놀려는 거지 ㅋ",
        f"뭐 {member_mention} 넌 오늘 할당량 했으니까 말 안 해. 그래도 여기 와서 논다는 건.... 역시.",
    ])


def study_leave_log_message(member_mention: str, this_session_min: int, today_total_min: int) -> str:
    """공부방 나갈 때 로그용: 방금 N분 + 오늘 총 M분"""
    s1 = format_minutes(this_session_min)
    s2 = format_minutes(today_total_min)
    return f"{member_mention} 공부방 나감. 방금 {s1} 공부했고, 오늘 총 {s2} 공부했음."


def pledge_priority_in_other_room_message(member_mention: str, declared_str: str, remaining_str: str) -> str:
    """약속했는데 다른 공부방 들어왔을 때: 약속한 시간이 우선이다"""
    return random.choice([
        f"{member_mention} 동작그만. 밑장빼기냐? 여기서 공부해도 넌 너가 말한 시간을 공부해야 하는 건 변하지 않아. (선언: {declared_str}, 앞으로 {remaining_str} 더)",
        f"{member_mention} 선언한 {declared_str} 잊지 마. 여기 있어도 그만큼은 해야 해. 앞으로 {remaining_str} 더.",
    ])


def pledge_room_no_declaration_message(member_mention: str) -> str:
    """선언 없이 선언 공부방에 그냥 들어왔을 때"""
    return random.choice([
        f"이 공부방은 스스로 시간을 선언한 사람만 오는 곳인데.... {member_mention} 너 진짜 공부 다짐한 거야?흉추 걸수있어?",
        f"{member_mention} 여긴 선언하고 오는 방이야. 자신있는거지?",
        f"이 방은 공약을 지킬수있는 사람만 쓰는 곳이야. 근데{member_mention}야 난 이렇게 생각해. 너 진짜 스스로 시간은 지킬수있어?",
    ])


def pledge_commit_message(member_mention: str, duration_str: str) -> str:
    """스스로 N시간 공부 선언 시 로그 꼽주기"""
    return random.choice([
        f"{member_mention} 너가 스스로 {duration_str} 공부한다 했으니까, 이건 꼭 지켜라.",
        f"{member_mention} {duration_str} 하겠다고 했으니 말만 하지 말고 해라.시간 충전해줬으니까 가서 공부 시작해.",
        f"{member_mention} 시간 충전해뒀다.{duration_str} 공부한다고 했으면 끝까지 해.",
    ])


def get_user_state(user_id: int) -> dict:
    if user_id not in study_state:
        study_state[user_id] = {
            "in_study": False,
            "current_channel_id": None,
            "last_join_at": None,
            "total_study_sec": 0.0,
            "session_study_sec": 0.0,
            "session_start_total_sec": 0.0,
        }
    return study_state[user_id]


def update_user_study_time(user_id: int) -> None:
    """현재 시간 기준으로 직전 입장 시각부터 누적 공부 시간 추가 (할당량 미달이면 total, 이미 채웠으면 session만)"""
    state = get_user_state(user_id)
    if not state["in_study"] or state["last_join_at"] is None:
        return

    now = time.time()
    diff = now - state["last_join_at"]
    if diff <= 0:
        return

    state["last_join_at"] = now
    if user_id in completed_quota_today:
        state["session_study_sec"] = state.get("session_study_sec", 0) + diff
    else:
        state["total_study_sec"] += diff


def get_remaining_minutes(user_id: int, room_channel_id: int) -> int:
    """해당 공부방 기준으로 남은 시간(분) 계산. 재방문(할당량 이미 채움)이면 세션 시간 기준."""
    state = get_user_state(user_id)
    limit = ROOM_LIMIT_MINUTES.get(room_channel_id, 9999)
    if user_id in completed_quota_today:
        session_minutes = int(state.get("session_study_sec", 0) // 60)
        return limit - session_minutes
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


# ---------- AI 채널 / 선언 채널: 공부시간 입력 파싱 ----------
def parse_study_minutes_from_message(text: str) -> int | None:
    """메시지에서 'N시간', 'N분', 'N시간 M분' 추출해서 총 분 단위로 반환. 없으면 None.
    예: '5시간 28분 공부할거야' → 328분, '1시간30분' → 90분, '45분' → 45분."""
    text = (text or "").strip()
    total_min = 0
    # N시간 (띄어쓰기 없어도 인식)
    m = re.search(r"(\d+)\s*시간", text)
    if m:
        total_min += int(m.group(1)) * 60
    # N분 (띄어쓰기 없어도 인식)
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
        f"{mention} 오늘 {s}정도 공부했네. 그거로 만족하지 말고 더 해라.지금 스트레스 좀 받을 거야.근데 공부 많이 된다.",
    ])


#---------------------- AI 채널용 시스템 프롬프트----------------------
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
GEMINI_QUOTA_MESSAGE = "지금 API 한도가 다 찼어요. 잠시 뒤에 다시 시도하거나,사용량·한도 확인해 주세요."

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


# ---------- 메시지 중복 처리 방지 (봇 여러 개 켜져 있을 때 / 이벤트 중복 시 한 번만 응답) ----------
_DEDUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_msg_locks")
_processed_msg_in_memory: dict[int, float] = {}  # message_id -> 처리 시각 (같은 프로세스 내 중복 방지)
_DEDUP_MEMORY_MAX_AGE = 5.0  # 초
_DEDUP_FILE_MAX_AGE = 120    # 초 (파일 락 삭제까지)


def _message_already_handled(message_id: int) -> bool:
    """이 메시지는 이미 처리됐으면 True (메모리 + 파일 락 확인)."""
    now = time.time()
    # 메모리: 같은 프로세스에서 최근에 처리한 메시지
    for mid, t in list(_processed_msg_in_memory.items()):
        if now - t > _DEDUP_MEMORY_MAX_AGE:
            del _processed_msg_in_memory[mid]
    if message_id in _processed_msg_in_memory:
        return True
    # 파일: 다른 프로세스가 이미 처리 중/완료
    try:
        os.makedirs(_DEDUP_DIR, exist_ok=True)
        lock_path = os.path.join(_DEDUP_DIR, f"{message_id}.lock")
        if os.path.exists(lock_path):
            if now - os.path.getmtime(lock_path) > _DEDUP_FILE_MAX_AGE:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
            else:
                return True
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        _processed_msg_in_memory[message_id] = now
        return False
    except FileExistsError:
        return True
    except OSError:
        return False  # 권한 등 문제 시 그냥 처리 (중복 가능성 감수)


def _release_message_lock(message_id: int) -> None:
    """처리 끝난 뒤 락 파일 삭제 (나중에 삭제해도 됨)."""
    try:
        lock_path = os.path.join(_DEDUP_DIR, f"{message_id}.lock")
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


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


@bot.command(name="AI횟수추가")
async def ai_count_add(ctx: commands.Context, member: discord.Member, count: int):
    """지정 유저에게 AI 사용 기회 N번 추가 (사용 가능: ID 764463640811143169만). 사용법: !AI횟수추가 @멤버 횟수"""
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("이 명령은 지정된 사용자만 사용할 수 있어요.")
        return
    if ctx.guild is None:
        await ctx.send("서버에서만 사용할 수 있어요.")
        return
    if count <= 0:
        await ctx.send("횟수는 1 이상으로 넣어 주세요.")
        return
    await maybe_reset_midnight()
    used_before = ai_usage_count_today.get(member.id, 0)
    ai_usage_count_today[member.id] = max(0, used_before - count)
    added = used_before - ai_usage_count_today[member.id]
    await ctx.send(
        f"{member.mention}에게 AI 사용 기회 **{added}번** 추가했어요. "
        f"(사용 기록: {used_before} → {ai_usage_count_today[member.id]})"
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    """!AI횟수추가 인자 누락 시 사용법 안내"""
    if getattr(ctx.command, "name", None) == "AI횟수추가":
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            try:
                await ctx.send("사용법: `!AI횟수추가 @멤버 횟수` (예: !AI횟수추가 @유저 3)")
            except discord.Forbidden:
                pass
            return
    raise error


@bot.event
async def on_message(message: discord.Message):
    """할당량 안 채운 사람 채팅 5회 제한, 초과 시 핀잔. 중복 응답 방지(봇 여러 개/이벤트 중복)."""
    if message.author.bot:
        await bot.process_commands(message)
        return

    # 메시지당 한 번만 처리 (다른 프로세스나 중복 이벤트 시 스킵)
    if _message_already_handled(message.id):
        return
    try:
        await _on_message_impl(message)
    finally:
        asyncio.create_task(_delete_lock_later(message.id))
    return  # process_commands는 _on_message_impl 안에서 호출


async def _delete_lock_later(message_id: int) -> None:
    """일정 시간 후 락 파일 삭제 (디스크 정리)."""
    await asyncio.sleep(_DEDUP_FILE_MAX_AGE)
    _release_message_lock(message_id)


async def _on_message_impl(message: discord.Message):
    """on_message 실제 처리 (중복 체크 후 여기서만 실행)."""
    await maybe_reset_midnight()
    user_id = message.author.id
    guild = message.guild

    # 스스로 N시간 공부 선언 채널: "5시간 공부하겠다" 등 파싱 → 해당 음성방으로 이동 + 타이머
    if guild and message.channel.id == STUDY_PLEDGE_TEXT_CHANNEL_ID and not (message.content or "").strip().startswith("!"):
        content = (message.content or "").strip()
        minutes = parse_study_minutes_from_message(content)
        if minutes and minutes >= 1:
            pledge_target_minutes[user_id] = minutes
            pledge_completed_minutes[user_id] = 0  # 새 선언 시 누적 완료 분 초기화
            duration_str = format_minutes(minutes)
            if message.author.voice and message.author.voice.channel:
                try:
                    target_voice = guild.get_channel(STUDY_PLEDGE_VOICE_CHANNEL_ID)
                    if isinstance(target_voice, discord.VoiceChannel):
                        await message.author.move_to(target_voice)
                    pledge_ch = guild.get_channel(STUDY_PLEDGE_TEXT_CHANNEL_ID)
                    if pledge_ch and isinstance(pledge_ch, (discord.TextChannel, discord.Thread)):
                        await pledge_ch.send(pledge_commit_message(message.author.mention, duration_str))
                except Exception as e:
                    print(f"[WARN] 선언 공부방 이동 실패: {e}")
                    pledge_ch = guild.get_channel(STUDY_PLEDGE_TEXT_CHANNEL_ID)
                    if pledge_ch and isinstance(pledge_ch, (discord.TextChannel, discord.Thread)):
                        await pledge_ch.send(pledge_commit_message(message.author.mention, duration_str))
            else:
                try:
                    ch = guild.get_channel(NOTICE_TEXT_CHANNEL_ID)
                    if ch and isinstance(ch, discord.TextChannel):
                        await ch.send(f"{message.author.mention} {duration_str} 공부하겠다고 했으니, 먼저 **아무 음성 채널**에 들어온 뒤에 다시 써줘.")
                except discord.Forbidden:
                    pass
        await bot.process_commands(message)
        return

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

    # 쉼터에서 나갔으면 누적 시간 반영 + 로그 후 기록 삭제
    if old_channel_id is not None and is_rest_channel(old_channel_id):
        entered = rest_entered_at.get(user_id)
        if entered is not None:
            elapsed = int(time.time() - entered)
            rest_total_seconds_today[user_id] = rest_total_seconds_today.get(user_id, 0) + elapsed
            visit_count = rest_visit_count_today.get(user_id, 0)
            m = elapsed // 60
            total_m = rest_total_seconds_today[user_id] // 60
            await send_notice(guild, f"{member.mention} 쉼터 나감. 이번에 {m}분 쉼. 오늘 총 {visit_count}번 방문, 누적 {total_m}분.")
        rest_entered_at.pop(user_id, None)
        rest_pinch_sent.pop(user_id, None)

    # 공부방/선언방에서 나갔으면 로그 (방금 N분 + 오늘 총 M분)
    if old_channel_id is not None and (is_study_channel(old_channel_id) or is_pledge_voice_channel(old_channel_id)):
        if is_pledge_voice_channel(old_channel_id):
            entered = pledge_room_entered_at.get(user_id)
            pledge_room_entered_at.pop(user_id, None)
            this_m = (time.time() - entered) / 60 if entered else 0
            pledge_completed_minutes[user_id] = pledge_completed_minutes.get(user_id, 0) + this_m
            target = pledge_target_minutes.get(user_id, 0)
            remain = max(0, target - pledge_completed_minutes[user_id])
            if pledge_completed_minutes[user_id] >= target and target > 0:
                pledge_target_minutes.pop(user_id, None)
                pledge_completed_minutes.pop(user_id, None)
            today_total_sec = state["total_study_sec"] + state.get("session_study_sec", 0)
            today_m = int(today_total_sec // 60)
            await send_notice(guild, f"{member.mention} 선언 공부방 나감. 방금 {format_minutes(int(this_m))} 공부했고, 오늘 총 {format_minutes(today_m)} 공부했음. (선언 목표 중 앞으로 {format_minutes(int(remain))} 더)")
        else:
            if user_id in completed_quota_today:
                this_sec = state.get("session_study_sec", 0)
                today_total_sec = state["total_study_sec"] + this_sec
                await send_notice(guild, study_leave_log_message(
                    member.mention,
                    int(this_sec // 60),
                    int(today_total_sec // 60),
                ))
                state["session_study_sec"] = 0.0
            else:
                start_sec = state.get("session_start_total_sec", 0)
                this_sec = max(0, state["total_study_sec"] - start_sec)
                this_mins = int(this_sec // 60)
                await send_notice(guild, study_leave_log_message(
                    member.mention,
                    this_mins,
                    int(state["total_study_sec"] // 60),
                ))
                if pledge_target_minutes.get(user_id):
                    pledge_completed_minutes[user_id] = pledge_completed_minutes.get(user_id, 0) + this_mins
                    if pledge_completed_minutes[user_id] >= pledge_target_minutes[user_id]:
                        pledge_target_minutes.pop(user_id, None)
                        pledge_completed_minutes.pop(user_id, None)
        state["in_study"] = False
        state["current_channel_id"] = None
        state["last_join_at"] = None

    # ===== 1) 완전히 보이스를 나간 경우 =====
    if old_channel_id is not None and new_channel_id is None:
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
            rest_visit_count_today[user_id] = rest_visit_count_today.get(user_id, 0) + 1
            total_rest_m = int(rest_total_seconds_today.get(user_id, 0) // 60)
            visit_count = rest_visit_count_today[user_id]
            try:
                await member.edit(mute=False)
            except discord.Forbidden:
                print(f"[WARN] {member} 서버 음소거 해제 권한 없음")
            await send_notice(guild, rest_entry_message(member.mention))
            await send_notice(guild, f"{member.mention} 쉼터 입장. 오늘 {visit_count}번째 방문, 지금까지 누적 {total_rest_m}분 쉼.")
            return

        # --- 해방 입장 (할당량 안 채우고 들어오면 음소거 + 꼽주기) ---
        if joined_freedom:
            state["in_study"] = False
            state["current_channel_id"] = None
            state["last_join_at"] = None
            if user_id in completed_quota_today:
                try:
                    await member.edit(mute=False)
                except discord.Forbidden:
                    print(f"[WARN] {member} 서버 음소거 해제 권한 없음")
                await send_notice(guild, freedom_quota_done_taunt(member.mention))
            else:
                # 할당량 안 채운 사람: 서버 음소거 + 꼽주기
                try:
                    await member.edit(mute=True)
                except discord.Forbidden:
                    print(f"[WARN] {member} 서버 음소거 권한 없음")
                await send_notice(guild, freedom_taunt_message(member.mention))
            return

        # --- 스스로 N시간 공부 선언 음성방 입장 (선언했을 때만 타이머, 아니면 안내만) ---
        if is_pledge_voice_channel(new_channel_id):
            try:
                await member.edit(mute=True)
            except discord.Forbidden:
                print(f"[WARN] {member} 서버 음소거 권한 없음")
            target = pledge_target_minutes.get(user_id)
            if not target or target <= 0:
                await send_notice(guild, pledge_room_no_declaration_message(member.mention))
                return
            state["in_study"] = True
            state["current_channel_id"] = new_channel_id
            state["last_join_at"] = time.time()
            pledge_room_entered_at[user_id] = time.time()
            completed = pledge_completed_minutes.get(user_id, 0)
            remain = max(0, target - int(completed))
            await send_notice(guild, f"{member.mention} 선언한 공부방 입장. 선언한 {format_minutes(target)} 중 앞으로 {format_minutes(remain)} 더 하면 됨. 지켜라.")
            return

        # --- 공부방 입장 ---
        if joined_study:
            # 이번 세션 시작 시점의 누적 시간 저장 (퇴장 시 "방금 N분" 계산용)
            state["session_start_total_sec"] = state["total_study_sec"]
            # 재방문(할당량 이미 채움): 세션만 0으로 시작, 꼽주기 멘트
            if user_id in completed_quota_today:
                state["session_study_sec"] = 0.0
            state["in_study"] = True
            state["current_channel_id"] = new_channel_id
            state["last_join_at"] = time.time()

            # 선언한 시간이 있으면: 여기서 공부해도 선언한 만큼 해야 한다고 안내
            target = pledge_target_minutes.get(user_id)
            if target and target > 0:
                completed = pledge_completed_minutes.get(user_id, 0)
                remain = max(0, target - int(completed))
                await send_notice(guild, pledge_priority_in_other_room_message(
                    member.mention, format_minutes(target), format_minutes(remain),
                ))
            else:
                # 재방문 시(이미 오늘 공부한 적 있음) 로그에 오늘 총 공부 시간 안내
                today_total_sec = state["total_study_sec"] + state.get("session_study_sec", 0)
                if today_total_sec > 0:
                    await send_notice(guild, f"{member.mention} 공부방 입장. 오늘 총 {format_minutes(int(today_total_sec // 60))} 공부했음.")

            try:
                await member.edit(mute=True)
            except discord.Forbidden:
                print(f"[WARN] {member} 서버 음소거 권한 없음 (Mute Members 권한 확인 필요)")

            remaining = get_remaining_minutes(user_id, new_channel_id)
            limit_minutes = ROOM_LIMIT_MINUTES.get(new_channel_id, 9999)

            if user_id in completed_quota_today:
                await send_notice(guild, study_reentry_message(member.mention))
                return

            # 선언한 시간이 있으면 이 공부방 입장 멘트는 생략 (위에서 선언 우선 안내만 함)
            if pledge_target_minutes.get(user_id):
                return

            total_minutes = int(state["total_study_sec"] // 60)
            # 시간무제한 음소거 공부방 전용 멘트
            if new_channel_id == CHANNELS["STUDY_UNLIMITED_MUTE"]:
                core = study_unlimited_mute_message()
            elif limit_minutes < 9999:
                used_str = format_minutes(total_minutes)
                remain_str = format_minutes(remaining)
                core = study_room_entry_finite(used_str, remain_str)
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
            if not is_study_or_pledge_channel(channel_id):
                continue

            # 스스로 선언한 공부방: (선언 - 이미 채운 분 - 이번 세션 경과) 로 남은 시간 계산 (나갔다 들어와도 유지)
            if is_pledge_voice_channel(channel_id):
                state = get_user_state(user_id)
                entered = pledge_room_entered_at.get(user_id)
                target_min = pledge_target_minutes.get(user_id, 0)
                completed_before = pledge_completed_minutes.get(user_id, 0)
                if entered is None or target_min <= 0:
                    continue
                now = time.time()
                this_session_min = (now - entered) / 60
                remaining = target_min - completed_before - this_session_min
                remaining = int(remaining) if remaining > 0 else 0
                study_hours = 0  # 아래 분기에서 사용 (pledge는 무제한방 아님)
            else:
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
                state = get_user_state(user_id)
                study_hours = int(state["total_study_sec"] // 3600)
            is_unlimited_mute_room = channel_id == CHANNELS["STUDY_UNLIMITED_MUTE"]
            is_3h_plus_room = channel_id == CHANNELS["STUDY_3H_PLUS"]

            # 정신과 시간(무제한)방: 5시간 되어도 해방으로 이동 안 함, 공부 로그에 "이동 가능하다" 알림만 (한 번만)
            if is_unlimited_mute_room and study_hours >= 5:
                completed_quota_today.add(user_id)
                if user_id in restricted_chat_user_ids and CHAT_RESTRICTED_ROLE_ID is not None:
                    role = guild.get_role(CHAT_RESTRICTED_ROLE_ID)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
                    restricted_chat_user_ids.discard(user_id)
                if user_id not in unlimited_room_5h_notified_today:
                    unlimited_room_5h_notified_today.add(user_id)
                    await send_notice(guild, unlimited_room_can_move_message(member.mention))
                continue

            # 3시간 이상 공부방 / 정신과 시간공부방: 5시간 되면 해방 이동. 그 외 유한 방·선언방은 remaining <= 0 시 이동
            if remaining <= 0 or (is_3h_plus_room and study_hours >= 5):
                completed_quota_today.add(user_id)
                if user_id in restricted_chat_user_ids and CHAT_RESTRICTED_ROLE_ID is not None:
                    role = guild.get_role(CHAT_RESTRICTED_ROLE_ID)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
                    restricted_chat_user_ids.discard(user_id)
                if is_pledge_voice_channel(channel_id):
                    now = time.time()
                    session_min = (now - pledge_room_entered_at.get(user_id, now)) / 60
                    pledge_completed_minutes[user_id] = pledge_completed_minutes.get(user_id, 0) + session_min
                    pledge_room_entered_at.pop(user_id, None)
                    pledge_target_minutes.pop(user_id, None)
                    pledge_completed_minutes.pop(user_id, None)
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
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN이 .env에 없습니다. .env 파일을 만들고 DISCORD_TOKEN=봇토큰 을 넣어 주세요.")
    bot.run(token)
