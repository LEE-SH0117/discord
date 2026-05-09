"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  인간형 게임/잡담 디스코드 봇  v1.0
  ✅ AI API 완전 없음 — 순수 코드 + 무료 공개 API만 사용
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[필수 패키지 설치]
    pip install discord.py aiohttp python-dotenv

[.env 파일 내용]
    DISCORD_TOKEN=봇토큰
    STEAM_API_KEY=스팀API키          <- 선택 (없으면 스팀 기능 비활성)
    RIOT_API_KEY=RGAPI-xxxx          <- 선택 (롤토체스 전적 조회용, developer.riotgames.com 발급)

[봇 권한 - Discord Developer Portal]
    Privileged Gateway Intents:
        PRESENCE INTENT
        SERVER MEMBERS INTENT
        MESSAGE CONTENT INTENT
    Bot Permissions:
        Send Messages / Read Message History
        Add Reactions / Embed Links
        Manage Messages (메시지 삭제용)
        Connect / Speak (음성 채널 반응용)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[기능 목록]
  대화    : 키워드/패턴 기반 자연 응답 + 랜덤 선톡
  게임    : Steam 무료 게임/할인 조회, 게임 추천
  유튜브  : !유튜브 [검색어] -> 검색 링크
  전적    : 롤/배그/오버워치 전적 사이트 바로가기
  미니게임: 끝말잇기, 초성퀴즈, 숫자맞추기, 주사위
  서버    : 멤버수, 역할, 채널 정보
  입퇴장  : 음성채널 입퇴장 재미있는 반응
  자동발화: 봇이 먼저 말 걸기 (랜덤 주기)
  레벨말투: 서버 레벨에 따라 말투 자동 변경 (0~5 까칠 / 51+ 친근)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import os
import re
import time
import random
import datetime
from datetime import timezone, timedelta
from urllib.parse import quote_plus

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════
#  기본 설정 - 여기만 수정하면 됩니다
# ═══════════════════════════════════════════════

KST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 채널 ID 설정 (0이면 비활성)
NOTICE_CHANNEL_ID        = 0    # 봇 알림 채널 (입퇴장 로그 등)
BOT_RANDOM_TALK_CHANNELS = []   # 봇이 랜덤 선톡하는 채널 ID 목록. 예: [123456, 789012]

# 관리자 ID
ADMIN_IDS = {764463640811143169}

# 자동 발화 설정
AUTO_TALK_MIN_MINUTES = 30
AUTO_TALK_MAX_MINUTES = 90
AUTO_TALK_ENABLED     = True

# Steam API
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")

# Riot API (TFT 전적 조회용)
# 발급: https://developer.riotgames.com/ → 무료 Development API Key (만료 24시간, 갱신 필요)
# 실서비스용 Personal/Production Key는 신청 필요
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")

# TFT 데이터 캐시 (Community Dragon 정적 데이터)
_tft_cache: dict = {}          # "traits" | "units" | "items" | "augments" → 파싱된 dict
_tft_cache_time: float = 0.0   # 마지막 갱신 시각 (1시간 TTL)
TFT_CACHE_TTL = 3600           # 초

# 레벨 파싱용 regex (닉네임에서 "레벨 N" 형식)
LEVEL_REGEX = re.compile(r"레벨\s*(\d+)", re.IGNORECASE)

# Health Check 포트
HEALTH_CHECK_PORT = 8000

# ─── 상태 변수 ───────────────────────────────────
# 끝말잇기 상태
wordchain_state: dict = {
    "active": False,
    "channel_id": None,
    "last_word": "",
    "last_user_id": None,
    "used_words": set(),
}
# 숫자맞추기 상태 (channel_id -> state)
number_game_state: dict[int, dict] = {}
# 초성 퀴즈 상태 (channel_id -> state)
chosung_state: dict[int, dict] = {}
# 자동 발화 마지막 시각
last_auto_talk_time: float = time.time()
# 채팅 컨텍스트 (channel_id -> [(user, content), ...])
chat_context: dict[int, list] = {}
CONTEXT_MAX = 10
# 중복 처리 방지
_processed_msgs: dict[int, float] = {}
_MSG_DEDUP_AGE = 5.0


# ═══════════════════════════════════════════════
#  레벨별 말투 시스템
# ═══════════════════════════════════════════════

def parse_level(member: discord.Member) -> int:
    """닉네임에서 레벨 숫자 파싱. 없으면 0."""
    name = (member.display_name or member.name or "").strip()
    m = LEVEL_REGEX.search(name)
    return int(m.group(1)) if m else 0


def get_tone(member: discord.Member, guild=None) -> str:
    """
    말투 6단계:
      loyal : 서버 주인 (신처럼 모시기)
      t5    : 레벨 51+  (완전 친근, 존댓말)
      t4    : 레벨 31~50 (친근 반말)
      t3    : 레벨 16~30 (보통 반말)
      t2    : 레벨 6~15  (조금 까칠)
      t1    : 레벨 0~5   (많이 까칠, 츤데레)
    """
    if guild and member.id == guild.owner_id:
        return "loyal"
    lv = parse_level(member)
    if lv >= 51: return "t5"
    if lv >= 31: return "t4"
    if lv >= 16: return "t3"
    if lv >= 6:  return "t2"
    return "t1"


def tr(tone: str, loyal: str, t5: str, t4: str, t3: str, t2: str, t1: str) -> str:
    """말투별 텍스트 선택 헬퍼"""
    return {"loyal": loyal, "t5": t5, "t4": t4, "t3": t3, "t2": t2, "t1": t1}.get(tone, t1)


# ─── 말투별 응답 함수들 ───────────────────────────

def msg_greeting(tone: str, name: str) -> str:
    opts = {
        "loyal": [f"어서 오시옵소서, {name}님. 오늘도 빛나시옵니다.", f"감히 {name}님을 뵙게 되어 영광이옵니다."],
        "t5":    [f"오~ {name}님 오셨어요? 반가워요!", f"어서 오세요 {name}님~"],
        "t4":    [f"오~ {name} 왔어? 반가워!", f"어서 와 {name}~ 오늘도 게임할 거야?"],
        "t3":    [f"{name} 왔구나. 뭐 하러 왔어", f"어 {name} 왔네."],
        "t2":    [f"어 {name}... 왔어?", f"{name} 또 왔네."],
        "t1":    [f"또 왔어? {name} 오늘도 할 거 없어서 왔나", f"{name} 왔네. 뭐야 또."],
    }
    return random.choice(opts.get(tone, opts["t1"]))


def msg_leave(tone: str, name: str) -> str:
    opts = {
        "loyal": [f"{name}님께서 자리를 비우시옵니다. 언제든 돌아오시옵소서.", f"삼가 {name}님의 안녕을 빌겠사옵니다."],
        "t5":    [f"{name}님 벌써 가세요? 또 놀러 오세요~", f"잘 가요 {name}님!"],
        "t4":    [f"{name} 가는 거야? 다음에 또 봐~", f"잘 가 {name}!"],
        "t3":    [f"{name} 갔네. 또 봐", f"어 {name} 갔어."],
        "t2":    [f"{name} 갔다. 잘 가", f"어 {name} 간다."],
        "t1":    [f"{name} 갔네. 뭐 또 올 거잖아", f"{name} 가는 거야? ㅋ 금방 또 오겠지"],
    }
    return random.choice(opts.get(tone, opts["t1"]))


def msg_voice_join(tone: str, name: str, ch_name: str) -> str:
    opts = {
        "loyal": f"오오, {name}님께서 **{ch_name}**에 입장하셨사옵니다.",
        "t5":    f"{name}님이 **{ch_name}**에 들어오셨어요~",
        "t4":    f"{name} **{ch_name}** 들어왔어! 뭐 할 거야?",
        "t3":    f"{name} **{ch_name}** 입장",
        "t2":    f"{name} **{ch_name}** 들어왔네",
        "t1":    f"{name} 또 들어왔어? **{ch_name}**",
    }
    return opts.get(tone, opts["t1"])


def msg_voice_leave(tone: str, name: str, ch_name: str) -> str:
    opts = {
        "loyal": f"{name}님께서 **{ch_name}**에서 나가셨사옵니다.",
        "t5":    f"{name}님 **{ch_name}** 나가셨네요~",
        "t4":    f"{name} **{ch_name}** 나갔어. 수고~",
        "t3":    f"{name} **{ch_name}** 퇴장",
        "t2":    f"{name} 나갔네",
        "t1":    f"{name} 갔다. 또 오겠지 뭐",
    }
    return opts.get(tone, opts["t1"])


def msg_voice_move(tone: str, name: str, old: str, new: str) -> str:
    opts = {
        "loyal": f"{name}님께서 **{old}** → **{new}**으로 이동하셨사옵니다.",
        "t5":    f"{name}님 **{new}**으로 옮겼어요~",
        "t4":    f"{name} **{new}**로 이동",
        "t3":    f"{name} **{old}** → **{new}**",
        "t2":    f"{name} 채널 이동",
        "t1":    f"{name} 또 옮겼어?",
    }
    return opts.get(tone, opts["t1"])


def msg_game_found(tone: str, game_name: str) -> str:
    opts = {
        "loyal": f"감히 **{game_name}** 정보를 올려드리겠사옵니다.",
        "t5":    f"**{game_name}** 찾아봤어요~",
        "t4":    f"**{game_name}** 찾아봤어!",
        "t3":    f"**{game_name}** 찾았어.",
        "t2":    f"**{game_name}** 찾아봄.",
        "t1":    f"**{game_name}** ㅋ 이거밖에 없어.",
    }
    return opts.get(tone, opts["t1"])


def msg_not_found(tone: str, query: str) -> str:
    opts = {
        "loyal": f"죄송하옵니다. '{query}' 검색 결과가 없사옵니다.",
        "t5":    f"'{query}' 검색 결과가 없어요 ㅠ",
        "t4":    f"'{query}' 없는 것 같아",
        "t3":    f"'{query}' 없음",
        "t2":    f"없어 그런 거",
        "t1":    f"그런 거 없는 것 같은데 ㅋ",
    }
    return opts.get(tone, opts["t1"])


