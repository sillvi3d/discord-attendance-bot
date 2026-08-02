"""
Discord 출석체크 / 스터디 타임랭킹 봇
--------------------------------------------------
- 지정한 음성채널(2개)에 입장/퇴장할 때마다 기록 채널에 스탬프
- 24시(자정) 기준 개인별 '일일' 세션 트래킹
    · 퇴장할 때마다 그날 쌓인 세션 목록 전체 + 오늘 누적 시간을 보여줌
    · 자정이 지나면 일일 기록은 자동 초기화
- 매주 수요일 오후 5시에 'Weekly 스터디 타임랭킹' 정산
    · 1~3등 메달, 나머지 명단, @멘션
    · 출석/미출석 명단, 정모 참석 예정자도 함께 표시
- 매일 자정에 '오늘의 공부시간' 개인별 요약 전송 (진행 중 세션은 자정 기준 스냅샷)
- 정모 참석 체크: ✅ 이모지 반응으로 집계
- 데이터는 JSON 파일에 저장되어 봇이 재시작돼도 유지됨

명령어:
    !오늘        내 오늘 세션 기록
    !현황        이번 주 타임랭킹 미리보기
    !내순위      내 순위/누적시간
    !정모현황    정모 참석 예정자
    (관리자) !리셋 · !시간추가 @멤버 분 · !정모체크

실행 준비:
    pip install discord.py pytz python-dotenv
"""

import os
import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# ==================== 설정 ====================
load_dotenv()
# 토큰은 같은 폴더의 .env 파일에서만 읽어옴 (코드/깃허브에 절대 넣지 않기)
TOKEN = os.getenv("DISCORD_TOKEN")

LOG_CHANNEL_NAME = "기록"        # 스탬프/정산을 남길 텍스트 채널 이름
BOT_NICKNAME = "Study Multi-Bot"       # 서버에서 표시될 봇 별명(한글·공백 가능). None이면 변경 안 함.

# 감지할 음성채널 지정 — 둘 중 하나만 채우면 됩니다.
#  (권장) 채널 ID로 지정: 이름이 같아도 정확히 구분됨.
#         디스코드 설정 > 고급 > 개발자 모드 ON → 음성채널 우클릭 > "채널 ID 복사"
VOICE_CHANNEL_IDS = []                        # 예: [123456789012345678, 987654321098765432]
#  (대안) 이름으로 지정: ID를 비워두면 이 이름들을 감지.
VOICE_CHANNEL_NAMES = ["공부방"]              # 두 채널 이름이 다르면 둘 다 넣기: ["공부방", "정모방"]

TIMEZONE = pytz.timezone("Asia/Seoul")
MIN_ATTENDANCE_MINUTES = 1     # 이 시간(분) 이상 누적해야 '출석 O' 인정. 0이면 잠깐이라도 들어오면 인정.
DATA_FILE = "attendance_data.json"

# 임베드 카드 색상(왼쪽 색 막대). 원하는 색으로 바꾸세요. (0xRRGGBB)
COLOR_JOIN = 0x57F287     # 입장 - 초록
COLOR_LEAVE = 0x95A5A6    # 퇴장 - 회색
COLOR_DAILY = 0x5865F2    # 자정 요약 - 블루플
COLOR_WEEKLY = 0xFEE75C   # 주간 랭킹 - 골드
# =============================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- 메모리 상태 ---
voice_sessions = {}   # {user_id(int): join_time(datetime)}  현재 접속 중인 사람

# --- 저장되는 상태 ---
# 주간 누적(수요일 5시~수요일 5시): {uid(str): {"name","seconds","attended"}}
weekly_time = {}
# 일일 세션(자정 리셋): {uid(str): {"name","date":"YYYY-MM-DD","sessions":[{"join","leave","dur"}]}}
daily_sessions = {}
week_start = None     # datetime
# 정모 참석 체크(이모지 반응): 체크 메시지 1개 + 참석자 목록
meetup = {"message_id": None, "channel_id": None, "attendees": {}}  # attendees: {uid(str): name}
MEETUP_EMOJI = "✅"


