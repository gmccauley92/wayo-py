import asyncio
import logging
import operator
import re
import string
from datetime import *
from functools import reduce
from itertools import product, repeat
from operator import attrgetter
from typing import List, Optional

import discord
import discord.ui as dui
import polars as pl
import portion as P
from datetime_matcher import DatetimeMatcher
from discord import app_commands
from discord.ext import commands
from humanize import ordinal
from more_itertools import chunked, value_chain
from sortedcontainers import SortedSet

from compendium import COMPENDIUM_NOTES, CURRENT_SEASON, WheelCompendium, dl_season
from util import (
    NONNEGATIVE_INT,
    POSITIVE_INT,
    logic_expression,
    season_portion_str_2,
    send_long_mes,
    pretty_print_polars as ppp,
)

_log = logging.getLogger('wayo_log')

SEASON_RANGE = commands.Range[int, 1, CURRENT_SEASON]


def gen_compendium_submes(df: pl.DataFrame) -> str:
    q = df.lazy()

    bool_cols = [col for col, dtype in df.schema.items() if dtype is pl.Boolean]

    if bool_cols:
        empty = (
            q.clone().select([pl.col(bool_cols).is_not().all(), (pl.col(q.columns[-1]).str.lengths() == 0).all()]).collect()
        )

        exclude = [col for r, col in zip(empty.row(0), empty.columns) if r]
        if exclude:
            q = q.select(pl.exclude(exclude))

        c_exprs = [
            pl.when(pl.col(col)).then(pl.lit(col)).otherwise(pl.lit('')).alias(col)
            for col, dtype in q.schema.items()
            if dtype is pl.Boolean and col not in exclude
        ]
        if c_exprs:
            q = q.with_columns(c_exprs)

    df = q.with_columns(
        [
            pl.col('DATE').dt.strftime('%b %d %Y'),
            pl.col('EP').cast(str).str.zfill(4),
            pl.col('E/S').cast(str).str.zfill(3),
        ]
    ).collect()

    return ppp(df)


def compendium_admin_check(ctx):
    return ctx.author.id in {
        149230437850415104,
        688572894036754610,
        314626694650527744,
        186628465376624640,
        542389298872451081,
        150096484908531712,
    }


class SearchFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    logicExpr: logic_expression = commands.flag(aliases=['logic'], default='all')
    conditions: List[str] = commands.flag(name='condition', aliases=['cond', 'c'])
    output: str = commands.flag(aliases=['o'], default='full')
    time: str = commands.flag(aliases=['version'], default='syndicated')
    dateFormat: str = commands.flag(aliases=['format'], default='%m/%d/%y')


class FrequencyFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    regex: str.upper = commands.flag(aliases=['r'], default=None)
    start: SEASON_RANGE = commands.flag(default=CURRENT_SEASON - 4)
    end: SEASON_RANGE = commands.flag(default=CURRENT_SEASON)
    by: POSITIVE_INT = commands.flag(default=1)
    puzzler: bool = commands.flag(aliases=['p'], default=None)


