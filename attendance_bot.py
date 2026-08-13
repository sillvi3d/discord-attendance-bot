"""
Discord 스터디 출석 / 타임랭킹 봇
--------------------------------------------------
- 지정 음성채널(이름 '포함' 매칭) 입장/퇴장 스탬프 (임베드 카드)
- 모든 세션을 '영구 로그'로 저장 → 임의 기간 조회/문서화 (경계 분할로 정확 집계)
- 자동 발표:
    · 아침 07:00  '어제의 스터디 랭킹' (전날 0시~23:59:59)
    · 매주 수 17:00  주간 랭킹 + 로그 (지난 회의 참석자 포함)
    · 매월 말일 21:00  월간 랭킹 + 로그
    · 매월 1일 00:00  월 구분선
- 회의 참석 체크(✅): 관리자가 !회의체크로 게시 → 참석자 기록 → 로그 저장

명령어:
  !주간 / !현황   이번 주 전체 타임랭킹
  !월간           이번 달 전체 타임랭킹
  !주간로그 / !월간로그 / !전체로그   기록 Markdown 파일
  !오늘           내 오늘 기록 (본인만)
  !내주간 / !내순위   내 이번 주 기록·순위 (본인만)
  !회의현황       오늘 회의 참석자 / !회의로그  회의 참석 이력 파일
  (관리자) !회의체크 · !시간추가 @멤버 분 · !구분선

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

VOICE_CHANNEL_IDS = []           # 채널 ID로 지정(권장). 비우면 아래 이름 '포함' 매칭.
VOICE_CHANNEL_NAMES = ["공부방"] # 이름에 이 키워드가 포함된 음성채널 감지 (공부방🔇, 🎧공부방 등)

TIMEZONE = pytz.timezone("Asia/Seoul")
DATA_FILE = "attendance_data.json"
LOG_DIR = "logs"

COLOR_JOIN = 0x57F287
COLOR_LEAVE = 0x95A5A6
COLOR_DAILY = 0x5865F2
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
sessions_log = []     # 영구 세션 로그: [{"uid","name","start","end","dur","manual"?}]
meetup = {"message_id": None, "channel_id": None, "created": None, "attendees": {}}
meeting_log = []      # 지난 회의 이력: [{"date": iso, "names": [...]}]
MEETUP_EMOJI = "✅"
MEDALS = ["🥇", "🥈", "🥉"]


# ==================== 유틸 ====================
def now_kst():
    return datetime.now(TIMEZONE)


def day_start(ref=None):
    now = ref or now_kst()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def get_week_start(ref=None):
    """가장 최근 수요일 17:00 (<= ref)"""
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
    neg = seconds < 0
    seconds = abs(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        txt = f"{h}시간 {m}분 {s}초"
    elif m > 0:
        txt = f"{m}분 {s}초"
    else:
        txt = f"{s}초"
    return ("-" + txt) if neg else txt


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessions": sessions_log, "meetup": meetup, "meetings": meeting_log},
                  f, ensure_ascii=False, indent=2)


def load_data():
    global sessions_log, meetup, meeting_log
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        sessions_log = data.get("sessions", [])
        meetup = data.get("meetup", meetup)
        meeting_log = data.get("meetings", [])
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


# ==================== 집계 (경계 분할) ====================
def overlap_seconds(s, start_dt, end_dt):
    """세션이 [start, end] 기간과 실제로 겹치는 시간(초). 수동 보정은 그 시각이 기간 안이면 dur."""
    try:
        st = datetime.fromisoformat(s["start"])
        en = datetime.fromisoformat(s["end"])
    except (KeyError, ValueError):
        return 0.0
    if s.get("manual"):
        return s["dur"] if start_dt <= en <= end_dt else 0.0
    lo, hi = max(st, start_dt), min(en, end_dt)
    return max(0.0, (hi - lo).total_seconds())


def totals_in_range(start_dt, end_dt):
    """{uid: {"name","sec","cnt"}} — 기간에 겹친 시간만 합산"""
    totals = {}
    for s in sessions_log:
        ov = overlap_seconds(s, start_dt, end_dt)
        if ov == 0:
            continue
        t = totals.setdefault(s["uid"], {"name": s["name"], "sec": 0.0, "cnt": 0})
        t["sec"] += ov
        t["cnt"] += 1
        t["name"] = s["name"]
    return totals


def personal_total(uid, start_dt, end_dt):
    return sum(overlap_seconds(s, start_dt, end_dt) for s in sessions_log if s["uid"] == uid)


def personal_count(uid, start_dt, end_dt):
    return sum(1 for s in sessions_log if s["uid"] == uid and overlap_seconds(s, start_dt, end_dt) != 0)


def personal_today(uid):
    """오늘(0시~지금) 겹치는 세션들: [(session, 오늘분_초)]"""
    start, now = day_start(), now_kst()
    out = []
    for s in sessions_log:
        if s["uid"] != uid:
            continue
        ov = overlap_seconds(s, start, now)
        if ov != 0:
            out.append((s, ov))
    return out


# --- 회의 참석 이력 ---
def all_meetings():
    result = list(meeting_log)
    if meetup.get("created") and meetup.get("attendees"):
        result.append({"date": meetup["created"], "names": list(meetup["attendees"].values())})
    return result


def meeting_names_in_range(start_dt, end_dt):
    names, seen = [], set()
    for mt in all_meetings():
        try:
            d = datetime.fromisoformat(mt["date"])
        except (KeyError, ValueError):
            continue
        if start_dt <= d <= end_dt:
            for n in mt.get("names", []):
                if n not in seen:
                    seen.add(n)
                    names.append(n)
    return names


# ==================== 임베드/문서 ====================
def display_name_of(guild, uid, fallback):
    """서버 별명(스터디방 이름)을 우선 반환. 모바일에서도 별명이 정확히 뜨도록 멘션 대신 사용."""
    try:
        m = guild.get_member(int(uid)) if guild else None
    except (ValueError, AttributeError):
        m = None
    return m.display_name if m else fallback


def build_ranking_embed(guild, title, start_dt, end_dt, color, show_meeting=True):
    period = f"{start_dt.strftime('%Y.%m.%d %H:%M')} ~ {end_dt.strftime('%m.%d %H:%M')}"
    embed = discord.Embed(title=title, color=color)
    embed.set_footer(text=period)
    totals = totals_in_range(start_dt, end_dt)
    if totals:
        ranked = sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True)
        lines, rest = [], []
        for i, (uid, d) in enumerate(ranked, 1):
            row = f"**{display_name_of(guild, uid, d['name'])}** — {format_duration(d['sec'])}"
            if i <= 3:
                lines.append(f"{i}등{MEDALS[i-1]} : {row}")
            else:
                rest.append(f"· {row}")
        body = "\n".join(lines)
        if rest:
            body += "\n-\n" + "\n".join(rest)
        embed.description = body
    else:
        embed.description = "이 기간엔 기록이 없어요."
    if show_meeting:
        names = meeting_names_in_range(start_dt, end_dt)
        embed.add_field(name=f"🏛️ 지난 회의 참석자 ({len(names)}명)",
                        value=", ".join(names) if names else "없음", inline=False)
    return embed


def build_leave_embed(name, channel_name, today_sessions):
    total = sum(ov for _, ov in today_sessions)
    lines = []
    for s, ov in today_sessions:
        if s.get("manual"):
            lines.append(f"보정 · **{format_duration(ov)}**")
        else:
            st = datetime.fromisoformat(s["start"]).strftime("%H:%M:%S")
            en = datetime.fromisoformat(s["end"]).strftime("%H:%M:%S")
            lines.append(f"입장 {st} → 퇴장 {en} · 머문 시간 **{format_duration(ov)}**")
    desc = "\n".join(lines) + f"\n\n오늘 누적: **{format_duration(total)}** (총 {len(today_sessions)}회)"
    embed = discord.Embed(title=f"🔴 퇴장 · {name}", description=desc, color=COLOR_LEAVE)
    embed.set_footer(text=f"채널: {channel_name}")
    return embed


def markdown_log(title, start_dt, end_dt):
    totals = totals_in_range(start_dt, end_dt)
    ranked = sorted(totals.items(), key=lambda x: x[1]["sec"], reverse=True)
    lines = [f"# {title}",
             f"기간: {start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}",
             "", "| 순위 | 이름 | 누적 시간 | 세션 수 |", "|---|---|---|---|"]
    for i, (uid, d) in enumerate(ranked, 1):
        lines.append(f"| {i} | {d['name']} | {format_duration(d['sec'])} | {d['cnt']} |")
    if not ranked:
        lines.append("| - | 기록 없음 | - | - |")
    names = meeting_names_in_range(start_dt, end_dt)
    lines += ["", "## 회의 참석자", "", (", ".join(names) if names else "없음")]
    return "\n".join(lines) + "\n"


def markdown_full():
    lines = ["# 전체 스터디 로그",
             f"생성: {now_kst().strftime('%Y-%m-%d %H:%M')}",
             f"총 세션 수: {len(sessions_log)}", ""]
    totals = {}
    for s in sessions_log:
        t = totals.setdefault(s["uid"], {"name": s["name"], "sec": 0.0})
        t["sec"] += s.get("dur", 0)
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
        tag = "보정" if s.get("manual") else ""
        lines.append(f"| {en.strftime('%Y-%m-%d')} | {s['name']}{tag} | "
                     f"{st.strftime('%H:%M:%S')} | {en.strftime('%H:%M:%S')} | {format_duration(s.get('dur', 0))} |")
    lines += ["", "## 회의 참석 이력", ""]
    for mt in sorted(all_meetings(), key=lambda x: x.get("date", "")):
        try:
            d = datetime.fromisoformat(mt["date"]).strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            d = "?"
        lines.append(f"- {d} ({len(mt.get('names', []))}명): {', '.join(mt.get('names', [])) or '없음'}")
    return "\n".join(lines) + "\n"


def build_log_file(md_text, tag, end_dt):
    fname = f"{tag}_{end_dt.strftime('%Y-%m-%d')}.md"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as f:
            f.write(md_text)
    except OSError:
        pass
    return discord.File(io.BytesIO(md_text.encode("utf-8")), filename=fname)


async def send_ranking_embed(channel, guild, title, start_dt, end_dt, color):
    await channel.send(embed=build_ranking_embed(guild, title, start_dt, end_dt, color))


async def send_ranking_with_log(channel, guild, title, start_dt, end_dt, color, tag):
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
    for loop in (daily_report, weekly_report, monthly_report, monthly_divider):
        if not loop.is_running():
            loop.start()
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
        ch = await get_log_channel(member.guild)
        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🟢 입장 · {member.display_name}",
                description=f"`{after.channel.name}` 에 들어왔어요 · {now.strftime('%H:%M:%S')}",
                color=COLOR_JOIN))

    elif before_t and not after_t:  # 퇴장
        if member.id not in voice_sessions:
            return
        join = voice_sessions.pop(member.id)
        sessions_log.append({
            "uid": str(member.id), "name": member.display_name,
            "start": join.isoformat(), "end": now.isoformat(),
            "dur": (now - join).total_seconds(),
        })
        save_data()
        ch = await get_log_channel(member.guild)
        if ch:
            await ch.send(embed=build_leave_embed(member.display_name, before.channel.name,
                                                  personal_today(str(member.id))))


# ==================== 자동 발표 ====================
@tasks.loop(minutes=1)
async def daily_report():
    """매일 07:00 — 어제(0시~23:59:59) 스터디 랭킹"""
    now = now_kst()
    if not (now.hour == 7 and now.minute == 0):
        return
    today0 = day_start(now)
    y_start = today0 - timedelta(days=1)
    y_end = today0 - timedelta(seconds=1)
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            embed = build_ranking_embed(guild, "☀️ 어제의 스터디 랭킹 — 어제 가장 많이 공부한 사람은?",
                                        y_start, y_end, COLOR_DAILY, show_meeting=False)
            await ch.send(embed=embed)


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
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            await send_ranking_with_log(ch, guild, "📅 Monthly 스터디 타임랭킹 📅",
                                        month_start(now), now, COLOR_MONTHLY, "monthly")


def month_divider_text(dt):
    line = "━" * 18
    return f"{line}\n## 📅  {dt.year}년 {dt.month}월  📅\n{line}"


@tasks.loop(minutes=1)
async def monthly_divider():
    now = now_kst()
    if not (now.day == 1 and now.hour == 0 and now.minute == 0):
        return
    for guild in bot.guilds:
        ch = await get_log_channel(guild)
        if ch:
            await ch.send(month_divider_text(now))


@daily_report.before_loop
async def _bd():
    await bot.wait_until_ready()


@weekly_report.before_loop
async def _bw():
    await bot.wait_until_ready()


@monthly_report.before_loop
async def _bm():
    await bot.wait_until_ready()


@monthly_divider.before_loop
async def _bv():
    await bot.wait_until_ready()


# ==================== 명령어 ====================
@bot.command(name="주간")
async def weekly_cmd(ctx):
    """!주간 - 이번 주 전체 타임랭킹"""
    await send_ranking_embed(ctx.channel, ctx.guild, "🏆 Weekly 스터디 타임랭킹 🏆",
                             get_week_start(), now_kst(), COLOR_WEEKLY)


@bot.command(name="현황")
async def status_cmd(ctx):
    await weekly_cmd(ctx)


@bot.command(name="월간")
async def monthly_cmd(ctx):
    """!월간 - 이번 달 전체 타임랭킹"""
    await send_ranking_embed(ctx.channel, ctx.guild, "📅 Monthly 스터디 타임랭킹 📅",
                             month_start(), now_kst(), COLOR_MONTHLY)


@bot.command(name="주간로그")
async def weekly_log_cmd(ctx):
    now = now_kst()
    file = build_log_file(markdown_log("🏆 Weekly 스터디 타임랭킹", get_week_start(), now), "weekly", now)
    await ctx.send("📄 이번 주 로그예요.", file=file)


@bot.command(name="월간로그")
async def monthly_log_cmd(ctx):
    now = now_kst()
    file = build_log_file(markdown_log("📅 Monthly 스터디 타임랭킹", month_start(), now), "monthly", now)
    await ctx.send("📄 이번 달 로그예요.", file=file)


@bot.command(name="전체로그")
async def full_log_cmd(ctx):
    file = build_log_file(markdown_full(), "full", now_kst())
    await ctx.send("📄 전체 로그예요.", file=file)


@bot.command(name="회의로그")
async def meeting_log_cmd(ctx):
    lines = ["# 회의 참석 이력", ""]
    mts = sorted(all_meetings(), key=lambda x: x.get("date", ""))
    if not mts:
        lines.append("기록 없음")
    for mt in mts:
        try:
            d = datetime.fromisoformat(mt["date"]).strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            d = "?"
        lines.append(f"- **{d}** ({len(mt.get('names', []))}명): {', '.join(mt.get('names', [])) or '없음'}")
    file = build_log_file("\n".join(lines) + "\n", "meetings", now_kst())
    await ctx.send("📄 회의 참석 이력이에요.", file=file)


@bot.command(name="구분선")
async def divider_cmd(ctx):
    await ctx.send(month_divider_text(now_kst()))


@bot.command(name="오늘")
async def today_cmd(ctx):
    """!오늘 - 내 오늘 기록 (본인만)"""
    today = personal_today(str(ctx.author.id))
    if not today:
        await ctx.send(f"**{ctx.author.display_name}** 님의 오늘 기록이 아직 없어요.")
        return
    await ctx.send(embed=build_leave_embed(ctx.author.display_name, "오늘", today))


def _pfooter(start, now):
    return f"{start.strftime('%Y.%m.%d %H:%M')} ~ {now.strftime('%m.%d %H:%M')}"


@bot.command(name="내주간")
async def my_week_cmd(ctx):
    """!내주간 - 내 이번 주 기록만 (본인만)"""
    uid = str(ctx.author.id)
    start, now = get_week_start(), now_kst()
    cnt = personal_count(uid, start, now)
    if cnt == 0:
        await ctx.send(f"**{ctx.author.display_name}** 님의 이번 주 기록이 아직 없어요.")
        return
    embed = discord.Embed(
        title=f"📊 {ctx.author.display_name} 님의 이번 주 기록",
        description=f"누적 **{format_duration(personal_total(uid, start, now))}** · 총 {cnt}회",
        color=COLOR_WEEKLY)
    embed.set_footer(text=_pfooter(start, now))
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
    embed.set_footer(text=_pfooter(start, now))
    await ctx.send(embed=embed)


# ---- 회의 참석 체크 ----
@bot.command(name="회의체크", aliases=["정모체크"])
@commands.has_permissions(manage_guild=True)
async def meeting_check_cmd(ctx):
    """!회의체크 - 오늘 회의 참석 체크 게시 (관리자)"""
    if meetup.get("created") and meetup.get("attendees"):
        meeting_log.append({"date": meetup["created"], "names": list(meetup["attendees"].values())})
    msg = await ctx.send("🏛️ **오늘 회의 참석 체크**\n오늘 회의에 참석하신 분은 아래 ✅ 를 눌러주세요!")
    await msg.add_reaction(MEETUP_EMOJI)
    meetup["message_id"] = msg.id
    meetup["channel_id"] = msg.channel.id
    meetup["created"] = now_kst().isoformat()
    meetup["attendees"] = {}
    save_data()


@bot.command(name="회의현황", aliases=["정모현황"])
async def meeting_status_cmd(ctx):
    att = meetup.get("attendees", {})
    if not att:
        await ctx.send("아직 회의 참석 체크가 없어요. 관리자가 `!회의체크` 로 시작할 수 있어요.")
        return
    await ctx.send(f"🏛️ **오늘 회의 참석** ({len(att)}명): {', '.join(att.values())}")


@bot.command(name="시간추가")
@commands.has_permissions(manage_guild=True)
async def add_time_cmd(ctx, member: discord.Member, minutes: int):
    """!시간추가 @멤버 분 - 수동 보정 세션 추가(음수 가능)"""
    now = now_kst()
    sessions_log.append({
        "uid": str(member.id), "name": member.display_name,
        "start": now.isoformat(), "end": now.isoformat(),
        "dur": minutes * 60, "manual": True,
    })
    save_data()
    await ctx.send(f"✏️ **{member.display_name}** 님 기록에 {minutes:+d}분 보정을 추가했어요.")


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
