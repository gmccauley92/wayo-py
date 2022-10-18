import asyncio
import re
from datetime import *
from typing import Literal

import orjson
import pytz
from dateutil.parser import isoparse
from discord import app_commands
from discord.ext import commands

from util import tz_autocomplete

_CHANNEL_MAPPING = {'barker': 1025, 'price': 1025, 'tbe': 1025, 'tpir': 1025, '1025': 1025}


class PSFlags(commands.FlagConverter, delimiter='=', case_insensitive=True):
    channel: Literal[tuple(_CHANNEL_MAPPING.keys())] = commands.flag(aliases=['ch', 'c'], default='barker')
    timezone: str = commands.flag(aliases=['tz'], default='US/Eastern')


class PlutoCog(commands.Cog, name='Pluto'):
    """Commands related to Pluto TV channels."""

    def __init__(self, bot):
        self.bot = bot
        self._pluto_api_url_template = 'http://api.pluto.tv/v2/channels?start={}&stop={}'

    async def _get_pluto_sched(self, channel: int, tz: pytz.timezone):
        now = datetime.now(tz=tz)
        url = self._pluto_api_url_template.format(
            now.isoformat(timespec='milliseconds'), (now + timedelta(hours=12)).isoformat(timespec='milliseconds')
        ).replace('+', '%2B')

        r = await self.bot.session.get(url)
        j = await r.json(loads=orjson.loads)

        for jj in j:
            if jj['number'] == channel:
                return jj['timelines']

        return None

    @commands.hybrid_command(aliases=['plutosched', 'pluto_schedule', 'pluto_sched', 'ps'])
    @app_commands.describe(
        channel="Which channel to get schedule from.",
        timezone='Time zone to render schedule in. Default US/Eastern. Any standard tz database value can be used.',
    )
    @app_commands.autocomplete(timezone=tz_autocomplete)
    async def plutoschedule(self, ctx, *, options: PSFlags):
        """Fetches listings for a Pluto channel for the next 12-13 hours, listed in the given time zone.

        Channel must be one of the following, (including channel numbers, case insensitive):
        -1025, ("barker", "tpir", "price", "tbe"), for TPiR: The Barker Era (current default bot-wide)
        -10xx, "wheel", for Wheel (coming Aug 1 2022 tentatively depending upon schedule info given)
        -10xx, "j", for Jeopardy (coming Aug 1 2022 tentatively depending upon schedule info given)

        Valid time zones can be found at https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"""

        cn = _CHANNEL_MAPPING.get(options.channel)

        if not cn:
            await ctx.send('Unsupported channel for this command. See `!help plutoschedule` for supported channels.')
            return

        async with ctx.typing():
            tz = pytz.timezone(options.timezone)
            listings = await self._get_pluto_sched(cn, tz)

        if listings:
            if cn == 1025:
                ss = [
                    isoparse(jjj['start']).astimezone(tz).strftime('%I%p')
                    + ': '
                    + (
                        m.group(1) + 'D'
                        if (m := re.search(r'\(S\d{2}E(\d+)\)', jjj['episode']['description']))
                        else jjj['episode']['description'][:10]
                    )
                    + f" (Pluto #{jjj['episode']['number']:03d})"
                    for jjj in listings
                ]
                ss = '\n'.join('`' + (scs[1:] if scs[0] == '0' else scs) + '`' for scs in ss)
                await ctx.send(f'>>> {ss}')
            else:
                pass
        else:
            await ctx.send("`Couldn't find channel in listings.`")

    async def cog_command_error(self, ctx, e):
        if isinstance(e, commands.BadArgument):
            if isinstance(e.__cause__, pytz.exceptions.UnknownTimeZoneError):
                await ctx.send('`Invalid time zone.`', ephemeral=True)
            elif isinstance(e.__cause__, commands.BadLiteralArgument):
                el = ', '.join(e.__cause__.literals)
                await ctx.send(f'Unsupported channel for this command. Supported channels are: `{el}`')
            else:
                await ctx.send(f'`{e}`', ephemeral=True)
        elif isinstance(e, commands.CommandError) and isinstance(e.__cause__, pytz.exceptions.UnknownTimeZoneError):
            await ctx.send('`Invalid time zone.`', ephemeral=True)
        else:
            await ctx.send(f'`{e}`', ephemeral=True)


async def setup(bot):
    p = PlutoCog(bot)
    await bot.add_cog(p)


if __name__ == '__main__':
    import requests

    # copy _get_pluto_sched but no await
    now = datetime.now(tz=pytz.timezone('US/Eastern'))
    url = 'http://api.pluto.tv/v2/channels?start={}&stop={}'.format(
        now.isoformat(timespec='milliseconds'), (now + timedelta(hours=12)).isoformat(timespec='milliseconds')
    ).replace('+', '%2B')
    import pyperclip

    t = requests.get(url).text
    pyperclip.copy(t)
    j = orjson.loads(t)
    for jj in j:
        if jj['number'] == 1010:
            print(jj['timelines'])
            pyperclip.copy(str(jj['timelines']))
            break
    else:
        print('Channnel not found')