_number_logic_to_str = {
    '=': '',
    '!=': 'not',
    '>': 'greater than',
    '<': 'less than',
    '<=': 'at most',
    '>=': 'at least',
    '(': 'greater than',
    '[': 'at least',
    ')': 'less than',
    ']': 'at most',
}
_date_logic_to_str = {
    '=': 'on',
    '!=': 'not on',
    '>': 'later than',
    '<': 'earlier than',
    '<=': 'on or before',
    '>=': 'on or after',
    '(': 'later than',
    '[': 'on or after',
    ')': 'earlier than',
    ']': 'on or before',
}
_col_name_remapping = {
    'SEASON': 'S',
    'EPISODE': 'EP',
    'ES': 'E/S',
    'ROUND': 'RD',
    'CAT': 'CATEGORY',
    'P': 'PUZZLE',
    'CB': 'CLUE/BONUS',
    'B': 'CLUE/BONUS',
    'CLUE': 'CLUE/BONUS',
    'BONUS': 'CLUE/BONUS',
    'D': 'DATE',
}
_dt_q_remapping = {
    'Y': 'YEAR',
    'D': 'DAY',
    'M': 'MONTH',
    'DOW': 'WEEKDAY',
    'WKDAY': 'WEEKDAY',
}
_wkday_mapping = {
    'M': '0',
    'MON': '0',
    'T': '1',
    'TUE': '1',
    'W': '2',
    'WED': '2',
    'R': '3',
    'THU': '3',
    'F': '4',
    'FRI': '4',
}
_wkday_backmapping = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_month_mapping = {
    'JAN': '1',
    'FEB': '2',
    'MAR': '3',
    'APR': '4',
    'MAY': '5',
    'JUN': '6',
    'JUL': '7',
    'AUG': '8',
    'SEP': '9',
    'OCT': '10',
    'NOV': '11',
    'DEC': '12',
}
_month_backmapping = [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December',
]
_letters_mapping = {'CONSONANT': 'BCDFGHJKLMNPQRSTVWXYZ'}
_word_regex = r"\b[A-Z-'\.]+\b"


def build_int_expression(base_expr, conds, date_hybrid=False):
    cond_builder = []
    desc_builder = []
    plural = False

    str_converter = (lambda s: _date_logic_to_str[s] + ' a') if date_hybrid else (lambda s: _number_logic_to_str[s])

    for c in conds:
        if m := re.fullmatch('([\[\(])(\d+),\s*(\d+)([\)\]])', c):
            b1, start, end, b2 = m.groups()
            start = int(start)
            end = int(end)
            cond_builder.append(base_expr.is_between(start, end, [b1 == '[', b2 == ']']))
            desc_builder.append(f'{str_converter(b1)} {start} and {str_converter(b2)} {end}')
            plural |= start != 1 or end != 1
        elif re.fullmatch('\d+(,\s*\d+)+', c):
            c_strs = re.split(',\s*', c)
            ns = [int(i) for i in c_strs]
            cond_builder.append(base_expr.is_in(ns))
            desc_builder.append('one of ' + (', '.join(c_strs)))
            plural |= ns != [1]
        elif m := re.fullmatch('=?\s*(\d+)', c):
            n = int(m.group(1))
            cond_builder.append(base_expr == n)
            desc_builder.append(f'{n}')
            plural |= n != 1
        elif m := re.fullmatch('(!=|>|<|<=|>=)\s*(\d+)', c):
            op, n = m.groups()
            cond_builder.append(eval(f'base_expr {c}'))
            desc_builder.append(f'{str_converter(op)} {n}')
            plural |= int(n) != 1
        else:
            raise ValueError(f'Malformed number expression: {c}')

    if len(conds) > 1:
        return pl.any(cond_builder), ', or '.join(desc_builder), plural
    else:
        return cond_builder[0], desc_builder[0], plural


_DTM = DatetimeMatcher()
# extract_dates in ^ this third-party library was iffy for me
# but the match functionality taking care of datetime formats under the hood
# was very useful, even though it's strict matching
def build_date_expression(base_expr, conds, dateFormat):
    cond_builder = []
    desc_builder = []
    df = lambda d: datetime.strptime(d, dateFormat).date()

    for c in conds:
        if m := _DTM.match(f'([\[\(])({dateFormat}),\s*({dateFormat})([\)\]])', c):
            b1, b2 = m.group(1, 4)
            start, end = [df(d) for d in m.group(2, 3)]
            cond_builder.append(base_expr.is_between(start, end, [b1 == '[', b2 == ']']))
            desc_builder.append(f'{_date_logic_to_str[b1]} {m.group(2)} and {_date_logic_to_str[b2]} {m.group(3)}')
        elif m := _DTM.match(f'^=?\s*({dateFormat})$', c):
            d = df(m.group(1))
            cond_builder.append(base_expr == d)
            desc_builder.append(m.group(1))
        elif m := _DTM.match(f'({dateFormat})(?:,\s*({dateFormat}))+', c):
            cond_builder.append(base_expr.is_in([df(d) for d in m.groups()]))
            desc_builder.append('on one of ' + (', '.join(m.groups())))
        elif m := _DTM.match(f'(!=|>|<|<=|>=)\s*({dateFormat})', c):
            op, ds = m.groups()
            d = df(ds)
            cond_builder.append(eval(f'base_expr {op} d'))
            desc_builder.append(f'{_date_logic_to_str[op]} {ds}')
        else:
            raise ValueError(f'Malformed date expression: {c}')

    if len(conds) > 1:
        return pl.any(cond_builder), ', or '.join(desc_builder)
    else:
        return cond_builder[0], desc_builder[0]