# ==================== 유틸 ====================
def now_kst():
    return datetime.now(TIMEZONE)


def today_str():
    return now_kst().strftime("%Y-%m-%d")


def get_week_start():
    """이번 정산 주기의 시작점(지난 수요일 오후 5시) 반환"""
    now = now_kst()
    days_since_wed = (now.weekday() - 2) % 7  # 수요일 = weekday 2
    last_wed = now - timedelta(days=days_since_wed)
    last_wed = last_wed.replace(hour=17, minute=0, second=0, microsecond=0)
    if last_wed > now:
        last_wed -= timedelta(weeks=1)
    return last_wed


def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}시간 {m}분 {s}초"
    elif m > 0:
        return f"{m}분 {s}초"
    else:
        return f"{s}초"


def save_data():
    data = {
        "week_start": week_start.isoformat() if week_start else None,
        "members": weekly_time,
        "daily": daily_sessions,
        "meetup": meetup,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_data():
    global weekly_time, daily_sessions, week_start, meetup
    if not os.path.exists(DATA_FILE):
        week_start = get_week_start()
        weekly_time, daily_sessions = {}, {}
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        weekly_time = data.get("members", {})
        daily_sessions = data.get("daily", {})
        meetup = data.get("meetup", meetup)
        ws = data.get("week_start")
        saved_start = datetime.fromisoformat(ws) if ws else get_week_start()
    except (json.JSONDecodeError, ValueError):
        saved_start, weekly_time, daily_sessions = get_week_start(), {}, {}

    # 저장된 정산 주기가 지난 주라면 주간 데이터 초기화
    current_start = get_week_start()
    if saved_start < current_start:
        weekly_time = {}
        week_start = current_start
    else:
        week_start = saved_start


def ensure_weekly(uid, name):
    if uid not in weekly_time:
        weekly_time[uid] = {"name": name, "seconds": 0, "attended": False}
    weekly_time[uid]["name"] = name


def get_today_record(uid, name):
    """오늘 날짜의 일일 기록을 반환. 날짜가 바뀌었으면 초기화(=자정 리셋)."""
    rec = daily_sessions.get(uid)
    if rec is None or rec.get("date") != today_str():
        rec = {"name": name, "date": today_str(), "sessions": []}
        daily_sessions[uid] = rec
    rec["name"] = name
    return rec


async def get_log_channel(guild):
    return discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)


def is_target_channel(channel):
    if channel is None:
        return False
    if VOICE_CHANNEL_IDS:
        return channel.id in VOICE_CHANNEL_IDS
    return channel.name in VOICE_CHANNEL_NAMES


# ==================== 이벤트 ====================
@bot.event
async def on_ready():
    load_data()
    # 서버 별명을 지정한 이름으로 설정 (한글·공백 가능)
    if BOT_NICKNAME:
        for guild in bot.guilds:
            try:
                await guild.me.edit(nick=BOT_NICKNAME)
            except discord.Forbidden:
                print(f"별명 변경 권한 없음: {guild.name} (봇에 '별명 변경' 권한 필요)")
    # 봇 재시작 시, 이미 대상 채널에 있는 사람들의 입장 시간을 지금으로 재설정
    now = now_kst()
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if is_target_channel(vc):
                for member in vc.members:
                    if not member.bot:
                        voice_sessions[member.id] = now
    if not weekly_report.is_running():
        weekly_report.start()
    if not daily_summary.is_running():
        daily_summary.start()
    print(f"봇 시작됨: {bot.user}")
    print(f"주간 시작 기준: {week_start.strftime('%Y-%m-%d %H:%M')}")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    now = now_kst()
    before_target = is_target_channel(before.channel)
    after_target = is_target_channel(after.channel)

    # --- 입장 (현상 유지: 스탬프만) ---
    if not before_target and after_target:
        voice_sessions[member.id] = now
        ensure_weekly(str(member.id), member.display_name)
        get_today_record(str(member.id), member.display_name)  # 날짜 리셋 체크
        save_data()
        log_channel = await get_log_channel(member.guild)
        if log_channel:
            embed = discord.Embed(
                title=f"🟢 입장 · {member.display_name}",
                description=f"`{after.channel.name}` 에 들어왔어요 · {now.strftime('%H:%M:%S')}",
                color=COLOR_JOIN,
            )
            await log_channel.send(embed=embed)

    # --- 퇴장 (그날 세션 전체 + 오늘 누적 표시) ---
    elif before_target and not after_target:
        if member.id not in voice_sessions:
            return
        join_time = voice_sessions.pop(member.id)
        duration = (now - join_time).total_seconds()
        uid = str(member.id)

        # 주간 누적
        ensure_weekly(uid, member.display_name)
        weekly_time[uid]["seconds"] += duration
        if weekly_time[uid]["seconds"] >= MIN_ATTENDANCE_MINUTES * 60:
            weekly_time[uid]["attended"] = True

        # 일일 세션 기록
        rec = get_today_record(uid, member.display_name)
        rec["sessions"].append({
            "join": join_time.strftime("%H:%M:%S"),
            "leave": now.strftime("%H:%M:%S"),
            "dur": duration,
        })
        save_data()

        log_channel = await get_log_channel(member.guild)
        if log_channel:
            await log_channel.send(embed=build_leave_embed(member.display_name, before.channel.name, rec))


