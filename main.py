import os
import re
import random
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.voice_states = True


def parse_duration(text: str) -> int:
    """Parse duration like '20m', '1h', '90s' into seconds."""
    m = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not m:
        raise ValueError("duration must be like 20m / 1h / 90s / 2d")
    value = int(m.group(1))
    unit = m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return value * mult


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MatchState:
    guild_id: int
    owner_id: int
    created_at: datetime

    category_id: int
    lobby_vc_id: int
    team_vc_ids: Dict[str, int]

    move_mode: str  # allow|deny
    locked: bool = False

    original_voice: Dict[int, Optional[int]] = field(default_factory=dict)  # user_id -> channel_id

    timer_task: Optional[asyncio.Task] = None
    timer_end_at: Optional[datetime] = None
    auto_end_on_timer: bool = False


class MatchManagerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.tree = app_commands.CommandTree(self)
        self.match: Optional[MatchState] = None
        self.pending_endmatch_requests: Dict[int, int] = {}  # requester_id -> owner_id

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = MatchManagerBot()


def ensure_guild(interaction: discord.Interaction) -> discord.Guild:
    if not interaction.guild:
        raise app_commands.AppCommandError("This command can only be used in a guild")
    return interaction.guild


async def ensure_manage_permissions(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        raise app_commands.AppCommandError("You need Manage Channels permission to run this")


async def set_team_overwrites(
    *,
    category: discord.CategoryChannel,
    lobby_vc: discord.VoiceChannel,
    team_vcs: List[discord.VoiceChannel],
    move_mode: str,
    locked: bool,
):
    # Base overwrites: inherit from category.
    overwrites = category.overwrites

    # If locked: prevent joining/moving (connect False) for @everyone.
    if locked:
        overwrites = dict(overwrites)
        overwrites[category.guild.default_role] = discord.PermissionOverwrite(connect=False)

    # Apply to each VC. In deny mode, connect is allowed but move is controlled by bot.
    for ch in [lobby_vc, *team_vcs]:
        await ch.edit(overwrites=overwrites)


async def create_match_channels(guild: discord.Guild) -> Tuple[discord.CategoryChannel, discord.VoiceChannel, Dict[str, discord.VoiceChannel]]:
    suffix = random.randint(1000, 9999)
    category = await guild.create_category(f"Match-{suffix}")
    lobby = await guild.create_voice_channel(f"Match-{suffix}", category=category)
    team1 = await guild.create_voice_channel(f"Match-{suffix} | Team1", category=category)
    team2 = await guild.create_voice_channel(f"Match-{suffix} | Team2", category=category)
    return category, lobby, {"team1": team1, "team2": team2}


async def move_member(guild: discord.Guild, member: discord.Member, channel: Optional[discord.VoiceChannel]):
    try:
        await member.move_to(channel)
    except discord.Forbidden:
        raise app_commands.AppCommandError("Bot lacks permission to move members")


async def get_member_voice_channel_id(member: discord.Member) -> Optional[int]:
    if member.voice and member.voice.channel:
        return member.voice.channel.id
    return None


class EndMatchApprovalView(discord.ui.View):
    def __init__(self, owner_id: int, requester_id: int, timeout: int = 60 * 10):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return
        if not bot.match:
            await interaction.followup.send("現在進行中のMatchがありません。", ephemeral=True)
            return
        if bot.match.owner_id != self.owner_id:
            await interaction.followup.send("既に管理者が変更されています。", ephemeral=True)
            return
        await end_match_internal(guild=guild, reason=f"approved by owner via DM", notify_channel=None)
        await interaction.followup.send("/endmatch を承認して終了しました。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("拒否しました。", ephemeral=True)
        self.stop()


async def end_match_internal(*, guild: discord.Guild, reason: str, notify_channel: Optional[discord.abc.Messageable]):
    match = bot.match
    if not match:
        return

    # Stop timer
    if match.timer_task and not match.timer_task.done():
        match.timer_task.cancel()

    # Move members back
    for user_id, orig_ch_id in match.original_voice.items():
        member = guild.get_member(user_id)
        if not member:
            continue
        if orig_ch_id is None:
            # Disconnect
            try:
                await member.move_to(None)
            except Exception:
                pass
            continue
        orig = guild.get_channel(orig_ch_id)
        if isinstance(orig, discord.VoiceChannel):
            try:
                await member.move_to(orig)
            except Exception:
                pass

    # Delete channels and category
    category = guild.get_channel(match.category_id)
    if isinstance(category, discord.CategoryChannel):
        # delete children first
        for ch in list(category.channels):
            try:
                await ch.delete(reason=reason)
            except Exception:
                pass
        try:
            await category.delete(reason=reason)
        except Exception:
            pass

    bot.match = None

    if notify_channel:
        try:
            await notify_channel.send("Matchを終了しました。")
        except Exception:
            pass


async def ensure_single_match(interaction: discord.Interaction):
    if bot.match:
        raise app_commands.AppCommandError("既に進行中のMatchがあります。/endmatch で終了してください")


async def ensure_match_exists(interaction: discord.Interaction):
    if not bot.match:
        raise app_commands.AppCommandError("進行中のMatchがありません")


def is_owner(user_id: int) -> bool:
    return bot.match is not None and bot.match.owner_id == user_id


async def ensure_owner(interaction: discord.Interaction):
    await ensure_match_exists(interaction)
    if not is_owner(interaction.user.id):
        raise app_commands.AppCommandError("この操作はMatch作成者のみ実行できます")


def mention_user(guild: discord.Guild, user_id: int) -> str:
    m = guild.get_member(user_id)
    return m.mention if m else f"<@{user_id}>"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Auto-transfer if owner leaves / disconnects while match exists
    match = bot.match
    if not match or member.guild.id != match.guild_id:
        return

    # Enforce lock: prevent join/move into match category
    if match.locked:
        category = member.guild.get_channel(match.category_id)
        if isinstance(category, discord.CategoryChannel):
            match_channel_ids: Set[int] = {ch.id for ch in category.channels if isinstance(ch, discord.VoiceChannel)}
            # If user tries to join a match voice channel, revert
            if after.channel and after.channel.id in match_channel_ids:
                # Allow bot moves and owner moves? Keep simple: revert everyone.
                try:
                    await member.move_to(before.channel)
                except Exception:
                    pass
                return

    # If owner left match area entirely, transfer
    if member.id == match.owner_id:
        left_voice = before.channel is not None and after.channel is None
        moved_out = before.channel is not None and after.channel is not None and before.channel.id != after.channel.id

        if left_voice or moved_out:
            # If owner not in any match vc, transfer
            category = member.guild.get_channel(match.category_id)
            if not isinstance(category, discord.CategoryChannel):
                return
            match_vcs = [ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)]
            # Is owner still in any of them?
            if after.channel and after.channel in match_vcs:
                return

            candidates: List[discord.Member] = []
            for vc in match_vcs:
                candidates.extend([m for m in vc.members if not m.bot])
            if not candidates:
                return
            new_owner = random.choice(candidates)
            old_owner = match.owner_id
            match.owner_id = new_owner.id
            # Notify in a reasonable channel (system channel or first text channel)
            text_target = member.guild.system_channel
            if not text_target:
                for ch in member.guild.text_channels:
                    text_target = ch
                    break
            if text_target:
                await text_target.send(
                    f"作成者が退出したため、権限を譲渡しました。新しい作成者：{new_owner.mention}（旧：{mention_user(member.guild, old_owner)}）"
                )
            try:
                await new_owner.send("あなたはこのマッチの管理者に変更されました")
            except Exception:
                pass


@bot.tree.command(name="startmatch", description="即席マッチ用のVCを作成しチームへ自動移動")
@app_commands.describe(
    team1="team1 のメンバー（最大10推奨）",
    team2="team2 のメンバー（最大10推奨）",
    move="allow:自由移動 / deny:作成者orBotのみ",
    random_teams="VCにいるメンバーを自動で均等分け（2=2チーム）",
)
@app_commands.choices(move=[
    app_commands.Choice(name="allow", value="allow"),
    app_commands.Choice(name="deny", value="deny"),
])
async def startmatch(
    interaction: discord.Interaction,
    move: app_commands.Choice[str],
    team1: Optional[str] = None,
    team2: Optional[str] = None,
    random_teams: Optional[int] = None,
):
    guild = ensure_guild(interaction)
    await ensure_manage_permissions(interaction)
    await ensure_single_match(interaction)

    await interaction.response.defer(ephemeral=True)

    # Collect members
    team1_members: List[discord.Member] = []
    team2_members: List[discord.Member] = []

    if random_teams:
        if random_teams != 2:
            raise app_commands.AppCommandError("現状 random は 2 のみ対応です")
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise app_commands.AppCommandError("random を使うには、実行者がVCに入っている必要があります")
        members = [m for m in interaction.user.voice.channel.members if not m.bot]
        random.shuffle(members)
        mid = (len(members) + 1) // 2
        team1_members = members[:mid]
        team2_members = members[mid:]
    else:
        if not team1 or not team2:
            raise app_commands.AppCommandError("team1 と team2 を指定するか、random_teams を指定してください")

        # Parse mention list from string
        def parse_members(text: str) -> List[discord.Member]:
            ids = [int(x) for x in re.findall(r"<@!?(\d+)>", text)]
            result: List[discord.Member] = []
            for uid in ids:
                m = guild.get_member(uid)
                if m and not m.bot:
                    result.append(m)
            return result

        team1_members = parse_members(team1)
        team2_members = parse_members(team2)

    # Track original voice channels
    original: Dict[int, Optional[int]] = {}
    for m in team1_members + team2_members:
        original[m.id] = await get_member_voice_channel_id(m)

    category, lobby_vc, team_vcs = await create_match_channels(guild)
    await set_team_overwrites(
        category=category,
        lobby_vc=lobby_vc,
        team_vcs=list(team_vcs.values()),
        move_mode=move.value,
        locked=False,
    )

    bot.match = MatchState(
        guild_id=guild.id,
        owner_id=interaction.user.id,
        created_at=now_utc(),
        category_id=category.id,
        lobby_vc_id=lobby_vc.id,
        team_vc_ids={k: v.id for k, v in team_vcs.items()},
        move_mode=move.value,
        locked=False,
        original_voice=original,
    )

    # Move members to team vcs
    for m in team1_members:
        await move_member(guild, m, team_vcs["team1"])
    for m in team2_members:
        await move_member(guild, m, team_vcs["team2"])

    await interaction.followup.send(
        f"Matchを作成しました。カテゴリ: {category.name} / move: {move.value}",
        ephemeral=True,
    )


@bot.tree.command(name="endmatch", description="Matchを終了し、全員を元のVCへ戻して削除")
async def endmatch(interaction: discord.Interaction):
    guild = ensure_guild(interaction)
    await ensure_match_exists(interaction)

    match = bot.match
    assert match

    if interaction.user.id == match.owner_id:
        await interaction.response.defer(ephemeral=True)
        await end_match_internal(guild=guild, reason="ended by owner", notify_channel=interaction.channel)
        await interaction.followup.send("Matchを終了しました。", ephemeral=True)
        return

    # Request approval
    await interaction.response.send_message("作成者に承認リクエストを送信しました。", ephemeral=True)

    owner = guild.get_member(match.owner_id)
    if owner:
        try:
            view = EndMatchApprovalView(owner_id=match.owner_id, requester_id=interaction.user.id)
            await owner.send(
                f"{interaction.user.display_name} さんが /endmatch をリクエストしています",
                view=view,
            )
        except Exception:
            pass


@bot.tree.command(name="move", description="Match内の移動管理")
@app_commands.describe(team="team1/team2", user="deny時のみ：移動するユーザー")
@app_commands.choices(team=[
    app_commands.Choice(name="team1", value="team1"),
    app_commands.Choice(name="team2", value="team2"),
])
async def move_cmd(
    interaction: discord.Interaction,
    team: app_commands.Choice[str],
    user: Optional[discord.Member] = None,
):
    guild = ensure_guild(interaction)
    await ensure_match_exists(interaction)
    match = bot.match
    assert match

    if match.move_mode == "allow":
        # self move
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise app_commands.AppCommandError("guild member only")
        target = guild.get_channel(match.team_vc_ids[team.value])
        if not isinstance(target, discord.VoiceChannel):
            raise app_commands.AppCommandError("target voice channel not found")
        await interaction.response.defer(ephemeral=True)
        await move_member(guild, member, target)
        await interaction.followup.send(f"{team.value} に移動しました", ephemeral=True)
        return

    # deny mode
    await ensure_owner(interaction)
    if not user:
        raise app_commands.AppCommandError("deny モードでは user を指定してください")
    target = guild.get_channel(match.team_vc_ids[team.value])
    if not isinstance(target, discord.VoiceChannel):
        raise app_commands.AppCommandError("target voice channel not found")
    await interaction.response.defer(ephemeral=True)
    await move_member(guild, user, target)
    await interaction.followup.send(f"{user.mention} を {team.value} に移動しました", ephemeral=True)


@app_commands.guild_only()
@bot.tree.command(name="swap", description="チーム間でユーザーを入れ替え")
@app_commands.describe(user1="入れ替えるユーザー1", user2="入れ替えるユーザー2")
async def swap(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    guild = ensure_guild(interaction)
    await ensure_owner(interaction)
    match = bot.match
    assert match

    def team_of(m: discord.Member) -> Optional[str]:
        if not m.voice or not m.voice.channel:
            return None
        for t, ch_id in match.team_vc_ids.items():
            if m.voice.channel.id == ch_id:
                return t
        return None

    t1 = team_of(user1)
    t2 = team_of(user2)
    if not t1 or not t2 or t1 == t2:
        raise app_commands.AppCommandError("両者が別チームVCにいる必要があります")

    ch1 = guild.get_channel(match.team_vc_ids[t1])
    ch2 = guild.get_channel(match.team_vc_ids[t2])
    assert isinstance(ch1, discord.VoiceChannel)
    assert isinstance(ch2, discord.VoiceChannel)

    await interaction.response.defer(ephemeral=True)
    await move_member(guild, user1, ch2)
    await move_member(guild, user2, ch1)
    await interaction.followup.send(f"{user1.mention} と {user2.mention} を入れ替えました", ephemeral=True)


class MatchGroup(app_commands.Group):
    pass


match_group = MatchGroup(name="match", description="Matchの管理")


@match_group.command(name="status", description="現在のMatch状態を表示")
async def match_status(interaction: discord.Interaction):
    guild = ensure_guild(interaction)
    await ensure_match_exists(interaction)
    match = bot.match
    assert match

    owner = guild.get_member(match.owner_id)
    elapsed = now_utc() - match.created_at

    def vc_members(ch_id: int) -> List[str]:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.VoiceChannel):
            return [m.mention for m in ch.members if not m.bot]
        return []

    lines = [
        f"作成者: {owner.mention if owner else f'<@{match.owner_id}>'}",
        f"move設定: {match.move_mode}",
        f"lock: {'on' if match.locked else 'off'}",
        f"経過時間: {int(elapsed.total_seconds() // 60)}分",
        f"Team1: {' '.join(vc_members(match.team_vc_ids['team1'])) or '(empty)'}",
        f"Team2: {' '.join(vc_members(match.team_vc_ids['team2'])) or '(empty)'}",
    ]

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@match_group.command(name="lock", description="途中参加・移動を防止")
async def match_lock(interaction: discord.Interaction):
    guild = ensure_guild(interaction)
    await ensure_owner(interaction)
    match = bot.match
    assert match

    match.locked = True
    category = guild.get_channel(match.category_id)
    lobby = guild.get_channel(match.lobby_vc_id)
    team_vcs = [guild.get_channel(cid) for cid in match.team_vc_ids.values()]
    if not (isinstance(category, discord.CategoryChannel) and isinstance(lobby, discord.VoiceChannel)):
        raise app_commands.AppCommandError("channel missing")
    team_vcs2 = [ch for ch in team_vcs if isinstance(ch, discord.VoiceChannel)]
    await set_team_overwrites(category=category, lobby_vc=lobby, team_vcs=team_vcs2, move_mode=match.move_mode, locked=True)
    await interaction.response.send_message("lock を有効にしました", ephemeral=True)


@match_group.command(name="unlock", description="lock解除")
async def match_unlock(interaction: discord.Interaction):
    guild = ensure_guild(interaction)
    await ensure_owner(interaction)
    match = bot.match
    assert match

    match.locked = False
    category = guild.get_channel(match.category_id)
    lobby = guild.get_channel(match.lobby_vc_id)
    team_vcs = [guild.get_channel(cid) for cid in match.team_vc_ids.values()]
    if not (isinstance(category, discord.CategoryChannel) and isinstance(lobby, discord.VoiceChannel)):
        raise app_commands.AppCommandError("channel missing")
    team_vcs2 = [ch for ch in team_vcs if isinstance(ch, discord.VoiceChannel)]
    await set_team_overwrites(category=category, lobby_vc=lobby, team_vcs=team_vcs2, move_mode=match.move_mode, locked=False)
    await interaction.response.send_message("lock を解除しました", ephemeral=True)


@match_group.command(name="transfer", description="マッチ管理者を変更")
@app_commands.describe(user="新しい作成者（Match参加中、BOT不可）")
async def match_transfer(interaction: discord.Interaction, user: discord.Member):
    guild = ensure_guild(interaction)
    await ensure_owner(interaction)
    match = bot.match
    assert match

    if user.bot:
        raise app_commands.AppCommandError("BOTには譲渡できません")

    # must be in match vcs
    category = guild.get_channel(match.category_id)
    if not isinstance(category, discord.CategoryChannel):
        raise app_commands.AppCommandError("category missing")
    match_vcs = [ch for ch in category.channels if isinstance(ch, discord.VoiceChannel)]
    in_match = user.voice and user.voice.channel in match_vcs
    if not in_match:
        raise app_commands.AppCommandError("指定ユーザーがMatch VCに参加している必要があります")

    old_owner_id = match.owner_id
    match.owner_id = user.id

    await interaction.response.send_message(
        f"マッチ管理者が変更されました。旧：{mention_user(guild, old_owner_id)} 新：{user.mention}",
        ephemeral=False,
    )
    try:
        await user.send("あなたはこのマッチの管理者に変更されました")
    except Exception:
        pass


async def timer_worker(guild: discord.Guild, duration_sec: int, channel: discord.abc.Messageable, auto_end: bool):
    match = bot.match
    if not match:
        return

    match.timer_end_at = now_utc() + asyncio.timedelta(seconds=duration_sec)  # type: ignore[attr-defined]


@match_group.command(name="timer", description="試合タイマー")
@app_commands.describe(action="start/stop", duration="例: 20m", auto_end="終了時に自動 /endmatch")
async def match_timer(interaction: discord.Interaction, action: str, duration: Optional[str] = None, auto_end: Optional[bool] = False):
    guild = ensure_guild(interaction)
    await ensure_owner(interaction)
    match = bot.match
    assert match

    action = action.lower()
    if action not in {"start", "stop"}:
        raise app_commands.AppCommandError("action は start/stop")

    if action == "stop":
        if match.timer_task and not match.timer_task.done():
            match.timer_task.cancel()
        match.timer_task = None
        match.timer_end_at = None
        await interaction.response.send_message("タイマーを停止しました", ephemeral=True)
        return

    if not duration:
        raise app_commands.AppCommandError("start には duration が必要です（例: 20m）")

    sec = parse_duration(duration)

    async def run():
        try:
            match.timer_end_at = now_utc() + discord.utils.utcnow() - discord.utils.utcnow()  # placeholder
            # 5 min before
            if sec > 5 * 60:
                await asyncio.sleep(sec - 5 * 60)
                await interaction.channel.send("残り5分です")
                await asyncio.sleep(5 * 60)
            else:
                await asyncio.sleep(sec)
            await interaction.channel.send("タイマー終了")
            if auto_end:
                await end_match_internal(guild=guild, reason="auto-ended by timer", notify_channel=interaction.channel)
        except asyncio.CancelledError:
            return

    if match.timer_task and not match.timer_task.done():
        match.timer_task.cancel()

    match.timer_task = asyncio.create_task(run())
    await interaction.response.send_message(f"タイマー開始: {duration}", ephemeral=True)


bot.tree.add_command(match_group)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

bot.run(TOKEN)
