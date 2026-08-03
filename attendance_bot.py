"""
Discord 스터디 출석 / 타임랭킹 봇
--------------------------------------------------
- 지정 음성채널(이름 '포함' 매칭) 입장/퇴장 스탬프 (임베드 카드)
- 모든 세션을 '영구 로그'로 저장 → 임의 기간(주간·월간) 조회/문서화 가능
- 자동 발표: 주간 랭킹(매주 수 17:00), 월간 랭킹(매월 말일 21:00)
- 기간 랭킹은 Markdown 로그 파일로 저장 + 디스코드에 파일로 첨부
- 정모 참석 체크(✅ 이모지)

명령어:
  !주간 / !현황   이번 주 전체 타임랭킹 (랭킹만)
  !월간           이번 달 전체 타임랭킹 (랭킹만)
  !주간로그        이번 주 기록 Markdown 파일
  !월간로그        이번 달 기록 Markdown 파일
  !전체로그        전체 기록 Markdown 파일
  !오늘           내 오늘 세션 기록
  !내주간         내 이번 주 기록만 (본인만)
  !내순위         내 이번 주 순위 + 누적 (본인만)
  !정모현황       정모 참석 예정자
  (관리자) !정모체크 · !시간추가 @멤버 분
※ 자동 발표(주간 수17시 / 월간 말일)에는 로그 파일이 함께 첨부됨.

실행: pip install discord.py pytz python-dotenv
"""

import os
import io
import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# ==================== 설정 ====================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

LOG_CHANNEL_NAME = "출석체크"    # 스탬프/정산 올릴 텍스트 채널 (이름 '포함' 매칭)
BOT_NICKNAME = "스터디 봇"       # 서버 표시 별명. None이면 변경 안 함.

VOICE_CHANNEL_IDS = []           # 채널 ID로 지정(권장, 정확). 비우면 아래 이름 '포함' 매칭.
VOICE_CHANNEL_NAMES = ["공부방"] # 이름에 이 키워드가 포함된 음성채널 감지 (공부방🔇, 🎧공부방 등)

TIMEZONE = pytz.timezone("Asia/Seoul")
MIN_ATTENDANCE_MINUTES = 1       # 이 시간(분) 이상이면 '참여 O'
DATA_FILE = "attendance_data.json"
LOG_DIR = "logs"                 # 기간 랭킹 Markdown 로그 저장 폴더

COLOR_JOIN = 0x57F287
COLOR_LEAVE = 0x95A5A6
COLOR_WEEKLY = 0xFEE75C
COLOR_MONTHLY = 0xEB459E
# =============================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 상태 ---
voice_sessions = {}   # {user_id(int): join_time(datetime)}  현재 접속 중
sessions_log = []     # 영구 세션 로그: [{"uid","name","start","end","dur"}]
meetup = {"message_id": None, "channel_id": None, "attendees": {}}
MEETUP_EMOJI = "✅"
MEDALS = ["🥇", "🥈", "🥉"]


# ==================== 유틸 ====================
def now_kst():
    return datetime.now(TIMEZONE)


def get_week_start(ref=None):
    """기준 시점의 '현재 주기 시작'(가장 최근 수요일 17:00 <= ref) 반환"""
    now = ref or now_kst()
    days_since_wed = (now.weekday() - 2) % 7
    last_wed = (now - timedelta(days=days_since_wed)).replace(hour=17, minute=0, second=0, microsecond=0)
    if last_wed > now:
        last_wed -= timedelta(weeks=1)
    return last_wed


def month_start(ref=None):
    now = ref or now_kst()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def is_last_day_of_month(dt):
    return (dt + timedelta(days=1)).month != dt.month


def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}시간 {m}분 {s}초"
    if m > 0:
        return f"{m}분 {s}초"
    return f"{s}초"


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessions": sessions_log, "meetup": meetup}, f, ensure_ascii=False, indent=2)


def load_data():
    global sessions_log, meetup
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        sessions_log = data.get("sessions", [])
        meetup = data.get("meetup", meetup)
    except (json.JSONDecodeError, ValueError):
        sessions_log = []


