from enum import Enum, unique
from abc import ABCMeta, abstractmethod
from collections import deque, Counter, OrderedDict, namedtuple
from PIL import Image, ImageDraw, ImageFont
from functools import wraps
from typing import *
from sortedcontainers import *
import random, itertools, copy, re, os


class ClueCard(Enum):
    def __str__(self):
        return self.value[0]

    def __repr__(self):
        return self.value[0]

    @property
    def rect_index(self):
        return self.value[1]

    @classmethod
    def image_size(cls):
        return (54, 62)

    def image(self):
        x0, y0 = (13, 12)
        xo, yo = self.value[1]
        w, h = ClueCard.image_size()
        x, y = x0 + xo * (w + 2), y0 + yo * (h + 2)
        return Image.open(f'clue_images{os.sep}{self.__class__.__name__.lower()}s.png').crop((x, y, x + w, y + h))

    @classmethod
    def multicard_image(cls, cards, magnitude=2):
        xc, yc = [round(magnitude * d) for d in ClueCard.image_size()]
        spacing = xc // 9
        i = Image.new('RGBA', (len(cards) * xc + spacing * (len(cards) - 1), yc), (0, 0, 0, 0))
        x0 = 0
        for c in cards:
            with c.image() as j:
                if magnitude != 1:
                    j = j.resize((xc, yc), Image.BILINEAR)
                i.paste(j, (x0, 0))
            x0 += xc + spacing
        return i


@unique
class Suspect(ClueCard):
    SCARLET = ('Ms. Scarlet', (0, 2), 0)
    MUSTARD = ('Col. Mustard', (0, 0), 1)
    WHITE = ('Mrs. White', (1, 2), 2)
    GREEN = ('Mr. Green', (0, 1), 3)
    PEACOCK = ('Mrs. Peacock', (1, 1), 4)
    PLUM = ('Prof. Plum', (1, 0), 5)

    def __ge__(self, other):
        return self.value[2] >= other.value[2] if self.__class__ is other.__class__ else NotImplemented

    def __gt__(self, other):
        return self.value[2] > other.value[2] if self.__class__ is other.__class__ else NotImplemented

    def __le__(self, other):
        return self.value[2] <= other.value[2] if self.__class__ is other.__class__ else NotImplemented

    def __lt__(self, other):
        return self.value[2] < other.value[2] if self.__class__ is other.__class__ else NotImplemented

    @property
    def color(self) -> int:
        with self.image() as i:
            r, g, b = i.getpixel((3, 3))
            return (r << 16) + (g << 8) + b

    def mosaic_image(self, mosaic, magnitude=2) -> Image:
        xo, yo = self.value[1]
        w, h = (187, 154)
        x0, y0 = (w * xo, h * yo)
        i = Image.open(f'clue_images{os.sep}{mosaic}.png').crop((x0, y0, x0 + w, y0 + h))
        if magnitude != 1:
            xc, yc = [round(magnitude * d) for d in i.size]
            i = i.resize((xc, yc), Image.BILINEAR)
        return i


class Weapon(ClueCard):
    KNIFE = ('Knife', (0, 0))
    REVOLVER = ('Revolver', (0, 1))
    PIPE = ('Lead Pipe', (0, 2))
    CANDLESTICK = ('Candlestick', (1, 0))
    ROPE = ('Rope', (1, 1))
    WRENCH = ('Wrench', (1, 2))


class Room(ClueCard):
    STUDY = ('Study', (2, 2), 'Quiet')
    HALL = ('Hall', (0, 0), 'Quiet')
    LOUNGE = ('Lounge', (1, 0), 'Stately')
    LIBRARY = ('Library', (1, 2), 'Stately')
    BILLIARD = ('Billiard Room', (0, 2), 'Quiet')
    DINING = ('Dining Room', (2, 0), 'Quiet')
    CONSERVATORY = ('Conservatory', (2, 1), 'Stately')
    BALL = ('Ballroom', (1, 1), 'Quiet')
    KITCHEN = ('Kitchen', (0, 1), 'Stately')

    @property
    def ambience(self) -> str:
        return self.value[2]