def msg_youtube(tone: str, query: str) -> str:
    opts = {
        "loyal": f"'{query}' 관련 영상을 찾아드리겠사옵니다.",
        "t5":    f"'{query}' 검색해봤어요!",
        "t4":    f"'{query}' 찾아봤어~",
        "t3":    f"'{query}' 찾아봄",
        "t2":    f"'{query}' 여기",
        "t1":    f"'{query}' ㅋ 이거 아니야?",
    }
    return opts.get(tone, opts["t1"])


def msg_record_found(tone: str, game: str, nick: str) -> str:
    opts = {
        "loyal": f"'{nick}'님의 **{game}** 전적이옵니다.",
        "t5":    f"'{nick}'님 **{game}** 전적이에요!",
        "t4":    f"'{nick}' **{game}** 전적 찾아봤어!",
        "t3":    f"**{game}** 전적 링크",
        "t2":    f"여기",
        "t1":    f"ㅋ 찾아봐라",
    }
    return opts.get(tone, opts["t1"])


def msg_wordchain_win(tone: str, name: str, word: str) -> str:
    opts = {
        "loyal": f"오오, {name}님께서 **{word}**(으)로 연결하셨사옵니다!",
        "t5":    f"{name}님 **{word}** 잘했어요!",
        "t4":    f"{name} **{word}** 잘했어",
        "t3":    f"{name} **{word}** 통과",
        "t2":    f"{name} **{word}** ㅇㅇ",
        "t1":    f"{name} **{word}**... 뭐 맞기는 하네",
    }
    return opts.get(tone, opts["t1"])


def msg_wordchain_lose(tone: str, name: str, reason: str) -> str:
    opts = {
        "loyal": f"안타깝사옵니다, {name}님. {reason}",
        "t5":    f"{name}님 {reason} 아쉽지만 게임 오버예요~",
        "t4":    f"{name} {reason} ㅋㅋ 졌다",
        "t3":    f"{name} {reason} 아웃",
        "t2":    f"{name} {reason} 넌 졌어",
        "t1":    f"{name} {reason} ㅋㅋㅋ 역시",
    }
    return opts.get(tone, opts["t1"])


def msg_number_up(tone: str) -> str:
    return tr(tone,
        "더 큰 숫자를 말씀해 주시옵소서.",
        "더 큰 숫자예요!",
        "더 크게!",
        "업",
        "↑",
        "작아 ㅋ"
    )


def msg_number_down(tone: str) -> str:
    return tr(tone,
        "더 작은 숫자를 말씀해 주시옵소서.",
        "더 작은 숫자예요!",
        "더 작게!",
        "다운",
        "↓",
        "커 ㅋ"
    )


def msg_number_correct(tone: str, name: str, tries: int, answer: int) -> str:
    opts = {
        "loyal": f"오오! {name}님 정답이옵니다! {tries}번 만에 **{answer}**을 맞추셨사옵니다!",
        "t5":    f"{name}님 정답이에요! {tries}번 만에 **{answer}** 맞췄어요!",
        "t4":    f"{name} 정답! {tries}번 만에 **{answer}** 맞췄어!",
        "t3":    f"{name} 정답. {tries}번 걸렸어.",
        "t2":    f"{name} 맞춰버렸네. {tries}번.",
        "t1":    f"ㅋㅋ {name} 맞았어. {tries}번이나 걸렸잖아.",
    }
    return opts.get(tone, opts["t1"])


def msg_chosung_correct(tone: str, name: str, answer: str) -> str:
    opts = {
        "loyal": f"정답이옵니다! **{answer}** 맞히셨사옵니다, {name}님!",
        "t5":    f"정답이에요! **{answer}** 맞혔어요, {name}님!",
        "t4":    f"정답! **{answer}** ㅋ 잘했어 {name}",
        "t3":    f"{name} 정답. **{answer}**",
        "t2":    f"{name} 맞췄네. **{answer}**",
        "t1":    f"ㅋ 맞았어 **{answer}** {name} 운 좋네",
    }
    return opts.get(tone, opts["t1"])


def msg_already_playing(tone: str) -> str:
    return tr(tone,
        "이미 게임이 진행 중이옵니다.",
        "이미 진행 중이에요~",
        "이미 하고 있잖아",
        "이미 진행 중",
        "하고 있잖아",
        "이미 하는 중인데 ㅋ"
    )


def msg_no_game(tone: str) -> str:
    return tr(tone,
        "현재 진행 중인 게임이 없사옵니다.",
        "지금 진행 중인 게임이 없어요~",
        "하던 게임이 없잖아",
        "게임 안 하는 중",
        "없잖아",
        "안 하고 있잖아 ㅋ"
    )


def msg_game_ended(tone: str) -> str:
    return tr(tone,
        "게임을 종료하였사옵니다.",
        "게임 종료했어요~",
        "게임 끝!",
        "게임 종료",
        "끝",
        "끝냈어 ㅋ"
    )


def msg_intro(tone: str, content: str) -> str:
    """일반 메시지 앞에 말투 프리픽스 붙이기"""
    prefixes = {
        "loyal": ["감사히 아뢰옵니다. ", "허락하신다면, "],
        "t5":    ["", "그렇군요~ ", "맞아요! "],
        "t4":    ["", "오~ ", "ㅋㅋ "],
        "t3":    ["", "어 ", "음 "],
        "t2":    ["", "...그래 "],
        "t1":    ["", "ㅋ ", "에휴 ", "또? "],
    }
    return random.choice(prefixes.get(tone, [""])) + content


# ═══════════════════════════════════════════════
#  대화 응답 엔진
# ═══════════════════════════════════════════════

# 키워드 → 응답 목록 (낮은 레벨 말투 기준, 실제 발송 시 msg_intro로 말투 적용)
KEYWORD_RESPONSES: list[tuple[list[str], list[str]]] = [
    (["안녕", "ㅎㅇ", "하이", "헬로", "hello", "hi"],
     ["안녕~", "오 왔어?", "ㅎㅇㅎㅇ", "야 왔냐"]),
    (["잘자", "ㅈㄱ", "굿나잇", "good night", "자야겠다"],
     ["잘 자~", "ㅈㄱ", "꿈에서 게임하지 마", "내일 또 보자"]),
    (["배고파", "배고프다", "밥", "먹었어", "점심", "저녁", "야식"],
     ["나도 배고파 ㅠ", "라면 끓여 먹어", "배달 시켜", "치킨이지 뭐"]),
    (["심심해", "심심하다", "할거없어", "지루"],
     ["게임이나 해", "나랑 얘기하면 되잖아", "!게임추천 해봐"]),
    (["뭐해", "뭐 해", "뭐하냐", "뭐함"],
     ["그냥 있어", "너 기다렸지", "채팅 보고 있었어"]),
    (["게임", "겜", "뭐 할까"],
     ["무슨 게임 하려고?", "!게임추천 해봐", "나도 끼워줘"]),
    (["롤", "리그오브레전드"],
     ["롤 OP 무엇?", "솔랭 돌리는 거야?", "!전적 롤 닉네임 써봐"]),
    (["배그", "배틀그라운드", "pubg"],
     ["치킨 먹었어?", "!전적 배그 써봐", "스쿼드야 솔로야?"]),
    (["오버워치", "overwatch"],
     ["요즘 오버워치 어때?", "!전적 오버워치 써봐", "힐러해 제발"]),
    (["마인크래프트", "마크"],
     ["서버 파는 거야?", "다이아 캤어?", "마크 아직도 해? ㅋ"]),
    (["스팀", "steam"],
     ["!스팀무료 해봐", "스팀 세일이야?", "뭐 사려고?"]),
    (["유튜브", "youtube"],
     ["!유튜브 [검색어] 쳐봐", "뭐 보려고?", "나도 같이 볼게"]),
    (["ㅋㅋ", "ㅋㅋㅋ", "lol", "lmao"],
     ["ㅋㅋㅋ", "뭐가 웃겨", "나도 웃겨 ㅋ"]),
    (["ㅠㅠ", "ㅜㅜ", "슬퍼", "힘들어", "우울"],
     ["왜 그래 ㅠ", "무슨 일이야?", "게임 하면 기분 풀려"]),
    (["고마워", "감사", "땡큐"],
     ["ㅇㅇ", "별말씀을", "뭐 이런 걸 가지고"]),
    (["대박", "미쳤다", "ㅁㅊ", "실화냐"],
     ["뭐가 대박이야", "설명해봐", "진짜? ㄷㄷ"]),
    (["봇", "bot", "너 봇이야", "ai야"],
     ["봇 아니야 나 사람임", "뭔 봇이야 ㅋㅋ", "봇이면 어때서 ㅋ"]),
    (["날씨"],
     ["!날씨 [도시명] 써봐", "나도 날씨 잘 몰라 ㅋ"]),
    (["발로란트", "valorant"],
     ["요즘 발로 해?", "!전적 발로란트 써봐", "에이전트 뭐 써?"]),
    (["에이펙스", "apex"],
     ["에이펙스 재밌어?", "!전적 에이펙스 써봐", "레전드 뭐 써?"]),
]

# 봇 선톡 메시지 목록
AUTO_TALK_MESSAGES = [
    "야 요즘 뭐하냐",
    "심심하다 누구 없냐",
    "요즘 핫한 게임 뭐야?",
    "오늘 뭐 먹었어?",
    "다들 주로 몇 시간 게임 해?",
    "나 오늘 기분 좋음 ㅋ",
    "아무도 없어? ㅠ 심심해",
    "다들 뭐 하는 거야 지금",
    "아 갑자기 치킨 먹고 싶다",
    "요즘 어떤 게임이 핫해?",
    "게임 추천 받아도 될까?",
    "다들 잘 있지?",
    "야 오늘 날씨 어때?",
    "다들 최고 랭크가 뭐야?",
    "좋아하는 게임 장르가 뭐야?",
    "혼자 게임하는 사람 있어? 같이 하고 싶다",
    "요즘 스팀 세일 뭐 살만한 거 있어?",
    "다들 RPG 좋아해 아니면 FPS?",
    "야 급식 밥은 맛있어?",
    "주말에 게임 할 거야?",
]

