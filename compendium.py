import asyncio
import csv
import html
import itertools
import logging
import operator
import os
import re
from copy import copy
from datetime import *
from functools import partial, reduce
from io import StringIO
from operator import attrgetter
from typing import *

import aiofile
import orjson
import polars as pl
import portion as P
from cachetools import LFUCache, cachedmethod
from cachetools.func import lfu_cache
from cachetools.keys import hashkey
from more_itertools import chunked
from requests_html import AsyncHTMLSession, HTMLSession
from sortedcontainers import SortedDict

from dropboxwayo import dropboxwayo

Range = Union[range, Iterable[int]]

_log = logging.getLogger('wayo_log')


_TRIVIA_CATEGORIES = frozenset(
    {
        'WHERE ARE WE?',
        'CLUE',
        'FILL IN THE BLANK',
        'FILL IN THE NUMBER',
        'NEXT LINE PLEASE',
        'SLOGAN',
        'WHAT ARE WE MAKING?',
        "WHAT'S THAT SONG?",
        'WHERE ARE WE?',
        'WHO IS IT?',
        'WHO ARE THEY?',
        'WHO SAID IT?',
    }
)

CURRENT_SEASON = 40
COMPENDIUM_NOTES = """-On November 2 1992 (S10, #1796), R1 (VANNA'S PREGNANT) was heavily edited due to Vanna's miscarriage. Despite this editing we know what hte puzzle was, so it does remain in the compendium.
-On November 20 1998 (S16, #2980) and November 10 2003 (S21, #3946), there were clip shows celebrating the 3000th and 4000th show milestones. There are no actual puzzles that day and those dates are omitted from this verison of the compendium entirely.
-On November 16 2005 (S23, #4338), R1 was edited out of the final cut to Hurricane Katrina. This row is omitted from this version of the compendium entirely, as unlike the PREGNANT puzzle we have no idea what the puzzle was.
-There are a relative handful of confirmed puzzles with incomplete data, or from shows where all the puzzles are not known. These are listed separately on <https://buyavowel.boards.net/page/compendiummisc>, outside the scope of this bot.
"""
_HEADER_ROW = ['PUZZLE', 'CATEGORY', 'DATE USED', 'WHEN USED']


async def dl_season(season: int, asession: AsyncHTMLSession = None):
    if not asession:
        asession = AsyncHTMLSession()

    page = await asession.get(f'https://buyavowel.boards.net/page/compendium{season}')

    if page.status_code != 200:
        if page.status_code == 403:
            try:
                proxies = {"http": os.environ['QUOTAGUARDSTATIC_URL'], "https": os.environ['QUOTAGUARDSTATIC_URL']}
                page = await asession.get(f'https://buyavowel.boards.net/page/compendium{season}', proxies=proxies)
                _log.info('Used a QGS request')
            except KeyError:
                raise ValueError(f'Could not find webpage on my end for S{season}. Getting status code {page.status_code}.')
            finally:
                if page.status_code != 200:
                    if page.status_code == 403:
                        raise ValueError(
                            f"The IP I'm currently on is banned by Proboards. I tried a static IP (limited to 250 free uses a month) as backup and it also seems to be banned (or Wayo goofed some code somewhere, as he is wont to do). Ask Wayoshi to manually restart the bot, as this should change the IP. The daily restart at 5:30am Eastern US time should also have the same effect."
                        )
                    else:
                        raise ValueError(
                            f'Could not find webpage on my end for S{season}. Getting status code {page.status_code}. (Used a static IP call)'
                        )
        else:
            raise ValueError(f'Could not find webpage on my end for S{season}. Getting status code {page.status_code}.')

    table = page.html.find('div.widget-content.content > table > tbody', first=True)
    if not table:
        raise ValueError(
            f'Could not find puzzle HTML table for S{season}. I am looking for this HTML element selector: `div.widget-content.content > table > tbody` (a `div` with both the widget-content and content classes, with a `table` as a direct child, which in turn has a `tbody` as a direct child)'
        )

    with StringIO() as s:
        f = csv.writer(s)
        f.writerow(['DATE', 'EP', 'UNC', 'ROUND', 'EXTRA', 'PUZZLE', 'CATEGORY', 'BONUS'])

        trs = table.find('tr')
        if not trs:
            raise ValueError('Could not find any rows (`<tr>` HTML elements) in table.')

        for row in trs[1:]:
            # a few seasons give really long rows at end without [:4], not sure why. also bold/italic parsing.
            # B ̲  B̲
            r = row.find('td')
            if not r or len(r) < 4:
                raise ValueError(f'Could not find enough `<td>` (cell) HTML elements in a puzzle row:\n\n`{row.full_text}`')

            # if ((h := r[0].html).find('b')) != -1:
            #     puzzle_bold = re.fullmatch('<td align="center">(.+?)(?:<br/><i>(.+?)</i>)?</td>', h)
            #     puzzle = html.unescape(re.sub("<b>([A-Z ]+)</b>", lambda m : ''.join([l + '̲' if l.isalpha() else l for l in m.group(1)]), puzzle_bold.group(1)))
            #     if (a := puzzle_bold.group(2)):
            #         puzzle += '\n' + html.unescape(a)
            # else:
            puzzle = r[0].text
            if not puzzle:
                break

            if season == 25 and r[1].find('i'):
                category = 'People™'
            else:
                category = r[1].text

            date_used, when_used = [td.text for td in r[2:4]]

            if puzzle.startswith('***'):  # Katrina puzzle
                continue

            m = re.match(r'(\d{1,2}/\d{1,2}/\d{2}) \(#(\d+)\)(\*?)', date_used)
            if m:
                date_, showno, uncertain = m.groups()

                if showno in ('2980', '3946'):
                    continue

                round_ = when_used[:2]
                round_extra = when_used[2:]

                if '\n' in puzzle:
                    puzzle, answer = puzzle.split('\n')
                    answer = answer[1:-1]  # no ()
                else:
                    answer = ''

                f.writerow([date_, showno, uncertain, round_, round_extra, puzzle, category.upper(), answer.upper()])
            elif (tdt := [td.text for td in r]) != _HEADER_ROW:
                raise ValueError(f'Row not parseable for S{season}: {tdt}')

        await asyncio.to_thread(dropboxwayo.upload, s.getvalue().encode(), f'/heroku/wayo-py/compendium/s{season:02d}.csv')