class ClueText:
    def __init__(self, fn: str = 'clue_text.txt') -> None:
        master_lists = [[]]
        for s in open(fn).read().split('\n'):
            if s:
                master_lists[-1].append(s)
            else:
                master_lists.append(list())
        self.room_text, self.suspect_text, self.weapon_text, self.murder_text = master_lists[:4]
        self.reaction_texts = master_lists[4:]

    def generate(self, accusation: Tuple[Suspect, Weapon, Room], accuser: Suspect) -> Sequence[str]:
        s, w, r = accusation
        generated = list()

        def flatten_index(c):
            x, y = c.rect_index
            return (3 if type(c) is Room else 2) * y + x

        generated.append(self.room_text[flatten_index(r)])
        si = flatten_index(s)
        generated.append(self.suspect_text[si])
        generated.append('{} {} the {}.'.format('He' if 0 <= si <= 2 else 'She', self.weapon_text[flatten_index(w)], w))
        generated.append(self.murder_text[si])

        if accuser is s:
            rtext = self.reaction_texts[si]
            generated.append(rtext[0])
            generated.append(rtext[2])
            generated.append(rtext[1])
        else:
            generated.append(self.reaction_texts[si][2])
            generated.append(self.reaction_texts[flatten_index(accuser)][0])

        return generated


class MoveDirection(Enum):
    UP = ('up', -1, 0, 'DOWN')
    LEFT = ('left', 0, -1, 'RIGHT')
    DOWN = ('down', 1, 0, 'UP')
    RIGHT = ('right', 0, 1, 'LEFT')
    SECRET = ('secret', 0, 0)
    DOOR = ('door', 0, 0)

    def __str__(self):
        return self.value[0]

    def translate(self, x: int, y: int) -> Tuple[int, int]:
        return x + self.value[1], y + self.value[2]

    @property
    def reverseDirection(self):
        return MoveDirection[self.value[3]] if len(self.value) == 4 else None


class Move(NamedTuple):
    direction: MoveDirection
    length: int