async def get_log_channel(guild):
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if ch:
        return ch
    for c in guild.text_channels:
        if LOG_CHANNEL_NAME in c.name:
            return c
    return None


def is_target_channel(channel):
    if channel is None:
        return False
    if VOICE_CHANNEL_IDS:
        return channel.id in VOICE_CHANNEL_IDS
    return any(k in channel.name for k in VOICE_CHANNEL_NAMES)


# ==================== 집계 ====================
def sessions_in_range(start_dt, end_dt):
    """종료 시각이 [start, end] 안에 든 세션들"""
    out = []
    for s in sessions_log:
        try:
            end = datetime.fromisoformat(s["end"])
        except (KeyError, ValueError):
            continue
        if start_dt <= end <= end_dt:
            out.append(s)
    return out


def totals_in_range(start_dt, end_dt):
    """기간 내 uid별 누적: {uid: {"name","sec"}}"""
    totals = {}
    for s in sessions_in_range(start_dt, end_dt):
        t = totals.setdefault(s["uid"], {"name": s["name"], "sec": 0.0})
        t["sec"] += s["dur"]
        t["name"] = s["name"]
    return totals


def personal_total(uid, start_dt, end_dt):
    return sum(s["dur"] for s in sessions_in_range(start_dt, end_dt) if s["uid"] == uid)


def personal_sessions_today(uid):
    start = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    return [s for s in sessions_in_range(start, now_kst()) if s["uid"] == uid]


# ==================== 임베드/문서 ====================
def build_ranking_embed(guild, title, start_dt, end_dt, color):
    period = f"{start_dt.strftime('%Y.%m.%d %H:%M')} ~ {end_dt.strftime('%m.%d %H:%M')}"
    embed = discord.Embed(title=title, color=color)
    embed.set_footer(text=period)
    totals = totals_in_range(start_dt, end_dt)
    if not totals:
        embed.description = "이 기간엔 기록이 없어요."
        return embed
    ranked = sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True)
    lines, rest = [], []
    for i, (uid, d) in enumerate(ranked, 1):
        row = f"<@{uid}> — {format_duration(d['sec'])}"
        if i <= 3:
            lines.append(f"{i}등{MEDALS[i-1]} : {row}")
        else:
            rest.append(f"· {row}")
    body = "\n".join(lines)
    if rest:
        body += "\n-\n" + "\n".join(rest)
    embed.description = body
    meetup_att = meetup.get("attendees", {})
    if meetup_att:
        embed.add_field(name=f"📢 정모 참석 예정 ({len(meetup_att)}명)",
                        value=", ".join(meetup_att.values()), inline=False)
    return embed


def build_leave_embed(name, channel_name, today_sessions):
    total = sum(s["dur"] for s in today_sessions)
    lines = [
        f"입장 {datetime.fromisoformat(s['start']).strftime('%H:%M:%S')} → "
        f"퇴장 {datetime.fromisoformat(s['end']).strftime('%H:%M:%S')} · 머문 시간 **{format_duration(s['dur'])}**"
        for s in today_sessions
    ]
    desc = "\n".join(lines) + f"\n\n오늘 누적: **{format_duration(total)}** (총 {len(today_sessions)}회)"
    embed = discord.Embed(title=f"🔴 퇴장 · {name}", description=desc, color=COLOR_LEAVE)
    embed.set_footer(text=f"채널: {channel_name}")
    return embed


def markdown_log(title, start_dt, end_dt):
    """기간 랭킹을 Markdown 문서 문자열로"""
    totals = totals_in_range(start_dt, end_dt)
    ranked = sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True)
    lines = [
        f"# {title}",
        f"기간: {start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| 순위 | 이름 | 누적 시간 | 세션 수 |",
        "|---|---|---|---|",
    ]
    counts = {}
    for s in sessions_in_range(start_dt, end_dt):
        counts[s["uid"]] = counts.get(s["uid"], 0) + 1
    for i, (uid, d) in enumerate(ranked, 1):
        lines.append(f"| {i} | {d['name']} | {format_duration(d['sec'])} | {counts.get(uid, 0)} |")
    if not ranked:
        lines.append("| - | 기록 없음 | - | - |")
    return "\n".join(lines) + "\n"