class WheelCompendium:
    _MAX_CACHE = 64
    _CACHE_GETTER = attrgetter('cache')

    def __init__(self, *, loop=None, debug: bool = False):
        self._loop = loop
        self._debug = debug
        self.cache = LFUCache(self._MAX_CACHE)

        self.df = None
        self._df_dict = SortedDict()
        self.coverage = None
        self._coverage_dict = SortedDict()

        self._cols = ['S', 'DATE', 'EP', 'E/S', 'UNC', 'ROUND', 'PP', 'RL', 'PR', 'PUZZLE', 'CATEGORY', 'CLUE/BONUS']

        load = self.load(range(1, CURRENT_SEASON + 1))
        if self._loop:
            self._loop.create_task(load)
        else:
            asyncio.run(load)

    @property
    def seasons(self):
        return self._df_dict.keys()

    # def slot_query(self, endpoints : Portion, )

    async def load(self, seasons: Range):
        _log.info('start loading wc at ' + str(datetime.now()))
        changed_cov = await asyncio.gather(*(self._load_season(s) for s in seasons))
        self.df = pl.concat(pl.collect_all(self._df_dict.values())).lazy()
        if any(changed_cov):
            self._reset_coverage()
        _log.info('end loading wc at ' + str(datetime.now()))

    def _reset_coverage(self):
        self.coverage = pl.from_dict({'S': self._coverage_dict.keys(), 'COV': self._coverage_dict.values()})
        self.coverage = self.coverage.with_column(
            pl.when(~pl.col('S').is_in([16, 21, 37, CURRENT_SEASON]))
            .then(pl.lit(195))
            .otherwise(
                pl.when(pl.col('S') == 37)
                .then(pl.lit(167))
                .otherwise(pl.when(pl.col('S') == CURRENT_SEASON).then(pl.col('COV')).otherwise(pl.lit(194)))
            )
            .alias('MAX')
        )
        self.calc_coverage.cache_clear(self)

    @cachedmethod(_CACHE_GETTER, key=partial(hashkey, 'calc_cov'))
    def calc_coverage(self, seasons, doRange: bool = False):
        if seasons:
            if doRange:
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
            c_df = self.coverage.filter(pl.col('S').is_in(seasons))
        else:
            c_df = self.coverage

        if c_df.height > 1:
            c_df = pl.concat(
                [
                    c_df.with_column(pl.col('S').cast(str)),
                    c_df.select([pl.lit('ALL').alias('S'), pl.col('COV').sum(), pl.col('MAX').sum()]),
                ]
            )

        return c_df.with_column((100.0 * pl.col('COV') / pl.col('MAX')).round(1).alias('PCT'))

    async def _load_season(self, season: int) -> bool:
        try:
            if self._debug:
                async with aiofile.async_open(
                    os.path.expanduser(f'~/Dropbox/heroku/wayo-py/compendium/s{season:02d}.csv')
                ) as afp:
                    file = await afp.read()
            else:
                file = await asyncio.to_thread(dropboxwayo.download, f'/heroku/wayo-py/compendium/s{season:02d}.csv')
        except Exception as e:
            # _log.warning(e)
            location = 'locally' if self._debug else 'from Dropbox'
            _log.warning(f'Could not download S{season:02d} {location}')
            return

        df = pl.read_csv(
            file.encode() if self._debug else file,
            # to do complicated groupbys, 64 byte preferred..
            dtypes={'EP': pl.UInt64, 'ROUND': pl.Categorical},
        )

        # sanity checks
        unique_dates = df.select(pl.col("DATE").str.strptime(pl.Date, "%m/%d/%y").unique())
        unique_eps = df.select(pl.col("EP").unique())
        if len(unique_dates) != len(unique_eps):
            _log.warning(
                f'dates / ep mismatch for S{season}: {len(unique_dates)} unique dates, {len(unique_eps)} unique eps'
            )
        unique_dates = unique_dates.select((pl.all(), pl.col('DATE').dt.weekday().alias('WD')))
        if unique_dates.select(pl.col('WD').unique()).to_series().max() >= 5:
            weekend_dates = unique_dates.filter(pl.col('WD') >= 5)
            if weekend_dates[0, 0] != date(2016, 11, 12):
                raise ValueError(
                    f'In the compendium, season {season} has invalid (weekend) dates: '
                    + str(weekend_dates.to_series().to_list())
                )
        if len(unique_dates) > 195:
            raise ValueError(f'In the compendium, season {season} has too many dates ({len(unique_dates)})')

        # passed checks.
        old_cov = self._coverage_dict.get(season, 0)
        new_cov = self._coverage_dict[season] = len(unique_dates)  # len(df.select(pl.col('EP').unique()))

        c_exprs = [
            pl.lit(season).alias('S').cast(pl.UInt8),
            # to do complicated groupbys, must be at least 32 byte int.
            ((((pl.col('EP') - 1) % 195) + 1) if season <= 36 else pl.col('EP').rank('dense')).alias('E/S').cast(pl.UInt64),
            pl.col("DATE").str.strptime(pl.Date, "%m/%d/%y"),
            pl.col("UNC").is_not_null(),
            pl.col('ROUND').cat.set_ordering('lexical'),
        ]

        if 11 <= season <= 12:
            c_exprs.append(
                (
                    pl.col('EXTRA').is_not_null()
                    & ~pl.col('CATEGORY').str.contains(
                        '(MEGA|WHERE|CLUE|FILL|BLANK|NEXT|SLOGAN|WHERE|WHO|WHAT ARE WE|WHAT\'S)'
                    )
                    & pl.col('BONUS').str.contains('^\w+$')
                ).alias('RL')
            )
        else:
            c_exprs.append(pl.lit(False).alias('RL'))

        if season >= 21 or season == 15:
            c_exprs.append(pl.col('EXTRA').str.contains(r'\*').fill_null(False).alias('PP'))
        else:
            c_exprs.append(pl.lit(False).alias('PP'))

        if 15 <= season <= 17:
            c_exprs.append(pl.col('EXTRA').str.ends_with("'").fill_null(False).alias('PR'))
        else:
            c_exprs.append(pl.lit(False).alias('PR'))

        if season >= 33:
            w = pl.when(pl.col('EXTRA').str.ends_with('^'))
            c_exprs.extend(
                [
                    w.then(pl.col('CATEGORY')).otherwise(pl.lit('')).alias('CLUE/BONUS'),
                    w.then(pl.lit('CROSSWORD'))
                    .otherwise(pl.col('CATEGORY'))
                    .alias('CATEGORY')
                    .cast(pl.Categorical)
                    .cat.set_ordering('lexical'),
                ]
            )
        else:
            c_exprs.extend(
                [
                    pl.col('CATEGORY').cast(pl.Categorical).cat.set_ordering('lexical'),
                    pl.col('BONUS').fill_null('').alias('CLUE/BONUS'),
                ]
            )

        # keep lazy.
        self._df_dict[season] = (
            df.lazy().with_columns(c_exprs).select(pl.col(self._cols)).rename({'ROUND': 'RD', 'UNC': 'UC'})
        )

        return old_cov != new_cov

    def __str__(self):
        return str(self.df)
