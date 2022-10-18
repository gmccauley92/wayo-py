# putty root@208.167.253.69 -pw Tk1+GkxmQq+$AXs$
# immortal -d wayo-py -l log.log python3.10 wayo.py
# https://janakiev.com/blog/python-background/

import asyncio
import io
import itertools
import re
import sys
import logging
import traceback

import aiohttp
import discord

from discord.ext import commands
from datetime import *

import pandas as pd
import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from requests_html import AsyncHTMLSession

from dropboxwayo import dropboxwayo
from util import SCHEDULER_TZ

DEBUG = 'DEBUG' in sys.argv
_log = logging.getLogger('wayo_log')


class WayoPyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix='$' if DEBUG else '!',
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            intents=intents,
        )
        self.ready = False
        self.initial_extensions = (
            'cog_help',
            'cog_joke',
            'cog_tpir',
            'cog_playalong',
            'cog_game',
            'cog_clue',
            'cog_mod',
            'cog_background',
            'cog_pluto',
            'cog_compendium',
            'cog_lineup',
        )
        self.pastebin_key = None

    async def on_ready(self):
        if self.ready:
            return
        _log.info('Python {0}\ndiscord.py {1}'.format(sys.version_info, discord.version_info))
        self.ac = await self.application_info()
        self.owner = self.ac.owner

        self.dig_emoji = self.get_emoji(350882871743086592)
        self.x_emoji = self.get_emoji(894575889629736980)

        _log.info(f'fetched owner as {self.owner}, dig_emoji as {self.dig_emoji}, x_emoji as {self.x_emoji}')
        _log.info('Ready as {0} (ID {1}) at {2}'.format(self.user.name, self.user.id, datetime.now()))
        self.ready = True

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.asession = AsyncHTMLSession()
        self.SCHEDULER = AsyncIOScheduler(timezone=SCHEDULER_TZ)
        self.SCHEDULER.start()

        if not DEBUG:
            # @self.scheduler.scheduled_job('date', run_date=datetime.now(tz=self.usetz) + dt)
            @self.SCHEDULER.scheduled_job('cron', hour='5', minute='30')
            async def timed_quit():
                try:
                    await self.close()
                except:
                    pass

            _log.info('timed_quit set up')
            pass
        else:
            self.loop.set_debug(True)
            _log.info('Local debug is on. timed_quit not set up')

        await self.login_pastebin()

        for ext in self.initial_extensions:
            await self.load_extension(ext)

        # build quick sub dict
        self.sub_dict = {}
        for c in self.walk_commands():
            if c.root_parent:
                self.sub_dict[c.name] = c.qualified_name
                for a in c.aliases:
                    self.sub_dict[a] = c.qualified_name

    async def login_pastebin(self):
        login_data = {
            'api_dev_key': '...',   # redacted
            'api_user_name': 'Wayoshi',
            'api_user_password': '...', # redacted
        }

        login = await self.session.post("https://pastebin.com/api/api_login.php", data=login_data)
        if login.status == 200:
            self.pastebin_key = await login.text()
            _log.info(f"Got pastebin token as: {self.pastebin_key}")
        else:
            self.pastebin_key = None
            _log.info(f"Failed to get pastebin token. login status was: {login.status}")

    async def do_pastebin(self, txt, fn):
        if self.pastebin_key:
            data = {
                'api_option': 'paste',
                'api_dev_key': '...',   # redacted
                'api_paste_code': txt,
                'api_paste_name': fn,
                'api_paste_private': 1,
                # 'api_paste_expire_date': '1H',
                'api_user_key': self.pastebin_key,
                'api_folder_key': 'pR23GEqX',
            }
            r = await self.session.post("https://pastebin.com/api/api_post.php", data=data)
            if r.status == 200:
                link = await r.text()
                slash_idx = link.rfind('/')
                return link[:slash_idx] + '/raw' + link[slash_idx:]
            else:
                return None
        else:
            return None

    async def on_command_error(self, ctx, error):
        if not issubclass(type(error), commands.CommandError):
            if hasattr(error, 'original'):
                original = error.original
                if not isinstance(original, AssertionError):
                    _log.error(f'In {ctx.command.qualified_name}: {original}')
                    traceback.print_tb(original.__traceback__)
            else:
                if ctx.command:
                    _log.error(f'In {ctx.command.qualified_name}: {error}')
                else:
                    _log.error(f'Non-command error: {error}')
                traceback.print_tb(error.__traceback__)

            if ctx.message:
                await self.owner.send(f'>>> Error from message {ctx.message.jump_url}\n`{error}`')
            else:
                await self.owner.send(f'>>> Error from not a message (ephemeral interaction?)\n`{error}`')
        else:
            if ctx.command:
                if DEBUG:
                    if hasattr(error, 'original'):
                        original = error.original
                        if not isinstance(original, AssertionError):
                            _log.error(f'In {ctx.command.qualified_name}: {original}')
                            traceback.print_tb(original.__traceback__)
                    else:
                        _log.error(error)
                        traceback.print_tb(error.__traceback__)
                else:
                    _log.info(f'Standard commands.CommandError in {ctx.command.qualified_name}: {error}')
            else:
                if isinstance(error, commands.CommandNotFound):
                    try:
                        com = re.match('Command "(.+?)" is not found', str(error)).group(1)
                        prefixes = await self.get_prefix(ctx.message)
                        if type(prefixes) is str:
                            prefixes = [prefixes]
                        if any(re.fullmatch(fr'\{p}+', com) for p in prefixes):
                            return
                        else:
                            q = self.sub_dict.get(com)
                            extra = f' Perhaps you meant `{q}`?' if q else ''
                    except:
                        extra = ''
                    await ctx.send(f'{error}.{extra}', ephemeral=True)
                else:
                    _log.info(f'\tStandard commands.CommandError, {error.__class__.__name__}: {error}', file=sys.stderr)

    async def close(self):
        await super().close()
        await self.session.close()
        await self.asession.close()
        self.SCHEDULER.shutdown(wait=False)
        for t in asyncio.all_tasks(self.loop):
            t.cancel()


