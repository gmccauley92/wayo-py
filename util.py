import operator
import random
import re
import string
from datetime import date, datetime
from functools import reduce
from sys import getsizeof
from typing import *

import portion as P
from cachetools.func import lfu_cache
from discord.ext import commands
from pytz import common_timezones, timezone
from sortedcontainers import SortedSet

Range = NewType('Range', range)
Portion = NewType('Portion', P.Interval)

SCHEDULER_TZ = timezone('US/Eastern')
PLAYING_FLAGS = tuple(reversed(('car', 'T', 'cars', '*', '@', 'R', '$', '^', '?', 'MDG')))
NAME_ATTRGET = operator.attrgetter('name')
MAX_MES_SIZE = 2000
SORT_PROD = lambda p: p[-1] + p[:-1]  # xxxx[DK] --> [DK]xxxx

NONNEGATIVE_INT = commands.Range[int, 0]
POSITIVE_INT = commands.Range[int, 1]
POSITIVE_CENT = commands.Range[float, 0.01]
ARP_RANGE = commands.Range[int, 20251]

from pg import CURRENT_SEASON, PG, PG_WILDCARD

SEASON_RANGE = commands.Range[int, 1, CURRENT_SEASON]

UNKNOWN_CASEFOLD = 'unknown'.casefold()


class PGConverter(commands.Converter):
    async def convert(self, ctx, argument):
        # argument = argument.strip()
        if ctx.command.name != 'search' and argument.casefold() == UNKNOWN_CASEFOLD:
            raise KeyError
        else:
            return PG.lookup(argument)


class PGPlayingConverter(commands.Converter):
    async def convert(self, ctx, argument):
        if ctx.command.name != 'search' and argument.casefold() == UNKNOWN_CASEFOLD:
            raise KeyError
        else:
            return PG.lookup(argument), argument


class PGGroupConverter(commands.Converter):
    async def convert(self, ctx, argument):
        # argument = argument.strip()
        if re.match('^[-*]+$', argument):
            return SortedSet(PG_WILDCARD, key=NAME_ATTRGET)
        else:
            return SortedSet(PG.partition_table[PG.partition_lookup[argument]], key=NAME_ATTRGET)


class TimeConverter(commands.Converter):
    async def convert(self, ctx, argument):
        if re.fullmatch('(night|prime)(time)?', argument, re.I):
            return 'primetime'
        elif re.fullmatch('day(time)?', argument, re.I):
            return 'daytime'
        elif re.fullmatch('(not |un)aired', argument, re.I):
            return 'unaired'
        elif re.fullmatch('syn(d|dicat(ed|ion))?', argument, re.I):
            return 'syndicated'
        else:
            raise ValueError


from clue import Room, Suspect, Weapon


class SuspectConverter(commands.Converter):
    async def convert(self, ctx, argument):
        return Suspect[argument.upper()]


class WeaponConverter(commands.Converter):
    async def convert(self, ctx, argument):
        return Weapon[argument.upper()]


class RoomConverter(commands.Converter):
    async def convert(self, ctx, argument):
        return Room[argument.upper().replace('ROOM', '').strip()]


def find_nth(string, substring, n):
    if n == 1:
        return string.find(substring)
    else:
        return string.find(substring, find_nth(string, substring, n - 1) + 1)


def season_portion_str(p: Portion):
    return 'S' + ','.join(
        s + ('-' if e == '+inf' else '-' + e if e else '') for s, e in re.findall('\[(\d+)(?:,(\d+|\+inf))?[\]\)]', str(p))
    )


# assumes sorted
def season_portion_str_2(s: List[int]):
    if len(s) == 1:
        p = P.singleton(s[0])
    else:
        p = P.closed(s[0], s[-1])
        for ss in range(s[0] + 1, s[-1]):
            if ss not in s:
                p -= P.open(ss - 1, ss + 1)
    return season_portion_str(p)


def excel_date_str(dt: datetime):
    return '{dt.month}/{dt.day}/{dt.year}'.format(dt=dt)


def csspoints(bid1, bid2, arp1, arp2, dsw_diff=250):
    points = 100 * ((bid1 + bid2) / (arp1 + arp2)) if bid1 <= arp1 and bid2 <= arp2 else 0
    if 0 <= arp1 - bid1 <= dsw_diff:
        points *= 2
        if not (arp1 - bid1):
            points *= 1.5
    if 0 <= arp2 - bid2 <= dsw_diff:
        points *= 2
        if not (arp2 - bid2):
            points *= 1.5
    return points


