import asyncio
import re
from collections import deque
from datetime import *
from itertools import product
from typing import Tuple, Union

import discord
import discord.ui as dui
from discord import app_commands
from discord.ext import commands
from more_itertools import value_chain
from numpy.lib import scimath
from sortedcontainers import SortedSet

from conflictsheet import CURRENT_SEASON
from groceryanalyzer import GROC_LIMIT
from groceryanalyzer import grocanalyze as ganalyze
from pg import *
from rentanalyzer import rentanalyze as ranalyze
from util import (
    NAME_ATTRGET,
    NONNEGATIVE_INT,
    POSITIVE_CENT,
    POSITIVE_INT,
    SEASON_RANGE,
    PGConverter,
    PGGroupConverter,
    csspoints,
    season_portion_str,
    send_long_mes,
)


class PGGroupAddView(dui.View):
    lock = asyncio.Lock()

    def __init__(self, groupLabel, pgs, pg_str, creator):
        super().__init__(timeout=scimath.logn(1.1, len(pgs)))
        self.group = groupLabel
        self.pgs = pgs
        self.pg_str = pg_str
        self.creator = creator

    @dui.button(emoji='🆗', style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        async with PGGroupAddView.lock:
            PG.partition_lookup[self.group] = self.group
            PG.partition_table[self.group] = frozenset(self.pgs)
        button.style = discord.ButtonStyle.success
        button.emoji = '✅'
        button.disabled = True
        await interaction.response.edit_message(content=f'`"{self.group}" mapped to {self.pg_str}.`', view=self)
        self.stop()

    async def interaction_check(self, interaction):
        return interaction.user == self.creator


class PGGroupFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    remove: Tuple[Union[PGConverter, PGGroupConverter], ...] = []


class TPIRCog(commands.Cog, name='TPIR'):
    """Commands related to The Price is Right in general. (See Lineup section for more)."""

    DEFAULT_PLINKO_VALS = [100, 500, 1000, 10000]
    cent_helper = {f'gp{i}': 'A price in dollars & cents. Will be rounded to nearest cent.' for i in range(1, 7)}

    def __init__(self, bot):
        self.bot = bot

        self._pluto_api_url_template = 'http://api.pluto.tv/v2/channels?start={}&stop={}'
        # self._topic = 'https://pluto.tv/live-tv/163 https://bit.ly/rokutpir | https://bit.ly/plutotpir | Current and upcoming eps on Pluto: '

        self.all_pg_str = ', '.join([str(pg) for pg in sorted(PG.partition_table['any game'], key=lambda p: p.sheetName)])
        self.all_pggroup_str = ', '.join([k for k in PG.partition_table.keys() if k != 'any game' and k != 'CAR_BOATABLE'])

    @commands.hybrid_group(invoke_without_command=True, case_insensitive=True)
    async def pg(self, ctx):
        """Commands related to basic info about Pricing Games."""
        if not ctx.invoked_subcommand:
            await ctx.send('Invalid subcommand (see `help pg`).')

    @pg.command(name='active', aliases=['ac'], with_app_command=False)
    async def pgactive(self, ctx, pgs: commands.Greedy[Union[PGConverter, PGGroupConverter]]):
        """Lists the active seasons of each PG given. PGGroups can be included as shorthand for multiple games.

        An active season is one the game has at least one playing in."""
        if not pgs:
            await ctx.send('`No PGs given. (Check your spelling from leftmost.)`', ephemeral=True)
        else:
            l = [f'{pg}: ' + season_portion_str(pg.activeSeasons) for pg in list(value_chain(*pgs))]
            if len(l) > 1:
                await send_long_mes(ctx, '\n'.join(l), newline_limit=14)
            else:
                await ctx.send(f'`{l[0]}`')

    @pg.command(name='group', aliases=['g'], with_app_command=False)
    async def pggroup(
        self, ctx, group: str.upper, pgs: commands.Greedy[Union[PGConverter, PGGroupConverter]], *, options: PGGroupFlags
    ):
        """If no PGs are given, fetch the mapping of the group name to the PGs it corresponds to.

        If PGs are given, propose a temporary group name (case-insensitive, no spaces) that maps to the given PGs as a PGGroup, for other commands. Other PGGroups can be used as shorthand for multiple PGs.

        If remove is specified, remove the PGs (or PGGroup as shorthand again) from the given resulting set of prior PGs to make the final proposed PGGroup.

        A confirmation button will be given with a variable-length timeout, depending upon the number of PGs in the proposed group. If confirmed, the group will be added. This group will be wiped whenever the bot restarts (usually once a day, in the middle of the night in USA).

        For more info on PGGroups, including the default mappings, see https://pastebin.com/FFEaQZHx

        PGs and PGGroups are all mapped to at least one single-word key. See the pastebin for a complete listing."""
        # The bot will react with an OK emoji when making a group. Confirm it is the mapping you wanted by reacting with the same OK emoji, and the bot will add the mapping and react with a checkmark. If you made a mistake, just let it go and after a few seconds it will react with an X emoji, and no mapping will be added.

        if re.search(r'\s+', group):
            await ctx.send('`Group names cannot contain spaces.`', ephemeral=True)
            return

        if pgs:
            if group in PG.partition_lookup:
                await ctx.send('`This name is already a PGGroup.`', ephemeral=True)
                return
            pgs = list(value_chain(*pgs))
            if len(pgs) == 1:
                await ctx.send("`There's no point to a PGGroup for a single PG.`", ephemeral=True)
            else:
                s = SortedSet(pgs, key=NAME_ATTRGET)
                assert len(s) == len(pgs)
                s -= frozenset(value_chain(*options.remove))

                if name := PG.partition_table.inverse.get(frozenset(s)):
                    await ctx.send(f'`This resulting set of PGs already exists as "{name}".`')
                else:
                    pg_str = ', '.join(str(pg) for pg in s)
                    v = PGGroupAddView(group, s, pg_str, ctx.message.author)
                    m = await ctx.send(f'`Map "{group}" to {pg_str}?`', view=v)
                    if await v.wait():
                        await m.edit(content=f'`Timeout. "{group}" not mapped.`', view=None)
        else:
            if group in PG.partition_lookup:
                g = PG.partition_lookup[group]
                s = ', '.join(str(pg) for pg in sorted(PG.partition_table[g], key=NAME_ATTRGET))
                await ctx.send(f'`{g}: {s}`')
            else:
                await ctx.send(f'`"{group.upper()}" is not a PGGroup.`', ephemeral=True)

    @pg.command(name='active_in', aliases=['activeIn', 'ai'])
    async def activeIn(self, ctx, start: SEASON_RANGE, end: Optional[SEASON_RANGE]):
        """Lists all games active in the given season(s)."""
        if end and start > end:
            await ctx.send('`Start season is more than end season.`', ephemeral=True)
            return

        portion = P.closed(start, end) if end else P.singleton(start)
        pg_str = ', '.join(str(pg) for pg in sorted(PG_WILDCARD, key=NAME_ATTRGET) if pg.activeIn(portion))
        await ctx.send(f'`{pg_str}`')

    @pg.command(name='abbr', aliases=['abbrs', 'ab'], with_app_command=False)
    async def pgAbbr(self, ctx, pgs: commands.Greedy[PGConverter]):
        """Lists the internal alternative keywords that can be used in other commands to identify the PGs given."""
        if not pgs:
            await ctx.send('`No PGs given. (Check your spelling from left-most.)`', ephemeral=True)
        else:
            l = [f'{pg.sheetName}: ' + ', '.join(pg.altNames) for pg in pgs]
            if len(l) > 1:
                await send_long_mes('\n'.join(l), newline_limit=14)
            else:
                await ctx.send(f'`{l[0]}`')

    @pg.command(name='groupAbbr', aliases=['groupAbbrs', 'ga'], with_app_command=False)
    async def groupAbbr(self, ctx, *, pggroups: str):
        """Lists the internal alternative keywords that can be used in other commands to identify the PGGroups given. Does not include custom PGGroups made with the pggroup command."""
        mes_vals = []
        for pggroup in value_chain(*re.split(r'\s+', pggroups)):
            master_val = PG.partition_lookup.get(pggroup)
            if master_val:
                mes_vals.append(', '.join(k for k, v in PG.partition_lookup.items() if v == master_val))
            else:
                mes_vals.append(f'Could not find "{pggroup}" as a valid keyword.')
        if len(mes_vals) > 1:
            await send_long_mes(ctx, '\n'.join(mes_vals), newline_limit=14)
        else:
            await ctx.send(f'`{mes_vals[0]}`')

    @pg.command(name='list', aliases=['l'])
    async def listPGs(self, ctx):
        """Lists all Pricing Games wayo.py has."""
        await ctx.send(f'`{self.all_pg_str}`', ephemeral=True)

    @pg.command(name='list_groups', aliases=['listGroups', 'lg'])
    async def listPGGroups(self, ctx):
        """Lists all default Pricing Games Groups wayo.py has."""
        await ctx.send(f'`{self.all_pggroup_str}`', ephemeral=True)

    @commands.hybrid_group(aliases=['analyze', 'ta'], case_insensitive=True)
    async def tpiranalyze(self, ctx):
        """Commands related to analyzing certain setups of Pricing Game playings."""
        if not ctx.invoked_subcommand:
            await ctx.send('Invalid subcommand (see `help tpiranalyze`).')

    @tpiranalyze.command()
    @app_commands.describe(amounts='Must be exactly four unique non-negative integers, like on the Plinko board.')
    async def plinko(
        self,
        ctx,
        chip_count: commands.Range[int, 1, 5],
        amounts: commands.Greedy[NONNEGATIVE_INT] = commands.parameter(
            default=lambda ctx: DEFAULT_PLINKO_VALS, displayed_default=str(tuple(DEFAULT_PLINKO_VALS))
        ),
    ):
        """Find all possible winning amounts for a Plinko playing with a certain chip count.

        If providing custom values, must be exactly four of them. Zero is automatically added as the 5th amount."""
        if len(set(amounts)) != 4:
            await ctx.send('`Exactly four unique, positive amounts for Plinko required.`', ephemeral=True)
            return

        plinko_vals = [0] + amounts
        combos = {
            sum(i * val for i, val in zip(plinko_vals, (a, b, c, d, chip_count - a - b - c - d)))
            for a in range(chip_count, -1, -1)
            for b in range(chip_count - a, -1, -1)
            for c in range(chip_count - a - b, -1, -1)
            for d in range(chip_count - a - b - c, -1, -1)
        }
        combo_str = ', '.join(f'{c}' for c in sorted(combos))

        await ctx.send(f'`{combo_str}`')

    @tpiranalyze.command(name='rent', aliases=['r'])
    @app_commands.describe(**cent_helper)
    async def rentanalyze(
        self,
        ctx,
        gp1: POSITIVE_CENT,
        gp2: POSITIVE_CENT,
        gp3: POSITIVE_CENT,
        gp4: POSITIVE_CENT,
        gp5: POSITIVE_CENT,
        gp6: POSITIVE_CENT,
    ):
        """Find all winning solutions for the six prices in a Pay the Rent setup.

        Stats are also given at the bottom: how many unique combinations are there (usually 180, can be a bit less), and then how many of those can win $1000 but not $5000, $5000 but not $10000, etc."""
        gps = [gp1, gp2, gp3, gp4, gp5, gp6]
        await ctx.send('```\n{}```'.format(ranalyze(gps)))

    del cent_helper['gp6']

    @tpiranalyze.command(name='grocery', aliases=['groc', 'g'])
    @app_commands.describe(
        min='Minimum total price to be a win. Default 20.00.',
        max='Minimum total price to be a win. Default 22.00.',
        **cent_helper,
    )
    async def groceryanalyze(
        self,
        ctx,
        gp1: POSITIVE_CENT,
        gp2: POSITIVE_CENT,
        gp3: POSITIVE_CENT,
        gp4: POSITIVE_CENT,
        gp5: POSITIVE_CENT,
        min: POSITIVE_CENT = 20.0,
        max: POSITIVE_CENT = 22.0,
    ):
        """Find all winning solutions for the five prices in a Grocery Game setup."""
        gps = [gp1, gp2, gp3, gp4, gp5]
        if not len(set(gps)) == len(gps):
            await ctx.send('`All GP prices must be unique.`', ephemeral=True)
            return
        elif max < min:
            await ctx.send('`The winning range must be at least a single price.`', ephemeral=True)
            return

        async with ctx.typing():
            sol_count, sol_str = ganalyze([round(a, 2) for a in gps], min_total=round(min, 2), max_total=round(max, 2))
            initial_str = (
                f'{sol_count} solutions: '
                if sol_count < GROC_LIMIT
                else f'Many, many solutions. Cutting off at {GROC_LIMIT}:'
            )
            await send_long_mes(ctx, f'{initial_str}\n\n{sol_str}')

    @tpiranalyze.command(name='balance84', aliases=['balance', 'b84', 'b'])
    @app_commands.describe(tolerance='The number of dollars each side must total within each other to "balance". Default 5.')
    async def balance84analyze(
        self,
        ctx,
        sp1: NONNEGATIVE_INT,
        sp2: NONNEGATIVE_INT,
        sp3: NONNEGATIVE_INT,
        sp4: NONNEGATIVE_INT,
        sp5: NONNEGATIVE_INT,
        tolerance: NONNEGATIVE_INT = 5,
    ):
        """Find all winning solutions for the five prices in a Balance Game '84 setup."""
        prices = [sp1, sp2, sp3, sp4, sp5]
        solutions = set()
        sol_strs = deque()
        for perm in product('LRU', repeat=5):
            if 'L' in perm and 'R' in perm:
                left = frozenset({p for loc, p in zip(perm, prices) if loc == 'L'})
                right = frozenset({p for loc, p in zip(perm, prices) if loc == 'R'})
                sol = frozenset({left, right})

                if not sol in solutions and -tolerance <= sum(left) - sum(right) <= tolerance:
                    solutions.add(sol)
                    sol_strs.append('+'.join([str(s) for s in left]) + ' ~= ' + '+'.join([str(s) for s in right]))

        if not solutions:
            sol_strs.append('No solutions.')
        else:
            s = 's' if len(solutions) != 1 else ''
            sol_strs.appendleft(f'{len(solutions)} solution{s}:\n')

        await send_long_mes(ctx, '\n'.join(sol_strs))

    async def cog_command_error(self, ctx, e):
        if isinstance(e, commands.RangeError):
            await ctx.send(f'`{e}`', ephemeral=True)
        elif isinstance(e, commands.BadArgument):
            await ctx.send(f'`{e}`', ephemeral=True)
        elif hasattr(e, 'original') and isinstance(e.original, AssertionError):
            await ctx.send('`All PGs must be unique.`', ephemeral=True)
        elif isinstance(e, commands.ConversionError) and isinstance(e.original, KeyError):
            await ctx.send(f'`The following is not a PG (or PGGroup): {e.original}`', ephemeral=True)
        else:
            await ctx.send(f'`{e}`', ephemeral=True)


async def setup(bot):
    p = TPIRCog(bot)
    await bot.add_cog(p)
