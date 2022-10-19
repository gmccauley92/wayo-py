from discord.ext import commands
import discord, random, asyncio
from clue import *
from util import SuspectConverter, WeaponConverter, RoomConverter, send_PIL_image
from sortedcontainers import SortedDict


class ClueCog(commands.Cog, name='Clue'):
    """Commands related to an active game of Clue."""

    def __init__(self, bot):
        self.bot = bot
        self.vc = None
        self.debug = False
        self.active_games = {}
        self.valid_options = frozenset(('twodice', 'nohints'))

    @commands.command(hidden=True)
    @commands.is_owner()
    async def debugclue(self, ctx, arg):
        self.debug = arg == 'on'
        await ctx.send('debug turned {}.'.format('on' if self.debug else 'off'))

    @commands.Cog.listener()
    async def on_ready(self):
        self.wayo_user = (await self.bot.application_info()).owner

    async def cog_check(self, ctx):
        if ctx.channel.id in self.active_games:
            a = self.active_games[ctx.channel.id]['begun']
            b = ctx.command.name.endswith('clue')
            if b:
                return not a
            else:
                return a and self.active_games[ctx.channel.id]['cg'].cur_player.id == ctx.author
        else:
            return ctx.command.hidden

    async def set_vc(self, host_user):
        try:
            vc = host_user.voice.channel
            if not vc:
                raise AttributeError
            self.vc = await vc.connect(reconnect=False)
        except AttributeError:
            return 'You are not currently in a voice channel for me to join, not starting game.'
        except discord.ClientException:
            return "I'm already busy in another voice channel, sorry! Not starting game."
        except asyncio.TimeoutError:
            return 'I timed out trying to join your voice channel, try again.'
        except discord.opus.OpusNotLoaded:
            return 'Voice not currently supported.'

    async def disconnect_voice(self):
        if self.vc:
            await self.vc.disconnect()
            self.vc = None

    async def play_clip_clue(self, fn, *, vi=0.5, vr_step=10, vr_factor=2, holdup=0):
        if self.vc:
            self.vc.stop()
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(f'clue_sounds{os.sep}{fn}.mp3'), vi)
            self.vc.play(source, after=lambda e: print('Player error: %s' % e) if e else None)

            async def vr():
                try:
                    while self.vc.is_playing():
                        await asyncio.sleep(vr_step)
                        source.volume /= vr_factor
                except:
                    pass

            self.loop.create_task(vr())
        if holdup:
            await asyncio.sleep(holdup)

    async def register(self, ctx, options):
        if self.debug:
            print(options)

        if 'all' in options:
            # options = ('voice', 'twodice', 'nohints')
            options = self.valid_options

        if 'voice' in options:
            e = await self.set_vc(ctx.author)
            if e:
                await ctx.send(str(e))
                return False

        self.active_games[ctx.channel.id] = {'begun': False, 'owner': ctx.author}
        cg = self.active_games[ctx.channel.id]['cg'] = ClueGame(dieCount=2 if 'twodice' in options else 1)
        self.active_games[ctx.channel.id]['ct'] = ClueText()
        self.active_games[ctx.channel.id]['sug_count'] = 1
        self.active_games[ctx.channel.id]['options'] = options
        self.active_games[ctx.channel.id]['cur_player'] = None

        if self.debug:
            players = self.active_games[ctx.channel.id]['players'] = SortedDict({s: ctx.author for s in list(Suspect)})
            cg.start(players=players)
            for p in cg.players:
                cg.board.player_positions[p.suspect] = random.choice(list(Room))

            await ctx.send(
                '```Answer: '
                + str(cg.answer)
                + '\n'
                + '\n'.join(str(cp) for cp in cg.players)
                + '\n\n'
                + 'now use !beginclue```'
            )
        else:
            self.active_games[ctx.channel.id]['players'] = SortedDict()
            await self.play_clip_clue('Mr. Boddy', vi=0.25, vr_step=60)
            # gather players
            await ctx.send(
                'Play now by typing `!playclue` then one of {} (Caps-insensitive)'.format(
                    ', '.join('`' + s.name + '`' for s in cg.suspects)
                )
            )
            if o := set(options) & self.valid_options:
                await ctx.send('Set options: ' + ', '.join(o))

        return True

    @commands.command()
    async def playclue(self, ctx, s: SuspectConverter):
        """Join a Clue game's list of players."""
        if (
            s not in self.active_games[ctx.channel.id]['players']
            and ctx.author not in self.active_games[ctx.channel.id]['players'].values()
        ):
            self.active_games[ctx.channel.id]['players'][s] = ctx.author
            e = discord.Embed(
                description='\n'.join(
                    f'{s}: {a.display_name}' for s, a in self.active_games[ctx.channel.id]['players'].items()
                )
            )
            await ctx.send(f'{ctx.author.display_name}, {s}: :white_check_mark:', embed=e)

    @commands.command()
    async def removeclue(self, ctx):
        """Remove yourself from an active Clue game's list of players."""
        for sr, a in self.active_games[ctx.channel.id]['players'].items():
            if ctx.author == a:
                del self.active_games[ctx.channel.id]['players'][sr]
                e = discord.Embed(
                    description='\n'.join(
                        f'{s}: {a.display_name}' for s, a in self.active_games[ctx.channel.id]['players'].items()
                    )
                )
                await ctx.send(f'{ctx.author.display_name}, {sr}: :x: ', embed=e)
                break

    @commands.command()
    async def beginclue(self, ctx):
        if len(self.active_games[ctx.channel.id]['players']) < 3:
            await ctx.send('Not enough players yet.')
        elif self.active_games[ctx.channel.id]['owner'] != ctx.author:
            await ctx.send('Only the owner of the game can begin it.')
        else:
            if self.vc:
                self.vc.stop()

            if not self.debug:
                self.active_games[ctx.channel.id]['cg'].start(players=self.active_games[ctx.channel.id]['players'])

                player_count = len(self.active_games[ctx.channel.id]['players'])
                status_embed = discord.Embed(title=f'{player_count}-player game:')
                for cp in self.active_games[ctx.channel.id]['cg'].players:
                    status_embed.add_field(name=f'{cp.id.display_name} as {cp.suspect}', value=f'{len(cp.cards)} cards')

                info_message = self.active_games[ctx.channel.id]['info_message'] = await ctx.send(
                    'I apologize for any sexism in this playing. :womens:'
                    if not self.active_games[ctx.channel.id]['cg'].even()
                    else '',
                    embed=status_embed,
                )
                try:
                    await info_message.pin()
                except discord.Forbidden:
                    pass

                # send cards
                for cp in self.active_games[ctx.channel.id]['cg'].players:
                    try:
                        await cp.id.send(''.join(itertools.repeat('=THE=LINE=', 7)))
                        with ClueCard.multicard_image(cp.cards) as i:
                            await send_PIL_image(cp.id, i, f'{cp.id.display_name}_{cp.suspect.name}_cards')
                    except discord.Forbidden:
                        await ctx.send(
                            f'{cp.id.display_name} cannot receive DMs from me, aborting game. Please use `!cancelgame`.'
                        )
                        return

                # spy my die
                if self.wayo_user not in [cp.id for cp in self.active_games[ctx.channel.id]['cg'].players]:
                    await self.wayo_user.send(
                        'INFO FOR NON-WAYO GAME',
                        embed=discord.Embed(
                            title='WHO HAD WHAT',
                            description='\n'.join(
                                f'{cp.id.display_name} as {cp.suspect}: {" ".join(c.name for c in cp.cards)}'
                                for cp in sorted(self.active_games[ctx.channel.id]['cg'].players, key=lambda cp: cp.suspect)
                            )
                            + '\nANSWER: '
                            + " ".join(c.name for c in self.active_games[ctx.channel.id]['cg'].answer),
                        ),
                    )

            self.active_games[ctx.channel.id]['begun'] = True

    @commands.command()
    async def endturn(self, ctx):
        self.active_games[ctx.channel.id]['cg'].endturn()

    @commands.command()
    async def roll(self, ctx):
        result = self.active_games[ctx.channel.id]['cg'].roll()
        await ctx.channel.send(self.active_games[ctx.channel.id]['cur_player'].id.display_name + f' rolled a {result}.')

    @commands.command()
    async def move(self, ctx, *moves):
        """one or more pairs of directions and step(s) to take in that direction, adding up to the immediately previous roll.
        If in a room with more than one door and rolling out, first specify which door according to the given map image."""
        # use cg_action here for move conversion
        _, move_args = self.active_games[ctx.channel.id]['cg'].translate('move ' + ' '.join(m.upper() for m in moves))
        # print(move_args)
        r = self.active_games[ctx.channel.id]['cg'].move(*move_args)
        if r:
            await self.announce_room(ctx, r)
        else:
            self.active_games[ctx.channel.id]['cg'].endturn()

    @commands.command()
    async def secret(self, ctx):
        await self.announce_room(ctx, self.active_games[ctx.channel.id]['cg'].secret())

    async def announce_room(self, ctx, r):
        extra = f'\n:musical_note: with {"i"*random.randint(5,30)}King! :musical_note:' if r is Room.LOUNGE else ''
        await ctx.channel.send(self.active_games[ctx.channel.id]['cur_player'].id.display_name + f' entered the {r}.{extra}')

    @commands.command()
    async def suggest(self, ctx, suspect: SuspectConverter, weapon: WeaponConverter):
        """if you are the first to disprove, I'll DM you which card(s) you can send.
        You must confirm by DMing me a message containing which card to send before the game can move forward.
        Creativity is allowed, any message containing one and exactly one of the eligible cards will be accepted."""
        suggestion = (
            suspect,
            weapon,
            self.active_games[ctx.channel.id]['cg'].board.player_positions[
                self.active_games[ctx.channel.id]['cur_player'].suspect
            ],
        )
        hint, disprove_cp, disprove_options = self.active_games[ctx.channel.id]['cg'].suggest(suspect, weapon)
        sug_count = self.active_games[ctx.channel.id]['sug_count']

        cp_embed = discord.Embed()
        if 'nohints' not in self.active_games[ctx.channel.id]['options']:
            cp_embed.title = hint
        cp_embed.description = ''
        cp_embed.set_footer(text=f'SUGGESTION {sug_count}')
        cp_iter = itertools.takewhile(lambda p: p is not disprove_cp, self.active_games[ctx.channel.id]['cg'].players)
        next(cp_iter)  # flush cur_player out
        sug_description = [f'{cp.id.display_name} cannot disprove.' for cp in cp_iter]
        if disprove_cp:
            sug_description.append(f'{disprove_cp.id.display_name} CAN disprove.')

        with ClueCard.multicard_image(suggestion) as i:
            await send_PIL_image(ctx.channel, i, f'suggestion{sug_count}')

        async with ctx.typing():
            await self.play_clip_clue(
                f'Room {suggestion[2].ambience}' if random.random() < 0.5 else str(suggestion[0]), holdup=10
            )
            if 'NOT' not in hint and 'nohints' not in self.active_games[ctx.channel.id]['options']:
                await self.play_clip_clue('Shock!', vi=1.5)

            sug_message = await ctx.channel.send(embed=cp_embed)
            for i in range(1, len(sug_description) + 1):
                cp_embed.description = '\n'.join(sug_description[:i])
                await asyncio.sleep(5)
                await sug_message.edit(embed=cp_embed)

        if disprove_cp:
            card_options = [c.name for c in disprove_options]
            d = await disprove_cp.id.send(
                'You must disprove (one of) the following to '
                + self.active_games[ctx.channel.id]['cur_player'].id.display_name
                + f': {" ".join(card_options)}'
            )
            cards_revealed = lambda mes: sum(bool(re.search(c, mes.content, re.I)) for c in card_options)
            mes = await self.bot.wait_for(
                'message', check=lambda m: m.channel == d.channel and m.author == disprove_cp.id and cards_revealed(m) == 1
            )
            for c in disprove_options:
                if re.search(c.name, mes.content, re.I):
                    await self.active_games[ctx.channel.id]['cur_player'].id.send(
                        f"{mes.content} | {disprove_cp.id.display_name}'s disproval for suggestion {sug_count}: {c.name}"
                    )
                    break
        else:
            await ctx.channel.send(':notes: ***END IT NOW, END IT NOW*** :notes:')

        self.active_games[ctx.channel.id]['sug_count'] += 1

    @commands.command()
    async def accuse(self, ctx, suspect: SuspectConverter, weapon: WeaponConverter, room: RoomConverter):
        """Be sure you're right or it's game over!"""
        accusation = (suspect, weapon, room)
        result = self.active_games[ctx.channel.id]['cg'].accuse(suspect, weapon, room)

        cp_embed = discord.Embed()

        with ClueCard.multicard_image(accusation) as i:
            await send_PIL_image(
                ctx.channel, i, 'accusation_' + self.active_games[ctx.channel.id]['cur_player'].id.display_name
            )

        texts = self.active_games[ctx.channel.id]['ct'].generate(
            accusation, self.active_games[ctx.channel.id]['cur_player'].suspect
        )
        cp_embed.title = ''
        cp_embed.description = texts[0]
        cp_embed.color = suspect.color
        e = await ctx.channel.send(embed=cp_embed)
        async with ctx.typing():
            await self.play_clip_clue(f'Room {room.ambience}', holdup=8)
            cp_embed.description += '\n' + texts[1]
            await e.edit(embed=cp_embed)
            await self.play_clip_clue(f'{suspect}', holdup=8)
            cp_embed.description += '\n' + texts[2]
            await e.edit(embed=cp_embed)
            await self.play_clip_clue('Shock!', holdup=2)

        if result:
            e = await ctx.channel.send(texts[3])
            cp_embed.description = ''
            await self.play_clip_clue('Terror!', holdup=3)
            for t in texts[4:-1]:
                cp_embed.description += '\n' + t
                await e.edit(embed=cp_embed)
                await asyncio.sleep(3)
            cp_embed.description = texts[-1]
            cp_embed.color = self.active_games[ctx.channel.id]['cur_player'].suspect.color
            with self.active_games[ctx.channel.id]['cur_player'].suspect.mosaic_image('win') as i:
                await send_PIL_image(ctx.channel, i, self.active_games[ctx.channel.id]['cur_player'].suspect.name + '_win')
            await ctx.channel.send(
                self.active_games[ctx.channel.id]['cur_player'].id.display_name + ' wins!', embed=cp_embed
            )
            await self.play_clip_clue('Elementary', holdup=20, vr_step=20)
            await ctx.channel.send(
                'gg shitheads!' if random.random() < 0.25 else 'Good game everyone!',
                embed=discord.Embed(
                    title='WHO HAD WHAT',
                    description='\n'.join(
                        f'{cp.id.display_name} as {cp.suspect}: {" ".join(c.name for c in cp.cards)}'
                        for cp in sorted(self.active_games[ctx.channel.id]['cg'].players, key=lambda cp: cp.suspect)
                    )
                    + '\nANSWER: '
                    + " ".join(c.name for c in self.active_games[ctx.channel.id]['cg'].answer),
                ),
            )
            await asyncio.sleep(10)
        else:
            with self.active_games[ctx.channel.id]['cur_player'].suspect.mosaic_image('gameover') as i:
                await send_PIL_image(
                    ctx.channel, i, self.active_games[ctx.channel.id]['cur_player'].suspect.name + '_gameover'
                )
            cp_embed.title = 'There is evidence that you are wrong!'
            cp_embed.description = ''
            cp_embed.color = self.active_games[ctx.channel.id]['cur_player'].suspect.color
            m = await ctx.channel.send(embed=cp_embed)
            await self.play_clip_clue('Disbelief!', holdup=4)
            cp_embed.description = 'GAME OVER ' + str(self.active_games[ctx.channel.id]['cur_player'].suspect)
            await m.edit(embed=cp_embed)
            await self.play_clip_clue('A Flaw in Your Theory', holdup=5)
            if self.active_games[ctx.channel.id]['cg'].accuseCount < len(self.active_games[ctx.channel.id]['cg'].players):
                self.active_games[ctx.channel.id]['cg'].endturn()
            else:
                await ctx.channel.send('... :disappointed:')
                await asyncio.sleep(5)
                await ctx.channel.send(
                    'GAME OVER',
                    embed=discord.Embed(
                        title='WHO HAD WHAT',
                        description='\n'.join(
                            f'{cp.id.display_name} as {cp.suspect}: {" ".join(c.name for c in cp.cards)}'
                            for cp in sorted(self.active_games[ctx.channel.id]['cg'].players, key=lambda cp: cp.suspect)
                        )
                        + '\nANSWER: '
                        + " ".join(c.name for c in self.active_games[ctx.channel.id]['cg'].answer),
                    ),
                )

    @commands.command(aliases=['showmethefuckingboardyoushit'])
    async def viewboard(self, ctx):
        """View the entire game board, zoomed out. can be done anytime it's your turn."""
        with self.active_games[ctx.channel.id]['cg'].board.image() as i:
            await send_PIL_image(ctx.channel, i, 'board_full')

    async def cog_after_invoke(self, ctx):
        if not ctx.command.name.startswith('debug') and self.active_games[ctx.channel.id]['begun']:
            if self.active_games[ctx.channel.id]['cg'].next_options:
                if self.active_games[ctx.channel.id]['cur_player'] != self.active_games[ctx.channel.id]['cg'].cur_player:
                    with self.active_games[ctx.channel.id]['cg'].board_zoomed_image() as i:
                        await send_PIL_image(
                            ctx.channel,
                            i,
                            'board_' + self.active_games[ctx.channel.id]['cg'].cur_player.suspect.name + '_zoomed',
                        )
                    self.active_games[ctx.channel.id]['cur_player'] = self.active_games[ctx.channel.id]['cg'].cur_player

                cp_embed = discord.Embed(color=self.active_games[ctx.channel.id]['cur_player'].suspect.color)

                cp_embed.title = 'Current player: ' + self.active_games[ctx.channel.id]['cur_player'].id.display_name
                if (
                    type(
                        self.active_games[ctx.channel.id]['cg'].board.player_positions[
                            self.active_games[ctx.channel.id]['cur_player'].suspect
                        ]
                    )
                    is Room
                ):
                    cp_embed.title += ' in ' + str(
                        self.active_games[ctx.channel.id]['cg'].board.player_positions[
                            self.active_games[ctx.channel.id]['cur_player'].suspect
                        ]
                    )
                cp_embed.description = 'You can: ' + ', '.join(self.active_games[ctx.channel.id]['cg'].next_options)
                await ctx.channel.send(embed=cp_embed)
            else:  # close and cleanup.
                await self.disconnect_voice()
                if (
                    'info_message' in self.active_games[ctx.channel.id]
                    and self.active_games[ctx.channel.id]['info_message'].pinned
                ):
                    await self.active_games[ctx.channel.id]['info_message'].unpin()
                del self.active_games[ctx.channel.id]
                self.bot.get_cog('Game').naturalendgame()

    async def cog_command_error(self, ctx, error):
        e = error.__cause__
        if ctx.command.name in ('move', 'suggest', 'accuse') and isinstance(
            e, (ValueError, KeyError, AssertionError, IndexError)
        ):
            await ctx.channel.send('Improperly formatted action, try again. The problem was: `' + str(e) + '`')


async def setup(bot):
    await bot.add_cog(ClueCog(bot))