class ClueBoard(metaclass=ABCMeta):
    @abstractmethod
    def __init__(
        self,
        boardfn: str,
        player_positions: Dict[Suspect, Any],
        imagefn: str,
        image_info: Tuple[int, int, float, float],
        *,
        secret_pairs: Dict[int, int] = {},
        entrance_exceptions: Iterable[Tuple[int, MoveDirection]] = set(),
    ) -> None:
        self.board = tuple(tuple(int(c) if c.isdigit() else c for c in s) for s in open(boardfn).read().split('\n'))

        self.X = len(self.board)  # vertical position
        self.Y = len(self.board[0])  # horizontal position
        assert all(len(self.board[i]) == self.Y for i in range(1, self.X))

        self.imagefn = imagefn
        self.image_offset_x, self.image_offset_y, self.space_size_x, self.space_size_y = image_info

        self.entrance_map = {i: r for i, r in enumerate(list(Room), 1)}  # type: ignore
        self.entrance_exceptions = frozenset(entrance_exceptions)
        self.secret_map = {self.entrance_map[i]: self.entrance_map[j] for i, j in secret_pairs.items()}
        self.player_positions = player_positions

        self.door_map = {r: list() for r in self.entrance_map.values()}  # type: Dict[Room, List[Tuple[int,int]]]
        self.door_blocks = {r: set() for r in self.entrance_map.values()}  # type: Dict[Room, Set[Tuple[int,int]]]
        room_letter_map = {c: r for r, c in zip(list(Room), 'ABCDEFGHI')}  # type: ignore
        self.room_player_map = {r: list() for r in self.entrance_map.values()}  # type: Dict[Room, List[Tuple[int,int]]]
        for y in range(0, self.Y):
            for x in range(0, self.X):
                if type(self.board[x][y]) is int and self.board[x][y]:
                    r = self.entrance_map[self.board[x][y]]
                    self.door_map[r].append((x, y))
                    for d in ('UP', 'LEFT', 'RIGHT', 'DOWN'):
                        m = MoveDirection[d]
                        x2, y2 = m.translate(x, y)
                        if not self.board[x2][y2] and (self.board[x][y], m.reverseDirection) not in entrance_exceptions:
                            self.door_blocks[r].add((x2, y2))
                elif str(self.board[x][y]) in 'ABCDEFGHI':
                    self.room_player_map[room_letter_map[self.board[x][y]]].append((x, y))

    def move(self, player: Suspect, roll: int, moves: Sequence[Move]) -> Optional[Room]:
        if player not in self.player_positions:
            raise ValueError(str(player) + ' not in this game')
        elif not moves:
            raise ValueError('Must have at least one move')

        moves = deque(moves)
        startingRoom = self.player_positions[player] if type(self.player_positions[player]) is Room else None
        x = None
        y = None

        if startingRoom:
            if moves[0].direction is MoveDirection.SECRET:
                if len(moves) > 1:
                    raise ValueError("'secret' must be only move")
                oldr = self.player_positions[player]
                if oldr not in self.secret_map:
                    raise ValueError('This room does not have a secret passage')
                else:
                    self.player_positions[player] = self.secret_map[oldr]
                    return self.secret_map[oldr]
            elif moves[0].direction is MoveDirection.DOOR:
                if len(moves) == 1:
                    raise ValueError('Must roll out of door afterwards')
                else:
                    try:
                        x, y = self.door_map[self.player_positions[player]][moves[0][1]]
                    except IndexError:
                        raise ValueError('This room does not have that many doors')
                    moves.popleft()
            else:
                raise ValueError('Must start with secret or door in room')
        else:
            x, y = self.player_positions[player]

        assert (m.dir not in (MoveDirection.SECRET, MoveDirection.DOOR) and m.length > 0 for m in moves)

        other_player_positions = set(v for k, v in self.player_positions.items() if k is not player and type(v) is tuple)
        for m in moves:
            for i in range(0, m.length):
                x, y = m.direction.translate(x, y)

                if type(self.board[x][y]) is int:
                    if self.board[x][y]:
                        if (self.board[x][y], m.direction) in self.entrance_exceptions:
                            raise ValueError('Illegal move into a room')
                        newr = self.entrance_map[self.board[x][y]]
                        if startingRoom is newr:
                            raise ValueError('Cannot reenter room just exited')
                        self.player_positions[player] = newr
                        return self.player_positions[player]
                    else:
                        roll -= 1
                        if roll < 0:
                            raise ValueError('Too many moves for this roll')
                        elif (x, y) in other_player_positions:
                            raise ValueError("Illegal move into another player's position")
                else:
                    # print('{}: {}'.format((x,y), self.board[x][y]))
                    where = 'into a room' if self.board[x][y] == 'R' else 'out of bounds'
                    raise ValueError('Illegal move ' + where)
        if roll:
            raise ValueError('Roll not fully used up')
        else:
            self.player_positions[player] = (x, y)

    def isBlocked(self, r: Room) -> bool:
        return self.door_blocks[r].issubset(
            self.player_positions.values()
        )  # self.door_blocks[r] <= set(self.player_positions.values())

    def __str__(self):
        bp_rev = self.gen_pretty_positions()
        return '\n'.join(
            ''.join(bp_rev[(x, y)].color if (x, y) in bp_rev else str(self.board[x][y]) for y in range(0, self.Y))
            for x in range(0, self.X)
        )

    def gen_pretty_positions(self) -> Dict[Tuple[int, int], Suspect]:
        pp = dict(self.player_positions)
        pp_rooms = {k: v for k, v in pp.items() if type(v) is Room}
        room_count = Counter(pp_rooms.values())
        pp_room_xy = {v: random.sample(self.room_player_map[v], room_count[v]) for v in pp_rooms.values()}
        for k, v in pp_rooms.items():
            pp[k] = pp_room_xy[v].pop()
        return {v: k for k, v in pp.items()}

    def image(self, *, suspect_return: Suspect = None):
        bi = Image.open(self.imagefn)
        xp, yp = (0, 0)
        for p, s in self.gen_pretty_positions().items():
            y, x = p
            pi = ClueBoard.get_piece_image(s)
            pi.thumbnail((round(self.space_size_x * 0.9), round(self.space_size_y * 0.9)))
            # find top left corner, then center it within square
            x0 = self.image_offset_x + round(x * self.space_size_x) + round((self.space_size_x - pi.width) / 2)
            y0 = self.image_offset_y + round(y * self.space_size_y) + round((self.space_size_y - pi.height) / 2)
            if suspect_return is s:
                xp, yp = x0, y0
            bi.paste(pi, (x0, y0), mask=pi)
        return (bi, (xp, yp)) if suspect_return else bi

    def door_help(self) -> Image:
        bi = Image.open(self.imagefn)
        di = Image.new('RGBA', bi.size, (0, 0, 0, 0))
        fs = int(min(self.space_size_x, self.space_size_y))
        df = ImageFont.truetype('myriadb.ttf', fs)
        did = ImageDraw.Draw(di)
        for dl in [dl for dl in self.door_map.values() if len(dl) > 1]:
            c = 65
            for y, x in dl:
                x0 = self.image_offset_x + round((x + 0.25) * self.space_size_x)
                y0 = self.image_offset_y + round(y * self.space_size_y)
                did.text((x0, y0), chr(c), font=df, fill=(0, 0, 0, 255))
                bi.paste(Image.new('RGBA', (fs, fs), (255, 255, 255, 127)), (x0, y0))
                c += 1
        return di

    @classmethod
    def get_piece_image(cls, s: Suspect) -> Image:
        pieces_image = Image.open(f'clue_images{os.sep}clue_pieces.png')
        i = list(Suspect).index(s)
        w, h = pieces_image.width / 3, pieces_image.height / 2
        x, y = w * (i // 2), h * (i % 2)
        return pieces_image.crop((x, y, x + w, y + h))


class BasicClueBoard(ClueBoard):
    def __init__(self, players: AbstractSet[Suspect]) -> None:
        pp = {
            Suspect.SCARLET: (0, 16),
            Suspect.MUSTARD: (7, 23),
            Suspect.WHITE: (24, 14),
            Suspect.GREEN: (24, 9),
            Suspect.PEACOCK: (18, 0),
            Suspect.PLUM: (5, 0),
        }
        for p in set(Suspect) - players:  # type: ignore
            del pp[p]
        super().__init__(
            'clue_basicboard.txt',
            pp,
            f'clue_images{os.sep}clue_basicboard2.png',
            (15, 15, 29.625, 27.875),
            secret_pairs={1: 9, 9: 1, 3: 7, 7: 3},
            entrance_exceptions=[(1, MoveDirection.LEFT), (3, MoveDirection.RIGHT), (7, MoveDirection.DOWN)],
        )


class CluePlayer(NamedTuple):
    id: Any
    suspect: Suspect
    cards: Tuple[ClueCard]


class ClueWWW(NamedTuple):
    suspect: Suspect
    weapon: Weapon
    room: Room


def limitCallsTo(limitedMethods: Iterable[str], option_var: str, option_gen: str):
    def srDecorator(cls):
        def srSenderDecorator(f):
            @wraps(f)
            def srSelfDecorator(self, *a, **k):
                o = getattr(self, option_var)
                if f.__name__ in o:
                    g = getattr(self, option_gen)
                    r = f(self, *a, **k)  # let exception propagate if necessary
                    setattr(self, option_var, g.send(f.__name__))
                    return r
                else:
                    raise RuntimeError('Limited method not within {} at the moment'.format(o))

            return srSelfDecorator

        for s in limitedMethods:
            m = getattr(cls, s)
            setattr(cls, s, srSenderDecorator(m))
        return cls

    return srDecorator


@limitCallsTo(
    limitedMethods=('start', 'roll', 'move', 'secret', 'suggest', 'accuse', 'endturn'),
    option_var='next_options',
    option_gen='_optionGen',
)
class ClueGame:
    def __init__(
        self, dieCount=1, all_cards: List[List[ClueCard]] = [list(t) for t in ClueCard.__subclasses__()]
    ) -> None:  # for now, just the basic board.
        self.suspects, self.weapons, self.rooms = copy.deepcopy(all_cards)
        for l in all_cards:
            random.shuffle(l)
        self.www = [ClueWWW(s, w, r) for s, w, r in zip(*all_cards)]  # who what where
        self.answer = random.choice(self.www)
        self._optionGen = self._gameplay_options()
        self.next_options = self._optionGen.send(None)
        self.dieCount = dieCount

    def start(
        self,
        *,
        players: Dict[Suspect, Any] = {s: s.value for s in list(Suspect)},
        boardType: Callable[..., ClueBoard] = BasicClueBoard,
    ) -> bool:
        player_cards = list(itertools.chain(self.suspects, self.weapons, self.rooms))
        for ce in self.answer:
            player_cards.remove(ce)
        random.shuffle(player_cards)

        # thanks stackoverflow, forgot about step splicing. what an elegant solution!
        player_cards_chunked = [player_cards[i :: len(players)] for i in range(len(players))]
        if len(player_cards) % len(players):
            random.shuffle(
                player_cards_chunked
            )  # so on uneven chunks, the bigger chunks aren't always frontloaded to the sorted Suspects

        self.players = deque(
            (
                CluePlayer(players[s], s, tuple(c))
                for s, c in zip((s for s in list(Suspect) if s in players), player_cards_chunked)
            ),
            maxlen=len(players),
        )
        self.board = boardType(set(cp.suspect for cp in self.players))
        self.gameover = set()
        self.accuseCount = 0

    def even(self) -> bool:
        # https://docs.python.org/3/library/itertools.html#itertools-recipes
        g = itertools.groupby(len(p.cards) for p in self.players)
        return next(g, True) and not next(g, False)

    def roll(self) -> int:  # generalize this for now
        self.cur_roll = sum(random.randint(1, 6) for i in range(0, self.dieCount))
        return self.cur_roll

    def move(self, *moves: Move) -> Optional[Room]:
        return self.board.move(self.cur_player.suspect, self.cur_roll, moves)

    def secret(self) -> Room:
        return self.board.move(self.cur_player.suspect, 0, (Move(MoveDirection.SECRET, 0),))

    def suggest(self, s: Suspect, w: Weapon) -> Tuple[str, CluePlayer, Optional[AbstractSet[ClueCard]]]:
        assert s in self.suspects and w in self.weapons

        r = self.board.player_positions[self.cur_player.suspect]
        if type(r) is not Room:
            raise ValueError('Cannot suggest outside a room')

        if s in self.board.player_positions:
            self.board.player_positions[s] = r

        suggestion = ClueWWW(s, w, r)

        hints = [
            (c1, c2, any(c1 in www and c2 in www for www in self.www)) for c1, c2 in itertools.combinations(suggestion, 2)
        ]
        hint = random.choice(hints)
        hint_types = tuple(type(t) for t in hint[0:2])
        verb = 'did' if hint_types == (Suspect, Weapon) else 'was'
        afterverb = 'have' if verb == 'did' else 'in'
        if hint[2]:
            verb = verb.upper()
        hint_str = f"{'The ' if hint_types[0] is Weapon else ''}{hint[0]} {verb}{' ' if hint[2] else ' NOT '}{afterverb} the {hint[1]}."

        for i in range(1, len(self.players)):
            u = set(suggestion) & set(self.players[i].cards)
            if u:
                return hint_str, self.players[i], u
        return hint_str, None, None

    def accuse(self, s: Suspect, w: Weapon, r: Room) -> bool:
        assert s in self.suspects and w in self.weapons and r in self.rooms
        self.accuseCount += 1

        correct = self.answer == ClueWWW(suspect=s, weapon=w, room=r)
        if not correct:
            del self.board.player_positions[self.cur_player.suspect]
            self.gameover.add(self.cur_player)
        return correct

    @property
    def cur_player(self) -> CluePlayer:
        return self.players[0]

    def board_zoomed_image(self, space_radius=7) -> Image:
        bi, t = self.board.image(suspect_return=self.cur_player.suspect)
        if type(self.board.player_positions[self.cur_player.suspect]) is Room:
            space_radius = int(space_radius * 1.5)
            bi = Image.alpha_composite(bi, self.board.door_help())
        sx, sy = self.board.space_size_x, self.board.space_size_y
        xc, yc = t
        bz = bi.crop(
            (
                max(0, xc - space_radius * sx),
                max(0, yc - space_radius * sy),
                min(bi.width, xc + space_radius * sx),
                min(bi.height, yc + space_radius * sy),
            )
        )
        bi.close()
        return bz

    def endturn(self) -> None:
        self.players.rotate(-1)
        while self.cur_player in self.gameover:
            self.players.rotate(-1)

    def translate(self, command: str) -> Tuple[Callable, Tuple[Any]]:
        args = command.split()
        action = args.pop(0)

        if action == 'endturn':
            return self.endturn, ()
        elif action == 'roll':
            return self.roll, ()
        elif action == 'secret':
            return self.secret, ()
        elif action == 'move':
            if len(args) % 2:
                raise ValueError('move requires full pairs of parameters')
            m = []

            r = self.board.player_positions[self.cur_player.suspect]
            if type(r) is Room and not re.fullmatch('DOOR', args[0], re.I) and len(self.board.door_map[r]) == 1:
                m.append(Move(MoveDirection.DOOR, 0))

            for i in range(0, len(args), 2):
                md = MoveDirection[args[i].upper()]
                l = args[i + 1]
                if md is MoveDirection.DOOR:
                    if l.upper() not in ('A', 'B', 'C', 'D'):
                        raise ValueError('Door must be one of ABCD')
                    l = ord(l.upper()) - ord('A')
                m.append(Move(md, int(l)))
            return self.move, m
        elif action == 'suggest':
            return self.suggest, (Suspect[args[0].upper()], Weapon[args[1].upper()])
        elif action == 'accuse':
            return self.accuse, (Suspect[args[0].upper()], Weapon[args[1].upper()], Room[args[2].upper()])
        else:
            raise ValueError('Improper command')

    # to be used in conjunction with limitCallsTo, to enforce actual gameplay
    def _gameplay_options(self) -> Generator[Tuple[str], str, None]:
        yield ('start,')

        last_suggest_room = {}
        while True:
            options = ['roll', 'accuse', 'endturn']
            r = self.board.player_positions[self.cur_player.suspect]
            if type(r) is Room:
                if r is not last_suggest_room.get(self.cur_player):
                    options.insert(1, 'suggest')
                if r in self.board.secret_map:
                    options.insert(1, 'secret')
                if self.board.isBlocked(r):
                    options.remove('roll')

            choice = yield tuple(options)

            if choice == 'accuse':
                if self.cur_player in self.gameover and self.accuseCount < len(self.players):
                    yield ('endturn',)
                    continue
                else:
                    yield tuple()  # game over
            elif choice == 'endturn':
                continue
            elif choice != 'suggest':
                last_suggest_room.pop(self.cur_player, None)
                if choice == 'roll':
                    yield ('move',)
                    if type(self.board.player_positions[self.cur_player.suspect]) is not Room:
                        yield ('endturn',)
                        continue
                yield ('suggest',)
            last_suggest_room[self.cur_player] = self.board.player_positions[self.cur_player.suspect]

            choice = yield ('accuse', 'endturn')
            if choice == 'accuse':
                if self.cur_player in self.gameover and self.accuseCount < len(self.players):
                    yield ('endturn',)
                else:
                    yield tuple()  # game over


if __name__ == '__main__':
    ct = ClueText()
    for s in ct.generate((Suspect.MUSTARD, Weapon.CANDLESTICK, Room.LIBRARY), Suspect.MUSTARD):
        print(s)

    ClueCard.multicard_image([Suspect.MUSTARD, Weapon.CANDLESTICK]).show()

    quit()

    p = {
        Suspect.PLUM: 'Wayo',
        Suspect.PEACOCK: 'jj',
        Suspect.SCARLET: 'Jess',
        Suspect.GREEN: 'Torgo',
        Suspect.WHITE: 'Kev',
        Suspect.MUSTARD: 'Guint',
    }
    cg = ClueGame()
    cg.start(players=p)
    print(cg.even())
    # cg.board.player_positions[Suspect.GREEN] = (4,6)
    # cg.board.player_positions[Suspect.WHITE] = (15,6)
    # cg.board.player_positions[Suspect.MUSTARD] = cg.answer.room

    print(cg.www)
    print(cg.answer)
    print(cg.endturn())
    print(cg.endturn())
    cg.roll()
    cg.cur_roll = 6
    print(cg.cur_player)
    print(cg.board.player_positions[cg.cur_player.suspect])
    m, a = cg.translate('move up 1 right 2 up 3')
    print(a)
    print(m(*a))
    print(cg.suggest(cg.answer.suspect, cg.answer.weapon))
    print(cg.next_options)
    # print(cg.roll())
    # print(cg.next_options)
    # print(cg.move(Move(MoveDirection.LEFT, cg.cur_roll)))
    # print(cg.endturn())
    print('{}\t{}'.format(cg.cur_player, cg.next_options))