def parse_endpoints(start, end, *pgs, dateF=None, syndicated=False, or_=False):
    if type(start) != type(end):
        # budget way of allowing date defaults
        if start == 1:
            start = date(1972, 9, 4).strftime(dateF)
        elif end == CURRENT_SEASON:
            end = date.today().strftime(dateF)
        else:
            return '`Cannot mix season number and date as a range.`'
    if type(start) == str:
        try:
            startDate = datetime.strptime(start, dateF)
        except ValueError as e:
            return f'`Malformed date: {e}`'
        try:
            endDate = datetime.strptime(end, dateF)
        except ValueError as e:
            return f'`Malformed date: {e}`'

        if startDate == endDate:
            return '`Dates cannot be equal, just use "lineupD" command for a single lineup.`'
        elif startDate > endDate:
            return '`No dates in that range.`'
        elif endDate > datetime.now():
            return "`I'm not from the future.`"
        return P.closed(startDate, endDate), startDate.strftime('%b %d %Y') + ' - ' + endDate.strftime('%b %d %Y')
    elif start > end:
        return '`No seasons in that range.`'
    elif end > CURRENT_SEASON:
        return "`I'm not from the future.`"

    season_startpoint = P.closed(start, end)
    if syndicated:
        season_startpoint &= P.closed(1, 8) | P.singleton(23)

    if or_:
        seasons = season_startpoint & reduce(operator.or_, [pg.activeSeasons for pg in pgs])
    else:
        seasons = reduce(operator.and_, [pg.activeSeasons for pg in pgs], season_startpoint)

    if not seasons.empty:
        return (seasons, season_portion_str(seasons))
    else:
        return '`Automatically 0: Active seasons of PG(s) (or show version if syndicated) and/or seasons given to command have no overlap.`'


async def parse_time_options(ctx, options, *pgs):
    isDate = type(options.start) == str or type(options.end) == str
    if options.time == 'primetime' and not isDate:
        ep = None
        epText = options.time.upper()
    else:
        if pgs:
            assert (all(pg) for pg in pgs)
            se = parse_endpoints(
                options.start, options.end, *pgs, dateF=options.dateFormat, syndicated=options.time == 'syndicated'
            )
        else:
            se = parse_endpoints(
                options.start, options.end, dateF=options.dateFormat, syndicated=options.time == 'syndicated'
            )
        if type(se) is str:
            await ctx.send(se, ephemeral=True)
            raise ValueError
        ep, epText = se
        if isDate and options.time not in ('daytime', 'primetime'):
            await ctx.send('`Dates only supported for daytime and primetime.`', ephemeral=True)
            raise ValueError
        if options.time == 'syndicated':
            epText = 'SYNDICATED ' + epText

    return ep, epText, isDate


import io

import discord


@lfu_cache
def _command_to_fn(command):
    commands = [command]
    if command.parents:
        commands.extend(reversed(command.parents))
    return '-'.join([min(c.aliases, key=len) if c.aliases else c.name for c in commands])


_MAX_FILESIZE = 8 * 2**20


async def send_long_mes(ctx, s, *, fn=None, newline_limit=19):
    if len(s) < MAX_MES_SIZE - 7 and s.count('\n') <= newline_limit:
        await ctx.send(f'```\n{s}```')
    elif getsizeof(s) > _MAX_FILESIZE:
        await ctx.send(
            '```The result is too big for a Discord file size (non-Nitro). You probably did not mean to get a result this large.\n\nIf you really want this result, contact Wayoshi directly and he can help get it for you. The first 500 characters of the result are included below as a convenience.\n\n'
            + s[:500]
            + '```'
        )
    else:
        # empty_surround = '```' if '\n' in initial_str else '`'
        # async with ctx.typing()
        # f'{empty_surround}{initial_str}{empty_surround}' if initial_str else ''
        # 3/31/21 - Discord made text attachments extremely more readable, eliminating the need for initial_str

        fn = fn or (_command_to_fn(ctx.command) + '_' + datetime.now().isoformat(timespec='seconds'))

        # 4/28/22 - pastebin!
        link = await ctx.bot.do_pastebin(s, fn)
        res = f'Pastebin mirror: <{link}>' if link else None
        await ctx.send(res, file=discord.File(io.StringIO(s), filename=fn + '.txt'))

        # if isinstance(ctx.author, discord.Member) and ctx.author.is_on_mobile():
        # 	await ctx.author.send('`Mobile user detected. Sending copy of file, will auto-delete after 5 minutes:`', delete_after=300.)
        # 	p = commands.Paginator()
        # 	for ss in s.split('\n'):
        # 		p.add_line(ss)
        # 	for pp in p.pages:
        # 		await ctx.author.send(pp, delete_after=300.)


async def send_PIL_image(channel, image, desc, content=None):
    with io.BytesIO() as b:
        image.save(b, format='png')
        b.seek(0)
        await channel.send(file=discord.File(b, desc + '.png'), content=content)


async def send_PIL_gif(channel, image_frames, desc, **PIL_options):
    with io.BytesIO() as b:
        image_frames[0].save(b, format='gif', save_all=True, append_images=image_frames[1:], optimize=True, **PIL_options)
        b.seek(0)
        return await channel.send(file=discord.File(b, filename=desc + '.gif'))


import discord.ui as dui


