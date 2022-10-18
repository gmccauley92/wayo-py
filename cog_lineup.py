import asyncio
import itertools
import logging
import operator
import random
import string
from collections import OrderedDict
from copy import copy
from datetime import *
from functools import reduce
from typing import List, Literal, Optional, Union

import discord
import discord.ui as dui
import numpy as np
import polars as pl
import portion as P
from discord.ext import commands
from more_itertools import chunked, value_chain
from sortedcontainers import SortedSet

from conflictsheet import *
from dropboxwayo import dropboxwayo
from pg import *
from util import (
    CancelButton,
    PGConverter,
    PGGroupConverter,
    PGPlayingConverter,
    TimeConverter,
    build_flag_expr as has_any_flags,
    excel_date_str,
    logic_expression,
    parse_endpoints,
    parse_time_options,
    pretty_print_polars as ppp,
    season_portion_str,
    send_long_mes,
    PLAYING_FLAGS,
    NAME_ATTRGET,
    SCHEDULER_TZ,
    SORT_PROD,
    SEASON_RANGE,
    NONNEGATIVE_INT,
)

_log = logging.getLogger('wayo_log')


ORDINAL_SUFFIXES = {1: 'st', 2: 'nd', 3: 'rd', 4: 'th', 5: 'th', 6: 'th'}
PLAYING_FLAGS_SINGLE = ('C', 'T', '&', '*', '@', 'R', '$', '^', '?', 'M', '0')
PLAYING_FLAGS_SINGLE_CERTAIN = tuple([pfs for pfs in PLAYING_FLAGS_SINGLE if pfs not in '^?'])
PF_TO_DESCRIPTIVE = {pf: pff for pf, pff in zip(PLAYING_FLAGS_SINGLE, reversed(PLAYING_FLAGS))}
PF_TO_DESCRIPTIVE['0'] = 'no flag'
SLOTS_REGEX = r'^(?:([123456])(?!.*\1)){1,6}$'
HALF_REGEX = r'[12]'
FREQS_REGEX = r'^(?:([0123456])(?!.*\1)){1,7}$'
FLAGS_REGEX = r'^(?:([' + ''.join(PLAYING_FLAGS_SINGLE) + r'])(?!.*\1)){1,' + str(len(PLAYING_FLAGS_SINGLE)) + r'}$'
FLAGS_REGEX_5 = (
    r'^(?:([' + ''.join(PLAYING_FLAGS_SINGLE_CERTAIN) + r'])(?!.*\1)){1,' + str(len(PLAYING_FLAGS_SINGLE_CERTAIN)) + r'}$'
)
FLAGS_REGEX_3 = '[' + ''.join(PLAYING_FLAGS_SINGLE) + ']'
FLAGS_REGEX_2 = FLAGS_REGEX_3 + '{6}'
FLAGS_REGEX_4 = r'^([123456]):(.+)$'
CONFLICTN_REGEX = '(max|m)'

LINEUP_SEP = '\n\n' + (' '.join('~' * 22)) + '\n\n'
BY_SEASON_UNCERTAIN_LABELS = ['PG^', 'PG?']

ALL_PLINKO_FLAGS = frozenset({0} | {len(PLAYING_FLAGS) - PLAYING_FLAGS.index(f) for f in ('car', 'T', '$')})


def prodStr(s):
    if re.fullmatch('(\d{3}[12345][KDL]|\d{3}SP)', s, re.I):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid daytime/primetime production string.')


def dayProdStr(s):
    if re.fullmatch('(\d{3}[12345][KDL])', s, re.I):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid daytime production string.')


def slotsStr(s):
    if re.fullmatch(SLOTS_REGEX, s):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid slots string.')


def flag2str(s):
    if re.fullmatch('any', s, re.I):
        return s.lower()
    if re.fullmatch(FLAGS_REGEX_5, s, re.I):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid certain flags string.')


def flagStr(s):
    if re.fullmatch(FLAGS_REGEX_2, s, re.I):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid flags string.')


def flag3str(s):
    if re.fullmatch(FLAGS_REGEX_3, s, re.I):
        return s.upper()
    else:
        raise commands.BadArgument(f'{s} is not a valid flags string.')


def flag4str(s):
    if m := re.fullmatch(FLAGS_REGEX_4, s, re.I):
        pg_slot, reg = m.groups()
        if re.fullmatch(FLAGS_REGEX, reg, re.I):
            return int(pg_slot), reg.upper()
        elif re.fullmatch('any', reg, re.I):
            return int(pg_slot), reg.lower()
        else:
            raise commands.BadArgument(f'{s} is not a valid flags string.')
    else:
        raise commands.BadArgument(f'{s} is not a valid flags string.')


def dateStr(arg):
    if re.search('\d', arg) or '%' not in arg:
        raise commands.BadArgument(f'{s} is not a valid date string.')
    else:
        return arg


def freqStr(arg):
    if re.fullmatch('(\d+([ywd]|mo))+', arg, re.I):
        return arg.lower()
    else:
        raise commands.BadArgument(f'{s} is not a valid frequency string.')


def sortStr(arg):
    if re.fullmatch('([pd]|PROD|DATE)', arg, re.I):
        return 'prod' if arg[0] == 'p' else 'date'
    else:
        return commands.BadArgument(f'{s} is not a valid sort string.')


def conflictNint(arg):
    if re.fullmatch(CONFLICTN_REGEX, arg, re.I):
        return P.inf
    else:
        i = int(arg)
        if i < 0:
            raise commands.BadArgument('N must be non-negative.')
        return i


def trim_query(q: pl.LazyFrame, sortBy: str = 'prod', since: bool = False):
    if byDate := sortBy == 'date' and 'AIRDATE' in q.columns:
        q = q.sort('AIRDATE')
    if since:
        if byDate:
            q = q.select(
                [pl.col('^(PROD|S|AIRDATE)$'), pl.col('AIRDATE').diff().alias('SINCE'), pl.exclude('^(PROD|S|AIRDATE)$')]
            )
        else:
            q = q.select([pl.col('PG_n').diff().alias('SINCE'), pl.all()])
    return q.select(pl.exclude('^PG\d?_.+$')).collect()


def gen_lineup_submes(sub_df: pl.DataFrame, initial_str: str):
    q = sub_df.lazy()

    notes_check = sub_df.select(pl.all(pl.col('NOTES').is_null())).to_series()
    if notes_check.all():
        q = q.drop('NOTES')
    elif notes_check.any():
        q = q.with_column(pl.col('NOTES').fill_null(''))

    if 'AIRDATE' in sub_df.columns:
        if sub_df.select(pl.all(pl.col('INT. DATE') == pl.col('AIRDATE'))).to_series().all():
            q = q.select(pl.exclude('INT. DATE'))
        q = q.with_column(pl.col('^.+DATE$').dt.strftime('%b %d %Y').fill_null('NEVER AIRED'))

        half_hour_check = sub_df.select(pl.all(pl.col('PG6').is_null())).to_series()
        if half_hour_check.all():
            q = q.drop(['PG4', 'PG5', 'PG6'])
        elif half_hour_check.any():
            q = q.with_column(pl.col('^PG[4-6]$').fill_null(''))

    sub_df = q.collect()

    sub_df_str = ppp(sub_df) if sub_df.height else '' if initial_str else 'None'
    return f'{initial_str}\n\n{sub_df_str}' if initial_str else sub_df_str


class CSUpdateView(dui.View):
    def __init__(self, cc, prodNumber, retro, guild, scheduler, pgUpdate=True):
        super().__init__(timeout=3600.0)

        self.callback_func = cc.cs_update if pgUpdate else cc.cs_metaupdate
        self.timeout_func = cc.cs_cancel
        data = 'lineup' if pgUpdate else 'metadata'
        self.prodNumber = prodNumber
        self.update.label = (
            f'OVERWRITE {prodNumber} with the above {data}' if retro else f'ADD {prodNumber} with the above {data}'
        )
        self.dig_emoji = cc.bot.dig_emoji
        self.update.emoji = self.dig_emoji

        self.retro = retro
        self.scheduler = scheduler
        self.check_role = cc.conflict_role
        self.pgUpdate = pgUpdate
        self.job = None

        self.cancel = CancelButton(emoji=cc.bot.x_emoji, extra_callback=self.on_timeout)
        self.add_item(self.cancel)

    @dui.button(style=discord.ButtonStyle.primary)
    async def update(self, interaction, button):
        assert not self.job
        self.job = self.scheduler.add_job(
            self.callback_func, 'date', args=(self,), run_date=datetime.now(tz=SCHEDULER_TZ) + timedelta(seconds=1)
        )
        button.label = 'Processing...'
        button.emoji = '🚧'
        button.disabled = True
        self.remove_item(self.cancel)
        await interaction.response.edit_message(view=self)
        # up to callback_func to call finish

    async def on_timeout(self):
        await self.timeout_func(self)

    def finish(self):
        self.update.label = f'Overwrote {self.prodNumber}.' if self.retro else f'Added {self.prodNumber}.'
        self.update.emoji = '🖋️' if self.retro else self.dig_emoji
        self.update.style = discord.ButtonStyle.success
        self.stop()

    async def interaction_check(self, interaction):
        return True  # (
        # self.check_role in interaction.user.roles
        # if self.pgUpdate
        # else interaction.user.id in (149230437850415104, 688572894036754610)
        # )


class EditLineupFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    prodNumber: prodStr = commands.flag(aliases=['prod'])
    airdate: str = commands.flag(aliases=['air'], default=None)
    intended_date: str = commands.flag(aliases=['id', 'intent'], default=None)
    dateFormat: dateStr = commands.flag(aliases=['format'], default='%m/%d/%y')
    notes: str = None


class TimeFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    time: TimeConverter = commands.flag(aliases=['version'], default='daytime')
    start: Union[SEASON_RANGE, str] = commands.flag(
        default=lambda ctx: 1 if ctx.command.name in ('search', 'lineupRandom') else CURRENT_SEASON - 4
    )
    end: Union[SEASON_RANGE, str] = commands.flag(default=CURRENT_SEASON)
    dateFormat: dateStr = commands.flag(aliases=['format'], default='%m/%d/%y')


class ConflictSheetFlags(TimeFlags):
    prodNumber: prodStr = commands.flag(aliases=['prod', 'p'], default=None)
    pgFlags: flagStr = commands.flag(aliases=['flags', 'f'], default='000000')
    hideSheet: bool = commands.flag(aliases=['hide', 'h'], default=None)


class ConflictBaseFlags(TimeFlags):
    excludeEducated: bool = commands.flag(aliases=['exclude'], default=False)
    pgFlags: List[flag4str] = commands.flag(aliases=['flags', 'f'], default=None)


class ConflictNFlags(ConflictBaseFlags):
    N1: NONNEGATIVE_INT = 1
    N2: conflictNint = 'max'
    pgGroupCompare: PGGroupConverter = commands.flag(aliases=['pgGroup', 'compare'], default=None)


class ConflictFlags(ConflictBaseFlags):
    bySeason: NONNEGATIVE_INT = commands.flag(aliases=['by'], default=0)
    showLineup: bool = commands.flag(aliases=['show'], default=False)
    sortBy: sortStr = commands.flag(aliases=['sort'], default='prod')
    since: bool = False


class SlotFlags(TimeFlags):
    bySeason: NONNEGATIVE_INT = commands.flag(aliases=['by'], default=0)
    pgFlags: flag2str = commands.flag(aliases=['flag', 'f'], default=None)
    pgsOnly: bool = commands.flag(aliases=['pgOnly', 'only'], default=False)


class SearchFlags(TimeFlags):
    logicExpr: logic_expression = commands.flag(aliases=['logic'], default='all')
    conditions: List[str] = commands.flag(name='condition', aliases=['cond', 'c'])
    excludeUncertain: bool = commands.flag(aliases=['exclude'], default=False)
    sortBy: sortStr = commands.flag(aliases=['sort'], default='prod')
    since: bool = False


class MostPlayedFlags(TimeFlags):
    N: NONNEGATIVE_INT = 3
    excludeUncertain: bool = commands.flag(aliases=['exclude'], default=False)


class LastPlayedFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    sortBy: sortStr = commands.flag(aliases=['sort'], default='prod')
    asOf: dayProdStr = None
    pgFlag: flag3str = commands.flag(aliases=['flag', 'f'], default=None)
    activeOnly: bool = commands.flag(aliases=['active'], default=True)


class LineupRFlags(TimeFlags):
    sort: bool = True


class GenerateFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    N: commands.Range[int, 1, 5] = commands.flag(name='n', default=1)
    smart: bool = commands.flag(aliases=['s'], default=True)
    retired: bool = commands.flag(aliases=['r'], default=False)
    unique: bool = commands.flag(aliases=['u'], default=False)
    half_hour: bool = commands.flag(aliases=['halfHour', 'hh'], default=False)