# 초성 퀴즈 문제 (정답, 초성, 힌트)
CHOSUNG_QUIZ: list[tuple[str, str, str]] = [
    ("마인크래프트", "ㅁㅇㅋㄹㅍㅌ", "네모난 블록으로 만드는 게임"),
    ("오버워치", "ㅇㅂㅇㅊ", "블리자드 히어로 FPS"),
    ("리그오브레전드", "ㄹㄱㅇㅂㄹㅈㄷ", "흔히 롤이라고 불리는 게임"),
    ("배틀그라운드", "ㅂㅌㄱㄹㄴㄷ", "치킨 먹으면 이기는 게임"),
    ("스타크래프트", "ㅅㅌㅋㄹㅍㅌ", "저그 테란 프로토스"),
    ("발로란트", "ㅂㄹㄹㅌ", "라이엇 게임즈 전술 FPS"),
    ("에이펙스레전드", "ㅇㅇㅍㅅㄹㅈㄷ", "배틀로얄 + 히어로 슈터"),
    ("디아블로", "ㄷㅇㅂㄹ", "지옥에서 온 악마와 싸우는 핵앤슬래시"),
    ("포트나이트", "ㅍㅌㄴㅇㅌ", "건물 짓는 배틀로얄"),
    ("원신", "ㅇㅅ", "가챠 오픈월드 RPG"),
    ("검은사막", "ㄱㅇㅅㅁ", "한국산 MMORPG"),
    ("로스트아크", "ㄹㅅㅌㅇㅋ", "한국산 액션 RPG"),
    ("스플래툰", "ㅅㅍㄹㅌ", "잉크로 싸우는 닌텐도 게임"),
    ("젤다의전설", "ㅈㄷㅇㅈㅅ", "링크가 주인공인 닌텐도 어드벤처"),
    ("슈퍼마리오", "ㅅㅍㅁㄹㅇ", "버섯 먹고 점프하는 게임"),
    ("포켓몬스터", "ㅍㅋㅁㅅㅌ", "귀여운 몬스터를 잡는 RPG"),
    ("하스스톤", "ㅎㅅㅅㅌ", "블리자드 디지털 카드 게임"),
    ("클래시오브클랜", "ㅋㄹㅅㅇㅂㅋㄹ", "모바일 마을 전쟁 게임"),
    ("사이버펑크", "ㅅㅇㅂㅍㅋ", "2077년 네온 도시 RPG"),
    ("엘든링", "ㅇㄷㄹ", "소울라이크 오픈월드"),
]

# 끝말잇기 봇 응답용 단어 (간단 사전)
WORDCHAIN_BOT_WORDS: dict[str, str] = {
    "게임": "임시방편",
    "방편": "편의점",
    "점심": "심부름",
    "부름": "름름",
    "공부": "부담",
    "담배": "배달",
    "달리기": "기분",
    "분식": "식당",
    "당연": "연습",
    "습관": "관심",
    "심심": "심장",
    "장점": "점수",
    "수업": "업무",
    "무기": "기회",
    "회사": "사람",
    "사과": "과자",
    "자동차": "차도",
    "도전": "전략",
    "략사": "사진",
    "진심": "심각",
    "각오": "오해",
    "해결": "결과",
    "과학": "학교",
    "교실": "실수",
    "수박": "박수",
    "박쥐": "쥐포",
    "포인트": "트럭",
    "럭비": "비교",
    "컴퓨터": "터미널",
    "날씨": "씨앗",
    "앗싸": "싸움",
    "움직임": "임무",
    "무도회": "회원",
    "원숭이": "이야기",
}


# ═══════════════════════════════════════════════
#  유틸 함수
# ═══════════════════════════════════════════════

def dedup_check(message_id: int) -> bool:
    """True면 이미 처리한 메시지 (스킵)"""
    now = time.time()
    expired = [k for k, v in _processed_msgs.items() if now - v > _MSG_DEDUP_AGE]
    for k in expired:
        del _processed_msgs[k]
    if message_id in _processed_msgs:
        return True
    _processed_msgs[message_id] = now
    return False


def add_context(channel_id: int, user_name: str, content: str) -> None:
    if channel_id not in chat_context:
        chat_context[channel_id] = []
    chat_context[channel_id].append((user_name, content))
    if len(chat_context[channel_id]) > CONTEXT_MAX:
        chat_context[channel_id].pop(0)


def get_current_time_str() -> str:
    now = datetime.datetime.now(KST)
    return now.strftime("%H시 %M분")