class CompendiumCog(commands.Cog, name='Compendium'):
    """https://buyavowel.boards.net/page/compendiumindex"""

    def __init__(self, bot):
        self.bot = bot
        self._debug = _log.getEffectiveLevel() == logging.DEBUG
        try:
            self.wc = WheelCompendium(loop=self.bot.loop, debug=self._debug)
        except ValueError as e:
            self.wc = None
            _log.error(f'loading wc failed! {e}')

    async def cog_check(self, ctx):
        return self.wc and self.wc.df is not None

    # bases

    @commands.hybrid_group(aliases=['wc'], case_insensitive=True)
    async def wheelcompendium(self, ctx):
        """Commands related to the Buy a Vowel boards' compendium of all known Wheel of Fortune puzzles."""
        if not ctx.invoked_subcommand:
            await ctx.send('Invalid subcommand (see `help wheelcompendium`).')

    # end bases

    @wheelcompendium.command(aliases=['r'], with_app_command=False)
    @commands.check(compendium_admin_check)
    async def refresh(self, ctx, seasons: commands.Greedy[SEASON_RANGE]):
        """Redownload compendium data from the given syndicated seasons (all seasons if not provided) and update wayo.py's version of the compendium.

        Only Wayoshi, dftackett, 9821, Kev347, and Thetrismix can currently run this command."""
        if not seasons:
            seasons = range(1, CURRENT_SEASON + 1)
        await ctx.message.add_reaction('🚧')
        try:
            await asyncio.gather(*(dl_season(s, self.bot.asession) for s in seasons))
            await self.wc.load(seasons)
            await ctx.message.remove_reaction('🚧', ctx.bot.user)
            await ctx.message.add_reaction('✅')
        except Exception as e:
            await ctx.send(f'Error refreshing: {e}')
            await ctx.message.remove_reaction('🚧', ctx.bot.user)
            await ctx.message.add_reaction('❌')
            return

        if not self._debug:
            try:
                await (await ctx.bot.fetch_user(688572894036754610)).send(
                    f'{ctx.author.name} refreshed {seasons} at {datetime.now()}'
                )
            except:
                pass

    @wheelcompendium.command(aliases=['s'], with_app_command=False)
    async def search(self, ctx, *, options: SearchFlags):
        """Lists every puzzle in the compendium that matches a set of conditions.

        If 'all' is given to logicExpr (short for logical expression), all the conditions must match. If 'any' is given, at least one condition must match. Custom logic expressions are also allowed, see the pastebin for more details.

        Each condition is a set of "words", separated by a semicolon. See the pastebin for exact formatting details.

        The "output" is by default the usual full table output of matching rows. You can specify one of the following instead:
        -"CATEGORY", "CAT", "ROUND, "RD", "R": Category or Round frequency table. All seasons & categories/rounds with all zeros (columns/rows) will be automatically omitted. The number of columns is determined by an additional "by" parameter, separated by a semicolon like a condition, determining how many seasons to sum up in one column.
        -"PUZZLE", "P": Puzzle frequency list. Simple listing of every puzzle that occurs at least N (default 2) times in the result. N can be supplied just like "by" above.

        The dataset used is specified by the "time" parameter. Currently only "syndicated" is supported."""

        f_exprs = []
        cond_descriptions = []

        if len(options.conditions) > 26:
            raise ValueError("Too many conditions given, max is 26. (You shouldn't need close to this many!)")
        if options.time.upper() != 'SYNDICATED':
            raise ValueError('Only syndicated supported at the moment.')

        for cond in options.conditions:
            words = [w.strip().upper() for w in cond.split(';')]

            match words:
                case ['BONUS' | 'B']:
                    f = (pl.col('CLUE/BONUS').str.lengths() > 0) & (~pl.col('CATEGORY').cast(str).str.contains('CROSSWORD'))
                    cd = 'has a bonus'
                case ['BONUS' | 'B' as col, '1' | '0' | 'YES' | 'NO' | 'Y' | 'N' | 'T' | 'F' | 'TRUE' | 'FALSE' as b]:
                    b = re.match('[1YT]', b)

                    f = (pl.col('CLUE/BONUS').str.lengths() > 0) & (~pl.col('CATEGORY').cast(str).str.contains('CROSSWORD'))
                    if not b:
                        f = f.is_not()
                    cd = 'has a bonus' if b else 'does not have a bonus'
                case [
                    'PUZZLE'
                    | 'P'
                    | 'CLUE/BONUS'
                    | 'CLUE'
                    | 'BONUS'
                    | 'CB'
                    | 'B'
                    | 'RD'
                    | 'R'
                    | 'ROUND'
                    | 'CAT'
                    | 'CATEGORY' as col,
                    lit,
                    'LITERAL' | 'LIT' | 'L' | 'EXACT' | 'E' as p_q,
                ]:
                    col = _col_name_remapping.get(col, col)

                    if p_q.startswith('L'):
                        f = pl.col(col).str.contains(lit, literal=True)
                        cd = f'{col} contains "{lit}"'
                    else:
                        f = pl.col(col).str.contains(f'^{lit}$')
                        cd = f'{col} is exactly "{lit}"'
                case ['PUZZLE' | 'P' | 'CLUE/BONUS' | 'CLUE' | 'BONUS' | 'CB' | 'B' as col, regex, *e]:
                    col = _col_name_remapping.get(col, col)
                    regex = re.sub(r'\\\w', lambda m: m.group().lower(), regex)

                    if e:
                        f, cd, p = build_int_expression(pl.col(col).str.count_match(regex), e)
                        cd = f'{col} matches "{regex}" {cd} time' + ('s' if p else '')
                    else:
                        f = pl.col(col).str.contains(regex)
                        cd = f'{col} matches "{regex}"'
                case ['RD' | 'ROUND' | 'R' | 'CAT' | 'CATEGORY' as col, regex]:
                    col = _col_name_remapping.get(col, col)
                    regex = re.sub(r'\\\w', lambda m: m.group().lower(), regex)
                    if col == 'CATEGORY' and ':tm:' in col:
                        regex = regex.replace(':tm:', '™️')

                    f = pl.col(col).cast(str).str.contains(regex)
                    cd = f'{col} matches "{regex}"'
                case ['SEASON' | 'S' | 'EPISODE' | 'EP' | 'ES' | 'E/S' as col, *e]:
                    col = _col_name_remapping.get(col, col)

                    f, cd, _ = build_int_expression(pl.col(col), e)
                    cd = f'{col} is {cd}'
                case [
                    'DATE' | 'D' as col,
                    'YEAR' | 'Y' | 'MONTH' | 'M' | 'DAY' | 'D' | 'DOW' | 'WKDAY' | 'WEEKDAY' as dt_q,
                    *e,
                ]:
                    col = _col_name_remapping.get(col, col)
                    dt_q = _dt_q_remapping.get(dt_q, dt_q)
                    special = dt_q in ('MONTH', 'WEEKDAY')

                    if special:
                        if dt_q == 'MONTH':
                            mapping = _month_mapping
                            backmapping = lambda cd: re.sub(
                                '([1-9]|1[012])', lambda m: _month_backmapping[int(m.group()) - 1], cd
                            )
                        else:
                            mapping = _wkday_mapping
                            backmapping = lambda cd: re.sub('[0-6]', lambda m: _wkday_backmapping[int(m.group())], cd)
                        try:
                            e = [re.sub('[A-Z]+', lambda m: mapping[m.group()], ee) for ee in e]
                        except KeyError as ke:
                            raise ValueError(f'Invalid {dt_q} string: {ke}')
                    else:
                        backmapping = lambda cd: cd

                    f, cd, _ = build_int_expression((attrgetter(dt_q.lower())(pl.col('DATE').dt))(), e, special)
                    cd = f'{dt_q} of DATE is ' + backmapping(cd)
                case ['DATE' | 'D' as col, *e]:
                    col = _col_name_remapping.get(col, col)
                    e = [
                        re.sub(
                            r'(\b\d\b|[A-Z]+)', lambda m: '0' + m.group() if m.group().isnumeric() else m.group().title(), ee
                        )
                        for ee in e
                    ]

                    f, cd = build_date_expression(pl.col(col), e, options.dateFormat)
                    cd = f'{col} is {cd}'
                case ['LENGTH' | 'LC' | 'L', *e]:
                    f, cd, _ = build_int_expression(pl.col('PUZZLE').str.extract_all('[A-Z]').arr.lengths(), e)
                    cd = f'length is {cd}'
                case ['LENGTH_UNIQUE' | 'LCU' | 'LU', *e]:
                    f, cd, _ = build_int_expression(pl.col('PUZZLE').str.extract_all('[A-Z]').arr.unique().arr.lengths(), e)
                    cd = f'total number of unique letters is {cd}'
                case ['COUNT' | 'C', letters, *e]:
                    if letters in _letters_mapping:
                        letters = _letters_mapping[letters]
                    elif not re.match('[A-Z]+', letters) or not len(set(letters)) == len(letters):
                        raise ValueError(f'Malformed letter string (must be all A-Z and all unique): {letters}')

                    f, cd, _ = build_int_expression(
                        pl.col('PUZZLE')
                        .str.extract_all('[A-Z]')
                        .arr.eval(pl.element().is_in(list(letters)), parallel=True)
                        .arr.sum(),
                        e,
                    )
                    cd = f'total number of {letters} is {cd}'
                case ['COUNT_UNIQUE' | 'CU', letters, *e]:
                    if letters in _letters_mapping:
                        letters = _letters_mapping[letters]
                    elif not re.match('[A-Z]+', letters) or not len(set(letters)) == len(letters):
                        raise ValueError(f'Malformed letter string (must be all A-Z and all unique): {letters}')

                    f, cd, _ = build_int_expression(
                        pl.col('PUZZLE')
                        .str.extract_all('[A-Z]')
                        .arr.unique()
                        .arr.eval(pl.element().is_in(list(letters)), parallel=True)
                        .arr.sum(),
                        e,
                    )
                    cd = f'total unique number of {letters} is {cd}'
                case ['WORD_COUNT' | 'WC', *e]:
                    f, cd, _ = build_int_expression(pl.col('PUZZLE').str.count_match(_word_regex), e)
                    cd = f'total word count is {cd}'
                case ['WORD' | 'W', regex]:
                    f = (
                        pl.col('PUZZLE')
                        .str.extract_all(_word_regex)
                        .arr.eval(pl.element().str.contains(regex), parallel=True)
                        .arr.contains(True)
                    )
                    cd = f'any word matches "{regex}"'
                case ['WORD' | 'W', word, 'LITERAL' | 'LIT' | 'L' | 'EXACT' | 'E' as w_q]:
                    if w_q.startswith('L'):
                        f = (
                            pl.col('PUZZLE')
                            .str.extract_all(_word_regex)
                            .arr.eval(pl.element().str.contains(word, literal=True), parallel=True)
                            .arr.contains(True)
                        )
                        cd = f'any word contains "{word}"'
                    else:
                        f = pl.col('PUZZLE').str.extract_all(_word_regex).arr.contains(word)
                        cd = f'any word is exactly "{word}"'
                case ['WORD' | 'W', regex, idx]:
                    idx = int(idx)
                    if idx > 0:
                        sub_cd = ordinal(idx)
                        idx -= 1
                    elif idx == 0:
                        raise ValueError('Zeroth word is not defined.')
                    else:
                        sub_cd = ordinal(-idx) + '-to-last' if idx < -1 else 'last'

                    f = pl.col('PUZZLE').str.extract_all(_word_regex).arr.get(idx).str.contains(regex)
                    cd = f'{sub_cd} word matches "{regex}"'
                case ['WORD' | 'W', word, 'LITERAL' | 'LIT' | 'L' | 'EXACT' | 'E' as w_q, idx]:
                    idx = int(idx)
                    if idx > 0:
                        sub_cd = ordinal(idx)
                        idx -= 1
                    elif idx == 0:
                        raise ValueError('Zeroth word is not defined.')
                    else:
                        sub_cd = ordinal(-idx) + '-to-last' if idx < -1 else 'last'

                    base_expr = pl.col('PUZZLE').str.extract_all(_word_regex).arr.get(idx)

                    if w_q.startswith('L'):
                        f = base_expr.str.contains(word, literal=True)
                        cd = f'{sub_cd} word contains "{word}"'
                    else:
                        f = base_expr == word
                        cd = f'{sub_cd} word is exactly "{word}"'
                case ['UC' | 'PP' | 'PR' | 'RL' as col]:
                    f = pl.col(col)
                    cd = f'is a {col} puzzle'
                case [
                    'UC' | 'PP' | 'PR' | 'RL' as col,
                    '1' | '0' | 'YES' | 'NO' | 'Y' | 'N' | 'T' | 'F' | 'TRUE' | 'FALSE' as b,
                ]:
                    b = re.match('[1YT]', b)

                    f = pl.col(col) if b else pl.col(col).is_not()
                    cd = f'is a {col} puzzle' if b else f'is not a {col} puzzle'
                case _:
                    raise ValueError(f'Malformed condition: {cond}')

            f_exprs.append(f)
            cond_descriptions.append(cd)

        if options.logicExpr == 'all':
            total_expr = pl.all(f_exprs)
            expr_str = ' all of'
        elif options.logicExpr == 'any':
            total_expr = pl.any(f_exprs)
            expr_str = ' any of'
        else:
            l = locals()
            l |= {letter: fe for fe, letter in zip(f_exprs, string.ascii_uppercase)}
            total_expr = eval(re.sub('([A-Z])', r'(\1)', options.logicExpr))
            expr_str = f'\n{options.logicExpr}; where'

        async with ctx.typing():
            sub_df = self.wc.df.filter(total_expr).collect()
            # _log.debug(f'\n{sub_df}')

            bonus_df = sub_df.lazy().filter(pl.col('CLUE/BONUS').str.lengths() > 0).collect()

            if bonus_df.height:
                if bonus_df[0, 'S'] >= 33:
                    sub_df = sub_df.rename({'CLUE/BONUS': 'CLUE'})
                elif bonus_df[-1, 'S'] < 33:
                    sub_df = sub_df.rename({'CLUE/BONUS': 'BONUS'})

            plural = 's' if sub_df.height != 1 else ''

            (cov_total_pct,) = self.wc.calc_coverage(None).to_series(-1).tail(1)
            description_str = (
                f'{sub_df.height} puzzle{plural} found in {options.time.upper()} ({cov_total_pct:.1f}% COV) for'
            )

            if len(cond_descriptions) > 1:
                description_str += f'{expr_str}\n\n'
                if expr_str.endswith('where'):
                    description_str += '\n'.join([f'{l} = {cd}' for cd, l in zip(cond_descriptions, string.ascii_uppercase)])
                else:
                    description_str += '\n'.join([f'* {cd}' for cd in cond_descriptions])
            else:
                description_str += ' ' + cond_descriptions[0]

            total_str = f'{description_str}\n\n'

            if sub_df.height:
                match options.output.upper().split(';'):
                    case ['RD' | 'ROUND' | 'R' | 'CAT' | 'CATEGORY' as col, *by]:
                        season_range = tuple(sub_df.select(pl.col('S').unique(True)).to_series())

                        by = int(by[0]) if by else 1
                        if by < 0:
                            raise ValueError(f'{by} must be non-negative.')

                        s_chunks = list(chunked(season_range, by))
                        col = _col_name_remapping.get(col, col)

                        vc = (
                            sub_df.lazy()
                            .groupby(col)
                            .agg(pl.col('S').value_counts(sort=True))
                            .collect()
                            .lazy()
                            .select(
                                [pl.col(col).cast(str)]
                                + [
                                    pl.col('S')
                                    .arr.eval(
                                        pl.when(pl.element().struct.field('').is_in(list(s)))
                                        .then(pl.element().struct.field('counts'))
                                        .otherwise(0),
                                        parallel=True,
                                    )
                                    .arr.sum()
                                    .alias(season_portion_str_2(s))
                                    for s in s_chunks
                                ]
                            )
                            .sort(col)
                        )

                        description_str = f'{col}, '
                        (cov_pct,) = self.wc.calc_coverage(season_range).to_series(-1).tail(1)

                        if len(s_chunks) > 1:
                            vc = vc.with_column(pl.sum(pl.exclude(col)).alias('ALL'))

                        df = vc.collect()

                        if df.height > 1:
                            total = df.select([pl.lit('ALL').alias(col)] + [pl.sum(c) for c in df.columns[1:]])
                            df.vstack(total, in_place=True)
                            ss = df.to_pandas().to_string(index=False).replace(col, ''.join(repeat(' ', len(col))))

                            # lift from cog_lineup slots
                            sss = ss.split('\n')
                            last_S_label = df.columns[-2]
                            sss[0] = sss[0].replace(last_S_label + ' ', last_S_label + ' | ')
                            j = sss[0].rindex('|')
                            for i in range(1, len(sss)):
                                sss[i] = sss[i][: j - 1] + ' | ' + sss[i][j:]
                            extra_line = ''.join(repeat('-', len(sss[0])))
                            extra_line = extra_line[:j] + '|' + extra_line[j + 1 :]
                            sss.insert(len(sss) - 1, extra_line)
                            ssss = '\n'.join(sss)
                        else:
                            ssss = df.to_pandas().to_string(index=False).replace(col, ''.join(repeat(' ', len(col))))

                        total_str += f'{col} FREQUENCY TABLE, {season_portion_str_2(season_range)} ({by}) ({cov_pct:.1f}% COV)\n\n{ssss}'
                    case ['PUZZLE' | 'P', *n]:
                        n = int(n[0]) if n else 2
                        if n < 2:
                            raise ValueError('N must be at least 2.')

                        vc = (
                            sub_df.lazy()
                            .select(pl.col('PUZZLE').value_counts(True, True))
                            .unnest('PUZZLE')
                            .filter(pl.col('counts') >= n)
                            .select([pl.col('counts').alias('N'), pl.col('PUZZLE')])
                            .select(pl.all().sort_by(['N', 'PUZZLE'], [True, False]))
                        )

                        nc = vc.select(pl.col('N').value_counts(True, True)).reverse().collect()

                        if nc.height:
                            total_str += (
                                'PUZZLE FREQUENCY LIST: '
                                + (', '.join(f'{d[0]["counts"]} {d[0]["N"]}x' for d in nc.rows()))
                                + '\n\n'
                                + str(
                                    vc.collect().to_pandas().to_string(index=False, formatters={'PUZZLE': lambda s: f' {s}'})
                                )
                            )
                        else:
                            total_str += f'`PUZZLE FREQUENCY: No puzzles that occurred more than {n} times.`'
                    case _:
                        total_str += gen_compendium_submes(sub_df)
            else:
                total_str = description_str

        await send_long_mes(ctx, total_str)

    @wheelcompendium.command(aliases=['pc'], description='Gives the total puzzle count in the given seasons.')
    async def puzzle_count(self, ctx, seasons: commands.Greedy[SEASON_RANGE], range: bool = False):
        """Gives the total puzzle count in the compendium in the given seasons (all by default, if range is True it will treat each pair of inputs as an inclusive range) compendium without any further results."""
        q = self.wc.df
        if seasons:
            if range:
                seasons = list(
                    P.iterate(
                        reduce(
                            operator.or_,
                            [P.closed(*s) if len(s) > 1 else P.singleton(*s) for s in chunked(seasons, 2)],
                            P.empty(),
                        ),
                        step=1,
                    )
                )
            q = q.filter(pl.col('S').is_in(seasons))
        await ctx.send(f'`{q.collect().height}`')

    @wheelcompendium.command(
        aliases=['cov'], description='Calculates the number of unique shows covered in the given seasons.'
    )
    async def coverage(self, ctx, seasons: commands.Greedy[SEASON_RANGE], doRange: bool = False):
        """The compendium is incomplete at points. This command calculates the number of unique shows it has in the given seasons (all by default, if doRange is True it will treat each pair of inputs as an inclusive range) and gives a percentage of "coverage" (COV).

        There are 195 shows every syndicated season, with the exceptions of the pandemic season 37, when there were 167, and S16 and 21 had one clip show each, so effectively 194 true shows. The current season is assumed to be at max coverage at all times.

        The compendium has several incomplete shows not in the database, that will go in if full copies of the show are ever found: https://buyavowel.boards.net/page/compendiumapp (Incomplete Shows)"""

        await send_long_mes(ctx, ppp(self.wc.calc_coverage(tuple(seasons), doRange).fill_null('')))

    @wheelcompendium.command(aliases=['a', 'notes', 'n'])
    async def addendum(self, ctx):
        """Prints a static message with some extra explanation on a few oddities in the compendium database.

        For more, see https://buyavowel.boards.net/page/compendiumapp"""
        await ctx.send('>>> ' + COMPENDIUM_NOTES)

    async def cog_command_error(self, ctx, e):
        if isinstance(e, (commands.errors.MissingRequiredArgument, commands.errors.MissingRequiredFlag)):
            if ctx.command.name == 'search':
                await ctx.send('`At least one condition required.`', ephemeral=True)
            else:  # freq
                await ctx.send('`Must specify column.`', ephemeral=True)
        elif isinstance(e, commands.CommandInvokeError):
            if isinstance(e.__cause__, pl.exceptions.ComputeError):
                await ctx.send(f'Error computing query:\n```\n{e.__cause__}\n```', ephemeral=True)
            else:  # if isinstance(e.__cause__, ValueError):
                await ctx.send(f'`{e.__cause__}`', ephemeral=True)
        elif isinstance(e, commands.CheckFailure):
            if not self.wc or not self.wc.df:
                await ctx.send('`Compendium is loading (try again shortly) or failed to load.`', ephemeral=True)
            else:
                await ctx.send('`Only compendium maintainers can run this command.`')
        else:
            await ctx.send(f'`{e}`')  # wayo.py handler


async def setup(bot):
    await bot.add_cog(CompendiumCog(bot))