WB = WayoPyBot()

# debug check
if DEBUG:

    @WB.check
    async def debug_check(ctx):
        return ctx.author == WB.owner


@WB.command(hidden=True)
@commands.is_owner()
@commands.dm_only()
async def login_pastebin(ctx):
    await ctx.message.add_reaction('🚧')
    stat = await asyncio.to_thread(WB.login_pastebin)
    await ctx.message.remove_reaction('🚧', ctx.bot.user)
    await ctx.message.add_reaction('✅' if WB.pastebin_key else '❌')


@WB.command(hidden=True)
@commands.is_owner()
@commands.dm_only()
async def reload_ext(ctx, cog):
    try:
        await ctx.bot.reload_extension(f'cog_{cog}')
        await ctx.message.add_reaction('✅')
    except commands.ExtensionError as e:
        await ctx.send('`{e}`')


# https://gist.github.com/AbstractUmbra/a9c188797ae194e592efe05fa129c57f?permalink_comment_id=4121434#gistcomment-4121434
from typing import Literal, Optional


@WB.command(hidden=True)
@commands.is_owner()
@commands.dm_only()
async def sync(ctx, guilds: commands.Greedy[commands.GuildConverter], spec: Optional[Literal["~", "~~"]] = None) -> None:
    if not guilds:
        if spec:
            if spec == '~~':
                ctx.bot.tree.copy_global_to(guild=ctx.guild)
            fmt = await ctx.bot.tree.sync(guild=ctx.guild)
        else:
            fmt = await ctx.bot.tree.sync()

        await ctx.send(
            f"Synced commands in the tree {'globally' if spec is None else 'to the current guild' + (' (global copied)' if spec == '~~' else '') + '.'}"
        )
        return

    assert guilds is not None
    # fmt = 0
    for guild in guilds:
        try:
            fmt = await ctx.bot.tree.sync(guild=guild)
            await ctx.send(f"Synced commands in the tree to {guild.name}.")
        except discord.HTTPException:
            await ctx.send(f"Failed to sync tree to {guild.name}.")


if __name__ == '__main__':
    pl.toggle_string_cache(True)

    (
        pl.cfg.Config
        .set_tbl_cols(-1)
        .set_tbl_rows(-1)
        .set_tbl_width_chars(1_000)
        .set_fmt_str_lengths(100)
        .set_tbl_formatting('NOTHING')
        .set_tbl_cell_alignment('RIGHT')
        .set_tbl_hide_dataframe_shape()
        .set_tbl_hide_column_data_types()
        .set_tbl_hide_column_separator()
    )

    # if sys.platform.startswith('linux') and datetime.now(tz=pytz.timezone('US/Eastern')).weekday() > 4:
    # quit()

    # https://stackoverflow.com/questions/27981545/suppress-insecurerequestwarning-unverified-https-request-is-being-made-in-pytho
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

    # def save_and_quit(signum, frame):
    # 	_log.info('SIGTERM received, quitting gracefully...')
    # 	WB.scheduler.shutdown(wait=False)
    # 	for t in asyncio.Task.all_tasks(WB.loop):
    # 		t.cancel()
    # 	_log.info('Graceful quit successful, exiting... (will potentially see some destroyed tasks)')

    # signal.signal(signal.SIGTERM, save_and_quit)
    # signal.signal(signal.SIGINT, save_and_quit)

    # logging
    # logger = logging.getLogger('wayo_log')
    lh = logging.StreamHandler(sys.stdout)
    _log.addHandler(lh)  # let discord library do log_formatter (commented out below)
    # dt_fmt = '%Y-%m-%d %H:%M:%S'
    # log_formatter = logging.Formatter('[{asctime}] [{levelname:<8}] {name} ({filename}, {lineno}): {message}', dt_fmt, style='{')
    # lh.setFormatter(log_formatter)
    _log.setLevel(logging.DEBUG if DEBUG else logging.INFO)

    # discord.py official 2.0 way to start a bot (cog loading abstracted to in-class)
    WB.run(
        '...',  # redacted
        log_handler=lh,
        log_level=logging.INFO,
    )