def build_leave_embed(name, channel_name, rec):
    """퇴장 임베드 카드: 오늘 쌓인 모든 세션 + 오늘 누적 시간"""
    total = sum(s["dur"] for s in rec["sessions"])
    lines = [
        f"입장 {s['join']} → 퇴장 {s['leave']} · 머문 시간 **{format_duration(s['dur'])}**"
        for s in rec["sessions"]
    ]
    desc = "\n".join(lines) + f"\n\n오늘 누적: **{format_duration(total)}** (총 {len(rec['sessions'])}회)"
    embed = discord.Embed(title=f"🔴 퇴장 · {name}", description=desc, color=COLOR_LEAVE)
    embed.set_footer(text=f"채널: {channel_name}")
    return embed


# ==================== 자정 일일 요약 ====================
def close_ongoing_sessions_at(now):
    """자정 시점에 아직 접속 중인 사람들의 세션을 '지금'까지로 마감(스냅샷)하고,
    입장 시각을 지금으로 초기화해 새 하루 세션을 시작시킴."""
    for uid_int, join_time in list(voice_sessions.items()):
        duration = (now - join_time).total_seconds()
        uid = str(uid_int)
        # 주간 누적 반영
        if uid in weekly_time:
            weekly_time[uid]["seconds"] += duration
            if weekly_time[uid]["seconds"] >= MIN_ATTENDANCE_MINUTES * 60:
                weekly_time[uid]["attended"] = True
        # 일일 기록에 마감된 세션 추가 (초기화 전이라 어제 기록에 들어감)
        rec = daily_sessions.get(uid)
        if rec is None:
            rec = {"name": uid, "date": join_time.strftime("%Y-%m-%d"), "sessions": []}
            daily_sessions[uid] = rec
        rec["sessions"].append({
            "join": join_time.strftime("%H:%M:%S"),
            "leave": now.strftime("%H:%M:%S"),
            "dur": duration,
        })
        # 새 하루 세션은 자정부터 시작
        voice_sessions[uid_int] = now


def build_daily_summary_embed(date):
    """하루 개인별 총 공부시간 요약 임베드 카드"""
    embed = discord.Embed(title="🌙 오늘의 공부시간 요약", color=COLOR_DAILY)
    embed.set_footer(text=date)
    rows = []
    for uid, rec in daily_sessions.items():
        if not rec.get("sessions"):
            continue
        total = sum(s["dur"] for s in rec["sessions"])
        rows.append((total, rec["name"], len(rec["sessions"])))
    if not rows:
        embed.description = "오늘은 공부 기록이 없었어요."
        return embed
    rows.sort(reverse=True)
    embed.description = "\n".join(
        f"**{name}** — {format_duration(total)} ({cnt}회)" for total, name, cnt in rows
    )
    return embed