def markdown_full():
    """전체 기간 누적 랭킹 + 모든 세션 목록"""
    lines = ["# 전체 스터디 로그",
             f"생성: {now_kst().strftime('%Y-%m-%d %H:%M')}",
             f"총 세션 수: {len(sessions_log)}", ""]
    totals = {}
    for s in sessions_log:
        t = totals.setdefault(s["uid"], {"name": s["name"], "sec": 0.0})
        t["sec"] += s["dur"]
        t["name"] = s["name"]
    lines += ["## 전체 누적 랭킹", "", "| 순위 | 이름 | 누적 시간 |", "|---|---|---|"]
    for i, (uid, d) in enumerate(sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True), 1):
        lines.append(f"| {i} | {d['name']} | {format_duration(d['sec'])} |")
    lines += ["", "## 전체 세션 목록", "", "| 날짜 | 이름 | 입장 | 퇴장 | 시간 |", "|---|---|---|---|---|"]
    for s in sorted(sessions_log, key=lambda x: x.get("end", "")):
        try:
            st = datetime.fromisoformat(s["start"])
            en = datetime.fromisoformat(s["end"])
        except (KeyError, ValueError):
            continue
        lines.append(f"| {en.strftime('%Y-%m-%d')} | {s['name']} | "
                     f"{st.strftime('%H:%M:%S')} | {en.strftime('%H:%M:%S')} | {format_duration(s['dur'])} |")
    return "\n".join(lines) + "\n"


def build_log_file(md_text, tag, end_dt):
    """Markdown을 logs/에 저장하고 discord.File 반환"""
    fname = f"{tag}_{end_dt.strftime('%Y-%m-%d')}.md"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as f:
            f.write(md_text)
    except OSError:
        pass
    return discord.File(io.BytesIO(md_text.encode("utf-8")), filename=fname)


async def send_ranking_embed(channel, guild, title, start_dt, end_dt, color):
    """랭킹 임베드만 전송 (로그 파일 없음)"""
    await channel.send(embed=build_ranking_embed(guild, title, start_dt, end_dt, color))


async def send_ranking_with_log(channel, guild, title, start_dt, end_dt, color, tag):
    """임베드 + 로그 파일 함께 (자동 발표용)"""
    embed = build_ranking_embed(guild, title, start_dt, end_dt, color)
    file = build_log_file(markdown_log(title, start_dt, end_dt), tag, end_dt)
    await channel.send(embed=embed, file=file)


# ==================== 이벤트 ====================
@bot.event
async def on_ready():
    load_data()
    if BOT_NICKNAME:
        for guild in bot.guilds:
            try:
                await guild.me.edit(nick=BOT_NICKNAME)
            except discord.Forbidden:
                print(f"별명 변경 권한 없음: {guild.name}")
    now = now_kst()
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if is_target_channel(vc):
                for m in vc.members:
                    if not m.bot:
                        voice_sessions[m.id] = now
    if not weekly_report.is_running():
        weekly_report.start()
    if not monthly_report.is_running():
        monthly_report.start()
    if not monthly_divider.is_running():
        monthly_divider.start()
    print(f"봇 시작됨: {bot.user} / 로그 세션 수: {len(sessions_log)}")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    now = now_kst()
    before_t = is_target_channel(before.channel)
    after_t = is_target_channel(after.channel)

    if not before_t and after_t:  # 입장
        voice_sessions[member.id] = now
        log_channel = await get_log_channel(member.guild)
        if log_channel:
            await log_channel.send(embed=discord.Embed(
                title=f"🟢 입장 · {member.display_name}",
                description=f"`{after.channel.name}` 에 들어왔어요 · {now.strftime('%H:%M:%S')}",
                color=COLOR_JOIN))

    elif before_t and not after_t:  # 퇴장
        if member.id not in voice_sessions:
            return
        join = voice_sessions.pop(member.id)
        dur = (now - join).total_seconds()
        sessions_log.append({
            "uid": str(member.id), "name": member.display_name,
            "start": join.isoformat(), "end": now.isoformat(), "dur": dur,
        })
        save_data()
        log_channel = await get_log_channel(member.guild)
        if log_channel:
            today = personal_sessions_today(str(member.id))
            await log_channel.send(embed=build_leave_embed(member.display_name, before.channel.name, today))