class CancelButton(dui.Button):
    def __init__(self, row=None, authority_user_id=None, emoji=None, extra_callback=None):
        super().__init__(style=discord.ButtonStyle.danger, label='Cancel', emoji=emoji or '❌', row=row)
        self.authority_user_id = authority_user_id
        self.extra_callback = extra_callback

    async def callback(self, interaction):
        if not self.authority_user_id or interaction.user.id == self.authority_user_id:
            if self.extra_callback:
                await self.extra_callback()
            await interaction.response.edit_message(view=None)
            self.view.stop()
        else:
            await interaction.response.send_message('You are not permitted to cancel.', ephemeral=True)


from discord import app_commands


async def pg_autocomplete(interaction, current):
    return [app_commands.Choice(name=pg, value=pg) for pg in PG.lookup_table.keys() if current.lower() in pg.lower()][:25]


async def tz_autocomplete(interaction, current):
    return [app_commands.Choice(name=tz, value=tz) for tz in common_timezones if current.lower() in tz.lower()][:25]


from sympy.core.singleton import S
from sympy.logic.boolalg import And as BAnd
from sympy.logic.boolalg import Or as BOr
from sympy.parsing.sympy_parser import parse_expr


def logic_expression(expr: str):
    if re.match('^(all|and)$', expr, re.I):
        return 'all'
    elif re.match('^(any|or)$', expr, re.I):
        return 'any'
    else:
        # there's some special characters in sympy such as E and S that throw errors, so convert to lower.
        symexpr = expr.lower()
        for cond in (('not', '~'), ('and', '&'), ('xor', '^'), ('or', '|')):
            symexpr = symexpr.replace(*cond)
        if not re.match('^[a-z\s~&|^()]+$', symexpr):
            raise ValueError
        sym = parse_expr(symexpr)
        return str(sym).upper()


class LogicExpression:
    def __init__(self, expr: str):
        if re.match('^(all|and)$', expr, re.I):
            self.func = all
            self.expr = 'all'
            self.sym = BAnd
        elif re.match('^(any|or)$', expr, re.I):
            self.func = any
            self.expr = 'any'
            self.sym = BOr
        else:
            # there's some special characters in sympy such as E and S that throw errors, so convert to lower.
            symexpr = expr.lower()
            for cond in (('not', '~'), ('and', '&'), ('xor', '^'), ('or', '|')):
                symexpr = symexpr.replace(*cond)
            if not re.match('^[a-z\s~&|^()]+$', symexpr):
                raise ValueError
            self.sym = parse_expr(symexpr)
            self.expr = str(self.sym).upper()
            # test one random application of bools.
            assert self.sym.subs(
                {string.ascii_lowercase[e]: random.choice((True, False)) for e in range(len(self.sym.free_symbols))}
            ) in (
                S.true,
                S.false,
            )
            # subs returns a boolalg.BooleanTrue - must actually compare to native True or, preferably, sympy True
            # let zip truncate automatically
            self.func = lambda bools: S.true == self.sym.subs({l: b for l, b in zip(string.ascii_lowercase, bools)})

    def __str__(self):
        return self.expr


import colorsys

import numpy as np
from PIL import Image, ImageColor


@lfu_cache
def scale_light(rgb, scale):
    if type(rgb) == str:
        rgb = ImageColor.getcolor(rgb, 'RGB')
    h, l, s = colorsys.rgb_to_hls(*rgb)
    return tuple([round(c) for c in colorsys.hls_to_rgb(h, l * scale, s)])


# https://gist.github.com/willprice/fd917d2cafe56732137e60154d456688


def vstack(images, *, fill=None):
    if len(images) == 0:
        raise ValueError("Need 0 or more images")

    if isinstance(images[0], np.ndarray):
        images = [Image.fromarray(img) for img in images]
    width = max([img.size[0] for img in images])
    height = sum([img.size[1] for img in images])
    stacked = Image.new(images[0].mode, (width, height), fill)

    y_pos = 0
    for img in images:
        stacked.paste(img, (0, y_pos))
        y_pos += img.size[1]
    return stacked


def hstack(images, *, fill=None):
    if len(images) == 0:
        raise ValueError("Need 0 or more images")

    if isinstance(images[0], np.ndarray):
        images = [Image.fromarray(img) for img in images]
    width = sum([img.size[0] for img in images])
    height = max([img.size[1] for img in images])
    stacked = Image.new(images[0].mode, (width, height), fill)

    x_pos = 0
    for img in images:
        stacked.paste(img, (x_pos, 0))
        x_pos += img.size[0]
    return stacked


import polars as pl


@lfu_cache(maxsize=64)
def build_flag_expr(col_name, flags: frozenset[int]):
    sub_expr = []
    if 0 in flags:
        sub_expr.append(pl.col(col_name) == 0)
    if flags != {0}:
        sub_expr.append(
            (pl.col(col_name) & pl.repeat(reduce(operator.or_, flags - {0}), pl.col(col_name).count())).cast(pl.Boolean)
        )
    return pl.any(sub_expr) if len(sub_expr) > 1 else sub_expr[0]


_ppp_newline = re.compile('(?:^ | (\n) | $)')


def pretty_print_polars(df: pl.DataFrame | pl.Series):
    return _ppp_newline.sub(r'\1', str(df))