@tasks.loop(minutes=1)
async def daily_summary():
    global daily_sessions
    now = now_kst()
    if not (now.hour == 0 and now.minute == 0):
        return
    # 1) 진행 중 세션을 자정 기준으로 마감 → 어제 시간에 포함
    close_ongoing_sessions_at(now)
    # 2) 어제 요약 전송
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for guild in bot.guilds:
        log_channel = await get_log_channel(guild)
        if not log_channel:
            continue
        await log_channel.send(embed=build_daily_summary_embed(yesterday))
    # 3) 일일 기록 초기화 (접속 중인 사람은 자정부터 새 세션)
    daily_sessions = {}
    save_data()


@daily_summary.before_loop
async def before_daily_summary():
    await bot.wait_until_ready()


# ==================== 주간 정산 ====================
@tasks.loop(minutes=1)
async def weekly_report():
    global week_start, weekly_time, daily_sessions
    now = now_kst()
    if not (now.weekday() == 2 and now.hour == 17 and now.minute == 0):
        return
    for guild in bot.guilds:
        log_channel = await get_log_channel(guild)
        if not log_channel:
            continue
        await log_channel.send(embed=build_ranking_embed(guild, now))
    # 정산 후 주간 데이터 초기화
    weekly_time = {}
    week_start = now
    save_data()


MEDALS = ["🥇", "🥈", "🥉"]


def build_ranking_embed(guild, now):
    """Weekly 스터디 타임랭킹 임베드 카드"""
    date_range = f"{week_start.strftime('%Y.%m.%d')}~{now.strftime('%m.%d')}"
    embed = discord.Embed(title="🏆 Weekly 스터디 타임랭킹 🏆", color=COLOR_WEEKLY)
    embed.set_footer(text=date_range)

    if not weekly_time:
        embed.description = "이번 주 활동 기록이 없습니다."
        return embed

    ranked = sorted(weekly_time.items(), key=lambda x: x[1]["seconds"], reverse=True)
    lines, rest = [], []
    for i, (uid, d) in enumerate(ranked, 1):
        mention = f"<@{uid}>"
        dur = format_duration(d["seconds"])
        if i <= 3:
            lines.append(f"{i}등{MEDALS[i-1]} : {mention} — {dur}")
        else:
            rest.append(f"· {mention} — {dur}")
    body = "\n".join(lines)
    if rest:
        body += "\n-\n" + "\n".join(rest)
    embed.description = body

    # 출석 / 미출석 명단
    attended_ids = {uid for uid, d in weekly_time.items() if d.get("attended")}
    present, absent = [], []
    for m in guild.members:
        if m.bot:
            continue
        (present if str(m.id) in attended_ids else absent).append(m.display_name)
    embed.add_field(
        name=f"✅ 출석 ({len(present)}명)",
        value=", ".join(present) if present else "없음", inline=False,
    )
    embed.add_field(
        name=f"❌ 미출석 ({len(absent)}명)",
        value=", ".join(absent) if absent else "없음", inline=False,
    )

    # 오늘 정모 참석 예정 (이모지 체크 결과)
    meetup_att = meetup.get("attendees", {})
    if meetup_att:
        embed.add_field(
            name=f"📢 오늘 정모 참석 예정 ({len(meetup_att)}명)",
            value=", ".join(meetup_att.values()), inline=False,
        )
    return embed


@weekly_report.before_loop
async def before_weekly_report():
    await bot.wait_until_ready()


# ==================== 명령어 ====================
@bot.command(name="오늘")
async def today_cmd(ctx):
    """!오늘 - 내 오늘 세션 기록 확인"""
    uid = str(ctx.author.id)
    rec = daily_sessions.get(uid)
    if not rec or rec.get("date") != today_str() or not rec["sessions"]:
        await ctx.send(f"**{ctx.author.display_name}** 님의 오늘 기록이 아직 없어요.")
        return
    await ctx.send(embed=build_leave_embed(ctx.author.display_name, "오늘", rec))


@bot.command(name="현황")
async def status_cmd(ctx):
    """!현황 - 이번 주 타임랭킹 미리보기"""
    await ctx.send(embed=build_ranking_embed(ctx.guild, now_kst()))