# ==================== 자동 발표 ====================
@tasks.loop(minutes=1)
async def weekly_report():
    now = now_kst()
    if not (now.weekday() == 2 and now.hour == 17 and now.minute == 0):
        return
    start = now - timedelta(days=7)
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            await send_ranking_with_log(ch, guild, "🏆 Weekly 스터디 타임랭킹 🏆",
                                        start, now, COLOR_WEEKLY, "weekly")


@tasks.loop(minutes=1)
async def monthly_report():
    now = now_kst()
    if not (is_last_day_of_month(now) and now.hour == 21 and now.minute == 0):
        return
    start = month_start(now)
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            await send_ranking_with_log(ch, guild, "📅 Monthly 스터디 타임랭킹 📅",
                                        start, now, COLOR_MONTHLY, "monthly")


def month_divider_text(dt):
    line = "━" * 18
    return f"{line}\n## 📅  {dt.year}년 {dt.month}월  📅\n{line}"


@tasks.loop(minutes=1)
async def monthly_divider():
    """매월 1일 00:00에 월 구분선 게시"""
    now = now_kst()
    if not (now.day == 1 and now.hour == 0 and now.minute == 0):
        return
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            await ch.send(month_divider_text(now))


@weekly_report.before_loop
async def _b1():
    await bot.wait_until_ready()


@monthly_report.before_loop
async def _b2():
    await bot.wait_until_ready()


@monthly_divider.before_loop
async def _b3():
    await bot.wait_until_ready()


# ==================== 명령어 ====================
@bot.command(name="주간")
async def weekly_cmd(ctx):
    """!주간 - 이번 주 전체 타임랭킹 (랭킹만)"""
    await send_ranking_embed(ctx.channel, ctx.guild, "🏆 Weekly 스터디 타임랭킹 🏆",
                             get_week_start(), now_kst(), COLOR_WEEKLY)


@bot.command(name="현황")
async def status_cmd(ctx):
    await weekly_cmd(ctx)


@bot.command(name="월간")
async def monthly_cmd(ctx):
    """!월간 - 이번 달 전체 타임랭킹 (랭킹만)"""
    await send_ranking_embed(ctx.channel, ctx.guild, "📅 Monthly 스터디 타임랭킹 📅",
                             month_start(), now_kst(), COLOR_MONTHLY)


@bot.command(name="주간로그")
async def weekly_log_cmd(ctx):
    """!주간로그 - 이번 주 기록 Markdown 파일"""
    now = now_kst()
    file = build_log_file(markdown_log("🏆 Weekly 스터디 타임랭킹", get_week_start(), now), "weekly", now)
    await ctx.send("📄 이번 주 로그예요.", file=file)


@bot.command(name="월간로그")
async def monthly_log_cmd(ctx):
    """!월간로그 - 이번 달 기록 Markdown 파일"""
    now = now_kst()
    file = build_log_file(markdown_log("📅 Monthly 스터디 타임랭킹", month_start(), now), "monthly", now)
    await ctx.send("📄 이번 달 로그예요.", file=file)


@bot.command(name="전체로그")
async def full_log_cmd(ctx):
    """!전체로그 - 전체 기록 Markdown 파일"""
    file = build_log_file(markdown_full(), "full", now_kst())
    await ctx.send("📄 전체 로그예요.", file=file)


@bot.command(name="구분선")
async def divider_cmd(ctx):
    """!구분선 - 이번 달 월 구분선 게시 (수동)"""
    await ctx.send(month_divider_text(now_kst()))