class LineupCog(commands.Cog, name='TPIRLineups'):
    """Commands related to an internal database of all known Price is Right lineups."""

    def __init__(self, bot):
        self.cs = None
        self.latest_conflict = {}
        self.latest_meta = {}
        self.latest_lock = asyncio.Lock()
        self.bot = bot

    # bases

    @commands.hybrid_group(aliases=['l'], case_insensitive=True)
    async def lineup(self, ctx):
        """Commands specifically related to viewing and editing TPiR lineups."""
        if not ctx.invoked_subcommand:
            await ctx.send('Invalid subcommand (see `help lineup`).')

    @commands.hybrid_group(aliases=['playings', 'playing', 'play', 'p'], invoke_without_command=True, case_insensitive=True)
    async def played(self, ctx):
        """Commands specifically related to statistics of Pricing Game playings in TPiR lineups."""
        await ctx.send('Invalid subcommand (see `help played`).')

    # end bases

    @commands.Cog.listener()
    async def on_ready(self):
        self.guild = self.bot.get_guild(314598609591074816)
        self.conflict_role = self.guild.get_role(493259556655202304)
        # _log.info(f'cog_lineup on_ready: fetched conflict_role as {self.conflict_role}')

    async def cog_load(self):
        dropboxfn = '/heroku/wayo-py/'
        _log.info('start creating cs at ' + str(datetime.now()))
        self.cs = await asyncio.to_thread(
            ConflictSheet,
            lambda fn: io.BytesIO(dropboxwayo.download(dropboxfn + fn)),
            lambda iob, fn: dropboxwayo.upload(iob.read(), dropboxfn + fn),
        )
        _log.info('end creating cs at ' + str(datetime.now()))

    async def cog_check(self, ctx):
        return self.cs and self.cs.is_ready()

    async def cs_update(self, view):
        async with self.latest_lock:
            mes, prodNumber, pgps, retro = self.latest_conflict[view]
            _log.info('start updating & reloading cs at ' + str(datetime.now()))
            await asyncio.to_thread(
                self.cs.update, prodNumber, pgps, not retro, datetime.now(tz=SCHEDULER_TZ).date(), None, None
            )
            await asyncio.to_thread(self.cs.initialize)
            _log.info('end cs at ' + str(datetime.now()))
            del self.latest_conflict[view]
            view.finish()
            await mes.edit(view=view)
            await asyncio.to_thread(self.cs.save_excel)
            _log.info('end saving excel at ' + str(datetime.now()))

    @played.command(aliases=['conflictsheet', 'sheet', 'cs'], with_app_command=False)
    async def concurrencesheet(
        self,
        ctx,
        pg1: PGPlayingConverter,
        pg2: PGPlayingConverter,
        pg3: PGPlayingConverter,
        pg4: PGPlayingConverter,
        pg5: PGPlayingConverter,
        pg6: PGPlayingConverter,
        *,
        options: ConflictSheetFlags,
    ):
        """Generates a concurrence sheet (an overview of the full lineup's pair-concurrencies and slot info) for the provided pricing games. Note for slot info in daytime, numbers are adjusted to exclude any uncertain slotting, if applicable.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin.

        PGs are all mapped to at least one single-word key. See the pastebin for a complete listing.

        If prodNumber is given, and a certified user reacts to the bot-produced message with the dig emoji, this sheet (presumably from an actual Price episode) will be added or overwritten to the overall lineup data, based on how prodNumber ends (D/K for daytime, SP for primetime), with the given flags (such as non-car for a car, one-time rule change to a game, etc.). This will only have an effect in #daytime and #retrotime.

        If hideSheet is true when prodNumber is also given, the lineup is parroted back instead of a conflict sheet. This is meant for shorthand lineup overwrites when the actual sheet is not of interest. By default this is true in #retrotime."""

        try:
            ep, epText, _ = await parse_time_options(ctx, options)
        except ValueError:
            return

        pg_args = ctx.args[2:8]
        pgs = [pga[0] for pga in pg_args]
        assert len(set(pgs)) == len(pgs) and all(pgs)
        playing_names = [pga[1] for pga in pg_args]

        pgps = [
            PGPlaying(
                pn.title() if ' ' in pn else pg.sheetName, 2 ** PLAYING_FLAGS_SINGLE.index(f) if f != '0' else 0, pg=pg
            )
            for pg, pn, f in zip(pgs, playing_names, options.pgFlags)
        ]

        gs = self.cs.gen_sheet(pgps, ep, epText, options.time)
        mc = f'```\n{gs}```'

        if options.prodNumber:
            dfd = self.cs.get(options.time)
            try:
                row = dfd.select(pl.col('^(PROD|PG\d)$')).row(by_predicate=pl.col('PROD') == options.prodNumber)
                retro = True
            except pl.exceptions.RowsException:
                retro = False

            print(retro)
            print(ctx.channel.id)

            if retro and ctx.channel.id == 783063090852921344:  # actual: 783063090852921344 test: 814907236023271454
                if options.hideSheet == None:
                    options.hideSheet = True
            elif not retro and ctx.channel.id == 314598609591074816:  # actual: 314598609591074816 test: 281324492146343946
                pass
            else:
                await ctx.send(content=mc)
                return

            v = CSUpdateView(self, options.prodNumber, retro, ctx.guild, self.bot.SCHEDULER)

            # if self.latest_conflict:
            # 	mm,_,_,_,_ = self.latest_conflict[-1]
            # 	await mm.edit(view=None)

            pgps_strs = [str(pgp) for pgp in pgps]
            mc = f'Proposed {options.prodNumber} - ' + ', '.join(pgps_strs) if options.hideSheet else gs
            if retro:
                old_pgps_strs = row[1:]
                mc += (
                    ('`\n`' if options.hideSheet else '\n\n') + f'Current {options.prodNumber} - ' + ', '.join(old_pgps_strs)
                )
                if pgps_strs == old_pgps_strs:
                    mc += ('`\n`' if options.hideSheet else '\n\n') + 'This proposal has no changes.'
                    v = None

            m = await ctx.send(content=f'>>> `{mc}`' if options.hideSheet else f'```\n{mc}```', view=v)
            if v:
                self.latest_conflict[v] = (m, options.prodNumber, pgps, retro)
        else:
            await ctx.send(content=mc)

    async def cs_metaupdate(self, view):
        async with self.latest_lock:
            mes, prodNumber, ad, ind, notes = self.latest_meta[view]
            _log.info('META: start updating & reloading cs at ' + str(datetime.now()))
            await asyncio.to_thread(self.cs.update, prodNumber, None, False, ad, ind, notes)
            await asyncio.to_thread(self.cs.initialize)
            _log.info('META: end cs at ' + str(datetime.now()))
            del self.latest_meta[view]
            view.finish()
            await mes.edit(view=view)
            await asyncio.to_thread(self.cs.save_excel)
            _log.info('META: end saving excel at ' + str(datetime.now()))

    @lineup.command(name='edit', aliases=['e'], with_app_command=False)
    async def editLineup(self, ctx, *, options: EditLineupFlags):
        """Edit a lineup's date(s) and/or notes. Currently only Wayoshi and dftackett can confirm this command, but anyone can set it up.

        Notes should NOT be in quotes. Use "notes=empty" to specifying removing the notes for the episode."""

        assert options.intended_date or options.airdate or options.notes

        try:
            ind = datetime.strptime(options.intended_date, options.dateFormat) if options.intended_date else None
            ad = datetime.strptime(options.airdate, options.dateFormat) if options.airdate else None
        except ValueError as e:
            await ctx.send(f'`Malformed date: {e}`', ephemeral=True)
            return

        for time in ('daytime', 'primetime'):
            dfd = self.cs.get(time)
            try:
                _, cur_ad, cur_id, cur_notes = dfd.select(pl.col('^(PROD|.*DATE|NOTES|SPECIAL)$')).row(
                    by_predicate=pl.col('PROD') == options.prodNumber
                )

                if type(cur_notes) != str:
                    cur_notes = ''
                current = (
                    f'CURRENT {options.prodNumber}:\n\tAIRDATE: '
                    + excel_date_str(cur_ad)
                    + '\n\tINT. DATE: '
                    + excel_date_str(cur_id)
                    + f'\n\tNOTES: {cur_notes}'
                )
                break
            except pl.exceptions.RowsException:
                pass
        else:
            await ctx.send('`Production code must exist and be in daytime or primetime.`', ephemeral=True)
            return

        ind_str = excel_date_str(ind) if ind else 'Do not change'
        ad_str = excel_date_str(ad) if ad else 'Do not change'
        empty_notes = options.notes and options.notes.lower() == 'empty'
        notes_str = 'Do not change' if not options.notes else '' if empty_notes else options.notes
        input_str = f'PROPOSED CHANGES:\n\tAIRDATE: {ad_str}\n\tINT. DATE: {ind_str}\n\tNOTES: {notes_str}'

        v = CSUpdateView(self, options.prodNumber, True, ctx.guild, self.bot.SCHEDULER, pgUpdate=False)

        # if self.latest_meta:
        # 	mm = self.latest_meta[-1]
        # 	await mm.edit(view=None)

        m = await ctx.send(f'```{input_str}\n\n{current}```', view=v)
        self.latest_meta[v] = (m, options.prodNumber, ad, ind, '' if empty_notes else options.notes)

    async def cs_cancel(self, view):
        async with self.latest_lock:
            del (self.latest_conflict if view.pgUpdate else self.latest_meta)[view]

    @played.command(aliases=['conflict', 'c'], with_app_command=False)
    async def concurrence(
        self,
        ctx,
        pg1: PGConverter,
        pg2: PGConverter,
        pg3: Optional[PGConverter],
        pg4: Optional[PGConverter],
        pg5: Optional[PGConverter],
        pg6: Optional[PGConverter],
        *,
        options: ConflictFlags,
    ):
        """Fetches concurrence info for the provided pricing games (at least 2, up to 6). (The number of times played together in the same lineup.)

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin.

        PGs are all mapped to at least one single-word key. See the pastebin for a complete listing.

        See !flagshelp for more on pgFlags.

        The output is the number of times, within the given dataset, all the PGs have shown together in one lineup.

        If showLineups is True (see the pastebin for more info on how to specify True), also print out the complete lineup info for every matching entry. Can then further be sorted by production number or date ("sort" option), with the additional option to show the number of shows/days since the prior sorted entry ("since" option).

        If bySeason is non-zero, partition the output by the given number of ep (provided the given time has ep).

        For daytime, playings with the educated guess flag (?) can be optionally excluded."""

        pgs = list(itertools.takewhile(operator.truth, ctx.args[2:8]))
        pgs_set = set(pgs)
        assert len(pgs_set) == len(pgs)

        try:
            ep, epText, isDate = await parse_time_options(ctx, options, *pgs)
        except ValueError:
            return

        bySeasonBool = (
            not isDate
            and options.bySeason > 0
            and not ep.empty
            and options.bySeason < len(ep_list := list(P.iterate(ep, step=1)))
        )

        async with ctx.typing():
            flags = [None] * len(pgs)
            fs_str = [None] * len(pgs)
            if options.pgFlags:
                for pg_idx, fs in options.pgFlags:
                    assert pg_idx <= len(pgs), 'Flags given for non-existent PG.'
                    flags[pg_idx - 1] = frozenset(
                        {2**f for f in range(10)}
                        if fs == 'any'
                        else {0 if f.isnumeric() else 2 ** PLAYING_FLAGS_SINGLE.index(f) for f in fs}
                    )
                    fs_str[pg_idx - 1] = (
                        'any flag' if fs == 'any' else '/'.join([PF_TO_DESCRIPTIVE[f] if f else 'no flag' for f in fs])
                    )

            # determine pg_strs before exclude
            pgs_str = ', '.join(str(pg) + (f' ({fss})' if fss else '') for pg, fss in zip(pgs, fs_str))

            # now, exclude
            if options.excludeEducated:
                flags = [fl - {2**q for q in Q_FLAG} if fl else ALL_FLAGS_BUT_GUESS for fl in flags]

            sub_df = trim_query(
                self.cs.concurrence_query(ep, options.time, tuple(pgs), tuple(flags)), options.sortBy, options.since
            )

            ttl = sub_df.height

            if bySeasonBool and ttl:
                season_chunks = [ep.replace(lower=sc[0], upper=sc[-1]) for sc in chunked(ep_list, options.bySeason)]
                season_chunk_lists = [list(P.iterate(sc, step=1)) for sc in season_chunks]
                sub_df_groups = [sub_df.filter(pl.col('S').is_in(scl)) for scl in season_chunk_lists]
                freq_chunks = [sdg.height for sdg in sub_df_groups]

                initial_str = '{}, {}{}{}: {} | {}'.format(
                    pgs_str,
                    epText,
                    f' ({options.bySeason})' if options.bySeason else '',
                    ', no ? flag' if options.excludeEducated else '',
                    ', '.join(str(fc) for fc in freq_chunks),
                    ttl,
                )
                if options.showLineup:
                    total_str = []
                    for fc, sc, scl, sdg in zip(freq_chunks, season_chunks, season_chunk_lists, sub_df_groups):
                        sub_is = '{}, {}: {}'.format(pgs_str, season_portion_str(sc), fc)
                        if fc:
                            if options.bySeason == 1:
                                sdg = sdg.select(pl.exclude('S'))
                            total_str.append(gen_lineup_submes(sdg, sub_is))
                        else:
                            total_str.append(sub_is)
                    total_str = initial_str + LINEUP_SEP + LINEUP_SEP.join(total_str)
            else:
                initial_str = '{}, {}{}: {}'.format(pgs_str, epText, ', no ? flag' if options.excludeEducated else '', ttl)
                if options.showLineup and ttl:
                    total_str = gen_lineup_submes(
                        sub_df.select(pl.exclude('S')) if ep and options.start == options.end else sub_df, initial_str
                    )

        if options.showLineup and ttl:
            await send_long_mes(ctx, total_str)
        else:
            await ctx.send(f'`{initial_str}`')

    @played.command(aliases=['slot', 's'], with_app_command=False)
    async def slots(self, ctx, pgQueries: commands.Greedy[Union[PGConverter, PGGroupConverter]], *, options: SlotFlags):
        """Fetches full slot counts (with the given flag if provided) for each query.

        A pgQuery in this context is a valid single PG, or a PGGroup. A PGGroup is treated as the sum of all its underlying PGs.

        See !flagshelp for more on pgFlags. This flag must be a certain one (no ^ or ?) as the slots for such flags are by nature undefined.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin.

        If bySeason is non-zero, partition the output by the given number of seasons (provided the given time has seasons).

        If pgsOnly is true, PGGroups are instead treated as shorthand for multiple single PGs.

        PGs and PGGroups are all mapped to at least one single-word key. See the pastebin for a complete listing.
        """

        try:
            ep, epText, isDate = await parse_time_options(ctx, options)
        except ValueError:
            return

        bySeasonBool = (
            not isDate
            and options.bySeason > 0
            and not ep.empty
            and options.bySeason < len(ep_list := list(P.iterate(ep, step=1)))
        )

        if pgQueries:
            if options.pgsOnly:
                pgQueries = SortedSet(value_chain(*pgQueries), key=NAME_ATTRGET)
        else:
            await ctx.send(
                '`No valid PG or PGGroups given. (If giving multiple arguments, double-check the first one).`',
                ephemeral=True,
            )
            return

        async with ctx.typing():
            fs = []
            sep = LINEUP_SEP if bySeasonBool else '\n'

            if options.pgFlags:
                flags_str = (
                    ' ('
                    + (
                        'any flag'
                        if options.pgFlags == 'any'
                        else '/'.join([PF_TO_DESCRIPTIVE[f] if f else 'no flag' for f in options.pgFlags])
                    )
                    + ')'
                )
                options.pgFlags = (
                    {2**f for f in range(10)}
                    if options.pgFlags == 'any'
                    else {0 if f.isnumeric() else 2 ** PLAYING_FLAGS_SINGLE.index(f) for f in options.pgFlags}
                )
            else:
                flags_str = ''

            sub_slots_df = self.cs.endpoint_sub(
                ep, options.time, q=self.cs.slot_table(options.time, 'S' if options.time != 'primetime' else None)
            )
            if options.pgFlags:
                sub_slots_df = sub_slots_df.filter(pl.col('flag').is_in(list(options.pgFlags)))

            for pgQ in pgQueries:
                isPGGroup = not type(pgQ) == PG
                if isPGGroup:
                    qPG = frozenset(pgQ)
                    qPGName = PG.partition_table.inverse[qPG]
                else:
                    qPG = frozenset([pgQ])
                    qPGName = str(pgQ)

                if options.time in ('daytime', 'syndicated'):
                    if not isDate:
                        if isPGGroup:
                            if not any(pg.activeIn(ep) for pg in qPG):
                                fs.append(
                                    'No {} game {} active in {}.'.format(
                                        qPGName, 'is' if CURRENT_SEASON in ep else 'was', epText
                                    )
                                )
                                continue
                        else:
                            if not pgQ.activeIn(ep):
                                if pgQ.lastSeason < ep.lower:
                                    fs.append('{} was retired after S{}.'.format(pgQ, pgQ.lastSeason))
                                elif pgQ.firstSeason > ep.upper:
                                    fs.append('{} was not created until S{}.'.format(pgQ, pgQ.firstSeason))
                                else:
                                    fs.append(
                                        '{} {} inactive in {}.'.format(pgQ, 'is' if CURRENT_SEASON in ep else 'was', epText)
                                    )
                                continue

                    pg_ep, epText = parse_endpoints(
                        options.start,
                        options.end,
                        *qPG,
                        dateF=options.dateFormat,
                        syndicated=options.time == 'syndicated',
                        or_=True,
                    )
                    if options.time == 'syndicated':
                        epText = 'SYNDICATED ' + epText

                if bySeasonBool:
                    ep_list = list(P.iterate(pg_ep, step=1))
                    season_chunks = [pg_ep.replace(lower=sc[0], upper=sc[-1]) for sc in chunked(ep_list, options.bySeason)]
                    sc_strs = [season_portion_str(sc) for sc in season_chunks]

                    h = (
                        sub_slots_df.filter(
                            pl.col('PG').is_in([str(pg) for pg in pgQ]) if isPGGroup else pl.col('PG') == str(pgQ)
                        )
                        .select(pl.exclude('PG'))
                        .groupby([(pl.col('S').rank('dense') - 1) // options.bySeason, 'flag'])
                        .agg(pl.exclude('S').sum())
                    )
                    if not options.pgFlags:
                        sc_t = (SlotCertainty.SLOT, SlotCertainty.GAME)
                        h_unc = (
                            h.select(
                                [pl.col('S')]
                                + [
                                    pl.when(pl.col('flag') == sc)
                                    .then(pl.concat_list(pl.col('^PG\d$')).arr.sum())
                                    .otherwise(pl.lit(0))
                                    .alias(f'PG{f}')
                                    for sc, f in zip(sc_t, '^?')
                                ]
                            )
                            .groupby('S')
                            .agg(pl.all().sum())
                        )
                        h = (
                            h.filter(~has_any_flags('flag', frozenset(sc_t)))
                            .join(h_unc, on='S')
                            .groupby('S')
                            .agg(pl.exclude('flag').sum())
                            .sort('S')
                            .with_columns(
                                [
                                    pl.Series('S', sc_strs),
                                    pl.concat_list(pl.exclude('S')).arr.sum().alias('ALL'),
                                ]
                            )
                        )
                    h = (
                        pl.concat([h, h.select([pl.lit(epText).alias('S'), pl.exclude('S').sum()])])
                        .rename({'S': ''})
                        .collect()
                    )

                    # add some line spacing between total row/column
                    ss = ppp(h)
                    sss = ss.split('\n')
                    last_PG_label = h.columns[-2]
                    sss[0] = sss[0].replace(last_PG_label + ' ', last_PG_label + ' | ')
                    j = sss[0].rindex('|')
                    for i in range(1, len(sss)):
                        sss[i] = sss[i][: j - 1] + ' | ' + sss[i][j:]
                    extra_line = ''.join(itertools.repeat('-', len(sss[0])))
                    extra_line = extra_line[:j] + '|' + extra_line[j + 1 :]
                    sss.insert(len(sss) - 1, extra_line)
                    ssss = '\n'.join(sss)

                    fs.append(f'{qPGName}{flags_str}, {epText} ({options.bySeason}):\n\n{ssss}')
                else:
                    h = (
                        sub_slots_df.filter(
                            pl.col('PG').is_in([str(pg) for pg in pgQ]) if isPGGroup else pl.col('PG') == str(pgQ)
                        )
                        .select(pl.exclude('PG'))
                        .groupby('flag')
                        .agg(pl.col('^PG\d$').sum())
                        # .with_column(pl.concat_list(pl.exclude('flag')).arr.sum().alias('ALL'))
                        .collect()
                    )

                    ssd_pg_certain = h.filter(pl.col('flag') < SlotCertainty.SLOT).sum().row(0)[1:]
                    try:
                        ssd_pg_slot = sum(h.row(by_predicate=pl.col('flag') == SlotCertainty.SLOT)[1:])
                    except pl.exceptions.RowsException:
                        ssd_pg_slot = 0
                    try:
                        ssd_pg_game = sum(h.row(by_predicate=pl.col('flag') == SlotCertainty.GAME)[1:])
                    except pl.exceptions.RowsException:
                        ssd_pg_game = 0

                    ssd_sum = sum(ssd_pg_certain) + ssd_pg_slot + ssd_pg_game

                    if ssd_sum:
                        if options.time == 'daytime':
                            uncertain_str = (
                                ' | {}{}{}'.format(
                                    f'{ssd_pg_slot}^' if ssd_pg_slot else '',
                                    ', ' if ssd_pg_slot and ssd_pg_game else '',
                                    f'{ssd_pg_game}?' if ssd_pg_game else '',
                                )
                                if ssd_pg_slot or ssd_pg_game
                                else ''
                            )

                            fs.append(
                                '{}{}, {}: {}{} | {}'.format(
                                    qPGName,
                                    flags_str,
                                    epText,
                                    ', '.join(str(freq) for freq in ssd_pg_certain),
                                    uncertain_str,
                                    ssd_sum,
                                )
                            )
                        else:
                            fs.append(
                                '{}{}, {}: {} | {}'.format(
                                    qPGName, flags_str, epText, ', '.join(str(freq) for freq in ssd_pg), ssd_sum
                                )
                            )
                    else:
                        fs.append(
                            '{}{} {} in {}.'.format(
                                qPGName,
                                flags_str,
                                'has not been played yet'
                                if (
                                    not isDate
                                    and (
                                        (options.time == 'daytime' and CURRENT_SEASON in pg_ep)
                                        or (
                                            options.time in ('daytime', 'primetime')
                                            and not any(pg.activeIn(pg_ep) for pg in qPG)
                                        )
                                    )
                                )
                                else 'was not played',
                                epText if options.time in ('daytime', 'syndicated') else options.time.upper(),
                            )
                        )

        if len(pgQueries) == 1 and not bySeasonBool:
            await ctx.send('`' + sep.join(fs) + '`')
        else:
            await send_long_mes(ctx, sep.join(fs))

    @played.command(name='most', aliases=['m'], with_app_command=False)
    async def mostPlayed(
        self, ctx, slots: slotsStr, pgGroups: commands.Greedy[PGGroupConverter], *, options: MostPlayedFlags
    ):
        """Fetches the N-most playings, out of all PGs (or only those PGs in the PGGroup(s), if given), in the slots given.

        Slots can be any combination of '123456'. The output will do the N-most calculation for each slot individually as well as the sum of the given slots.

        A pgQuery in this context is a valid single PG, or a PGGroup. PGGroups are treated as shorthand for multiple single PGs.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin."""

        try:
            ep, epText, _ = await parse_time_options(ctx, options)
        except ValueError:
            return

        # not sure why copy is needed? reset_index?
        sub_slots_df = self.cs.endpoint_sub(
            ep,
            options.time,
            q=self.cs.slot_table(
                options.time, 'S' if options.time != 'primetime' else None
            ),  # 'S' if options.time != 'primetime' else
        )

        pgQueries = list(value_chain(*pgGroups)) or list(PG)

        if options.N > len(pgQueries):
            options.N = len(pgQueries)

        filt_exprs = []
        if pgGroups:
            filt_exprs.append(pl.col('PG').is_in([str(pg) for pg in pgQueries]))
        if options.excludeUncertain:
            filt_exprs.append(~has_any_flags('flag', frozenset({2**qu for qu in QU_FLAGS})))
        if filt_exprs:
            sub_slots_df = sub_slots_df.filter(pl.all(filt_exprs))

        ddf = sub_slots_df.collect()

        if ddf.height:
            q = ddf.lazy()
            result = []

            for slot in slots:
                ser = (
                    q.groupby('PG')
                    .agg(pl.exclude('flag').sum())
                    .select(pl.col(f'^PG{slot}?$').sort_by(f'PG{slot}', True).head(options.N))
                    .collect()
                )
                result.append(
                    slot
                    + ORDINAL_SUFFIXES[int(slot)]
                    + ':'
                    + (' ' if options.N == 1 else '\n')
                    + ('\n'.join(f'\t{pg} ({freq})' for pg, freq in ser.rows()))
                )
            if len(slots) > 1:
                ser = (
                    q.groupby('PG')
                    .agg(pl.exclude('flag').sum())
                    .select([pl.col('PG'), pl.fold(pl.lit(0), operator.add, pl.col(f'^PG[{slots}]$')).alias('sum')])
                    .select(pl.all().sort_by('sum', True).head(options.N))
                    .collect()
                )
                result.append('\nALL:\n' + ('\n'.join(f'\t{pg} ({freq})' for pg, freq in ser.rows())))

            pg_str = (
                'only {} games'.format(', '.join([PG.partition_table.inverse[frozenset(pgG)] for pgG in pgGroups]))
                if pgGroups
                else 'all PGs'
            )
            await send_long_mes(ctx, '{}, top {}, {}:\n\n{}'.format(epText, options.N, pg_str, '\n'.join(result)))
        else:
            await ctx.send(f'`None of the PG(s) given have been / were played in {epText}.`')

    @played.command(aliases=['conflictN', 'cN'], with_app_command=False)
    async def concurrenceN(
        self,
        ctx,
        pg1: PGConverter,
        pg2: Optional[PGConverter],
        pg3: Optional[PGConverter],
        pg4: Optional[PGConverter],
        pg5: Optional[PGConverter],
        *,
        options: ConflictNFlags,
    ):
        """Lists every game (if any) given in pgGroupCompare (if not specified, every other game) that has between played between N1 and N2 entries (inclusive), or exactly N1 entries if only N1 is provided, with the provided pricing game(s) (with the provided flag(s) if given), within the given season range (e.g. inactive games are excluded from the listing).

        N2 must be more than N1 if provided. "Max" or "m" can be provided for N2 to automatically be set to the total number of playings of the provided pricing game. N1 must be non-negative.

        See !flagshelp for more on pgFlags.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin.

        For daytime, playings with the educated guess flag (?) can be optionally excluded.

        PGs are all mapped to at least one single-word key. See the pastebin for a complete listing."""

        pgs = list(itertools.takewhile(operator.truth, ctx.args[2:7]))
        assert len(set(pgs)) == len(pgs)

        N1 = options.N1
        N2 = options.N2
        if type(N2) == str:
            N2 = P.inf

        if N1 < 0 or (N2 and N1 > N2):
            await ctx.send('`Invalid N parameters. N1 must be >= 0, N2 must be >= N1 if provided.`', ephemeral=True)
            return

        try:
            ep, epText, _ = await parse_time_options(ctx, options, *pgs)
        except ValueError:
            return

        async with ctx.typing():
            flags = [None] * len(pgs)
            fs_str = [None] * len(pgs)
            if options.pgFlags:
                for pg_idx, fs in options.pgFlags:
                    assert pg_idx <= len(pgs), 'Flags given for non-existent PG.'
                    flags[pg_idx - 1] = frozenset(
                        [2**f for f in range(10)]
                        if fs == 'any'
                        else [0 if f.isnumeric() else 2 ** PLAYING_FLAGS_SINGLE.index(f) for f in fs]
                    )
                    fs_str[pg_idx - 1] = (
                        'any flag' if fs == 'any' else '/'.join([PF_TO_DESCRIPTIVE[f] if f else 'no flag' for f in fs])
                    )

            # determine pg_strs before exclude
            pgs_str = ', '.join(str(pg) + (f' ({fss})' if fss else '') for pg, fss in zip(pgs, fs_str))

            # now, exclude
            if options.excludeEducated:
                flags = [fl - {2**q for q in Q_FLAG} if fl else ALL_FLAGS_BUT_GUESS for fl in flags]

            sub_df = self.cs.concurrence_query(ep, options.time, tuple(pgs), tuple(flags)).collect()
            total_playings = sub_df.height

            # all-Dinko check
            try:
                check = sub_df.row(by_predicate=pl.col('PROD') == '6435K')
                all_dinko = True
            except pl.exceptions.RowsException:
                all_dinko = False

            sub_df = (
                sub_df
                .select(pl.concat_list('^PG\d_p$').alias('PG').explode().value_counts(True, True))
                .unnest('PG')
                .filter(~pl.col('PG').is_in([str(pg) for pg in pgs]))
            )

            if options.pgGroupCompare:
                sub_df = sub_df.filter(pl.col('PG').is_in([str(pg2) for pg2 in options.pgGroupCompare if pg2.activeIn(ep)]))

            if N1 > total_playings:
                N1 = total_playings
            if N2 and N2 > total_playings:
                N2 = total_playings
            if N1 == N2:
                N2 = None

            # all-Dinko show handling. will be true only when exactly only Dinko in PGs and S42 included.
            if all_dinko and (not (fs := flags[0]) or (fs & ALL_PLINKO_FLAGS)):
                val = 5  # 6 - len(pgs)

                sub_df = sub_df.vstack(
                    pl.DataFrame([pl.Series('PG', ['Plinko']), pl.Series('counts', [val], dtype=pl.UInt32)])
                    # unfortunately forced to resort here
                ).sort('counts', True)

                if N1 == val and not N2:
                    pass
                elif not N2 and N1 != val:
                    if N1 > val:
                        N2 = N1
                        N1 = val
                    else:
                        N2 = val
                elif N1 > val:
                    N1 = val
                elif N2 < val:
                    N2 = val

            if not total_playings:
                pgGroupStr = (
                    ' with {} games'.format(PG.partition_table.inverse[frozenset(options.pgGroupCompare)])
                    if options.pgGroupCompare
                    else ''
                )
                if len(pgs) == 1:
                    await ctx.send(
                        '`{} {} played in {} ({}){}.`'.format(
                            pgs_str,
                            'has not been' if CURRENT_SEASON in ep and not pgs[0].retired else 'was not',
                            options.time if options.time != 'daytime' else 'this time period',
                            epText,
                            pgGroupStr,
                        )
                    )
                else:
                    await ctx.send(
                        '`{} have no concurrences in {} ({}){}.`'.format(
                            pgs_str,
                            options.time if options.time != 'daytime' else 'this time period',
                            epText,
                            pgGroupStr,
                        )
                    )
                return

            if not N1:
                result0 = filter(
                    lambda pg2: pg2 != PG._UNKNOWN if options.time != 'daytime' else pg2.activeIn(ep),
                    (options.pgGroupCompare or set(list(PG))) - {PG.lookup(s) for s in sub_df.to_series()},
                )

            pgGroupStr = (
                ' (only {} games)'.format(PG.partition_table.inverse[frozenset(options.pgGroupCompare)])
                if options.pgGroupCompare
                else ''
            )
            cr_str = 'playing' if len(pgs) == 1 else 'concurrence'

            if N2:
                r_text = []
                sdf = sub_df.filter(pl.col('counts').is_between(N1, N2, True))

                for sdfg in sdf.groupby('counts'):
                    N = sdfg[0, 1]
                    r_text.append(f'{N}: ' + ', '.join(sorted(sdfg.to_series())))
                if not N1:
                    if zero_str := ', '.join([str(pg2) for pg2 in sorted(result0, key=NAME_ATTRGET)]):
                        r_text.append(f'0: {zero_str}')

                initial_str = '{}, {}, {}{}, out of {} {}{}:'.format(
                    pgs_str,
                    epText,
                    f'{N1} <= N <= {N2}' + pgGroupStr,
                    ', no ? flag' if options.excludeEducated else '',
                    total_playings,
                    cr_str,
                    's' if total_playings != 1 else '',
                )
                total_str = initial_str + '\n\n' + ('\n'.join(r_text) if r_text else 'None')

                await send_long_mes(ctx, total_str)
            else:
                if N1:
                    r_text = (', '.join(sub_df.filter(pl.col('counts') == N1).to_series())) or 'None'
                else:
                    r_text = (', '.join([str(pg2) for pg2 in sorted(result0, key=NAME_ATTRGET)])) or 'None'
                await ctx.send(
                    '`{}, {}, {}{}, out of {} {}{}: {}`'.format(
                        pgs_str,
                        epText,
                        f'N = {N1}' + pgGroupStr,
                        ', no ? flag' if options.excludeEducated else '',
                        total_playings,
                        cr_str,
                        's' if total_playings != 1 else '',
                        r_text,
                    )
                )

    @lineup.command(name='prod', aliases=['p', 'production'])
    async def lineupProd(self, ctx, *, production_numbers):
        """Lists every lineup given in a space-separated input of any desired length.

        For valid production code patterns, see the pastebin.
        """
        sent_any = False
        prods = re.split(r'\s+', production_numbers)

        for time in ('daytime', 'primetime', 'syndicated', 'unaired'):
            sub_df = trim_query(self.cs.get(time).lazy().filter(pl.col('PROD').is_in(prods)))
            if sub_df.height:
                await send_long_mes(ctx, gen_lineup_submes(sub_df, ''))
                sent_any = True

        if not sent_any:
            await ctx.send('`No existing production codes given.`', ephemeral=True)

    @lineup.command(name='date', aliases=['d'], with_app_command=False)
    async def lineupDate(
        self, ctx, time: Optional[TimeConverter] = 'daytime', dateFormat: Optional[dateStr] = '%m/%d/%y', *, dates
    ):
        """Lists every lineup that aired on any of the given date(s). Only applies to daytime and primetime.

        Date formatting by default, for example, is "03/26/20" (leading zeros optional)."""
        dates = re.split(r'\s+', dates)
        try:
            dts = [(datetime.strptime(d, dateFormat) - datetime(1970, 1, 1)).days for d in dates]
        except ValueError as e:
            await ctx.send(f'`Malformed date: {e}`', ephemeral=True)
            return

        df = self.cs.get(time)
        sub_df = trim_query(df.lazy().filter(pl.col('AIRDATE').dt.epoch('d').is_in(dts)))
        if sub_df.height:
            await send_long_mes(ctx, gen_lineup_submes(sub_df, ''))
        else:
            await ctx.send(f'`No lineups in {time} for any of these dates.`')

    @lineup.command(name='prod_range', aliases=['prodRange', 'pr', 'productionRange'])
    async def lineupProdRange(self, ctx, start: str.upper, end: str.upper, time: Optional[TimeConverter] = 'daytime'):
        """Lists every lineup in a range of production numbers from start to end, inclusive, in the given time.

        For valid production code patterns, see the pastebin.
        """
        sub_df = self.cs.get(time)
        n_idx = sub_df.columns.index('PG_n')

        try:
            start_idx = sub_df.row(by_predicate=pl.col('PROD') == start)[n_idx]
        except pl.exceptions.RowsException as e:
            await ctx.send(f'`Invalid production code for {time}: {start}`', ephemeral=True)
            return
        try:
            end_idx = sub_df.row(by_predicate=pl.col('PROD') == end)[n_idx]
        except pl.exceptions.RowsException as e:
            await ctx.send(f'`Invalid production code for {time}: {end}`', ephemeral=True)
            return

        if start_idx < end_idx:
            await send_long_mes(
                ctx, gen_lineup_submes(trim_query(sub_df.lazy().slice(start_idx - 1, end_idx - start_idx + 1)), '')
            )
        else:
            await ctx.send(
                '`No production codes within that range. Are you sure start < end? Are you sure the time is right?`',
                ephemeral=True,
            )

    @lineup.command(name='dateRange', aliases=['dr'], with_app_command=False)
    async def lineupDateRange(
        self,
        ctx,
        start: str,
        end: str,
        step: Optional[freqStr] = '1d',
        time: Optional[TimeConverter] = 'daytime',
        dateFormat: Optional[dateStr] = '%m/%d/%y',
    ):
        """Lists every lineup that aired within the range of start to end, inclusive, stepped by step. Only applies to daytime and primetime.

        Step can be any combination of number of days, weeks, months or years (in any order, e.g. "2mo1y3w4d"). Dates with no shows will automatically be excluded from the listing.

        Date formatting by default, for example, is "03/26/20" (leading zeros optional)."""
        try:
            startDate = datetime.strptime(start, dateFormat).date()
            endDate = date.today() if re.fullmatch('today', end, re.I) else datetime.strptime(end, dateFormat).date()
        except ValueError as e:
            await ctx.send(f'`Malformed input: {e}`', ephemeral=True)
            return

        dts = pl.date_range(startDate, endDate, step)

        df = self.cs.get(time)
        sub_df = trim_query(df.lazy().filter(pl.col('AIRDATE').is_in(dts)))
        if sub_df.height:
            await send_long_mes(ctx, gen_lineup_submes(sub_df, ''))
        else:
            await ctx.send(f'`No lineups in {time} for any of these dates.`')

    @lineup.command(name='random', aliases=['r'], with_app_command=False)
    async def lineupRandom(self, ctx, N: Optional[NONNEGATIVE_INT] = 1, *, options: LineupRFlags):
        """Randomly picks and prints out N lineups from the dataset. If sort is False, random print order as well.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin."""

        try:
            ep, epText, isDate = await parse_time_options(ctx, options)
        except ValueError:
            return

        df = self.cs.endpoint_sub(ep, options.time).collect()

        if options.sort:
            comp = operator.lt
            extra_str = ' (and randomizing order of)'
        else:
            comp = operator.le
            extra_str = ''

        if comp(N, df.height):
            sub_df = trim_query(df.sample(n=N, with_replacement=False, shuffle=not options.sort).lazy())
            await send_long_mes(ctx, gen_lineup_submes(sub_df, ''))
        else:
            await ctx.send(
                f'`No point to picking{extra_str} {N} shows out of a sample size of {df.height}.`', ephemeral=True
            )

    @lineup.command(aliases=['s'], with_app_command=False)
    async def search(self, ctx, *, options: SearchFlags):
        """Lists every lineup that matches a set of conditions. If 'all' is given to logicExpr (short for logical expression), all the conditions must match. If 'any' is given, at least one condition must match. Custom logic expressions are also allowed, see the pastebin for more details.

        Each condition is one to four "words", separated by commas or semicolons. See the pastebin for exact formatting details.

        The dataset used is determined by the time, start and end parameters. For more on these, see the pastebin.

        PGs and PGGroups are all mapped to at least one single-word key. See the pastebin for a complete listing.

        If excludeUncertain is True, it's a shorthand for specifying every flag except ^ and ? in every condition as the default.

        The resulting lineups can be sorted by production number or date ("sort" option), with the additional option to show the number of shows/days since the prior sorted entry ("since" option).

        A regular expression can be used for the "notes" parameter to search that column of the data. It will be case insensitive. See pastebin for more on this.
        """

        try:
            ep, epText, isDate = await parse_time_options(ctx, options)
        except ValueError:
            return

        overallCond = []
        warning_strs = []
        slot_queried = False
        warned_slot = False
        warned_game = False
        DEFAULT_FLAGS = ALL_FLAGS_BUT_UNCERTAIN if options.excludeUncertain else ANY_FLAG
        noteRegex = None

        for cond in options.conditions:
            m = re.fullmatch('n(?:otes)?[,;](.+?)', cond)
            if m:
                if noteRegex:
                    await ctx.send(f'`More than one notes regex specified in conditions.`', ephemeral=True)
                    return
                noteRegex = m.group(1)
                continue

            workingPGs = None
            workingSlots = ANY_SLOT
            workingFlags = DEFAULT_FLAGS
            workingFreqs = ANY_FREQ
            for pgCond in re.split(r'[,;]+', cond):
                if (
                    pg := PG.lookup_table.get(pgCond) or PG.partition_table.get(PG.partition_lookup.get(pgCond))
                ) or re.match('^[-*]+$', pgCond):
                    if workingPGs:
                        await ctx.send(f'`More than one PG/PGGroup in condition "{cond}".`', ephemeral=True)
                        return
                    workingPGs = (pg if type(pg) == frozenset else (pg,)) if pg else PG_WILDCARD
                elif pgCond[0].lower() == 's' and re.match(SLOTS_REGEX, pgc := pgCond[1:]):
                    if workingSlots != ANY_SLOT:
                        await ctx.send(f'`More than one slot specification in condition "{cond}".`', ephemeral=True)
                        return
                    workingSlots = frozenset(int(i) for i in pgc)
                elif pgCond[0].lower() == 'h' and re.match(HALF_REGEX, pgc := pgCond[1:]):
                    if workingSlots != ANY_SLOT:
                        await ctx.send(f'`More than one slot specification in condition "{cond}".`', ephemeral=True)
                        return
                    workingSlots = FIRST_HALF if pgCond[1] == '1' else SECOND_HALF
                elif pgCond[0].lower() == 'c' and re.match(FREQS_REGEX, pgc := pgCond[1:]):
                    if workingFreqs != ANY_FREQ:
                        await ctx.send(f'`More than one count specification in condition "{cond}".`', ephemeral=True)
                        return
                    workingFreqs = frozenset(int(i) for i in pgc)
                elif pgCond[0].lower() == 'f' and re.match(FLAGS_REGEX, pgcu := pgCond[1:].upper()):
                    if workingFlags != DEFAULT_FLAGS:
                        await ctx.send(f'`More than one flag specification in condition "{cond}".`', ephemeral=True)
                        return
                    workingFlags = tuple(0 if i.isnumeric() else 2 ** PLAYING_FLAGS_SINGLE.index(i) for i in pgcu)
                else:
                    await ctx.send(f"`Malformed condition string: '{pgCond}'`", ephemeral=True)
                    return

            if not workingPGs:
                await ctx.send(f'`No valid PG or wildcard specified in condition "{cond}"`', ephemeral=True)
                return
            if workingSlots != ANY_SLOT:
                if PG._UNKNOWN in workingPGs:
                    warning_strs.append('DISCLAIMER: Missing games have uncertain slots by definition.')
                elif SlotCertainty.SLOT in workingFlags:
                    warning_strs.append('DISCLAIMER: Slotting of uncertainly slotted playings specified in a condition.')
                    warned_slot = True
                else:
                    slot_queried = True
            if SlotCertainty.GAME in workingFlags:
                warning_strs.append(
                    'DISCLAIMER: Uncertain playing flag specified in a condition. Playings marked with the ? flag belong to a lineup that is, at worst, close to the given production number.'
                )
                warned_game = True

            overallCond.append((workingPGs, workingSlots, workingFlags, workingFreqs))

        if len(overallCond) > 26:
            await ctx.send(
                "`More than 26 conditions in a custom logical expression. You don't need anywhere near this many, stop trying to break me! If you really need this many, consider using PGGroups to whittle down the expression count.",
                ephemeral=True,
            )
            return
        elif (sym_free := len(set(re.findall('[A-Z]', options.logicExpr)))) and sym_free != len(overallCond):
            await ctx.send(
                f'`Logical expression mismatch. Expecting {len(overallCond)} variables, got {sym_free} instead in "{options.logicExpr}"`',
                ephemeral=True,
            )
            return

        async with ctx.typing():
            sub_df = trim_query(
                self.cs.lineup_query(ep, options.time, options.logicExpr, tuple(overallCond)), options.sortBy, options.since
            )

            if noteRegex:
                sub_df = sub_df.filter(
                    pl.col('NOTES' if options.time == 'daytime' else 'SPECIAL')
                    .cast(str)
                    .str.to_uppercase()
                    .str.contains(noteRegex.upper())
                )

            all_full_hour = not (options.time == 'syndicated' or sub_df.select(pl.any(pl.col('PG6').is_null()))[0, 0])

            # this could be done all in one line, but it would be a gigantic, messy line.
            condition_strs = []
            for pgs, slots, flags, freqs in overallCond:
                slots_l = sorted(slots)
                slots_str = 'played ' + (
                    'in any slot'
                    if slots == ANY_SLOT
                    else 'in the first half'
                    if all_full_hour and slots == FIRST_HALF
                    else 'in the second half'
                    if all_full_hour and slots == SECOND_HALF
                    else (
                        str(slots_l[0]) + ORDINAL_SUFFIXES[slots_l[0]]
                        if len(slots) == 1
                        else (
                            ', '.join(str(s) + ORDINAL_SUFFIXES[s] for s in slots_l[:-1])
                            + (',' if len(slots) != 2 else '')
                            + ' or '
                            + str(slots_l[-1])
                            + ORDINAL_SUFFIXES[slots_l[-1]]
                        )
                    )
                )

                flags_str = (
                    ''
                    if flags == ANY_FLAG
                    else 'with no guess flags'
                    if flags == ALL_FLAGS_BUT_UNCERTAIN
                    else (
                        f'with the "{PLAYING_FLAGS_SINGLE[int(np.log2(flags[0]))]}" flag'
                        if len(flags) == 1
                        else 'with at least one of the "'
                        + ''.join(PLAYING_FLAGS_SINGLE[int(np.log2(j))] if j else '0' for j in flags)
                        + '" flags'
                    )
                )

                freqs_str = '' if freqs == ANY_FREQ else ' (x' + ','.join(str(i) for i in sorted(freqs)) + '),'

                pg_str = (
                    str(pgs[0])
                    if len(pgs) == 1
                    else (
                        ('any ' + pti + ('' if pti.endswith('PRIZER') else ' game'))
                        if (pti := PG.partition_table.inverse.get(pgs)) != 'any game'
                        else pti
                    )
                )

                condition_strs.append(pg_str + freqs_str + ' ' + slots_str + (', ' + flags_str if flags_str else ''))

            final_cond_str = (
                ''
                if (len(overallCond) == 1 or noteRegex and not overallCond) and not sym_free
                else (f'\n{options.logicExpr} ; where\n\n' if sym_free else f'{options.logicExpr} of\n')
            ) + '\n'.join(
                [
                    (l + ' = ' if sym_free else ('* ' if len(overallCond) > 1 else '')) + cs
                    for l, cs in zip(string.ascii_uppercase, condition_strs)
                ]
            )
            if noteRegex:
                if not final_cond_str:
                    final_cond_str += f'Notes matching the regular expresion "{noteRegex}" (case-insensitive)'
                else:
                    final_cond_str += f'\n\nand with notes matching the regular expression "{noteRegex}" (case-insensitive)'

            initial_str = '{} lineup{} found {} {} for {}'.format(
                sub_df.height,
                '' if sub_df.height == 1 else 's',
                'from' if isDate else 'in',
                epText.replace(' - ', ' to '),
                final_cond_str,
            )

            if options.start == options.end:
                sub_df = sub_df.select(pl.exclude('S'))

            main_mes = gen_lineup_submes(
                sub_df,
                initial_str,
            )

            if slot_queried and not warned_slot and '(^)' in main_mes:
                warning_strs.append('DISCLAIMER: Slotting of uncertainly slotted playings factored into results.')
            if not warned_game and '(?)' in main_mes:
                warning_strs.append(
                    'DISCLAIMER: Playings marked with the ? flag belong to a lineup that is, at worst, close to the given production number.'
                )

        await send_long_mes(ctx, '\n'.join(warning_strs) + ('\n\n' if warning_strs else '') + main_mes)

    @played.command(name='last', aliases=['l'], with_app_command=False)
    async def lastPlayed(
        self,
        ctx,
        pgs: commands.Greedy[Union[PGConverter, PGGroupConverter, dayProdStr]],
        nth: Optional[NONNEGATIVE_INT] = 1,
        *,
        options: LastPlayedFlags,
    ):
        """Lists the n-th to last playing in daytime (production number and airdate) of the given PGs, with the playing flag if given. PGGroups and (daytime-only) lineup codes can be included as shorthand for multiple games.

        If asOf is given (a daytime-only production number). start searching backwards from that lineup.

        If pgFlag is provided, must be exactly one character (see !flags for more info).

        Results are listed most recent first. If sortBy is date, sort by date instead.

        If you try to go back too far on a game, it will cap at the game's premiere."""

        try:
            unfound_pgs = set()
            for q in pgs:
                if type(q) is str:
                    unfound_pgs |= {
                        PG.lookup(p)
                        for p in self.cs.get('daytime')
                        .select(pl.col('^(PROD|PG\d_p)$'))
                        .row(by_predicate=pl.col('PROD') == q)[1:]
                        if p
                    }
                elif type(q) is PG:
                    unfound_pgs.add(q)
                else:
                    unfound_pgs |= q

            if options.asOf:
                cutoff = self.cs.get('daytime').select(pl.col('PROD')).row(by_predicate=pl.col('PROD') == options.asOf)[0]
        except pl.exceptions.RowsException:
            await ctx.send(
                '(One of) the daytime codes given to `pgs` or `asOf`, despite being properly formatted, does not exist. Some codes do get skipped.',
                ephemeral=True,
            )
            return

        if options.activeOnly:
            unfound_pgs &= PG.partition_table['ACTIVE']
        if not unfound_pgs:
            await ctx.send('`No PGs given. (If doing retired games only, set activeOnly to False.)`', ephemeral=True)
            return

        seasons = P.closed(1, CURRENT_SEASON) & reduce(operator.or_, [pg.activeSeasons for pg in unfound_pgs])

        if options.pgFlag:
            extra_str = (
                PLAYING_FLAGS[len(PLAYING_FLAGS) - 1 - PLAYING_FLAGS_SINGLE.index(options.pgFlag)]
                if options.pgFlag != '0'
                else 'no flag'
            )
            options.pgFlag = 2 ** PLAYING_FLAGS_SINGLE.index(options.pgFlag) if options.pgFlag != '0' else 0
            fs = frozenset((options.pgFlag,))
        else:
            fs = ANY_FLAG

        unfound_pgs = {str(pg) for pg in unfound_pgs}

        async with ctx.typing():
            results = []
            for pg in unfound_pgs:
                q = self.cs.lineup_query(seasons, 'daytime', 'any', ((frozenset({pg}), ANY_SLOT, fs, ANY_FREQ),))
                if options.asOf:
                    q = q.filter(pl.col('PG_n') <= cutoff)

                q = q.select(pl.col('^(PROD|AIRDATE)$')).tail(nth).collect()
                if q.height:
                    prod, airdate = q.row(0)
                    results.append((pg, prod, airdate))

            if results:
                results.sort(key=lambda t: SORT_PROD(t[1]) if options.sortBy == 'prod' else t[2], reverse=True)
                extra = f' ({extra_str})' if options.pgFlag is not None else ''
                if options.sortBy == 'prod':
                    result_strs = [f'{pg}{extra}: {ind}, ' + ts.strftime('%b %d %Y') for pg, ind, ts in results]
                else:
                    result_strs = [f'{pg}{extra}: ' + ts.strftime('%b %d %Y') + f', {ind}' for pg, ind, ts in results]

                if len(result_strs) > 1:
                    await send_long_mes(ctx, '\n'.join(result_strs), newline_limit=14)
                else:
                    await ctx.send(f'`{result_strs[0]}`')
            else:
                await ctx.send(
                    f'`No results found. (Check the flag, nth, activeOnly options if you were expecting results.)`'
                )

    @played.command(aliases=['proj', 'p'], with_app_command=False)
    async def projected(self, ctx, pgs: commands.Greedy[Union[PGConverter, PGGroupConverter, dayProdStr]]):
        """Does a simple pro-rated calculation projecting the number of playings for the PG this season, then if possible, compares that projection to this completed calculation for last season. PGGroups, or a (daytime-only) lineup code, can be included as shorthand for multiple games.

        Each game must be active in the current season to show as output in this command.

        This command makes the most sense to run in the second half of an ongoing season, or over summer break."""

        try:
            qs = []
            for q in pgs:
                if type(q) is str:
                    qs.extend(
                        [
                            PG.lookup(p)
                            for p in self.cs.get('daytime')
                            .select(pl.col('^(PROD|PG\d_p)$'))
                            .row(by_predicate=pl.col('PROD') == q)[1:]
                            if p
                        ]
                    )
                elif type(q) is PG:
                    qs.append(q)
                else:
                    qs.extend(q)
        except pl.exceptions.RowsException:
            await ctx.send(
                '(One of) the daytime codes given to `pgs` or `asOf`, despite being properly formatted, does not exist. Some codes do get skipped.',
                ephemeral=True,
            )
            return

        # this is the recommended way to remove duplicates while keeping order (no set)
        pgs = list(OrderedDict.fromkeys(filter(lambda p: p.activeIn(CURRENT_SEASON), qs)))

        if not pgs:
            await ctx.send(
                '`No PGs given. (Check the PG is active in S{}-{}, and check your spelling from leftmost.)`'.format(
                    CURRENT_SEASON - 1, CURRENT_SEASON
                ),
                ephemeral=True,
            )
        else:
            pgs = [str(pg) for pg in pgs]
            async with ctx.typing():
                sub_slots_df = self.cs.slot_table('daytime', 'S')
                prior_count, current_count = (
                    self.cs.get('daytime')
                    .filter(pl.col('S') >= CURRENT_SEASON - 1)
                    .select(pl.col('S').value_counts(sort=True))
                    .unnest('S')
                    .to_series(1)
                )

                res = []
                current = (
                    sub_slots_df.filter((pl.col('S') == CURRENT_SEASON) & (pl.col('PG').is_in(pgs)))
                    .groupby('PG')
                    .agg(pl.col('ALL').sum())
                    .select(
                        [
                            pl.col('PG'),
                            (pl.col('ALL') * 190.0 / current_count).round(0).cast(pl.Int8).alias(f'S{CURRENT_SEASON}'),
                        ]
                    )
                )
                prior = (
                    sub_slots_df.filter((pl.col('S') == CURRENT_SEASON - 1) & (pl.col('PG').is_in(pgs)))
                    .groupby('PG')
                    .agg(pl.col('ALL').sum())
                    .select(
                        [
                            pl.col('PG'),
                            (pl.col('ALL') * 190.0 / prior_count).round(0).cast(pl.Int8).alias(f'S{CURRENT_SEASON-1}'),
                        ]
                    )
                )

                result_df = (
                    current.join(prior, on='PG', how='outer')
                    .fill_null(strategy='zero')
                    .with_column((pl.col(f'S{CURRENT_SEASON}') - pl.col(f'S{CURRENT_SEASON-1}')).alias('DIF'))
                )
                result_df = result_df.with_column(
                    pl.when(pl.col('DIF') >= 0).then(('+' + pl.col('DIF').cast(str)).alias('DIF')).otherwise(pl.col('DIF'))
                )

                await send_long_mes(
                    ctx, result_df.collect().to_pandas().set_index('PG').to_string(index_names=False), newline_limit=14
                )

    @lineup.command(aliases=['g'])
    async def generate(self, ctx, *, options: GenerateFlags):
        """Generates N random lineups, up to 5 (to simulate a standard week if unique is True).

        For full-hour shows, "smart" logic can be attempted (default True) to create a plausible modern-day lineup. The logic here is very experimental. Otherwise, the lineup is purely random.

        By default, only includes active games. Set retired=True to include those."""
        pgs = set(PG.partition_table['ACTIVE'])
        if options.retired:
            pgs |= PG.partition_table['RETIRED']

        lineup_strs = []
        for n in range(options.N):
            pg_sample = copy(pgs)

            if options.smart and not options.half_hour:
                nPGs, non_car = self.cs.gen_lineup(pg_sample)
                lineup_strs.append(', '.join([f'{pg}{" (car)" if n else ""}' for pg, n in zip(nPGs, non_car)]))
            else:
                nPGs = random.sample(tuple(pg_sample), k=3 if options.half_hour else 6)
                lineup_strs.append(', '.join([str(pg) for pg in nPGs]))

            if options.unique:
                pgs -= set(nPGs)

        await ctx.send(('>>> ' if options.N > 1 else '') + '\n'.join([f'`{ls}`' for ls in lineup_strs]))

    @lineup.command(aliases=['a', 'notes', 'n'])
    async def addendum(self, ctx):
        """Prints a static message with some extra explanation on a few oddities in the lineup database."""
        await ctx.send('>>> ' + self.cs.notes)

    @lineup.command(aliases=['flags', 'f'])
    async def flag(self, ctx, flags: Optional[str]):
        """Prints a static message with explanations on PG playing flags. If no flags are given, print all."""
        if flags:
            flag_strs = []
            for f in flags:
                try:
                    flag_strs.append(f'`{f}` {FLAG_INFO[f]}')
                except KeyError:
                    pass
            if flag_strs:
                await ctx.send('>>> ' + '\n'.join(flag_strs))
            else:
                await ctx.send('`None of those flags exist. Valid flags: ' + (''.join(FLAG_INFO.keys())) + '`')
        else:
            await ctx.send('>>> ' + '\n'.join(f'`{k}` {v}' for k, v in FLAG_INFO.items()))

    @lineup.command(hidden=True)
    @commands.is_owner()
    async def reload(self, ctx):
        await ctx.message.add_reaction('🚧')
        _log.info('start loading cs at ' + str(datetime.now()))
        await asyncio.to_thread(self.cs.load_excel)
        await asyncio.to_thread(self.cs.initialize)
        _log.info('end loading cs at ' + str(datetime.now()))
        await ctx.message.remove_reaction('🚧', ctx.bot.user)
        await ctx.message.add_reaction('✅')

    async def cog_command_error(self, ctx, e):
        if isinstance(e, commands.CheckFailure):
            await ctx.send('`Lineups are being reloaded right now, try again in a few seconds.`', ephemeral=True)
        else:
            if ctx.command.name == 'search':
                if isinstance(e, (commands.errors.MissingRequiredArgument, commands.errors.MissingRequiredFlag)):
                    await ctx.send(
                        '`At least one condition required. Use "condition=" or "cond=" (new syntax).`', ephemeral=True
                    )
                elif isinstance(e, commands.ConversionError):
                    if isinstance(e.original, AssertionError):
                        await ctx.send('`Logic expression does not evaluate to True or False.`', ephemeral=True)
                    elif isinstance(e.original, TypeError):
                        await ctx.send('`Malformed logic expression.`', ephemeral=True)
                else:
                    await ctx.send(f'`{e}`', ephemeral=True)
            elif ctx.command.name == 'editLineup':
                if hasattr(e, 'original') and isinstance(e.original, AssertionError):
                    await ctx.send('`At least one of intended date, airdate, notes must be specified.`', ephemeral=True)
                elif isinstance(e, (commands.errors.MissingRequiredArgument, commands.errors.MissingRequiredFlag)):
                    await ctx.send('`prodNumber required.`', ephemeral=True)
                else:
                    await ctx.send(f'`{e}`', ephemeral=True)
            else:
                if isinstance(e, commands.ConversionError):
                    if isinstance(e.original, KeyError):
                        await ctx.send(f'`The following is not a PG (or PGGroup): {e.original}`', ephemeral=True)
                    elif isinstance(e.original, ValueError):
                        await ctx.send(f'`The following value is not properly formatted: {e.original}`', ephemeral=True)
                    else:
                        await ctx.send(f'`{e.__cause__}`', ephemeral=True)
                elif isinstance(e, commands.BadArgument):
                    await ctx.send(f'`{e.__cause__}`', ephemeral=True)
                elif isinstance(e, commands.CommandError):  # and isinstance(e.original, AssertionError):
                    await ctx.send(f'`{e}`', ephemeral=True)
                else:
                    pass  # wayo.py


async def setup(bot):
    await bot.add_cog(LineupCog(bot))