@bot.command(name="내순위")
async def my_rank_cmd(ctx):
    """!내순위 - 내 이번 주 순위/누적시간 확인"""
    uid = str(ctx.author.id)
    if uid not in weekly_time:
        await ctx.send(f"**{ctx.author.display_name}** 님의 이번 주 기록이 아직 없어요.")
        return
    ranked = sorted(weekly_time.items(), key=lambda x: x[1]["seconds"], reverse=True)
    for i, (u, d) in enumerate(ranked, 1):
        if u == uid:
            medal = MEDALS[i - 1] if i <= 3 else ""
            await ctx.send(
                f"📊 **{ctx.author.display_name}** 님 — 현재 **{i}등**{medal} · "
                f"누적 **{format_duration(d['seconds'])}** (총 {len(ranked)}명 중)"
            )
            return


# ---- 관리자 전용 ----
@bot.command(name="리셋")
@commands.has_permissions(manage_guild=True)
async def reset_cmd(ctx):
    """!리셋 - 이번 주 타임랭킹 데이터 초기화 (관리자)"""
    global weekly_time, week_start
    weekly_time = {}
    week_start = now_kst()
    save_data()
    await ctx.send("🔄 이번 주 타임랭킹 데이터를 초기화했어요.")


@bot.command(name="시간추가")
@commands.has_permissions(manage_guild=True)
async def add_time_cmd(ctx, member: discord.Member, minutes: int):
    """!시간추가 @멤버 분 - 주간 누적 시간 수동 조정 (관리자). 음수도 가능."""
    uid = str(member.id)
    ensure_weekly(uid, member.display_name)
    weekly_time[uid]["seconds"] = max(0, weekly_time[uid]["seconds"] + minutes * 60)
    if weekly_time[uid]["seconds"] >= MIN_ATTENDANCE_MINUTES * 60:
        weekly_time[uid]["attended"] = True
    save_data()
    await ctx.send(
        f"✏️ **{member.display_name}** 님 주간 시간 {minutes:+d}분 조정 → "
        f"현재 **{format_duration(weekly_time[uid]['seconds'])}**"
    )


# ---- 정모 참석 체크 (이모지) ----
@bot.command(name="정모체크")
@commands.has_permissions(manage_guild=True)
async def meetup_check_cmd(ctx):
    """!정모체크 - 정모 참석 체크 메시지 게시 (관리자). ✅ 반응으로 집계."""
    msg = await ctx.send(
        "📢 **이번 주 정모 참석 체크**\n"
        "참석하시는 분은 아래 ✅ 를 눌러주세요! (정모: 수요일 오후 8시)"
    )
    await msg.add_reaction(MEETUP_EMOJI)
    meetup["message_id"] = msg.id
    meetup["channel_id"] = msg.channel.id
    meetup["attendees"] = {}
    save_data()


@bot.command(name="정모현황")
async def meetup_status_cmd(ctx):
    """!정모현황 - 정모 참석 예정자 확인"""
    att = meetup.get("attendees", {})
    if not att:
        await ctx.send("아직 정모 참석 체크가 없어요. 관리자가 `!정모체크` 로 시작할 수 있어요.")
        return
    await ctx.send(f"📢 **정모 참석 예정** ({len(att)}명): {', '.join(att.values())}")


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    if (meetup.get("message_id") and payload.message_id == meetup["message_id"]
            and str(payload.emoji) == MEETUP_EMOJI):
        name = payload.member.display_name if payload.member else str(payload.user_id)
        meetup["attendees"][str(payload.user_id)] = name
        save_data()


@bot.event
async def on_raw_reaction_remove(payload):
    if (meetup.get("message_id") and payload.message_id == meetup["message_id"]
            and str(payload.emoji) == MEETUP_EMOJI):
        meetup["attendees"].pop(str(payload.user_id), None)
        save_data()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ 이 명령어는 관리자(서버 관리 권한)만 쓸 수 있어요.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("사용법이 올바르지 않아요. 예: `!시간추가 @홍길동 30`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("입력값을 확인해주세요. 멤버는 @멘션, 분은 숫자로 넣어야 해요.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        raise error


# ==================== 실행 ====================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