@bot.command(name="오늘")
async def today_cmd(ctx):
    """!오늘 - 내 오늘 세션 기록 (본인만)"""
    today = personal_sessions_today(str(ctx.author.id))
    if not today:
        await ctx.send(f"**{ctx.author.display_name}** 님의 오늘 기록이 아직 없어요.")
        return
    await ctx.send(embed=build_leave_embed(ctx.author.display_name, "오늘", today))


def _personal_footer(start, now):
    return f"{start.strftime('%Y.%m.%d %H:%M')} ~ {now.strftime('%m.%d %H:%M')}"


@bot.command(name="내주간")
async def my_week_cmd(ctx):
    """!내주간 - 내 이번 주 기록만 (본인만)"""
    uid = str(ctx.author.id)
    start, now = get_week_start(), now_kst()
    cnt = len([s for s in sessions_in_range(start, now) if s["uid"] == uid])
    if cnt == 0:
        await ctx.send(f"**{ctx.author.display_name}** 님의 이번 주 기록이 아직 없어요.")
        return
    total = personal_total(uid, start, now)
    embed = discord.Embed(
        title=f"📊 {ctx.author.display_name} 님의 이번 주 기록",
        description=f"누적 **{format_duration(total)}** · 총 {cnt}회",
        color=COLOR_WEEKLY)
    embed.set_footer(text=_personal_footer(start, now))
    await ctx.send(embed=embed)


@bot.command(name="내순위")
async def my_rank_cmd(ctx):
    """!내순위 - 내 이번 주 순위 + 누적 (본인만)"""
    uid = str(ctx.author.id)
    start, now = get_week_start(), now_kst()
    totals = totals_in_range(start, now)
    if uid not in totals:
        await ctx.send(f"**{ctx.author.display_name}** 님의 이번 주 기록이 아직 없어요.")
        return
    ranked = sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True)
    rank = next(i for i, (u, _) in enumerate(ranked, 1) if u == uid)
    medal = MEDALS[rank - 1] if rank <= 3 else ""
    embed = discord.Embed(
        title=f"📊 {ctx.author.display_name} 님의 이번 주 순위",
        description=f"**{rank}등**{medal} · 누적 **{format_duration(totals[uid]['sec'])}** (총 {len(ranked)}명 중)",
        color=COLOR_WEEKLY)
    embed.set_footer(text=_personal_footer(start, now))
    await ctx.send(embed=embed)


# ---- 정모 참석 체크 ----
@bot.command(name="정모체크")
@commands.has_permissions(manage_guild=True)
async def meetup_check_cmd(ctx):
    msg = await ctx.send("📢 **이번 주 정모 참석 체크**\n참석하시는 분은 아래 ✅ 를 눌러주세요!")
    await msg.add_reaction(MEETUP_EMOJI)
    meetup["message_id"] = msg.id
    meetup["channel_id"] = msg.channel.id
    meetup["attendees"] = {}
    save_data()


@bot.command(name="정모현황")
async def meetup_status_cmd(ctx):
    att = meetup.get("attendees", {})
    if not att:
        await ctx.send("아직 정모 참석 체크가 없어요. 관리자가 `!정모체크` 로 시작할 수 있어요.")
        return
    await ctx.send(f"📢 **정모 참석 예정** ({len(att)}명): {', '.join(att.values())}")


@bot.command(name="시간추가")
@commands.has_permissions(manage_guild=True)
async def add_time_cmd(ctx, member: discord.Member, minutes: int):
    """!시간추가 @멤버 분 - 수동 보정 세션 추가(음수 가능)"""
    now = now_kst()
    sessions_log.append({
        "uid": str(member.id), "name": member.display_name,
        "start": now.isoformat(), "end": now.isoformat(), "dur": minutes * 60,
    })
    save_data()
    await ctx.send(f"✏️ **{member.display_name}** 님 기록에 {minutes:+d}분 보정 세션을 추가했어요.")


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
        await ctx.send("⛔ 이 명령어는 관리자만 쓸 수 있어요.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("사용법 확인: 예) `!시간추가 @홍길동 30`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("입력값을 확인해주세요. 멤버는 @멘션, 분은 숫자.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        raise error


# ==================== 실행 ====================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
