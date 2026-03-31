import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import functools
import re
import datetime
from .matcherino import (
    fetch_ticket_context,
    fetch_payout_report,
    fetch_bracket_progress,
    find_match_by_team_name,
    clear_bracket_teams_cache,
)

from database.mongo import (
    add_blacklisted_user,
    remove_blacklisted_user,
    get_all_blacklisted_users,
    get_blacklisted_user,
    create_tourney_session,
    get_active_tourney_session,
    end_tourney_session,
    reset_tourney_session_start_time,
    increment_tourney_message_count,
    update_matcherino_id,
    update_tourney_queue,
    increment_staff_closure,
    get_top_staff_stats,
    get_matcherino_id_from_active,
)

from features.config import (
    ALLOWED_STAFF_ROLES,
    TOURNEY_UPDATES_CHANNEL_ID,
    TOURNEY_VS_EMOJI,
    TOURNEY_MATCHERINO_WIN_EMOJI,
    TOURNEY_SUPPORT_CHANNEL_ID,
    TOURNEY_ADMIN_CHANNEL_ID,
    PRE_TOURNEY_SUPPORT_CHANNEL_ID,
    TOURNEY_CATEGORY_ID,
    PRE_TOURNEY_CATEGORY_ID,
    TOURNEY_CLOSED_CATEGORY_ID,
    PRE_TOURNEY_CLOSED_CATEGORY_ID,
    HALL_OF_FAME_CHANNEL_ID,
    BOT_VERSION,
    TOURNEY_ADMIN_ROLE_ID,
)
from .tourney_utils import (
    close_ticket_via_command,
    reset_ticket_counter,
    delete_ticket_with_transcript,
    delete_ticket_via_command,
    reopen_ticket_via_command,
)
from .tourney_views import TourneyOpenTicketView, PreTourneyOpenTicketView

TOURNEY_STAGE_HYPE_GIF_URL = "https://cdn.discordapp.com/attachments/807243155698352138/1314223834018222142/4M7IWwP.gif?ex=693ebd53&is=693d6bd3&hm=2a7e2767c8c441f51fad04d147e99b5db2faad7e28a2c799a21356da05ad2294"


def is_staff(member: discord.Member) -> bool:
    """Return True if the member has any of the allowed staff roles."""
    return any(role.id in ALLOWED_STAFF_ROLES for role in member.roles)


class QueueDashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_message_id: int | None = None
        self._progress_dashboard_lock = asyncio.Lock()
        self._announcement_lock = asyncio.Lock()
        self._stage_announcement_state: dict[str, dict[str, str | int | None]] = {
            "semi_finals": {
                "signature": None,
                "message_id": None,
                "hype_message_id": None,
            },
            "finals": {"signature": None, "message_id": None, "hype_message_id": None},
        }
        self._winner_announcement_state: dict[str, str | int | None] = {
            "winner": None,
            "message_id": None,
        }
        self._announcement_matcherino_id: str | None = None
        self.match_refresher_task.start()

    def cog_unload(self):
        self.dashboard_task.cancel()
        self.progress_dashboard_task.cancel()
        self.match_refresher_task.cancel()

    async def start_dashboard(self):
        """Starts dashboard loops if not already running."""
        if not self.dashboard_task.is_running():
            self.dashboard_task.start()
            print("📊 Queue Dashboard Started")
        if not self.progress_dashboard_task.is_running():
            self.progress_dashboard_task.start()
            print("📈 Tourney Progress Dashboard Started")

        await self.update_progress_dashboard()

    async def stop_dashboard(self):
        """Stops dashboard loops and deletes dashboard messages."""
        if self.dashboard_task.is_running():
            self.dashboard_task.cancel()
            print("📊 Queue Dashboard Stopped")
        if self.progress_dashboard_task.is_running():
            self.progress_dashboard_task.cancel()
            print("📈 Tourney Progress Dashboard Stopped")

        channel = self.bot.get_channel(TOURNEY_SUPPORT_CHANNEL_ID)
        if channel and isinstance(channel, discord.TextChannel):
            try:
                async for m in channel.history(limit=10):
                    if (
                        m.author == self.bot.user
                        and m.embeds
                        and m.embeds[0].title == "📊 Live Tournament Queue"
                    ):
                        await m.delete()
                        break
            except Exception as e:
                print(f"Failed to cleanup dashboard message: {e}")

        admin_channel = self.bot.get_channel(TOURNEY_ADMIN_CHANNEL_ID)
        if admin_channel and isinstance(admin_channel, discord.TextChannel):
            try:
                if self.dashboard_message_id:
                    msg = await admin_channel.fetch_message(self.dashboard_message_id)
                    await msg.delete()
                else:
                    async for m in admin_channel.history(limit=20):
                        if (
                            m.author == self.bot.user
                            and m.embeds
                            and m.embeds[0].title == "📈 Live Tournament Progress"
                        ):
                            await m.delete()
                            break
            except Exception as e:
                print(f"Failed to cleanup progress dashboard message: {e}")
            finally:
                self.dashboard_message_id = None

    def _reset_announcement_state_if_needed(self, matcherino_id: str):
        if self._announcement_matcherino_id != matcherino_id:
            self._announcement_matcherino_id = matcherino_id
            for stage_key in self._stage_announcement_state:
                self._stage_announcement_state[stage_key]["signature"] = None
                self._stage_announcement_state[stage_key]["message_id"] = None
                self._stage_announcement_state[stage_key]["hype_message_id"] = None
            self._winner_announcement_state["winner"] = None
            self._winner_announcement_state["message_id"] = None

    @staticmethod
    def _is_known_team(team_name: str | None) -> bool:
        if not team_name:
            return False
        return team_name.strip().upper() not in {
            "TBD",
            "BYE",
            "UNKNOWN",
            "UNKNOWN TEAM",
        }

    def _is_fully_matched(self, match: dict) -> bool:
        return self._is_known_team(match.get("team_a")) and self._is_known_team(
            match.get("team_b")
        )

    @staticmethod
    def _build_stage_signature(matches: list[dict]) -> str:
        sorted_matches = sorted(
            matches,
            key=lambda m: m.get("id") if isinstance(m.get("id"), int) else 9999,
        )
        return "|".join(
            f"{m.get('id')}::{m.get('team_a', 'TBD')}::{m.get('team_b', 'TBD')}"
            for m in sorted_matches
        )

    async def _delete_previous_stage_messages(
        self, channel: discord.TextChannel, stage_key: str
    ):
        stage_state = self._stage_announcement_state[stage_key]
        message_ids = [
            stage_state.get("message_id"),
            stage_state.get("hype_message_id"),
        ]

        for msg_id in message_ids:
            if not isinstance(msg_id, int):
                continue
            try:
                old_msg = await channel.fetch_message(msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except Exception as e:
                print(f"Stage announcement cleanup error ({stage_key}): {e}")

        stage_state["message_id"] = None
        stage_state["hype_message_id"] = None
        stage_state["signature"] = None

    async def _sync_stage_announcement(
        self,
        channel: discord.TextChannel,
        stage_key: str,
        stage_title: str,
        matches: list[dict],
        required_count: int,
    ):
        stage_state = self._stage_announcement_state[stage_key]

        if len(matches) != required_count:
            return

        signature = self._build_stage_signature(matches)
        current_signature = stage_state.get("signature")
        current_message_id = stage_state.get("message_id")

        if signature == current_signature and isinstance(current_message_id, int):
            return

        sorted_matches = sorted(
            matches, key=lambda m: m.get("id") if isinstance(m.get("id"), int) else 9999
        )

        match_lines = []
        for m in sorted_matches:
            team_a = m.get("team_a", "TBD")
            team_b = m.get("team_b", "TBD")
            match_lines.append(f"{team_a}  {TOURNEY_VS_EMOJI}  {team_b}")

        content = f"# {stage_title}\n" + "\n".join(match_lines)

        async for recent in channel.history(limit=8):
            if recent.author == self.bot.user and recent.content == content:
                stage_state["message_id"] = recent.id
                stage_state["signature"] = signature
                stage_state["hype_message_id"] = None
                return

        if isinstance(current_message_id, int):
            try:
                existing_msg = await channel.fetch_message(current_message_id)
                await existing_msg.edit(content=content)
                stage_state["signature"] = signature
                return
            except discord.NotFound:
                stage_state["message_id"] = None
                stage_state["hype_message_id"] = None
            except Exception as e:
                print(f"Stage announcement edit error ({stage_key}): {e}")
                stage_state["message_id"] = None
                stage_state["hype_message_id"] = None

        new_message = await channel.send(content)
        hype_message = await channel.send(TOURNEY_STAGE_HYPE_GIF_URL)
        stage_state["message_id"] = new_message.id
        stage_state["hype_message_id"] = hype_message.id
        stage_state["signature"] = signature

    async def _sync_winner_announcement(
        self,
        channel: discord.TextChannel,
        tournament_complete: bool,
        winner_team: str | None,
    ):
        winner_state = self._winner_announcement_state
        current_winner = winner_state.get("winner")
        current_message_id = winner_state.get("message_id")

        if not tournament_complete or not winner_team:
            return

        if winner_team == current_winner and isinstance(current_message_id, int):
            return

        content = f"# GGs!\n{winner_team} won !! {TOURNEY_MATCHERINO_WIN_EMOJI}"

        async for recent in channel.history(limit=10):
            if recent.author == self.bot.user and recent.content == content:
                winner_state["winner"] = winner_team
                winner_state["message_id"] = recent.id
                return

        new_msg = await channel.send(content)
        winner_state["winner"] = winner_team
        winner_state["message_id"] = new_msg.id

    async def announce_high_stakes_matches(
        self, matcherino_id: str, progress_data: dict
    ):
        async with self._announcement_lock:
            self._reset_announcement_state_if_needed(matcherino_id)

            updates_channel = self.bot.get_channel(TOURNEY_UPDATES_CHANNEL_ID)
            if not updates_channel:
                try:
                    fetched = await self.bot.fetch_channel(TOURNEY_UPDATES_CHANNEL_ID)
                    updates_channel = (
                        fetched if isinstance(fetched, discord.TextChannel) else None
                    )
                except Exception:
                    updates_channel = None
            if not updates_channel or not isinstance(
                updates_channel, discord.TextChannel
            ):
                print(
                    f"High-stakes announcements skipped: updates channel not found ({TOURNEY_UPDATES_CHANNEL_ID})."
                )
                return

            max_round = progress_data.get("max_round")
            active_matches = progress_data.get("active_matches", [])
            all_matches = progress_data.get("all_matches", active_matches)
            if not isinstance(max_round, int) or max_round < 1:
                return
            if not isinstance(active_matches, list):
                active_matches = []
            if not isinstance(all_matches, list):
                all_matches = active_matches

            semi_round = max_round - 1
            semi_candidates = [
                m
                for m in all_matches
                if semi_round >= 1
                and m.get("round") == semi_round
                and self._is_fully_matched(m)
            ]
            final_candidates = [
                m
                for m in all_matches
                if m.get("round") == max_round and self._is_fully_matched(m)
            ]

            semi_candidates = sorted(
                semi_candidates,
                key=lambda m: m.get("id") if isinstance(m.get("id"), int) else 9999,
            )
            final_candidates = sorted(
                final_candidates,
                key=lambda m: m.get("id") if isinstance(m.get("id"), int) else 9999,
            )

            semi_finals = semi_candidates[:2] if len(semi_candidates) >= 2 else []
            finals = final_candidates[:1] if len(final_candidates) >= 1 else []

            await self._sync_stage_announcement(
                updates_channel,
                "semi_finals",
                "Semi Finals",
                semi_finals,
                required_count=2,
            )

            semi_finals_posted = isinstance(
                self._stage_announcement_state["semi_finals"].get("message_id"), int
            )
            has_semi_finals_stage = semi_round >= 1 and len(semi_candidates) >= 2
            if finals and has_semi_finals_stage and not semi_finals_posted:
                finals = []

            await self._sync_stage_announcement(
                updates_channel,
                "finals",
                "Finals",
                finals,
                required_count=1,
            )

            remaining_matches = max(
                0,
                int(progress_data.get("total", 0))
                - int(progress_data.get("closed", 0)),
            )
            tournament_complete = (
                progress_data.get("completion_pct", 0) >= 100 or remaining_matches == 0
            )
            winner_team = progress_data.get("winner_team")
            if isinstance(winner_team, str):
                winner_team = winner_team.strip()

            finals_posted = isinstance(
                self._stage_announcement_state["finals"].get("message_id"), int
            )
            if tournament_complete and winner_team and not finals_posted:
                tournament_complete = False

            await self._sync_winner_announcement(
                updates_channel, tournament_complete, winner_team
            )

    async def update_progress_dashboard(self):
        """Build or update a single persistent progress panel in the admin channel."""
        async with self._progress_dashboard_lock:
            session = await get_active_tourney_session()
            if not session or not session.get("matcherino_id"):
                return

            admin_channel = self.bot.get_channel(TOURNEY_ADMIN_CHANNEL_ID)
            if not admin_channel or not isinstance(admin_channel, discord.TextChannel):
                return

            m_id = session["matcherino_id"]
            bracket_url = f"https://matcherino.com/tournaments/{m_id}/bracket"
            data = fetch_bracket_progress(bracket_url)
            if data.get("status") != "success":
                return

            try:
                await self.announce_high_stakes_matches(m_id, data)
            except Exception as e:
                print(f"High-stakes announcement error: {e}")

            start_time = session["start_time"]
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=datetime.timezone.utc)
            duration = discord.utils.utcnow() - start_time
            hours, mins = divmod(int(duration.total_seconds()), 3600)
            mins, _ = divmod(mins, 60)

            embed = discord.Embed(
                title="📈 Live Tournament Progress", color=discord.Color.gold()
            )
            embed.description = (
                f"**⏱️ Total Duration:** `{hours}h {mins}m` | "
                f"**📈 Completion:** `{data['completion_pct']}%` ({data['closed']}/{data['total']})\n"
                f"**Last Updated:** <t:{int(discord.utils.utcnow().timestamp())}:R>"
            )

            remaining_matches = max(0, data["total"] - data["closed"])
            tournament_complete = (
                data["completion_pct"] >= 100 or remaining_matches == 0
            )

            if tournament_complete:
                path_text = "🏆 **Tournament Over!**"
            else:
                rounds_left = max(0, data["max_round"] - data["dominant_round"])
                path_text = (
                    f"{rounds_left} rounds remaining"
                    if rounds_left > 0
                    else "🏆 **Finals in progress!**"
                )

            active_matches_text = (
                "No matches remaining"
                if tournament_complete
                else f"{data['active_count']} Currently Playable"
            )

            embed.add_field(
                name="🏆 Bracket Status",
                value=(
                    f"• **Dominant Round:** Round {data['dominant_round']}\n"
                    f"• **Path to Finals:** {path_text}\n"
                    f"• **Active Matches:** {active_matches_text}"
                ),
                inline=False,
            )

            if data["bottlenecks"]:
                bn_text = ""
                for bn in data["bottlenecks"][:5]:
                    bn_text += f"**#{bn['id']}** (Round {bn['round']}) | {bn['team_a']} vs {bn['team_b']} ({bn['score_a']}-{bn['score_b']})\n"
                embed.add_field(
                    name="⚠️ Bottleneck Matches", value=bn_text, inline=False
                )
            else:
                embed.add_field(
                    name="⚠️ Bottleneck Matches",
                    value="✅ All playable matches are current with the dominant round.",
                    inline=False,
                )

            embed.set_footer(text=f"Matcherino ID: {m_id} | Auto Refresh: 5m")

            try:
                existing_msg = None
                if self.dashboard_message_id:
                    try:
                        existing_msg = await admin_channel.fetch_message(
                            self.dashboard_message_id
                        )
                    except discord.NotFound:
                        existing_msg = None

                if existing_msg is None:
                    async for m in admin_channel.history(limit=30):
                        if (
                            m.author == self.bot.user
                            and m.embeds
                            and m.embeds[0].title == "📈 Live Tournament Progress"
                        ):
                            existing_msg = m
                            self.dashboard_message_id = m.id
                            break

                latest = [msg async for msg in admin_channel.history(limit=1)]
                latest_msg = latest[0] if latest else None

                if existing_msg and latest_msg and latest_msg.id == existing_msg.id:
                    await existing_msg.edit(embed=embed)
                    return

                new_msg = await admin_channel.send(embed=embed)
                self.dashboard_message_id = new_msg.id

                if existing_msg:
                    try:
                        await existing_msg.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass

            except Exception as e:
                print(f"Progress Dashboard Error: {e}")

    @tasks.loop(seconds=15)
    async def dashboard_task(self):
        """Original 15-second loop: Updates the live queue status in support channel."""
        await self.bot.wait_until_ready()

        channel = self.bot.get_channel(TOURNEY_SUPPORT_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        guild = channel.guild
        cat = guild.get_channel(TOURNEY_CATEGORY_ID)

        active_tickets = []
        active_nums = []

        if cat and isinstance(cat, discord.CategoryChannel):
            active_tickets = [
                c
                for c in cat.channels
                if isinstance(c, discord.TextChannel) and "ticket-" in c.name
            ]
            active_tickets.sort(key=lambda c: c.created_at)

            for t in active_tickets:
                match = re.search(r"ticket-(\d+)", t.name)
                if match:
                    try:
                        active_nums.append(int(match.group(1)))
                    except Exception:
                        pass
            active_nums.sort()

        count = len(active_tickets)
        embed = discord.Embed(
            title="📊 Live Tournament Queue", color=discord.Color.blurple()
        )

        if count == 0:
            embed.color = discord.Color.green()
            embed.description = (
                "✅ **No tickets currently in the queue.**\nStaff are standing by!"
            )
            serving_display = None
        else:
            max_closed_num = 0
            closed_cat = guild.get_channel(TOURNEY_CLOSED_CATEGORY_ID)
            if closed_cat and isinstance(closed_cat, discord.CategoryChannel):
                for ch in closed_cat.channels:
                    match = re.search(r"ticket-(\d+)", ch.name)
                    if match:
                        try:
                            num = int(match.group(1))
                            if num > max_closed_num:
                                max_closed_num = num
                        except Exception:
                            pass

            target_num = max_closed_num + 1
            final_serving_num = (
                target_num
                if target_num in active_nums
                else (min(active_nums) if active_nums else 0)
            )
            serving_display = f"ticket-{final_serving_num:03d}"
            embed.color = discord.Color.orange()

        current_timestamp = int(discord.utils.utcnow().timestamp())
        embed.description = (
            f"**Last Updated:** <t:{current_timestamp}:R>\n\n{embed.description or ''}"
        )

        if serving_display:
            embed.add_field(
                name="🟢 Currently Serving", value=f"**{serving_display}**", inline=True
            )
            embed.add_field(
                name="👥 In Line", value=f"**{count}** tickets waiting", inline=True
            )

        try:
            old_dashboard_msg = None
            async for m in channel.history(limit=10):
                if (
                    m.author == self.bot.user
                    and m.embeds
                    and m.embeds[0].title == "📊 Live Tournament Queue"
                ):
                    old_dashboard_msg = m
                    break

            msgs = [msg async for msg in channel.history(limit=1)]
            last_message = msgs[0] if msgs else None

            if (
                old_dashboard_msg
                and last_message
                and last_message.id == old_dashboard_msg.id
            ):
                await old_dashboard_msg.edit(embed=embed)
            else:
                if old_dashboard_msg:
                    await old_dashboard_msg.delete()
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Queue Dashboard Error: {e}")

    @tasks.loop(minutes=1)
    async def match_refresher_task(self):
        """Refreshes Matcherino scores in active tickets every 1 minute."""
        await self.bot.wait_until_ready()

        m_id = await get_matcherino_id_from_active()
        if not m_id:
            return

        bracket_url = f"https://matcherino.com/tournaments/{m_id}/bracket"

        dashboard_channel = self.bot.get_channel(TOURNEY_SUPPORT_CHANNEL_ID)
        if not dashboard_channel:
            return
        guild = dashboard_channel.guild
        category = guild.get_channel(TOURNEY_CATEGORY_ID)

        if not category or not isinstance(category, discord.CategoryChannel):
            return

        for channel in category.channels:
            if (
                not isinstance(channel, discord.TextChannel)
                or "ticket-" not in channel.name
            ):
                continue
            if "👍" in channel.name or "❗" not in channel.name:
                continue

            match_num = None
            topic_team_name = None
            if channel.topic:
                match_res = re.search(r"bracket:(\d+)", channel.topic)
                if match_res:
                    try:
                        match_num = int(match_res.group(1))
                    except Exception:
                        continue
                team_res = re.search(r"team:(.*?)(?:\||$)", channel.topic)
                if team_res:
                    topic_team_name = team_res.group(1).strip() or None

            if match_num is None:
                if not topic_team_name:
                    continue
                loop = asyncio.get_event_loop()
                lookup = await loop.run_in_executor(
                    None, find_match_by_team_name, bracket_url, topic_team_name
                )
                if lookup.get("status") != "found":
                    continue
                match_num = lookup["match_number"]
                try:
                    updated_topic = re.sub(
                        r"bracket:[^|]*",
                        f"bracket:{match_num}",
                        channel.topic,
                    )
                    await channel.edit(topic=updated_topic)
                except Exception:
                    pass

            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None,
                functools.partial(
                    fetch_ticket_context,
                    bracket_url,
                    match_num,
                    topic_team_name=topic_team_name,
                ),
            )
            if data.get("status") != "success":
                if topic_team_name:
                    lookup = await loop.run_in_executor(
                        None, find_match_by_team_name, bracket_url, topic_team_name
                    )
                    if lookup.get("status") == "found":
                        match_num = lookup["match_number"]
                        data = await loop.run_in_executor(
                            None,
                            functools.partial(
                                fetch_ticket_context,
                                bracket_url,
                                match_num,
                                topic_team_name=topic_team_name,
                            ),
                        )
                        if data.get("status") == "success":
                            if channel.topic:
                                try:
                                    updated_topic = re.sub(
                                        r"bracket:[^|]*",
                                        f"bracket:{match_num}",
                                        channel.topic,
                                    )
                                    await channel.edit(topic=updated_topic)
                                except Exception:
                                    pass
                        else:
                            continue
                    else:
                        continue
                else:
                    continue

            now_ts = int(discord.utils.utcnow().timestamp())
            is_mismatch = data.get("team_name_mismatch", False)
            best_match_team = data.get("team_name_best_match")
            embed = discord.Embed(
                title=f"📊 Live Match Update: Match #{match_num}",
                description=f"**Last Update:** <t:{now_ts}:R>",
                color=discord.Color.red() if is_mismatch else discord.Color.gold(),
            )

            embed.add_field(
                name="Match Status",
                value=f"`{data['match_status'].upper()}`",
                inline=True,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            team_a, team_b = data["team_a"], data["team_b"]
            p_a = "\n".join([f"• {p}" for p in team_a["players"]]) or "• *No players*"
            p_b = "\n".join([f"• {p}" for p in team_b["players"]]) or "• *No players*"

            embed.add_field(
                name=f"🔵 {team_a['name']} ({team_a['score']})",
                value=f"**Roster:**\n{p_a}",
                inline=True,
            )
            embed.add_field(name="⚔️", value="\u200b", inline=True)
            embed.add_field(
                name=f"🔴 {team_b['name']} ({team_b['score']})",
                value=f"**Roster:**\n{p_b}",
                inline=True,
            )

            if is_mismatch and topic_team_name:
                lookup = find_match_by_team_name(bracket_url, topic_team_name)
                if lookup.get("status") == "found":
                    resolved_num = lookup["match_number"]
                    data = fetch_ticket_context(
                        bracket_url, resolved_num, topic_team_name=topic_team_name
                    )
                    if data.get("status") == "success":
                        match_num = resolved_num
                        is_mismatch = data.get("team_name_mismatch", False)
                        best_match_team = data.get("team_name_best_match")

                        if channel.topic:
                            try:
                                updated_topic = re.sub(
                                    r"bracket:[^|]*",
                                    f"bracket:{resolved_num}",
                                    channel.topic,
                                )
                                await channel.edit(topic=updated_topic)
                            except Exception:
                                pass

                        now_ts = int(discord.utils.utcnow().timestamp())
                        embed = discord.Embed(
                            title=f"📊 Live Match Update: Match #{resolved_num}",
                            description=f"**Last Update:** <t:{now_ts}:R>",
                            color=discord.Color.red()
                            if is_mismatch
                            else discord.Color.gold(),
                        )
                        embed.add_field(
                            name="Match Status",
                            value=f"`{data['match_status'].upper()}`",
                            inline=True,
                        )
                        embed.add_field(name="\u200b", value="\u200b", inline=True)
                        embed.add_field(name="\u200b", value="\u200b", inline=True)

                        team_a, team_b = data["team_a"], data["team_b"]
                        p_a = (
                            "\n".join([f"• {p}" for p in team_a["players"]])
                            or "• *No players*"
                        )
                        p_b = (
                            "\n".join([f"• {p}" for p in team_b["players"]])
                            or "• *No players*"
                        )
                        embed.add_field(
                            name=f"🔵 {team_a['name']} ({team_a['score']})",
                            value=f"**Roster:**\n{p_a}",
                            inline=True,
                        )
                        embed.add_field(name="⚔️", value="\u200b", inline=True)
                        embed.add_field(
                            name=f"🔴 {team_b['name']} ({team_b['score']})",
                            value=f"**Roster:**\n{p_b}",
                            inline=True,
                        )

            if is_mismatch:
                warning_text = "The team name in this ticket does not closely match either team in the bracket for this match."
                if topic_team_name:
                    warning_text += f"\nTeam entered: `{topic_team_name}`"
                warning_text += "\nUse `/set-ticket-match` to correct the match number or team name."

                embed.add_field(
                    name="⚠️ Team name / Match number Mismatch",
                    value=warning_text,
                    inline=False,
                )
            elif topic_team_name and best_match_team:
                embed.add_field(
                    name="Detected Team",
                    value=f"```\n{best_match_team}\n```",
                    inline=False,
                )

            embed.set_footer(text=f"Matcherino ID: {m_id}")

            try:
                old_info_msg = None
                async for msg in channel.history(limit=20):
                    if msg.author != self.bot.user or not msg.embeds:
                        continue
                    title = msg.embeds[0].title or ""
                    if (
                        "Matcherino Data" not in title
                        and "Live Match Update" not in title
                    ):
                        continue
                    title_match = re.search(r"Match #(\d+)", title)
                    if title_match and int(title_match.group(1)) == match_num:
                        old_info_msg = msg
                        break

                if old_info_msg:
                    if channel.last_message_id == old_info_msg.id:
                        await old_info_msg.edit(embed=embed)
                    else:
                        try:
                            await old_info_msg.delete()
                        except (discord.NotFound, discord.Forbidden):
                            pass
                        await channel.send(embed=embed)
                else:
                    await channel.send(embed=embed)

                await asyncio.sleep(1.5)

            except Exception as e:
                print(f"Refresher error in {channel.name}: {e}")

    @tasks.loop(minutes=5)
    async def progress_dashboard_task(self):
        """Refreshes the tournament progress dashboard every 5 minutes."""
        await self.bot.wait_until_ready()
        await self.update_progress_dashboard()


class BlacklistGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(
            name="blacklist", description="Manage tournament blacklisted users"
        )
        self.bot = bot

    @app_commands.command(name="add", description="Blacklist a user from tournaments.")
    @app_commands.describe(
        user="The user to blacklist",
        reason="Why are they being blacklisted?",
        matcherino="Link to their Matcherino profile (Optional)",
        alts="List of Alt User IDs or mentions (space separated) (Optional)",
    )
    async def blacklist_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
        matcherino: str = None,
        alts: str = None,
    ):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return

        alt_ids = []
        if alts:
            raw_ids = re.findall(r"\d+", alts)
            alt_ids = list(set(raw_ids))

        await add_blacklisted_user(
            user_id=str(user.id),
            reason=reason,
            admin_id=str(interaction.user.id),
            matcherino=matcherino,
            alts=alt_ids,
        )

        embed = discord.Embed(
            title="⛔ User Blacklisted", color=discord.Color.dark_red()
        )
        embed.add_field(
            name="User", value=f"{user.mention} (`{user.id}`)", inline=False
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        if matcherino:
            embed.add_field(name="Matcherino", value=matcherino, inline=False)
        if alt_ids:
            alt_mentions = ", ".join([f"<@{aid}>" for aid in alt_ids])
            embed.add_field(name="Registered Alts", value=alt_mentions, inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="remove", description="Remove a user from the blacklist."
    )
    async def blacklist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return

        existing = await get_blacklisted_user(str(user.id))
        if not existing:
            await interaction.response.send_message(
                f"⚠️ {user.mention} is not currently blacklisted.", ephemeral=True
            )
            return

        await remove_blacklisted_user(str(user.id))
        await interaction.response.send_message(
            f"✅ {user.mention} has been removed from the blacklist."
        )

    @app_commands.command(name="list", description="View all blacklisted users.")
    async def blacklist_list(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return

        users = await get_all_blacklisted_users()
        if not users:
            await interaction.response.send_message(
                "✅ No users are currently blacklisted.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⛔ Blacklisted Users", color=discord.Color.dark_red()
        )

        description_lines = []
        for doc in users:
            uid = doc["_id"]
            reason = doc.get("reason", "No reason provided")
            date_str = (
                doc.get("timestamp").strftime("%Y-%m-%d")
                if doc.get("timestamp")
                else "Unknown Date"
            )
            description_lines.append(
                f"• <@{uid}> (`{uid}`) — {date_str}\n  Reason: *{reason}*"
            )

        full_text = "\n\n".join(description_lines)
        if len(full_text) > 4000:
            full_text = full_text[:3990] + "... (list truncated)"

        embed.description = full_text
        await interaction.response.send_message(embed=embed)


def setup_tourney_commands(bot: commands.Bot):
    @bot.command(name="close", aliases=["c"])
    async def close_command(ctx: commands.Context):
        """Close a tourney ticket (staff only)."""
        active_session = await get_active_tourney_session()
        if active_session:
            await increment_staff_closure(
                active_session["_id"], ctx.author.id, ctx.author.name
            )
            await update_tourney_queue(active_session["_id"], change=-1)

        await close_ticket_via_command(ctx)

    @bot.command(name="delete", aliases=["del"])
    async def delete_command(ctx: commands.Context):
        """Delete a ticket (backup for button)."""
        await delete_ticket_via_command(ctx)

    @bot.command(name="reopen")
    async def reopen_command(ctx: commands.Context):
        """Reopen a closed tourney ticket channel."""
        if ctx.channel.category_id in (
            TOURNEY_CLOSED_CATEGORY_ID,
            PRE_TOURNEY_CLOSED_CATEGORY_ID,
        ):
            await reopen_ticket_via_command(ctx)
        else:
            await ctx.reply(
                "⚠️ This command can only be used in a closed tourney ticket channel."
            )

    @bot.command(name="starttourney")
    async def start_tourney_command(ctx: commands.Context):
        import features.config as config

        """Start a tourney session."""
        if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
            await ctx.reply("You don't have permission to start the tourney.")
            return

        guild = ctx.guild
        if not guild:
            return

        reset_ticket_counter()
        existing_session = await get_active_tourney_session()
        if not existing_session:
            await create_tourney_session()
        else:
            await reset_tourney_session_start_time(existing_session["_id"])

        # Ensure closed categories deny send_messages for @everyone
        # (R7 has this pre-configured on the server; we enforce it here)
        for closed_cat_id in (
            TOURNEY_CLOSED_CATEGORY_ID,
            PRE_TOURNEY_CLOSED_CATEGORY_ID,
        ):
            closed_cat = guild.get_channel(closed_cat_id)
            if isinstance(closed_cat, discord.CategoryChannel):
                await closed_cat.set_permissions(
                    guild.default_role,
                    send_messages=False,
                )

        # Update MAIN Tourney Support Channel
        main_channel = guild.get_channel(TOURNEY_SUPPORT_CHANNEL_ID)
        if isinstance(main_channel, discord.TextChannel):
            overwrites = main_channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            )

            for role_id in ALLOWED_STAFF_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )

            await main_channel.edit(overwrites=overwrites)
            await main_channel.purge()

            panel_text = (
                "Experiencing a match issue? We've got you covered.\n"
                "Use this if you're dealing with:\n\n"
                "⚠️ **No-show opponents**\n"
                "⚔️ **Score disputes**\n"
                "🛜 **Lobby / connection problems**\n"
                "📜 **Rule questions or clarifications**\n"
                "🔧 **Anything else blocking your match**\n\n"
                "Click the button below to open a **private support ticket**.\n\n"
                "You'll be prompted to provide:\n"
                "📛 **Team Name**\n"
                "🔢 **Match / Bracket Number**\n"
                "📝 **Description of the Issue**\n\n"
                "A Tourney Admin will assist you as soon as possible. 🛠️"
            )

            if config.TOURNEY_TEST_MODE:
                panel_text += "\n\n🧪 **TEST MODE ACTIVE**: Limits set to 100 tickets | 0.1s cooldown."

            embed = discord.Embed(
                title="🎟️ Tournament Support Ticket",
                description=panel_text,
                color=discord.Color.red()
                if config.TOURNEY_TEST_MODE
                else discord.Color.blurple(),
            )

            await main_channel.send(embed=embed, view=TourneyOpenTicketView())
            asyncio.create_task(main_channel.edit(name="「🔴」tourney-support"))
        else:
            await ctx.send(
                f"⚠️ Could not find Main Tourney Channel (ID: {TOURNEY_SUPPORT_CHANNEL_ID})"
            )

        # Update PRE-Tourney Support Channel — hide it
        pre_channel = guild.get_channel(PRE_TOURNEY_SUPPORT_CHANNEL_ID)
        if isinstance(pre_channel, discord.TextChannel):
            overwrites = pre_channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=False
            )

            for role_id in ALLOWED_STAFF_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )

            await pre_channel.edit(overwrites=overwrites)
            await pre_channel.purge()
            asyncio.create_task(
                pre_channel.edit(name="「❌❌❌」「🟡」pre-tourney-support")
            )
        else:
            await ctx.send(
                f"⚠️ Could not find Pre-Tourney Channel (ID: {PRE_TOURNEY_SUPPORT_CHANNEL_ID})"
            )

        # Delete ALL Pre-Tourney Tickets
        deleted_count = 0
        categories_to_check = [PRE_TOURNEY_CATEGORY_ID, PRE_TOURNEY_CLOSED_CATEGORY_ID]

        for cat_id in categories_to_check:
            pre_category = guild.get_channel(cat_id)
            if isinstance(pre_category, discord.CategoryChannel):
                for ch in pre_category.channels:
                    if (
                        isinstance(ch, discord.TextChannel)
                        and "ticket-" in ch.name
                        and ch.id != PRE_TOURNEY_SUPPORT_CHANNEL_ID
                    ):
                        try:
                            await delete_ticket_with_transcript(
                                guild, ch, ctx.author, bot
                            )
                            deleted_count += 1
                        except Exception as e:
                            print(f"Failed to delete pre-tourney ticket {ch.name}: {e}")

        await ctx.send(
            f"✅ Tourney Started! Channels updated and {deleted_count} pre-tourney tickets deleted."
        )

        # Grant Tourney Admin the Timeout Members permission
        tourney_admin_role = guild.get_role(TOURNEY_ADMIN_ROLE_ID)
        if tourney_admin_role:
            try:
                updated_perms = tourney_admin_role.permissions
                updated_perms.update(moderate_members=True)
                await tourney_admin_role.edit(permissions=updated_perms)
            except Exception as e:
                print(f"Failed to grant timeout permission to Tourney Admin role: {e}")

        # START THE DASHBOARD
        dashboard_cog = bot.get_cog("QueueDashboard")
        if dashboard_cog:
            await dashboard_cog.start_dashboard()

    @bot.command(name="endtourney")
    async def end_tourney_command(ctx: commands.Context):
        """End the tourney: close all tickets, generate stats, switch to pre-tourney mode."""
        if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
            await ctx.reply("You don't have permission to end the tourney.")
            return

        guild = ctx.guild
        if guild is None:
            return

        # Force one last high-stakes/winner announcement sync
        dashboard_cog = bot.get_cog("QueueDashboard")
        active_session_for_announcement = await get_active_tourney_session()
        endtourney_matcherino_id: str | None = None
        if (
            dashboard_cog
            and active_session_for_announcement
            and active_session_for_announcement.get("matcherino_id")
        ):
            try:
                endtourney_matcherino_id = active_session_for_announcement[
                    "matcherino_id"
                ]
                bracket_url = f"https://matcherino.com/tournaments/{endtourney_matcherino_id}/bracket"
                data = fetch_bracket_progress(bracket_url)
                if data.get("status") == "success":
                    await dashboard_cog.announce_high_stakes_matches(
                        endtourney_matcherino_id, data
                    )
            except Exception as e:
                print(f"!endtourney announcement sync error: {e}")

        if dashboard_cog:
            try:
                await dashboard_cog.update_progress_dashboard()
            except Exception as e:
                print(f"!endtourney final progress update error: {e}")

        winner_was_posted = (
            dashboard_cog is not None
            and dashboard_cog._winner_announcement_state.get("winner") is not None
        )

        if dashboard_cog:
            await dashboard_cog.stop_dashboard()

        if not winner_was_posted and endtourney_matcherino_id:
            await ctx.send(
                "⏳ Winner not yet available from Matcherino. Will retry in 5 minutes and post automatically."
            )

            async def _retry_winner_post():
                await asyncio.sleep(300)
                try:
                    retry_url = f"https://matcherino.com/tournaments/{endtourney_matcherino_id}/bracket"
                    retry_data = fetch_bracket_progress(retry_url)
                    winner = (
                        retry_data.get("winner_team")
                        if retry_data.get("status") == "success"
                        else None
                    )
                    if isinstance(winner, str):
                        winner = winner.strip()
                    if winner and winner.upper() not in {"UNKNOWN", "TBD", "BYE", ""}:
                        updates_channel = bot.get_channel(TOURNEY_UPDATES_CHANNEL_ID)
                        if updates_channel and isinstance(
                            updates_channel, discord.TextChannel
                        ):
                            content = f"# GGs!\n{winner} won !! {TOURNEY_MATCHERINO_WIN_EMOJI}"
                            await updates_channel.send(content)
                            print(f"[ENDTOURNEY RETRY] posted winner: {winner}")
                    else:
                        print(
                            "[ENDTOURNEY RETRY] winner still unavailable after 5-minute retry"
                        )
                except Exception as e:
                    print(f"[ENDTOURNEY RETRY] error: {e}")

            asyncio.create_task(_retry_winner_post())

        # Revoke Tourney Admin's Timeout Members permission
        tourney_admin_role = guild.get_role(TOURNEY_ADMIN_ROLE_ID)
        if tourney_admin_role:
            try:
                updated_perms = tourney_admin_role.permissions
                updated_perms.update(moderate_members=False)
                await tourney_admin_role.edit(permissions=updated_perms)
            except Exception as e:
                print(
                    f"Failed to revoke timeout permission from Tourney Admin role: {e}"
                )

        session = await get_active_tourney_session()
        if session:
            start_time = session["start_time"]
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=datetime.timezone.utc)

            duration = datetime.datetime.now(datetime.timezone.utc) - start_time
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)

            top_staff = await get_top_staff_stats(session["_id"], limit=12)
            staff_msg = ""
            for i, s in enumerate(top_staff):
                if i == 0:
                    icon = "🥇"
                elif i == 1:
                    icon = "🥈"
                elif i == 2:
                    icon = "🥉"
                else:
                    icon = f"**{i + 1}.**"
                staff_msg += (
                    f"{icon} **{s['username']}**: {s['tickets_closed']} tickets\n"
                )
            if not staff_msg:
                staff_msg = "No tickets closed."

            stat_embed = discord.Embed(
                title="📊 Tournament Report", color=discord.Color.gold()
            )
            stat_embed.add_field(
                name="⏱️ Duration", value=f"`{hours}h {minutes}m`", inline=True
            )
            stat_embed.add_field(
                name="📩 Total Tickets",
                value=f"`{session['total_tickets']}`",
                inline=True,
            )
            stat_embed.add_field(
                name="💬 Total Messages",
                value=f"`{session['total_messages']}`",
                inline=True,
            )
            stat_embed.add_field(
                name="📈 Peak Queue",
                value=f"**{session['peak_queue']}** tickets",
                inline=False,
            )
            stat_embed.add_field(
                name="🏆 Top Tourney Admins", value=staff_msg, inline=False
            )

            report_msg = await ctx.send(embed=stat_embed)
            try:
                await report_msg.pin()
            except Exception as e:
                print(f"⚠️ Could not pin report: {e}")

            await end_tourney_session(session["_id"])
            clear_bracket_teams_cache()

        # Update MAIN Tourney Support Channel — hide it
        main_channel = guild.get_channel(TOURNEY_SUPPORT_CHANNEL_ID)
        if isinstance(main_channel, discord.TextChannel):
            overwrites = main_channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=False
            )
            for role_id in ALLOWED_STAFF_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
            await main_channel.edit(overwrites=overwrites)
            await main_channel.purge()
            asyncio.create_task(
                main_channel.edit(name="「❌❌❌」「🔴」tourney-support")
            )

        # Update PRE-Tourney Support Channel — show it with panel
        pre_channel = guild.get_channel(PRE_TOURNEY_SUPPORT_CHANNEL_ID)
        if isinstance(pre_channel, discord.TextChannel):
            overwrites = pre_channel.overwrites
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            )
            for role_id in ALLOWED_STAFF_ROLES:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
            await pre_channel.edit(overwrites=overwrites)
            await pre_channel.purge()

            embed = discord.Embed(
                title="📩 Pre-Tournament Support",
                description=(
                    "Need help before the tournament starts? Use this for:\n\n"
                    "📋 **Registration Issues**\n"
                    "🤝 **Team / Roster Questions**\n"
                    "❓ **General Inquiries**\n\n"
                    "Click the button below to open a ticket. **Team Name** is optional."
                ),
                color=discord.Color.orange(),
            )
            await pre_channel.send(embed=embed, view=PreTourneyOpenTicketView())
            asyncio.create_task(pre_channel.edit(name="「🟡」pre-tourney-support"))

        # Delete ALL MAIN Tourney Tickets
        ticket_channels: list[discord.TextChannel] = []
        categories_to_check = [TOURNEY_CATEGORY_ID, TOURNEY_CLOSED_CATEGORY_ID]
        for cat_id in categories_to_check:
            cat = guild.get_channel(cat_id)
            if isinstance(cat, discord.CategoryChannel):
                for ch in cat.channels:
                    if (
                        isinstance(ch, discord.TextChannel)
                        and "ticket-" in ch.name
                        and ch.id != TOURNEY_SUPPORT_CHANNEL_ID
                    ):
                        ticket_channels.append(ch)

        if not ticket_channels:
            await ctx.reply("No tourney tickets found to delete.")
            return

        await ctx.reply(
            f"Ending tourney. Deleting {len(ticket_channels)} ticket(s) with transcripts..."
        )
        for ch in ticket_channels:
            try:
                await delete_ticket_with_transcript(
                    guild=guild, channel=ch, deleter=ctx.author, client=bot
                )
            except Exception as e:
                print(f"Error deleting ticket {ch.id} ({ch.name}): {e}")

    # =========================================================================
    #  SLASH COMMANDS
    # =========================================================================

    @app_commands.command(
        name="tourney-panel", description="Post the tourney support button."
    )
    async def tourney_panel(interaction: discord.Interaction):
        import features.config as config

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        panel_desc = (
            "Experiencing a match issue? We've got you covered.\n"
            "Use this if you're dealing with:\n\n"
            "⚠️ **No-show opponents**\n"
            "⚔️ **Score disputes**\n"
            "🛜 **Lobby / connection problems**\n"
            "📜 **Rule questions or clarifications**\n"
            "🔧 **Anything else blocking your match**\n\n"
            "Click the button below to open a **private support ticket**.\n\n"
            "You'll be prompted to provide:\n"
            "📛 **Team Name**\n"
            "🔢 **Match / Bracket Number**\n"
            "📝 **Description of the Issue**\n\n"
            "A Tourney Admin will assist you as soon as possible. 🛠️"
        )
        embed_color = discord.Color.blurple()
        if config.TOURNEY_TEST_MODE:
            panel_desc += "\n\n🧪 **TEST MODE ACTIVE**: Limits set to 100 tickets | 0.1s cooldown."
            embed_color = discord.Color.red()

        embed = discord.Embed(
            title="🎟️ Tournament Support Ticket",
            description=panel_desc,
            color=embed_color,
        )
        await interaction.response.send_message(
            embed=embed, view=TourneyOpenTicketView()
        )

    @app_commands.command(
        name="pre-tourney-panel", description="Post the Pre-Tourney support button."
    )
    async def pre_tourney_panel(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📩 Pre-Tournament Support",
            description=(
                "Need help before the tournament starts? Use this for:\n\n"
                "📋 **Registration Issues**\n"
                "🤝 **Team / Roster Questions**\n"
                "❓ **General Inquiries**\n\n"
                "Click the button below to open a ticket. **Team Name** is optional."
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(
            embed=embed, view=PreTourneyOpenTicketView()
        )

    @app_commands.command(name="add", description="Add a user to this tourney ticket.")
    async def add_to_ticket(interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member) or not is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                "You don't have permission to add users to tickets.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a ticket text channel.",
                ephemeral=True,
            )
            return

        valid_categories = {TOURNEY_CATEGORY_ID, PRE_TOURNEY_CATEGORY_ID}
        if channel.category_id not in valid_categories:
            await interaction.response.send_message(
                "This command can only be used inside a tourney ticket channel.",
                ephemeral=True,
            )
            return

        await channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            use_application_commands=True,
        )
        await interaction.response.send_message(
            f"✅ Added {user.mention} to this ticket.", ephemeral=True
        )
        await channel.send(
            f"{user.mention} has been added to this ticket by {interaction.user.mention}."
        )

    @app_commands.command(
        name="remove", description="Remove a user from this tourney ticket."
    )
    async def remove_from_ticket(
        interaction: discord.Interaction, user: discord.Member
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member) or not is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                "You don't have permission to remove users from tickets.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in a ticket text channel.",
                ephemeral=True,
            )
            return

        valid_categories = {TOURNEY_CATEGORY_ID, PRE_TOURNEY_CATEGORY_ID}
        if channel.category_id not in valid_categories:
            await interaction.response.send_message(
                "This command can only be used inside a tourney ticket channel.",
                ephemeral=True,
            )
            return

        await channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message(
            f"✅ Removed {user.mention} from this ticket.", ephemeral=True
        )
        await channel.send(
            f"{user.mention} has been removed from this ticket by {interaction.user.mention}."
        )

    @app_commands.command(
        name="hall-of-fame",
        description="Automatically fetch results and post to Hall of Fame.",
    )
    @app_commands.describe(tournament_id="The Matcherino ID (e.g. 183089)")
    async def hall_of_fame(interaction: discord.Interaction, tournament_id: str):
        if not isinstance(interaction.user, discord.Member) or not is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return

        target_channel = interaction.guild.get_channel(HALL_OF_FAME_CHANNEL_ID)
        if not target_channel or not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                f"❌ Could not find Hall of Fame channel (ID: {HALL_OF_FAME_CHANNEL_ID}).",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        clean_id = "".join(filter(str.isdigit, tournament_id))
        data = fetch_payout_report(clean_id)

        if "error" in data:
            await interaction.followup.send(
                f"❌ **Error:** {data['error']}", ephemeral=True
            )
            return

        tourney_name = data["tourney_name"]
        link = f"https://matcherino.com/tournaments/{clean_id}"
        total = data["total"]
        res = data["results"]

        embed = discord.Embed(
            title=f"🏆 {tourney_name}",
            url=link,
            description=(
                f"💰 **Total Prize:** ${total:.2f}\n\n"
                f"🥇 **{res['1st']}** — ${res['p1']:.2f} (50%)\n"
                f"🥈 **{res['2nd']}** — ${res['p2']:.2f} (30%)\n"
                f"🥉 **{res['3rd']}** — ${res['p3']:.2f} (12.5%)\n"
                f"4️⃣ **{res['4th']}** — ${res['p4']:.2f} (7.5%)"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Congratulations to the winners! 🎉")

        try:
            await target_channel.send(embed=embed)
            await interaction.followup.send(
                f"✅ Hall of Fame post sent to {target_channel.mention}!"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ I don't have permission to post in {target_channel.mention}.",
                ephemeral=True,
            )

    @app_commands.command(
        name="queue", description="Check your current position in the ticket line."
    )
    async def check_queue(interaction: discord.Interaction):
        channel = interaction.channel
        if (
            not isinstance(channel, discord.TextChannel)
            or "ticket-" not in channel.name
        ):
            await interaction.response.send_message(
                "❌ This command can only be used inside a ticket channel.",
                ephemeral=True,
            )
            return

        if channel.category_id == TOURNEY_CATEGORY_ID:
            cat = interaction.guild.get_channel(TOURNEY_CATEGORY_ID)
        elif channel.category_id == PRE_TOURNEY_CATEGORY_ID:
            cat = interaction.guild.get_channel(PRE_TOURNEY_CATEGORY_ID)
        else:
            await interaction.response.send_message(
                "❌ This ticket is not in an active queue.", ephemeral=True
            )
            return

        tickets = [
            c
            for c in cat.channels
            if isinstance(c, discord.TextChannel) and "ticket-" in c.name
        ]
        tickets.sort(key=lambda c: c.created_at)

        try:
            position = tickets.index(channel) + 1
            total = len(tickets)
        except ValueError:
            await interaction.response.send_message(
                "Could not determine position.", ephemeral=True
            )
            return

        if position == 1:
            status = "🟢 **NOW SERVING**"
            desc = f"You are **1/{total}** in the queue.\nA staff member should be with you momentarily!"
            color = discord.Color.green()
        else:
            status = "🟠 **WAITING**"
            desc = f"You are **{position}/{total}** in the queue.\nPlease wait for a staff member."
            color = discord.Color.orange()

        embed = discord.Embed(title="⏳ Queue Status", description=desc, color=color)
        embed.add_field(name="Current Status", value=status, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="tourney-admin-help",
        description="STAFF ONLY: Guide to Tournament Management commands.",
    )
    async def tourney_admin_help(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                "❌ Permission denied. This command is for Tournament Staff only.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🛠️ Tournament Admin Guide | {BOT_VERSION}",
            description="Welcome to the Tourney Staff portal. Here is your cheat sheet for managing tournaments and support tickets.",
            color=discord.Color.dark_theme(),
        )

        session_text = (
            "`!starttourney` - Wipes old tickets, posts the live panel, and starts the dashboard.\n"
            "`!endtourney` - Closes all active tickets, generates staff stats, and posts the Pre-Tourney panel.\n"
            "`/tourney-test-mode` - Toggle 100-ticket limit and 0.1s cooldown for testing."
        )
        embed.add_field(name="⚙️ Session Management", value=session_text, inline=False)

        ticket_text = (
            "`!close` (or `!c`) - Closes the current ticket and adds to your completed stats.\n"
            "`!delete` (or `!del`) - Deletes a ticket with transcript.\n"
            "`!reopen` - Moves a closed ticket back to the active category.\n"
            "`/add` / `/remove` - Add or remove a specific user to/from the current ticket."
        )
        embed.add_field(name="🎫 Ticket Control", value=ticket_text, inline=False)

        matcherino_text = (
            "`/set-matcherino` - Set the active Matcherino bracket ID for the session.\n"
            "`/match-info` - Show live rosters, scores, and match status for a match number.\n"
            "`/match-history` - Show a team's previous rounds for a given match.\n"
            "`/set-ticket-match` - Correct this ticket's match number or team name.\n"
            "`/tourney-progress` - Real-time bracket health check with stage announcements."
        )
        embed.add_field(
            name="📊 Live Bracket / Matcherino", value=matcherino_text, inline=False
        )

        mod_text = (
            "`/blacklist` `add/remove/list` - Manage users banned from participating.\n"
            "`/hall-of-fame <tourney_id>` - Uses the Matcherino Tourney ID to fetch the top 4 teams, calculates prize splits, and posts the results embed."
        )
        embed.add_field(name="⚖️ Moderation & Results", value=mod_text, inline=False)

        workflow_text = (
            "**1. Claiming:** When a user opens a ticket, read their submitted Team Name and Issue.\n"
            "**2. Assisting:** Request screenshot proof for no-shows or score disputes.\n"
            "**3. Matcherino:** Perform the necessary actions on the bracket on the Matcherino website.\n"
            "**4. Closing:** Once resolved, let the players know and type `!close` to archive."
        )
        embed.add_field(name="🔄 Support Workflow", value=workflow_text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.active_brackets = {}

    @app_commands.command(
        name="set-matcherino", description="STAFF ONLY: Set the active Matcherino ID."
    )
    @app_commands.describe(m_id="The numeric Matcherino ID (e.g., 180454)")
    async def set_matcherino(interaction: discord.Interaction, m_id: str):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return
        active_session = await get_active_tourney_session()
        if not active_session:
            await interaction.response.send_message(
                "❌ No active tourney session found. Start one first!", ephemeral=True
            )
            return
        clean_id = "".join(filter(str.isdigit, m_id))
        if not clean_id:
            await interaction.response.send_message(
                "❌ Please provide a numeric ID.", ephemeral=True
            )
            return
        await update_matcherino_id(active_session["_id"], clean_id)
        await interaction.response.send_message(
            f"✅ Active Matcherino ID set to: `{clean_id}`", ephemeral=True
        )

    @app_commands.command(
        name="tourney-test-mode",
        description="Toggle 100 tickets/0.1s cooldown for testing.",
    )
    @app_commands.describe(
        enabled="True to enable test mode, False to return to production."
    )
    async def tourney_test_mode(interaction: discord.Interaction, enabled: bool):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Staff permissions required.", ephemeral=True
            )
            return
        from features import config

        config.TOURNEY_TEST_MODE = enabled
        status = (
            "ENABLED 🧪 (100 tickets, 0.1s cooldown)"
            if enabled
            else "DISABLED ✅ (Production limits)"
        )
        await interaction.response.send_message(
            f"📢 Tournament Test Mode is now **{status}**."
        )

    @app_commands.command(
        name="match-info", description="Display roster for a specific match."
    )
    @app_commands.describe(match_num="The Match Number from the bracket (e.g. 189)")
    async def match_info(interaction: discord.Interaction, match_num: int):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Staff permissions required.", ephemeral=True
            )
            return
        session = await get_active_tourney_session()
        if not session or not session.get("matcherino_id"):
            await interaction.response.send_message(
                "❌ No active Matcherino ID set. Use `/set-matcherino` first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        m_id = session["matcherino_id"]
        bracket_url = f"https://matcherino.com/tournaments/{m_id}/bracket"
        topic_team_name = None
        if (
            isinstance(interaction.channel, discord.TextChannel)
            and interaction.channel.topic
        ):
            team_res = re.search(r"team:(.*?)(?:\||$)", interaction.channel.topic)
            if team_res:
                topic_team_name = team_res.group(1).strip() or None
        match_data = fetch_ticket_context(
            bracket_url, match_num, topic_team_name=topic_team_name
        )
        if match_data.get("status") != "success":
            await interaction.followup.send(f"❌ **Error:** {match_data.get('error')}")
            return
        is_mismatch = match_data.get("team_name_mismatch", False)
        best_match_team = match_data.get("team_name_best_match")
        embed = discord.Embed(
            title=f"📊 Matcherino Data: Match #{match_num}",
            color=discord.Color.red() if is_mismatch else discord.Color.gold(),
        )
        embed.add_field(
            name="Match Status",
            value=f"`{match_data['match_status'].upper()}`",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        team_a = match_data["team_a"]
        team_b = match_data["team_b"]
        players_a = (
            "\n".join([f"• {p}" for p in team_a["players"]]) or "• *No players found*"
        )
        players_b = (
            "\n".join([f"• {p}" for p in team_b["players"]]) or "• *No players found*"
        )
        embed.add_field(
            name=f"🔵 {team_a['name']} (Score: {team_a['score']})",
            value=f"**Matcherino Names:**\n{players_a}",
            inline=True,
        )
        embed.add_field(name="⚔️", value="\u200b", inline=True)
        embed.add_field(
            name=f"🔴 {team_b['name']} (Score: {team_b['score']})",
            value=f"**Matcherino Names:**\n{players_b}",
            inline=True,
        )
        if is_mismatch:
            warning_text = "The team name in this ticket does not closely match either team in the bracket for this match."
            if topic_team_name:
                warning_text += f"\nTeam entered: `{topic_team_name}`"
            warning_text += (
                "\nUse `/set-ticket-match` to correct the match number or team name."
            )
            embed.add_field(
                name="⚠️ Team name / Match number Mismatch",
                value=warning_text,
                inline=False,
            )
        elif topic_team_name and best_match_team:
            embed.add_field(
                name="Detected Team", value=f"```\n{best_match_team}\n```", inline=False
            )
        embed.set_footer(
            text=f"Matcherino ID: {m_id} | Tourney Admin: {interaction.user.name}"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="match-history",
        description="View the standardized tournament run of teams in a matchup.",
    )
    @app_commands.describe(
        match_num="The visual match number from the bracket (e.g. 189)"
    )
    async def match_history(interaction: discord.Interaction, match_num: int):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Staff permissions required.", ephemeral=True
            )
            return
        session = await get_active_tourney_session()
        if not session or not session.get("matcherino_id"):
            await interaction.response.send_message(
                "❌ No active Matcherino ID set. Use `/set-matcherino` first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        m_id = session["matcherino_id"]
        bracket_url = f"https://matcherino.com/tournaments/{m_id}/bracket"
        data = fetch_ticket_context(bracket_url, match_num)
        if data.get("status") != "success":
            await interaction.followup.send(f"❌ **Error:** {data.get('error')}")
            return
        embed = discord.Embed(
            title=f"📜 Match History: Match #{match_num}",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        team_a_name = data["team_a"]["name"]
        team_b_name = data["team_b"]["name"]
        hist_a = "\n".join(data.get("team_a_history", []))
        hist_b = "\n".join(data.get("team_b_history", []))
        embed.add_field(
            name=f"🔵 {team_a_name}",
            value=hist_a if hist_a else "*No previous matches (First Round)*",
            inline=False,
        )
        embed.add_field(
            name=f"🔴 {team_b_name}",
            value=hist_b if hist_b else "*No previous matches (First Round)*",
            inline=False,
        )
        embed.set_footer(text=f"Matcherino ID: {m_id}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="set-ticket-match",
        description="STAFF ONLY: Update match # or team name for this specific ticket.",
    )
    @app_commands.describe(
        match_num="The correct visual match number (e.g., 42)",
        team_name="The correct Matcherino team name for this ticket",
    )
    async def set_ticket_match(
        interaction: discord.Interaction, match_num: int = None, team_name: str = None
    ):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Staff permissions required.", ephemeral=True
            )
            return
        channel = interaction.channel
        if (
            not isinstance(channel, discord.TextChannel)
            or "ticket-" not in channel.name
        ):
            await interaction.response.send_message(
                "❌ This command must be used inside a ticket channel.", ephemeral=True
            )
            return
        if match_num is None and team_name is None:
            await interaction.response.send_message(
                "⚠️ Provide at least one field to update.", ephemeral=True
            )
            return
        await interaction.response.defer()
        topic = channel.topic or ""
        updates = []
        if match_num is not None:
            topic = (
                re.sub(r"bracket:[^|]+", f"bracket:{match_num}", topic)
                if "bracket:" in topic
                else f"{topic}|bracket:{match_num}"
            )
            updates.append(f"Match Number: **#{match_num}**")
        if team_name is not None:
            topic = (
                re.sub(r"team:[^|]+", f"team:{team_name}", topic)
                if "team:" in topic
                else f"{topic}|team:{team_name}"
            )
            updates.append(f"Team Name: **{team_name}**")
        try:
            edit_task = asyncio.create_task(
                channel.edit(
                    topic=topic, reason=f"Details updated by {interaction.user.name}"
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(edit_task), timeout=2.0)
            except asyncio.TimeoutError:
                edit_task.cancel()
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="🚫 Discord Rate Limit Hit",
                        description="Discord allows only **2 channel edits every 10 minutes**.\n\nThe bot has **cancelled** this update to avoid hanging for 10 minutes. Please wait a few minutes and try again.",
                        color=discord.Color.red(),
                    )
                )
                return
            update_list = "\n".join([f"✅ {item}" for item in updates])
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚙️ Ticket Details Adjusted",
                    description=f"Changes applied successfully:\n\n{update_list}\n\nThe live scoreboard will update in the next 1-minute cycle.",
                    color=discord.Color.green(),
                )
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to update channel: {e}")

    @app_commands.command(
        name="tourney-progress",
        description="STAFF ONLY: Real-time tournament health check.",
    )
    async def tourney_progress(interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Permission denied.", ephemeral=True
            )
            return
        await interaction.response.defer()
        session = await get_active_tourney_session()
        if not session or not session.get("matcherino_id"):
            await interaction.followup.send("❌ No active session found.")
            return
        m_id = session["matcherino_id"]
        bracket_url = f"https://matcherino.com/tournaments/{m_id}/bracket"
        data = fetch_bracket_progress(bracket_url)
        if data.get("status") != "success":
            await interaction.followup.send(f"❌ **Error:** {data.get('error')}")
            return
        start_time = session["start_time"]
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=datetime.timezone.utc)
        duration = discord.utils.utcnow() - start_time
        hours, mins = divmod(int(duration.total_seconds()), 3600)
        mins, _ = divmod(mins, 60)
        embed = discord.Embed(
            title="📊 Tournament Progress Report", color=discord.Color.gold()
        )
        embed.description = f"**⏱️ Total Duration:** `{hours}h {mins}m` | **📈 Completion:** `{data['completion_pct']}%` ({data['closed']}/{data['total']})"
        remaining_matches = max(0, data["total"] - data["closed"])
        tournament_complete = data["completion_pct"] >= 100 or remaining_matches == 0
        if tournament_complete:
            path_text = "🏆 **Tournament Over!**"
        else:
            rounds_left = max(0, data["max_round"] - data["dominant_round"])
            path_text = (
                f"{rounds_left} rounds remaining"
                if rounds_left > 0
                else "🏆 **Finals in progress!**"
            )
        active_matches_text = (
            "No matches remaining"
            if tournament_complete
            else f"{data['active_count']} Currently Playable"
        )
        embed.add_field(
            name="🏆 Bracket Status",
            value=f"• **Dominant Round:** Round {data['dominant_round']}\n• **Path to Finals:** {path_text}\n• **Active Matches:** {active_matches_text}",
            inline=False,
        )
        if data["bottlenecks"]:
            bn_text = ""
            for bn in data["bottlenecks"][:5]:
                bn_text += f"**#{bn['id']}** (Round {bn['round']}) | {bn['team_a']} vs {bn['team_b']} ({bn['score_a']}-{bn['score_b']})\n"
            embed.add_field(name="⚠️ Bottleneck Matches", value=bn_text, inline=False)
        else:
            embed.add_field(
                name="⚠️ Bottleneck Matches",
                value="✅ All playable matches are current with the dominant round.",
                inline=False,
            )
        embed.set_footer(text=f"Matcherino ID: {m_id} | Staff: {interaction.user.name}")
        await interaction.followup.send(embed=embed)

    # --- Register the QueueDashboard Cog ---
    if bot.get_cog("QueueDashboard") is None:
        asyncio.create_task(bot.add_cog(QueueDashboard(bot)))
        print("✅ Queue Dashboard task started.")
    else:
        print("ℹ️ QueueDashboard already loaded; skipping duplicate add.")

    # --- Register all slash commands ---
    bot.tree.add_command(tourney_panel)
    bot.tree.add_command(pre_tourney_panel)
    bot.tree.add_command(add_to_ticket)
    bot.tree.add_command(remove_from_ticket)
    bot.tree.add_command(hall_of_fame)
    bot.tree.add_command(check_queue)
    bot.tree.add_command(tourney_admin_help)
    bot.tree.add_command(set_matcherino)
    bot.tree.add_command(tourney_test_mode)
    bot.tree.add_command(match_info)
    bot.tree.add_command(match_history)
    bot.tree.add_command(set_ticket_match)
    bot.tree.add_command(tourney_progress)
    bot.tree.add_command(BlacklistGroup(bot))

    async def background_stats_update():
        try:
            active = await get_active_tourney_session()
            if active:
                await increment_tourney_message_count(active["_id"])
        except Exception:
            pass

    @bot.listen()
    async def on_message(message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        valid_categories = (TOURNEY_CATEGORY_ID, PRE_TOURNEY_CATEGORY_ID)
        if (
            "ticket-" in message.channel.name
            and message.channel.category_id in valid_categories
        ):
            asyncio.create_task(background_stats_update())


async def restore_tourney_panels(bot: commands.Bot):
    """On startup, repost any active support panels so buttons remain functional after a restart."""
    panels = [
        (
            TOURNEY_SUPPORT_CHANNEL_ID,
            "🎟️ Tournament Support Ticket",
            TourneyOpenTicketView,
        ),
        (
            PRE_TOURNEY_SUPPORT_CHANNEL_ID,
            "📩 Pre-Tournament Support",
            PreTourneyOpenTicketView,
        ),
    ]

    for channel_id, embed_title, ViewClass in panels:
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            async for message in channel.history(limit=10):
                if (
                    message.author == bot.user
                    and message.embeds
                    and message.embeds[0].title == embed_title
                ):
                    embed = message.embeds[0]
                    await message.delete()
                    await channel.send(embed=embed, view=ViewClass())
                    print(f"✅ Restored support panel in #{channel.name}")
                    break
        except Exception as e:
            print(f"⚠️ Could not restore panel in channel {channel_id}: {e}")