def format_number(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


async def send_to_notice(content: str) -> None:
    """공지 채널로 메시지 전송"""
    if not NOTICE_CHANNEL_ID:
        return
    for guild in bot.guilds:
        ch = guild.get_channel(NOTICE_CHANNEL_ID)
        if ch and isinstance(ch, discord.TextChannel):
            try:
                await ch.send(content)
            except discord.Forbidden:
                pass


# ═══════════════════════════════════════════════
#  Steam 무료 공개 API
# ═══════════════════════════════════════════════

async def fetch_steam_free_games() -> list[dict]:
    url = "https://store.steampowered.com/api/featuredcategories?cc=kr&l=korean"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        specials = data.get("specials", {}).get("items", [])
        return [g for g in specials if g.get("final_price", 1) == 0][:5]
    except Exception as e:
        print(f"[WARN] Steam 무료게임 조회 실패: {e}")
        return []


async def fetch_steam_game_search(game_name: str) -> dict | None:
    url = f"https://store.steampowered.com/api/storesearch/?term={quote_plus(game_name)}&l=korean&cc=kr"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        items = data.get("items", [])
        if not items:
            return None
        item = items[0]
        app_id = item.get("id")
        price_info = item.get("price", {})
        final = price_info.get("final", 0)
        price_str = "무료" if final == 0 else f"{format_number(final // 100)}원"
        return {
            "name":   item.get("name", "알 수 없음"),
            "price":  price_str,
            "url":    f"https://store.steampowered.com/app/{app_id}/",
            "app_id": app_id,
        }
    except Exception as e:
        print(f"[WARN] Steam 검색 실패: {e}")
        return None


async def fetch_steam_top_sellers() -> list[dict]:
    url = "https://store.steampowered.com/api/featuredcategories?cc=kr&l=korean"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        return data.get("top_sellers", {}).get("items", [])[:5]
    except Exception as e:
        print(f"[WARN] Steam 인기게임 조회 실패: {e}")
        return []


# ═══════════════════════════════════════════════
#  전적 조회 사이트 링크
# ═══════════════════════════════════════════════

RECORD_SITES: dict[str, tuple[str, str]] = {
    "롤":         ("https://www.op.gg/summoners/kr/{}", "OP.GG"),
    "league":     ("https://www.op.gg/summoners/kr/{}", "OP.GG"),
    "배그":        ("https://pubg.op.gg/user/{}", "PUBG OP.GG"),
    "배틀그라운드": ("https://pubg.op.gg/user/{}", "PUBG OP.GG"),
    "pubg":       ("https://pubg.op.gg/user/{}", "PUBG OP.GG"),
    "오버워치":    ("https://overbuff.com/players/{}", "Overbuff"),
    "overwatch":  ("https://overbuff.com/players/{}", "Overbuff"),
    "발로란트":    ("https://tracker.gg/valorant/profile/riot/{}/overview", "Tracker.GG"),
    "valorant":   ("https://tracker.gg/valorant/profile/riot/{}/overview", "Tracker.GG"),
    "에이펙스":    ("https://apex.tracker.gg/apex/profile/ea/{}/overview", "Apex Tracker"),
    "apex":       ("https://apex.tracker.gg/apex/profile/ea/{}/overview", "Apex Tracker"),
}


def make_record_embed(game: str, nickname: str) -> discord.Embed | None:
    key = game.strip().lower()
    if key not in RECORD_SITES:
        return None
    url_fmt, site = RECORD_SITES[key]
    url = url_fmt.format(quote_plus(nickname))
    embed = discord.Embed(title=f"🏆 {game} 전적: {nickname}", url=url, color=0x7289DA)
    embed.description = f"[{site}에서 보기]({url})"
    embed.set_footer(text=f"출처: {site}")
    return embed


# ═══════════════════════════════════════════════
#  날씨 / 환율 (무료 API)
# ═══════════════════════════════════════════════

async def fetch_weather(city: str) -> str:
    url = f"https://wttr.in/{quote_plus(city)}?format=3&lang=ko"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return f"{city} 날씨 정보 없음"
                return (await resp.text()).strip()
    except Exception:
        return f"{city} 날씨 조회 실패 ㅠ"


async def fetch_exchange(from_: str, to_: str) -> str:
    url = f"https://api.exchangerate-api.com/v4/latest/{from_.upper()}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return "환율 정보 없음"
                data = await resp.json()
        rate = data.get("rates", {}).get(to_.upper())
        if rate is None:
            return f"{to_} 환율 없음"
        return f"1 {from_.upper()} = {format_number(round(rate, 2))} {to_.upper()}"
    except Exception:
        return "환율 조회 실패 ㅠ"


# ═══════════════════════════════════════════════
#  롤토체스(TFT) — Riot Games API + Community Dragon
# ═══════════════════════════════════════════════
#
#  사용하는 엔드포인트 (모두 무료, Riot API Key 필요):
#    계정 조회  : https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
#    소환사 조회: https://kr.api.riotgames.com/tft/summoner/v1/summoners/by-puuid/{puuid}
#    랭크 조회  : https://kr.api.riotgames.com/tft/league/v1/entries/by-summoner/{summonerId}
#    최근 매치  : https://asia.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids?count=5
#    매치 상세  : https://asia.api.riotgames.com/tft/match/v1/matches/{matchId}
#    정적 데이터 : Community Dragon (API Key 불필요)
#
# ─────────────────────────────────────────────

# TFT 티어 이모지 매핑
TFT_TIER_EMOJI = {
    "IRON":        "🩶 아이언",
    "BRONZE":      "🥉 브론즈",
    "SILVER":      "🥈 실버",
    "GOLD":        "🥇 골드",
    "PLATINUM":    "💎 플래티넘",
    "EMERALD":     "💚 에메랄드",
    "DIAMOND":     "💠 다이아",
    "MASTER":      "🔮 마스터",
    "GRANDMASTER": "🏅 그랜드마스터",
    "CHALLENGER":  "🏆 챌린저",
}

# TFT 순위 → 한글
TFT_RANK_KR = {"I": "1부", "II": "2부", "III": "3부", "IV": "4부"}

# Community Dragon TFT 세트 정적 데이터 기본 URL
# 최신 세트 번호는 자동으로 조회
CDRAGON_BASE = "https://raw.communitydragon.org/latest/cdragon/tft"


async def _riot_get(url: str) -> dict | list | None:
    """Riot API GET 요청 공통 함수. 실패 시 None 반환."""
    if not RIOT_API_KEY:
        return None
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 404:
                    return None
                if resp.status == 429:
                    print("[WARN] Riot API 속도 제한(429)")
                    return None
                print(f"[WARN] Riot API {resp.status}: {url}")
                return None
    except Exception as e:
        print(f"[WARN] Riot API 요청 오류: {e}")
        return None


async def tft_get_account(game_name: str, tag_line: str) -> dict | None:
    """Riot ID(닉네임#태그)로 계정 PUUID 조회"""
    url = (
        f"https://asia.api.riotgames.com/riot/account/v1/accounts"
        f"/by-riot-id/{quote_plus(game_name)}/{quote_plus(tag_line)}"
    )
    return await _riot_get(url)


async def tft_get_summoner_by_puuid(puuid: str) -> dict | None:
    """PUUID로 소환사 정보 조회"""
    url = f"https://kr.api.riotgames.com/tft/summoner/v1/summoners/by-puuid/{puuid}"
    return await _riot_get(url)


async def tft_get_rank(summoner_id: str) -> list | None:
    """소환사 ID로 TFT 랭크 조회"""
    url = f"https://kr.api.riotgames.com/tft/league/v1/entries/by-summoner/{summoner_id}"
    return await _riot_get(url)


async def tft_get_match_ids(puuid: str, count: int = 5) -> list[str]:
    """최근 TFT 매치 ID 목록 조회"""
    url = (
        f"https://asia.api.riotgames.com/tft/match/v1/matches"
        f"/by-puuid/{puuid}/ids?count={count}"
    )
    result = await _riot_get(url)
    return result if isinstance(result, list) else []


async def tft_get_match(match_id: str) -> dict | None:
    """TFT 매치 상세 데이터 조회"""
    url = f"https://asia.api.riotgames.com/tft/match/v1/matches/{match_id}"
    return await _riot_get(url)


async def tft_get_top_players(tier: str = "CHALLENGER") -> list | None:
    """TFT 상위 리그 플레이어 목록 (CHALLENGER / GRANDMASTER / MASTER)"""
    tier_up = tier.upper()
    if tier_up not in ("CHALLENGER", "GRANDMASTER", "MASTER"):
        return None
    url = f"https://kr.api.riotgames.com/tft/league/v1/{tier_up.lower()}"
    data = await _riot_get(url)
    if not data:
        return None
    entries = data.get("entries", [])
    entries.sort(key=lambda x: x.get("leaguePoints", 0), reverse=True)
    return entries[:10]


async def _fetch_cdragon_json(path: str) -> dict | list | None:
    """Community Dragon에서 JSON 데이터 가져오기 (API Key 불필요)"""
    url = f"{CDRAGON_BASE}/{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"[WARN] CDragon 조회 실패 ({path}): {e}")
        return None


async def tft_ensure_cache() -> bool:
    """TFT 정적 데이터 캐시 갱신 (1시간 TTL). True = 캐시 유효."""
    global _tft_cache, _tft_cache_time
    now = time.time()
    if _tft_cache and (now - _tft_cache_time) < TFT_CACHE_TTL:
        return True

    # Community Dragon: en_us.json 에 trait/unit/item/augment 모두 포함
    data = await _fetch_cdragon_json("en_us.json")
    if not data or not isinstance(data, dict):
        return bool(_tft_cache)  # 갱신 실패해도 이전 캐시 있으면 사용

    # 특성(시너지)
    traits: dict[str, str] = {}
    for t in data.get("setData", [{}])[-1].get("traits", []):
        key = t.get("apiName", "")
        name = t.get("name", key)
        traits[key] = name

    # 챔피언(유닛)
    units: dict[str, dict] = {}
    for u in data.get("setData", [{}])[-1].get("champions", []):
        key = u.get("apiName", "")
        cost = u.get("cost", 0)
        name = u.get("name", key)
        traits_list = [traits.get(tr, tr) for tr in u.get("traits", [])]
        units[key] = {"name": name, "cost": cost, "traits": traits_list}

    # 아이템
    items: dict[str, str] = {}
    for it in data.get("items", []):
        key = str(it.get("id", ""))
        name = it.get("name", key)
        items[key] = name

    # 증강
    augments: dict[str, str] = {}
    for aug in data.get("augments", []):
        key = aug.get("apiName", "")
        name = aug.get("name", key)
        augments[key] = name

    _tft_cache = {
        "traits":   traits,
        "units":    units,
        "items":    items,
        "augments": augments,
        "raw":      data,
    }
    _tft_cache_time = now
    print(f"[TFT] 정적 데이터 갱신 완료 — 챔피언 {len(units)}개 / 아이템 {len(items)}개 / 특성 {len(traits)}개")
    return True


def _tft_placement_emoji(place: int) -> str:
    emojis = {1: "🥇", 2: "🥈", 3: "🥉", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣"}
    return emojis.get(place, f"{place}위")


def _tft_rank_str(tier: str, rank: str, lp: int) -> str:
    tier_str = TFT_TIER_EMOJI.get(tier.upper(), tier)
    if tier.upper() in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{tier_str} **{lp} LP**"
    rank_str = TFT_RANK_KR.get(rank, rank)
    return f"{tier_str} {rank_str} **{lp} LP**"


def _parse_riot_id(raw: str) -> tuple[str, str]:
    """
    '닉네임#KR1' 또는 '닉네임 KR1' 형태 파싱.
    태그 없으면 기본 태그 'KR1' 사용.
    """
    raw = raw.strip()
    if "#" in raw:
        parts = raw.split("#", 1)
        return parts[0].strip(), parts[1].strip()
    parts = raw.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return raw, "KR1"


# ── TFT 전적 임베드 빌더 ──────────────────────────

async def build_tft_profile_embed(game_name: str, tag_line: str, tone: str) -> discord.Embed | str:
    """
    TFT 프로필 + 랭크 + 최근 5게임 임베드 생성.
    실패 시 에러 문자열 반환.
    """
    if not RIOT_API_KEY:
        return "Riot API 키가 설정되지 않았어요. `.env`에 `RIOT_API_KEY=RGAPI-...` 를 추가해 주세요."

    # 1) 계정 조회
    account = await tft_get_account(game_name, tag_line)
    if not account:
        return msg_not_found(tone, f"{game_name}#{tag_line}")

    puuid = account.get("puuid", "")

    # 2) 소환사 정보
    summoner = await tft_get_summoner_by_puuid(puuid)
    if not summoner:
        return msg_not_found(tone, f"{game_name}#{tag_line}")

    summoner_id = summoner.get("id", "")
    summoner_level = summoner.get("summonerLevel", 0)

    # 3) 랭크 정보
    rank_data = await tft_get_rank(summoner_id)
    rank_info = None
    if rank_data:
        for entry in rank_data:
            if entry.get("queueType") == "RANKED_TFT":
                rank_info = entry
                break

    # 4) 최근 매치 5판
    match_ids = await tft_get_match_ids(puuid, count=5)

    # 5) 정적 데이터 캐시 확보 (실패해도 계속)
    await tft_ensure_cache()

    # ── 임베드 구성 ──
    color = 0xC89B3C  # TFT 골드 색
    embed = discord.Embed(
        title=f"🎮 TFT 전적: {game_name}#{tag_line}",
        color=color,
        url=f"https://lolchess.gg/profile/kr/{quote_plus(game_name)}-{quote_plus(tag_line)}",
    )
    embed.set_footer(text="lolchess.gg 바로가기 | Riot Games API")

    # 랭크
    if rank_info:
        tier  = rank_info.get("tier", "UNRANKED")
        rank  = rank_info.get("rank", "")
        lp    = rank_info.get("leaguePoints", 0)
        wins  = rank_info.get("wins", 0)
        losses= rank_info.get("losses", 0)
        total = wins + losses
        winrate = round(wins / total * 100, 1) if total > 0 else 0
        embed.add_field(
            name="🏅 랭크",
            value=(
                f"{_tft_rank_str(tier, rank, lp)}\n"
                f"📊 {wins}승 {losses}패 (승률 {winrate}%)\n"
                f"🎯 총 {total}게임"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="🏅 랭크", value="언랭 (랭크 데이터 없음)", inline=True)

    embed.add_field(name="🔰 소환사 레벨", value=f"Lv.{summoner_level}", inline=True)

    # 최근 매치 파싱
    if match_ids:
        match_lines = []
        placements = []
        for mid in match_ids:
            match = await tft_get_match(mid)
            if not match:
                continue
            info = match.get("info", {})
            participants = info.get("participants", [])
            my_data = next((p for p in participants if p.get("puuid") == puuid), None)
            if not my_data:
                continue

            place    = my_data.get("placement", 0)
            level    = my_data.get("level", 0)
            gold_left= my_data.get("gold_left", 0)
            last_round = my_data.get("last_round", 0)
            placements.append(place)

            # 증강
            augments_raw = my_data.get("augments", [])
            augment_names = []
            for aug_key in augments_raw[:3]:
                name = _tft_cache.get("augments", {}).get(aug_key, aug_key.split("_")[-1])
                augment_names.append(name)
            aug_str = " / ".join(augment_names) if augment_names else "없음"

            # 유닛 (코스트 5 위주로 표시)
            units_raw = my_data.get("units", [])
            cost5 = [u for u in units_raw if u.get("rarity", 0) >= 4]
            unit_names = []
            for u in (cost5 or units_raw)[:5]:
                char_id = u.get("character_id", "")
                uinfo = _tft_cache.get("units", {}).get(char_id, {})
                uname = uinfo.get("name", char_id.split("_")[-1]) if uinfo else char_id.split("_")[-1]
                tier_u = u.get("tier", 1)
                stars = "★" * tier_u
                unit_names.append(f"{uname}{stars}")
            unit_str = ", ".join(unit_names) if unit_names else "정보 없음"

            place_emoji = _tft_placement_emoji(place)
            match_lines.append(
                f"{place_emoji} Lv.{level} | 라운드 {last_round}R | 골드 {gold_left}\n"
                f"　증강: {aug_str}\n"
                f"　유닛: {unit_str}"
            )

        if match_lines:
            embed.add_field(
                name=f"📋 최근 {len(match_lines)}게임",
                value="\n\n".join(match_lines),
                inline=False,
            )
        if placements:
            avg = round(sum(placements) / len(placements), 2)
            top4 = sum(1 for p in placements if p <= 4)
            embed.add_field(
                name="📈 최근 평균 순위",
                value=f"평균 **{avg}위** | TOP4 {top4}/{len(placements)}게임",
                inline=False,
            )
    else:
        embed.add_field(name="📋 최근 게임", value="최근 TFT 게임 기록이 없어요.", inline=False)

    return embed


async def build_tft_top_embed(tier: str, tone: str) -> discord.Embed | str:
    """TFT 상위 티어 랭킹 임베드"""
    if not RIOT_API_KEY:
        return "Riot API 키가 설정되지 않았어요."

    players = await tft_get_top_players(tier)
    if players is None:
        return tr(tone,
            f"'{tier}'은 챌린저/그랜드마스터/마스터만 가능하옵니다.",
            f"'{tier}'은 챌린저/그랜드마스터/마스터만 돼요~",
            f"챌린저/그랜드마스터/마스터 중에서 골라봐",
            f"챌린저/GM/마스터 중 하나 써",
            f"잘못된 티어",
            f"그런 티어 없어 ㅋ"
        )
    if not players:
        return "상위 플레이어 데이터를 가져올 수 없어요."

    tier_kr = TFT_TIER_EMOJI.get(tier.upper(), tier)
    embed = discord.Embed(
        title=f"👑 TFT KR {tier_kr} TOP 10",
        color=0xC89B3C,
    )
    lines = []
    for i, p in enumerate(players[:10], 1):
        name   = p.get("summonerName", "알 수 없음")
        lp_val = p.get("leaguePoints", 0)
        wins   = p.get("wins", 0)
        losses = p.get("losses", 0)
        total  = wins + losses
        wr     = round(wins / total * 100, 1) if total > 0 else 0
        lines.append(f"**{i}.** {name} — **{lp_val} LP** | {wins}승 {losses}패({wr}%)")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Riot Games API — KR 서버")
    return embed


async def build_tft_meta_embed(tone: str) -> discord.Embed | str:
    """현재 세트 챔피언 코스트별 간단 목록 (Community Dragon, API Key 불필요)"""
    await tft_ensure_cache()
    units = _tft_cache.get("units", {})
    if not units:
        return "TFT 챔피언 데이터를 가져올 수 없어요. 잠시 후 다시 시도해 주세요."

    cost_groups: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for udata in units.values():
        cost = udata.get("cost", 0)
        name = udata.get("name", "")
        if cost in cost_groups and name:
            cost_groups[cost].append(name)

    embed = discord.Embed(title="🃏 TFT 현재 세트 챔피언 목록", color=0xC89B3C)
    cost_emoji = {1: "⚪", 2: "🟢", 3: "🔵", 4: "🟣", 5: "🟡"}
    for cost in range(1, 6):
        champs = sorted(cost_groups.get(cost, []))
        if champs:
            embed.add_field(
                name=f"{cost_emoji[cost]} {cost}코스트 ({len(champs)}명)",
                value=", ".join(champs) or "없음",
                inline=False,
            )
    embed.set_footer(text="Community Dragon 데이터 (API Key 불필요)")
    return embed


async def build_tft_traits_embed(tone: str) -> discord.Embed | str:
    """현재 세트 특성(시너지) 목록"""
    await tft_ensure_cache()
    traits = _tft_cache.get("traits", {})
    if not traits:
        return "TFT 특성 데이터를 가져올 수 없어요."

    embed = discord.Embed(title="✨ TFT 현재 세트 특성(시너지) 목록", color=0xC89B3C)
    trait_list = sorted(traits.values())

    # 15개씩 두 컬럼으로 나눠 표시
    mid = len(trait_list) // 2 + len(trait_list) % 2
    embed.add_field(name="특성 (1)", value="\n".join(trait_list[:mid]) or "없음", inline=True)
    embed.add_field(name="특성 (2)", value="\n".join(trait_list[mid:]) or "없음", inline=True)
    embed.set_footer(text="Community Dragon 데이터")
    return embed


# ═══════════════════════════════════════════════
#  끝말잇기 로직
# ═══════════════════════════════════════════════

JONGSEONG_MAP = [
    None,'ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ',
    'ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'
]
CHOSUNG_MAP = [
    'ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ',
    'ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'
]


def get_chosung_char(char: str) -> str | None:
    code = ord(char)
    if 0xAC00 <= code <= 0xD7A3:
        return CHOSUNG_MAP[(code - 0xAC00) // 588]
    return None


def get_jongseong_char(char: str) -> str | None:
    code = ord(char)
    if 0xAC00 <= code <= 0xD7A3:
        return JONGSEONG_MAP[(code - 0xAC00) % 28]
    return None


def get_full_chosung(text: str) -> str:
    """전체 초성 추출"""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            result.append(CHOSUNG_MAP[(code - 0xAC00) // 588])
        else:
            result.append(ch)
    return "".join(result)


def check_wordchain(prev_word: str, new_word: str) -> tuple[bool, str]:
    """끝말잇기 유효성. (True, 'ok') 또는 (False, 이유)"""
    if not new_word or len(new_word) < 2:
        return False, "두 글자 이상이어야 해요!"
    prev_last = prev_word[-1]
    # 종성이 없으면 글자 그대로 매칭
    jong = get_jongseong_char(prev_last)
    if jong is None:
        if prev_last != new_word[0]:
            return False, f"**{prev_last}**로 시작해야 해요!"
    else:
        # 두음법칙 없이 단순 비교
        if prev_last != new_word[0]:
            return False, f"**{prev_last}**로 시작해야 해요!"
    return True, "ok"


# ═══════════════════════════════════════════════
#  Health Check 서버
# ═══════════════════════════════════════════════

async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
    await site.start()
    print(f"[Health] 서버 시작 - port {HEALTH_CHECK_PORT}")


async def ping_self():
    koyeb_url = os.getenv("KOYEB_URL", "")
    if not koyeb_url:
        return
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(
                    koyeb_url.rstrip("/") + "/health",
                    timeout=aiohttp.ClientTimeout(total=10)
                )
        except Exception:
            pass
        await asyncio.sleep(180)


# ═══════════════════════════════════════════════
#  이벤트 핸들러
# ═══════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"[봇] 로그인: {bot.user} (ID: {bot.user.id})")
    print(f"[봇] 서버 수: {len(bot.guilds)}")
    bot.loop.create_task(start_web_server())
    bot.loop.create_task(ping_self())
    if not auto_talk_loop.is_running():
        auto_talk_loop.start()
    print("[봇] 준비 완료!")


@bot.event
async def on_member_join(member: discord.Member):
    if not member.guild:
        return
    tone = get_tone(member, member.guild)
    msg = msg_greeting(tone, member.display_name)
    ch = member.guild.get_channel(NOTICE_CHANNEL_ID) if NOTICE_CHANNEL_ID else member.guild.system_channel
    if ch:
        try:
            await ch.send(f"{member.mention} {msg} 🎉")
        except discord.Forbidden:
            pass


@bot.event
async def on_member_remove(member: discord.Member):
    if not member.guild:
        return
    tone = get_tone(member, member.guild)
    msg = msg_leave(tone, member.display_name)
    ch = member.guild.get_channel(NOTICE_CHANNEL_ID) if NOTICE_CHANNEL_ID else member.guild.system_channel
    if ch:
        try:
            await ch.send(msg)
        except discord.Forbidden:
            pass


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    if before.channel == after.channel:
        return

    tone = get_tone(member, member.guild)
    old_ch = before.channel
    new_ch = after.channel

    notice_ch = (
        member.guild.get_channel(NOTICE_CHANNEL_ID)
        if NOTICE_CHANNEL_ID else None
    )
    if notice_ch is None:
        return

    if new_ch and not old_ch:
        msg = msg_voice_join(tone, member.display_name, new_ch.name)
    elif old_ch and not new_ch:
        msg = msg_voice_leave(tone, member.display_name, old_ch.name)
    elif old_ch and new_ch:
        msg = msg_voice_move(tone, member.display_name, old_ch.name, new_ch.name)
    else:
        return

    try:
        await notice_ch.send(msg)
    except discord.Forbidden:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if dedup_check(message.id):
        return

    await bot.process_commands(message)

    if message.content.startswith("!"):
        return

    content = message.content.strip()
    if not content:
        return

    add_context(message.channel.id, message.author.display_name, content)
    cl = content.lower().replace(" ", "")
    tone = get_tone(message.author, message.guild)

    # 끝말잇기 진행 중
    if wordchain_state["active"] and wordchain_state["channel_id"] == message.channel.id:
        await _handle_wordchain(message, tone)
        return

    # 숫자맞추기 진행 중
    if message.channel.id in number_game_state:
        await _handle_number(message, tone)
        return

    # 초성 퀴즈 진행 중
    if message.channel.id in chosung_state:
        await _handle_chosung(message, tone)
        return

    # 시간 질문
    if any(k in cl for k in ["몇시야", "몇시임", "지금몇시", "몇시인지"]):
        t = get_current_time_str()
        resp = tr(tone,
            f"지금은 {t}이옵니다.",
            f"지금 {t}이에요~",
            f"지금 {t}이야",
            f"{t}임",
            f"{t}",
            f"{t}인데 왜 나한테 물어봐"
        )
        await message.channel.send(resp)
        return

    # 봇 멘션
    if bot.user in message.mentions:
        responses = [
            msg_intro(tone, "왜 불러"),
            msg_intro(tone, "나 여기 있어"),
            msg_intro(tone, "응 뭐야"),
            msg_intro(tone, "!도움말 해봐"),
            msg_intro(tone, "나한테 볼일 있어?"),
        ]
        await message.channel.send(random.choice(responses))
        return

    # 키워드 매칭 (25% 확률)
    if random.random() < 0.25:
        for keywords, resps in KEYWORD_RESPONSES:
            if not resps:
                continue
            if any(kw in cl for kw in keywords):
                await message.channel.send(msg_intro(tone, random.choice(resps)))
                return

    # 3% 확률로 이모지 리액션
    if random.random() < 0.03:
        try:
            await message.add_reaction(random.choice(["👀", "🔥", "😂", "👍", "ㅋ"]))
        except (discord.Forbidden, discord.HTTPException):
            pass


# ═══════════════════════════════════════════════
#  미니게임 내부 핸들러
# ═══════════════════════════════════════════════

async def _handle_wordchain(message: discord.Message, tone: str):
    word = message.content.strip()
    if not word or len(word) < 2:
        return

    state = wordchain_state
    valid, reason = check_wordchain(state["last_word"], word)

    if not valid:
        out = msg_wordchain_lose(tone, message.author.display_name, reason)
        await message.channel.send(
            f"{out}\n게임 오버! (마지막 단어: **{state['last_word']}**)\n"
            f"다시 하려면 `!끝말잇기`"
        )
        wordchain_state.update({"active": False, "channel_id": None, "last_word": "", "used_words": set()})
        return

    if word in state["used_words"]:
        out = msg_wordchain_lose(tone, message.author.display_name, "이미 쓴 단어예요!")
        await message.channel.send(f"{out}\n게임 오버!")
        wordchain_state.update({"active": False, "channel_id": None, "last_word": "", "used_words": set()})
        return

    state["used_words"].add(word)
    state["last_word"] = word
    state["last_user_id"] = message.author.id

    out = msg_wordchain_win(tone, message.author.display_name, word)

    bot_next = WORDCHAIN_BOT_WORDS.get(word)
    if bot_next:
        state["used_words"].add(bot_next)
        state["last_word"] = bot_next
        state["last_user_id"] = bot.user.id if bot.user else None
        await message.channel.send(f"{out}\n봇: **{bot_next}** — **{bot_next[-1]}**로 이어가봐!")
    else:
        await message.channel.send(f"{out}\n다음은 **{word[-1]}**로 시작하는 단어!")


async def _handle_number(message: discord.Message, tone: str):
    channel_id = message.channel.id
    state = number_game_state.get(channel_id)
    if not state:
        return
    try:
        guess = int(message.content.strip())
    except ValueError:
        return

    answer = state["answer"]
    state["tries"] = state.get("tries", 0) + 1

    if guess == answer:
        await message.channel.send(
            msg_number_correct(tone, message.author.display_name, state["tries"], answer)
        )
        del number_game_state[channel_id]
    elif guess < answer:
        await message.channel.send(msg_number_up(tone))
    else:
        await message.channel.send(msg_number_down(tone))


async def _handle_chosung(message: discord.Message, tone: str):
    channel_id = message.channel.id
    state = chosung_state.get(channel_id)
    if not state:
        return
    answer = state["answer"]
    content = message.content.strip()
    if content == answer or content.replace(" ", "") == answer.replace(" ", ""):
        await message.channel.send(
            msg_chosung_correct(tone, message.author.display_name, answer)
        )
        del chosung_state[channel_id]


# ═══════════════════════════════════════════════
#  커맨드
# ═══════════════════════════════════════════════

@bot.command(name="도움말", aliases=["help", "명령어"])
async def cmd_help(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    embed = discord.Embed(title="📋 명령어 목록", color=0x5865F2)
    embed.add_field(name="🎮 게임 정보", value=(
        "`!게임검색 [게임명]` — Steam 게임 검색\n"
        "`!스팀무료` — 현재 무료 게임 목록\n"
        "`!스팀인기` — Steam 인기 게임\n"
        "`!게임추천` — 랜덤 게임 추천\n"
        "`!전적 [게임] [닉네임]` — 전적 사이트 바로가기\n"
        "지원: 롤, 배그, 오버워치, 발로란트, 에이펙스"
    ), inline=False)
    embed.add_field(name="📺 유튜브", value="`!유튜브 [검색어]` — 유튜브 검색 링크", inline=False)
    embed.add_field(name="♟️ 롤토체스(TFT)", value=(
        "`!롤토체스 닉네임#태그` — 전적 + 랭크 + 최근 5게임\n"
        "`!TFT랭킹 [챌린저|그랜드마스터|마스터]` — 상위 랭킹 TOP10\n"
        "`!TFT챔피언` — 현재 세트 챔피언 코스트별 목록\n"
        "`!TFT시너지` — 현재 세트 특성(시너지) 목록\n"
        "⚠️ 전적·랭킹은 `.env`에 `RIOT_API_KEY` 필요\n"
        "챔피언·시너지는 API Key 없어도 됩니다"
    ), inline=False)
    embed.add_field(name="🎲 미니게임", value=(
        "`!끝말잇기` — 끝말잇기 시작\n"
        "`!숫자게임 [최대값]` — 숫자맞추기\n"
        "`!초성퀴즈` — 게임 초성 퀴즈\n"
        "`!주사위 [면수]` — 주사위 굴리기\n"
        "`!게임종료` — 현재 게임 종료"
    ), inline=False)
    embed.add_field(name="🔧 유틸", value=(
        "`!날씨 [도시]` — 날씨 조회\n"
        "`!환율 [통화] [대상통화]` — 환율 조회\n"
        "`!서버정보` — 서버 정보\n"
        "`!내정보` — 내 정보 + 레벨 확인\n"
        "`!핑` — 봇 응답 속도"
    ), inline=False)
    intro = tr(tone,
        "감히 명령어 목록을 올려드리옵니다.",
        "명령어 목록이에요~ 뭐든 물어보세요!",
        "명령어 목록이야. 모르면 물어봐",
        "명령어 목록",
        "여기",
        "알려줄게 대신 잘 써라"
    )
    await ctx.send(intro, embed=embed)


@bot.command(name="핑", aliases=["ping"])
async def cmd_ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    tone = get_tone(ctx.author, ctx.guild)
    msg = tr(tone,
        f"봇의 응답 속도는 **{latency}ms**이옵니다.",
        f"응답 속도: **{latency}ms** 에요~",
        f"**{latency}ms** 이야",
        f"**{latency}ms**",
        f"{latency}ms",
        f"{latency}ms인데 왜 물어봄"
    )
    await ctx.send(msg)


@bot.command(name="게임검색", aliases=["스팀검색"])
async def cmd_game_search(ctx: commands.Context, *, game_name: str = ""):
    if not game_name:
        await ctx.send("사용법: `!게임검색 [게임명]`")
        return
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        info = await fetch_steam_game_search(game_name)
    if not info:
        await ctx.send(msg_not_found(tone, game_name))
        return
    embed = discord.Embed(title=info["name"], url=info["url"], color=0x1B2838)
    embed.description = f"💰 가격: **{info['price']}**\n🔗 [Steam 스토어 바로가기]({info['url']})"
    embed.set_thumbnail(
        url=f"https://cdn.akamai.steamstatic.com/steam/apps/{info['app_id']}/header.jpg"
    )
    await ctx.send(msg_game_found(tone, game_name), embed=embed)


@bot.command(name="스팀무료")
async def cmd_steam_free(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        games = await fetch_steam_free_games()
    if not games:
        await ctx.send(msg_not_found(tone, "무료 게임"))
        return
    embed = discord.Embed(title="🎮 Steam 현재 무료 게임", color=0x1B2838)
    for g in games:
        name = g.get("name", "알 수 없음")
        app_id = g.get("id", "")
        url = f"https://store.steampowered.com/app/{app_id}/" if app_id else ""
        embed.add_field(name=name, value=f"[스토어 가기]({url})" if url else "링크 없음", inline=False)
    intro = tr(tone,
        "현재 무료 게임 목록이옵니다.",
        "현재 무료 게임이에요~",
        "지금 무료 게임 목록이야",
        "무료 게임 목록",
        "여기",
        "공짜 게임 목록 ㅋ"
    )
    await ctx.send(intro, embed=embed)


@bot.command(name="스팀인기")
async def cmd_steam_top(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        games = await fetch_steam_top_sellers()
    if not games:
        await ctx.send("Steam 인기 게임 정보를 가져올 수 없어요.")
        return
    embed = discord.Embed(title="🔥 Steam 인기 판매 게임", color=0xFF4500)
    for i, g in enumerate(games, 1):
        name = g.get("name", "알 수 없음")
        app_id = g.get("id", "")
        price = g.get("final_price", 0)
        price_str = "무료" if price == 0 else f"{format_number(price // 100)}원"
        url = f"https://store.steampowered.com/app/{app_id}/"
        embed.add_field(name=f"{i}. {name}", value=f"💰 {price_str} | [바로가기]({url})", inline=False)
    intro = tr(tone,
        "Steam 인기 게임 목록이옵니다.",
        "요즘 Steam 인기 게임이에요~",
        "지금 Steam 인기 게임이야",
        "Steam 인기 게임",
        "인기 게임 목록",
        "인기 게임 ㅋ"
    )
    await ctx.send(intro, embed=embed)


@bot.command(name="게임추천")
async def cmd_game_recommend(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    games = [
        ("스타듀 밸리",       "농사+마을 생활 힐링 게임",            "https://store.steampowered.com/app/413150/"),
        ("홀로우 나이트",      "어둡고 깊은 2D 액션 어드벤처",        "https://store.steampowered.com/app/367520/"),
        ("하데스",            "로그라이크 + 스토리가 미친 게임",       "https://store.steampowered.com/app/1145360/"),
        ("셀레스트",           "난이도 높은 플랫포머, 스토리 감동",    "https://store.steampowered.com/app/504230/"),
        ("아이작의 번제",      "중독성 갑 로그라이크",                 "https://store.steampowered.com/app/250900/"),
        ("리스크 오브 레인 2", "3인칭 로그라이크 슈터",               "https://store.steampowered.com/app/632360/"),
        ("데드 셀",           "메트로배니아 + 로그라이크",            "https://store.steampowered.com/app/588650/"),
        ("테라리아",           "2D 마인크래프트 느낌 어드벤처",       "https://store.steampowered.com/app/105600/"),
        ("더 위쳐 3",         "역대급 오픈월드 RPG",                  "https://store.steampowered.com/app/292030/"),
        ("엘든 링",           "소울라이크 오픈월드 명작",              "https://store.steampowered.com/app/1245620/"),
    ]
    name, desc, url = random.choice(games)
    embed = discord.Embed(title=f"🎲 오늘의 게임 추천: {name}", url=url, color=0x57F287)
    embed.description = f"📝 {desc}\n\n[Steam에서 보기]({url})"
    recom = tr(tone,
        f"감히 **{name}**을 추천드리옵니다.",
        f"오늘은 **{name}** 어때요?",
        f"**{name}** 해봐! {desc}",
        f"**{name}** 추천",
        f"**{name}** 어때",
        f"**{name}** 해봐 재밌어 ㅋ"
    )
    await ctx.send(recom, embed=embed)


@bot.command(name="전적")
async def cmd_record(ctx: commands.Context, game: str = "", *, nickname: str = ""):
    tone = get_tone(ctx.author, ctx.guild)
    if not game or not nickname:
        games_list = ", ".join(RECORD_SITES.keys())
        usage = tr(tone,
            "사용법: `!전적 [게임] [닉네임]`",
            "사용법: `!전적 [게임] [닉네임]` 이에요~",
            "이렇게 써봐: `!전적 롤 닉네임`",
            "`!전적 [게임] [닉네임]`",
            "`!전적 롤 닉네임`",
            "사용법도 몰라? `!전적 롤 닉네임`"
        )
        await ctx.send(f"{usage}\n지원: `{games_list}`")
        return
    embed = make_record_embed(game, nickname)
    if embed is None:
        await ctx.send(msg_not_found(tone, game))
        return
    await ctx.send(msg_record_found(tone, game, nickname), embed=embed)


@bot.command(name="유튜브", aliases=["youtube", "yt"])
async def cmd_youtube(ctx: commands.Context, *, query: str = ""):
    tone = get_tone(ctx.author, ctx.guild)
    if not query:
        no_q = tr(tone,
            "검색어를 입력해 주시옵소서.",
            "검색어를 써줘요~",
            "검색어 써봐",
            "`!유튜브 [검색어]`",
            "검색어 넣어",
            "검색어 안 넣었잖아 ㅋ"
        )
        await ctx.send(no_q)
        return
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    embed = discord.Embed(title=f"🔍 유튜브 검색: {query}", url=url, color=0xFF0000)
    embed.description = f"[검색 결과 보러가기]({url})"
    embed.set_footer(text="유튜브 바로가기 클릭!")
    await ctx.send(msg_youtube(tone, query), embed=embed)


@bot.command(name="날씨")
async def cmd_weather(ctx: commands.Context, *, city: str = "Seoul"):
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        result = await fetch_weather(city)
    msg = tr(tone,
        f"**{city}** 날씨 정보이옵니다:\n{result}",
        f"**{city}** 날씨에요~\n{result}",
        f"**{city}** 날씨야:\n{result}",
        f"**{city}** 날씨:\n{result}",
        f"{result}",
        f"{city} 날씨 ㅋ:\n{result}"
    )
    await ctx.send(msg)


@bot.command(name="환율")
async def cmd_exchange(ctx: commands.Context, from_cur: str = "USD", to_cur: str = "KRW"):
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        result = await fetch_exchange(from_cur, to_cur)
    msg = tr(tone,
        f"환율 정보이옵니다: {result}",
        f"환율이에요~\n{result}",
        f"환율: {result}",
        result,
        result,
        f"{result} ㅇㅇ"
    )
    await ctx.send(msg)


@bot.command(name="주사위", aliases=["dice", "roll"])
async def cmd_dice(ctx: commands.Context, faces: int = 6):
    if faces < 2 or faces > 10000:
        faces = 6
    result = random.randint(1, faces)
    tone = get_tone(ctx.author, ctx.guild)
    msg = tr(tone,
        f"주사위 결과: **{result}** (1~{faces})",
        f"주사위 결과: **{result}** (1~{faces}) 🎲",
        f"주사위: **{result}** (1~{faces})",
        f"**{result}** / {faces}",
        f"{result}",
        f"{result} ㅋ 이게 결과야"
    )
    await ctx.send(msg)


@bot.command(name="끝말잇기")
async def cmd_wordchain(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    if wordchain_state["active"]:
        await ctx.send(msg_already_playing(tone))
        return
    start_word = random.choice(["사과", "바나나", "게임", "컴퓨터", "키보드", "마우스", "모니터", "스팀", "사람", "나무"])
    wordchain_state.update({
        "active": True,
        "channel_id": ctx.channel.id,
        "last_word": start_word,
        "last_user_id": bot.user.id if bot.user else None,
        "used_words": {start_word},
    })
    intro = tr(tone,
        f"끝말잇기를 시작하겠사옵니다! 제가 먼저: **{start_word}**\n**{start_word[-1]}**로 시작하는 단어를 이어주시옵소서!",
        f"끝말잇기 시작해요~ 제가 먼저: **{start_word}**\n**{start_word[-1]}**로 시작하는 단어 이어받아요!",
        f"끝말잇기 시작! 내가 먼저: **{start_word}** → **{start_word[-1]}**로 이어봐!",
        f"끝말잇기 시작. **{start_word}** → **{start_word[-1]}**로 이어가",
        f"시작: **{start_word}** — **{start_word[-1]}**로 이어봐",
        f"할 거야? 시작: **{start_word}** — **{start_word[-1]}**로 이어봐 ㅋ"
    )
    await ctx.send(intro)


@bot.command(name="숫자게임", aliases=["숫자맞추기"])
async def cmd_number(ctx: commands.Context, max_num: int = 100):
    tone = get_tone(ctx.author, ctx.guild)
    if ctx.channel.id in number_game_state:
        await ctx.send(msg_already_playing(tone))
        return
    if max_num < 2:
        max_num = 100
    number_game_state[ctx.channel.id] = {"answer": random.randint(1, max_num), "tries": 0}
    intro = tr(tone,
        f"1부터 {max_num}까지 숫자를 맞춰주시옵소서!",
        f"1~{max_num} 사이 숫자를 맞춰봐요!",
        f"1~{max_num} 숫자 맞춰봐!",
        f"1~{max_num} 숫자 맞추기 시작",
        f"1~{max_num} 맞춰봐",
        f"1~{max_num} 맞춰봐 ㅋ 쉬울 것 같지?"
    )
    await ctx.send(intro)


@bot.command(name="초성퀴즈")
async def cmd_chosung_quiz(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    if ctx.channel.id in chosung_state:
        await ctx.send(msg_already_playing(tone))
        return
    answer, chosung, hint = random.choice(CHOSUNG_QUIZ)
    chosung_state[ctx.channel.id] = {"answer": answer, "chosung": chosung, "hint": hint}
    intro = tr(tone,
        f"초성 퀴즈!\n초성: **{chosung}**\n힌트: {hint}\n정답을 맞춰주시옵소서!",
        f"초성 퀴즈 시작~!\n초성: **{chosung}**\n힌트: {hint}\n뭔지 알겠어요?",
        f"초성 퀴즈!\n초성: **{chosung}**\n힌트: {hint}\n맞춰봐!",
        f"초성: **{chosung}** (힌트: {hint})\n정답은?",
        f"초성: **{chosung}** — {hint}",
        f"초성: **{chosung}** / {hint}\n맞춰봐 ㅋ"
    )
    await ctx.send(intro)



# ═══════════════════════════════════════════════
#  롤토체스(TFT) 커맨드
# ═══════════════════════════════════════════════

@bot.command(name="롤토체스", aliases=["tft", "TFT"])
async def cmd_tft_profile(ctx: commands.Context, *, riot_id: str = ""):
    """
    TFT 전적 조회.
    사용법: !롤토체스 닉네임#태그    (예: !롤토체스 Hide on bush#KR1)
    태그 없으면 기본 KR1 사용.
    """
    tone = get_tone(ctx.author, ctx.guild)

    if not riot_id:
        usage = tr(tone,
            "사용법: `!롤토체스 닉네임#태그` 이옵니다. 예) `!롤토체스 Hide on bush#KR1`",
            "사용법: `!롤토체스 닉네임#태그` 예) `!롤토체스 Hide on bush#KR1`",
            "이렇게 써봐: `!롤토체스 닉네임#태그`",
            "`!롤토체스 닉네임#태그`",
            "`!롤토체스 닉네임#태그`",
            "사용법 모름? `!롤토체스 닉네임#태그`"
        )
        await ctx.send(usage)
        return

    if not RIOT_API_KEY:
        await ctx.send(
            "Riot API 키가 없어요.\n"
            "`.env` 파일에 `RIOT_API_KEY=RGAPI-...` 를 추가하고 봇을 재시작해 주세요.\n"
            "발급: <https://developer.riotgames.com/>"
        )
        return

    game_name, tag_line = _parse_riot_id(riot_id)

    loading = tr(tone,
        f"**{game_name}#{tag_line}** 전적을 조회하겠사옵니다. 잠시만 기다려 주시옵소서...",
        f"**{game_name}#{tag_line}** 전적 조회 중이에요~ 잠깐만요!",
        f"**{game_name}#{tag_line}** 찾아보는 중...",
        f"잠깐, **{game_name}#{tag_line}** 조회 중",
        f"조회 중...",
        f"잠깐만 ㅋ"
    )
    msg = await ctx.send(loading)

    async with ctx.typing():
        result = await build_tft_profile_embed(game_name, tag_line, tone)

    await msg.delete()

    if isinstance(result, str):
        await ctx.send(result)
    else:
        intro = tr(tone,
            f"**{game_name}#{tag_line}**님의 TFT 전적이옵니다.",
            f"**{game_name}#{tag_line}**님 TFT 전적이에요!",
            f"**{game_name}#{tag_line}** TFT 전적 찾아봤어!",
            f"**{game_name}#{tag_line}** 전적",
            f"전적 나왔어",
            f"나왔어 ㅋ"
        )
        await ctx.send(intro, embed=result)


@bot.command(name="TFT랭킹", aliases=["tft랭킹", "롤토체스랭킹"])
async def cmd_tft_top(ctx: commands.Context, tier: str = "challenger"):
    """
    TFT 상위 티어 랭킹.
    사용법: !TFT랭킹 [챌린저|그랜드마스터|마스터]
    """
    tone = get_tone(ctx.author, ctx.guild)

    tier_map = {
        "챌린저": "CHALLENGER", "challenger": "CHALLENGER",
        "그랜드마스터": "GRANDMASTER", "grandmaster": "GRANDMASTER", "gm": "GRANDMASTER",
        "마스터": "MASTER", "master": "MASTER",
    }
    tier_en = tier_map.get(tier.lower(), "CHALLENGER")

    if not RIOT_API_KEY:
        await ctx.send("Riot API 키가 없어요. `.env`에 `RIOT_API_KEY=RGAPI-...` 추가 후 재시작해 주세요.")
        return

    async with ctx.typing():
        result = await build_tft_top_embed(tier_en, tone)

    if isinstance(result, str):
        await ctx.send(result)
    else:
        intro = tr(tone,
            "TFT 상위 랭킹이옵니다.",
            "TFT 상위 랭킹이에요~",
            "TFT 상위 랭킹이야",
            "TFT 랭킹",
            "랭킹",
            "랭킹 ㅋ"
        )
        await ctx.send(intro, embed=result)


@bot.command(name="TFT챔피언", aliases=["tft챔피언", "롤토체스챔피언"])
async def cmd_tft_meta(ctx: commands.Context):
    """현재 TFT 세트 챔피언 코스트별 목록 (API Key 불필요)"""
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        result = await build_tft_meta_embed(tone)
    if isinstance(result, str):
        await ctx.send(result)
    else:
        intro = tr(tone,
            "현재 세트 챔피언 목록이옵니다.",
            "현재 세트 챔피언 목록이에요~",
            "현재 세트 챔피언 목록이야",
            "챔피언 목록",
            "챔피언 목록",
            "챔피언 목록 ㅋ"
        )
        await ctx.send(intro, embed=result)


@bot.command(name="TFT시너지", aliases=["tft시너지", "롤토체스시너지", "TFT특성"])
async def cmd_tft_traits(ctx: commands.Context):
    """현재 TFT 세트 특성(시너지) 목록 (API Key 불필요)"""
    tone = get_tone(ctx.author, ctx.guild)
    async with ctx.typing():
        result = await build_tft_traits_embed(tone)
    if isinstance(result, str):
        await ctx.send(result)
    else:
        intro = tr(tone,
            "현재 세트 특성 목록이옵니다.",
            "현재 세트 시너지 목록이에요~",
            "현재 세트 시너지 목록이야",
            "시너지 목록",
            "시너지 목록",
            "시너지 목록 ㅋ"
        )
        await ctx.send(intro, embed=result)


@bot.command(name="게임종료", aliases=["종료"])
async def cmd_end_game(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    ended = False
    if wordchain_state["active"] and wordchain_state["channel_id"] == ctx.channel.id:
        wordchain_state.update({"active": False, "channel_id": None, "last_word": "", "used_words": set()})
        ended = True
    if ctx.channel.id in number_game_state:
        del number_game_state[ctx.channel.id]
        ended = True
    if ctx.channel.id in chosung_state:
        del chosung_state[ctx.channel.id]
        ended = True
    await ctx.send(msg_game_ended(tone) if ended else msg_no_game(tone))


@bot.command(name="서버정보")
async def cmd_server_info(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send("서버에서만 사용 가능해요.")
        return
    tone = get_tone(ctx.author, ctx.guild)
    g = ctx.guild
    embed = discord.Embed(title=f"📊 {g.name}", color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👑 서버 주인",   value=str(g.owner),                          inline=True)
    embed.add_field(name="👥 멤버 수",     value=f"{g.member_count}명",                 inline=True)
    embed.add_field(name="📅 생성일",      value=g.created_at.strftime("%Y-%m-%d"),     inline=True)
    embed.add_field(name="💬 채널",        value=f"텍스트 {len(g.text_channels)}개 / 음성 {len(g.voice_channels)}개", inline=True)
    embed.add_field(name="🎭 역할",        value=f"{len(g.roles)}개",                   inline=True)
    embed.add_field(name="🔖 부스트",      value=f"Lv.{g.premium_tier} ({g.premium_subscription_count}개)", inline=True)
    intro = tr(tone, "서버 정보이옵니다.", "서버 정보에요~", "서버 정보야", "서버 정보", "여기", "서버 정보 ㅋ")
    await ctx.send(intro, embed=embed)


@bot.command(name="내정보")
async def cmd_my_info(ctx: commands.Context):
    tone = get_tone(ctx.author, ctx.guild)
    m = ctx.author
    lv = parse_level(m) if isinstance(m, discord.Member) else 0
    embed = discord.Embed(title=f"👤 {m.display_name}", color=0x57F287)
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name="🏷️ 닉네임",  value=m.display_name,                                                inline=True)
    embed.add_field(name="🆔 태그",    value=str(m),                                                        inline=True)
    embed.add_field(name="⭐ 레벨",    value=f"Lv.{lv}",                                                    inline=True)
    embed.add_field(name="🎭 말투단계",value=tone,                                                          inline=True)
    if isinstance(m, discord.Member) and m.joined_at:
        embed.add_field(name="📅 참가일", value=m.joined_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="🗓️ 계정생성", value=m.created_at.strftime("%Y-%m-%d"),                           inline=True)
    intro = tr(tone, "회원님 정보이옵니다.", "내 정보에요~", "내 정보야", "정보", "여기", "정보 ㅋ")
    await ctx.send(intro, embed=embed)


@bot.command(name="선톡")
@commands.check(lambda ctx: ctx.author.id in ADMIN_IDS)
async def cmd_say(ctx: commands.Context, *, text: str):
    """관리자용: 봇이 해당 채널에 메시지 전송"""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    await ctx.send(text)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        tone = get_tone(ctx.author, ctx.guild)
        await ctx.send(tr(tone,
            "인자가 부족하옵니다. `!도움말` 참고하시옵소서.",
            "인자가 부족해요. `!도움말` 참고해요~",
            "인자 빠뜨렸어. `!도움말` 봐봐",
            "인자 빠짐. `!도움말` 봐",
            "인자 빠짐",
            "제대로 입력해 ㅋ `!도움말` 봐라"
        ))
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("권한이 없어요.")
        return
    print(f"[ERROR] {ctx.command}: {error}")


# ═══════════════════════════════════════════════
#  자동 발화 루프
# ═══════════════════════════════════════════════

@tasks.loop(minutes=1)
async def auto_talk_loop():
    global last_auto_talk_time
    if not AUTO_TALK_ENABLED:
        return
    now = time.time()
    interval = random.randint(AUTO_TALK_MIN_MINUTES * 60, AUTO_TALK_MAX_MINUTES * 60)
    if now - last_auto_talk_time < interval:
        return
    last_auto_talk_time = now

    target_channels = []
    if BOT_RANDOM_TALK_CHANNELS:
        for guild in bot.guilds:
            for ch_id in BOT_RANDOM_TALK_CHANNELS:
                ch = guild.get_channel(ch_id)
                if ch and isinstance(ch, discord.TextChannel):
                    target_channels.append(ch)
    else:
        for guild in bot.guilds:
            if guild.system_channel:
                target_channels.append(guild.system_channel)

    if not target_channels:
        return

    ch = random.choice(target_channels)
    try:
        await ch.send(random.choice(AUTO_TALK_MESSAGES))
    except discord.Forbidden:
        pass


# ═══════════════════════════════════════════════
#  실행
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN이 .env에 없습니다.\n"
            ".env 파일에 DISCORD_TOKEN=봇토큰 을 넣어 주세요."
        )
    print("봇 시작 중...")
    bot.run(token)
