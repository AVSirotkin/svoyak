"""План турнира: разбор правил-схемы и разрешение боёв.

Модуль не зависит от Flask и базы: на вход получает JSON правил, посев
и сохранённые результаты, на выходе отдаёт стадии, бои, готовность
и очереди столов.

Термины:
    узел (node)   — запись из matches: бой, посев или пересчёт;
    бой (game)    — узел типа match; его номер = match_id = gameindex;
    стадия        — round из правил; посев и пересчёт тоже занимают стадию;
    посев         — виртуальный узел, раздающий места 1..N участникам;
    пересчёт      — виртуальный узел, суммирующий поле (по умолчанию points)
                    по источникам и выдающий ранжирование.

Инвариант: результат — факт, план и ранжирование — производные.
Сыгранный бой всегда отдаёт свой фактический состав из результатов,
поэтому правка раннего боя не может изменить уже сыгранный поздний.
"""

import copy
import json
from dataclasses import dataclass, field

SEED = 'seed'
AGGREGATOR = 'aggregator'
MATCH = 'match'

# В правилах встречаются разные написания одного и того же типа.
TYPE_ALIASES = {
    'seed': SEED,
    'player list': SEED,
    'playerlist': SEED,
    'players': SEED,
    'aggregator': AGGREGATOR,
    'aggregate': AGGREGATOR,
    'match': MATCH,
    'game': MATCH,
}

PLANNED = 'planned'      # не хватает участников
READY = 'ready'          # состав известен, результата нет
DONE = 'done'            # результат есть

WARN_ROSTER = 'roster_mismatch'   # фактический состав отличается от ожидаемого
WARN_UNPLANNED = 'played_unplanned'  # бой сыгран, хотя его участники не разрешились


class PlanError(ValueError):
    """Схема правил сломана так, что план построить нельзя."""


@dataclass(frozen=True)
class Player:
    playerid: object
    name: str

    @property
    def key(self):
        if self.playerid is not None:
            return ('id', self.playerid)
        return ('name', self.name)


@dataclass
class Stage:
    number: int
    kind: str
    match_id: str
    title: str


@dataclass
class Game:
    gameindex: int
    match_id: str
    stage: int
    table: int
    order: int
    name: str
    expected: list = field(default_factory=list)
    participants: list = field(default_factory=list)
    scores: list = field(default_factory=list)
    status: str = PLANNED
    warning: str = None
    tie: bool = False
    blockers: list = field(default_factory=list)

    @property
    def ready(self):
        return self.status == READY

    @property
    def done(self):
        return self.status == DONE

    @property
    def missing_slots(self):
        return [index + 1 for index, player in enumerate(self.expected) if player is None]

    def roster(self):
        return [player.name for player in self.participants if player is not None]


@dataclass
class Plan:
    name: str
    stages: list = field(default_factory=list)
    games: list = field(default_factory=list)
    queues: dict = field(default_factory=dict)
    standings: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    ties: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    final_gameindex: int = None

    def game(self, gameindex):
        for item in self.games:
            if item.gameindex == int(gameindex):
                return item
        return None

    def queue(self, table):
        return list(self.queues.get(int(table), []))

    def next_game(self, table):
        """Первый готовый бой в очереди стола: его и предлагаем ведущему."""
        for gameindex in self.queue(table):
            item = self.game(gameindex)
            if item is not None and item.ready:
                return item
        return None

    def current_game(self, table):
        """Бой, который столу играть следующим: первый несыгранный по порядку.

        Правила требуют играть бои стола по очереди, поэтому в отличие от
        next_game здесь несыгранный, но ещё не готовый бой останавливает очередь.
        """
        for gameindex in self.queue(table):
            item = self.game(gameindex)
            if item is not None and not item.done:
                return item
        return None

    def ready_games(self):
        return [item for item in self.games if item.ready]

    def winner(self):
        if self.final_gameindex is None:
            return None
        final = self.game(self.final_gameindex)
        if final is None or not final.done:
            return None
        for score in final.scores:
            if int(score.get('position') or 0) == 1:
                return Player(score.get('playerid'), score.get('name') or '')
        return None


# --------------------------------------------------------------------------
# Схема
# --------------------------------------------------------------------------


def normalize_schema(raw):
    """Приводит JSON правил к одному виду.

    Поддерживаются оба формата: как в базе ({"name": ..., "matches": [...]})
    и как в файлах ({"brackets": {...}}). Тип узла по умолчанию — match.
    """
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise PlanError('Правила не разбираются как JSON: %s' % error)
    elif isinstance(raw, dict):
        data = copy.deepcopy(raw)
    else:
        raise PlanError('Правила должны быть объектом или строкой JSON')

    if 'matches' not in data and isinstance(data.get('brackets'), dict):
        data = copy.deepcopy(data['brackets'])

    matches = data.get('matches')
    if not isinstance(matches, list) or not matches:
        raise PlanError('В правилах нет ни одного узла matches')

    seen = set()
    normalized = []
    for source in matches:
        if not isinstance(source, dict):
            raise PlanError('Узел правил должен быть объектом')
        match_id = str(source.get('id', '')).strip()
        if not match_id:
            raise PlanError('У узла нет id')
        if match_id in seen:
            raise PlanError('Повторяющийся id узла: %s' % match_id)
        seen.add(match_id)

        kind = TYPE_ALIASES.get(str(source.get('type') or MATCH).strip().lower())
        if kind is None:
            raise PlanError('Неизвестный тип узла %s: %s' % (match_id, source.get('type')))

        try:
            slots = int(source.get('participantSlots') or source.get('participantSlots') or 4)
        except (TypeError, ValueError):
            raise PlanError('participantSlots узла %s должен быть числом' % match_id)
        if slots < 1:
            raise PlanError('participantSlots узла %s должен быть положительным' % match_id)

        normalized.append({
            'id': match_id,
            'type': kind,
            'name': source.get('name') or match_id,
            'round': int(source.get('round') or 0),
            'participantSlots': slots,
            'virtual': bool(source.get('virtual')),
            'aggregateBy': source.get('aggregateBy') or 'sum',
            'sources': [dict(item) for item in (source.get('sources') or [])],
            'table': source.get('table'),
            'order': source.get('order'),
            'final': bool(source.get('final')),
        })

    final_id = data.get('final_id')
    return {
        'name': data.get('name') or '',
        'final_id': str(final_id).strip() if final_id not in (None, '') else None,
        'matches': normalized,
    }


def assign_tables(schema, tables=1):
    """Проставляет table и order, возвращая новый словарь схемы.

    Если столы заданы в правилах явно, они сохраняются (лишние сжимаются
    по кругу при tables меньше максимального). Если столов в правилах нет,
    бои раскладываются по порядку внутри стадии.
    """
    try:
        tables = int(tables)
    except (TypeError, ValueError):
        raise PlanError('Количество столов должно быть числом')
    if tables < 1:
        raise PlanError('Количество столов должно быть не меньше одного')

    result = copy.deepcopy(schema)
    games = [item for item in result['matches'] if item['type'] == MATCH]
    if not games:
        return result

    explicit = all(item.get('table') for item in games)
    counters = {}

    for item in games:
        if explicit:
            try:
                table = int(item['table'])
            except (TypeError, ValueError):
                raise PlanError('Стол боя %s должен быть числом' % item['id'])
            if table < 1:
                raise PlanError('Стол боя %s должен быть положительным' % item['id'])
            item['table'] = (table - 1) % tables + 1
        else:
            index = counters.get(item['round'], 0)
            counters[item['round']] = index + 1
            item['table'] = index % tables + 1

    # Порядок: явный order уважаем, остальным выдаём следующий свободный.
    used = {int(item['order']) for item in games if item.get('order')}
    counter = 0
    for item in games:
        if item.get('order'):
            item['order'] = int(item['order'])
            continue
        counter += 1
        while counter in used:
            counter += 1
        item['order'] = counter
    return result


def prepare(raw, tables=1):
    """Нормализация + раскладка по столам. Это и есть финальный JSON турнира."""
    return assign_tables(normalize_schema(raw), tables)


def max_table(schema):
    """Сколько столов заложено в правилах. 1, если столы не заданы явно."""
    tables = [
        int(node['table'])
        for node in schema['matches']
        if node['type'] == MATCH and node.get('table')
    ]
    return max(tables) if tables else 1


def tables_capacity(schema):
    """Сколько столов схема допускает одновременно.

    Если столы заданы явно — это максимальный номер. Если нет, то столько,
    сколько боёв стоит в самой параллельной стадии.
    """
    explicit = [node for node in schema['matches'] if node['type'] == MATCH and node.get('table')]
    if explicit:
        return max(int(node['table']) for node in explicit)

    per_stage = {}
    for node in schema['matches']:
        if node['type'] == MATCH:
            per_stage[node['round']] = per_stage.get(node['round'], 0) + 1
    return max(per_stage.values()) if per_stage else 1


def room_rules(raw, tables=1, parameters=None):
    """Снимок правил комнаты: явные столы в схеме и число столов в параметрах.

    Возвращает (schema, parameters). Схема уже готова к записи в rules.brackets,
    параметры — к записи в rules.parameters.
    """
    schema = prepare(raw, tables)
    params = dict(parameters or {})
    params['tables'] = int(tables)
    return schema, params


# --------------------------------------------------------------------------
# План
# --------------------------------------------------------------------------


def _as_results_map(results):
    """Принимает и список [{gameindex, scores}], и словарь {gameindex: scores}."""
    if not results:
        return {}
    if isinstance(results, dict):
        return {int(key): list(value or []) for key, value in results.items()}
    mapped = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        gameindex = entry.get('gameindex')
        if gameindex is None:
            continue
        mapped[int(gameindex)] = list(entry.get('scores') or [])
    return mapped


def _scores_to_players(scores):
    """Ранжирование по фактическому результату: место 1 первым."""
    ordered = sorted(
        scores,
        key=lambda score: (
            float(score.get('position') or 0) or 999,
            -float(score.get('points') or 0),
        ),
    )
    players = []
    for score in ordered:
        name = score.get('name')
        if name is None:
            continue
        players.append(Player(score.get('playerid'), str(name)))
    return players


def _seed_players(node, seed_places):
    """Кто стоит на местах посева: сохранённая жеребьёвка важнее имён из правил."""
    players = [None] * node['participantSlots']
    if seed_places:
        for place, player in seed_places.items():
            place = int(place)
            if 1 <= place <= node['participantSlots']:
                players[place - 1] = player
        return players

    for source in node['sources']:
        if source.get('type') != 'player':
            continue
        try:
            place = int(source.get('slot') or source.get('place') or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= place <= node['participantSlots']:
            players[place - 1] = Player(None, str(source.get('name') or ''))
    return players


def _aggregate(node, results, nodes):
    """Сумма field по источникам и ранжирование. None, если источники не готовы."""
    totals = {}
    players = {}
    for source in node['sources']:
        if source.get('type') != MATCH:
            continue
        source_id = str(source.get('matchId') or '')
        source_node = nodes.get(source_id)
        if source_node is None or source_node['type'] != MATCH:
            return None
        scores = results.get(int(source_id))
        if not scores:
            return None
        field_name = source.get('field') or 'points'
        for score in scores:
            name = score.get('name')
            if name is None:
                continue
            player = Player(score.get('playerid'), str(name))
            players[player.key] = player
            value = score.get(field_name)
            try:
                value = float(value or 0)
            except (TypeError, ValueError):
                value = 0.0
            totals[player.key] = totals.get(player.key, 0.0) + value

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0][1]))
    ranking = [players[key] for key, _ in ranked]

    # Ничьи показываем отдельно: порядок внутри равных значений не выводится
    # из результатов, его разруливает ведущий.
    ties = []
    group = []
    previous = None
    for key, value in ranked:
        rounded = round(value, 6)
        if previous is not None and rounded != previous:
            if len(group) > 1:
                ties.append([players[item[0]] for item in group])
            group = []
        group.append((key, rounded))
        previous = rounded
    if len(group) > 1:
        ties.append([players[item[0]] for item in group])

    return {'ranking': ranking, 'ties': ties, 'totals': totals}


def build_plan(schema, seed_places=None, results=None):
    """Строит план: стадии, бои, готовность, очередь столов.

    schema       — результат normalize_schema/assign_tables;
    seed_places  — {место: Player} из жеребьёвки;
    results      — фактические результаты боёв.
    """
    if not isinstance(schema, dict) or 'matches' not in schema:
        raise PlanError('Ожидалась нормализованная схема')

    nodes = {item['id']: item for item in schema['matches']}
    results_map = _as_results_map(results)
    plan = Plan(name=schema.get('name') or '')

    errors = plan.errors
    for node in schema['matches']:
        for source in node['sources']:
            if source.get('type') != MATCH:
                continue
            source_id = str(source.get('matchId') or '')
            if source_id not in nodes:
                errors.append('Узел %s ссылается на несуществующий бой %s' % (node['id'], source_id))

    resolved = {}     # id -> список участников (Player | None), если известны все
    partial = {}      # id -> частично известный состав, для показа
    rankings = {}     # id -> ранжирование по факту результата

    for node in schema['matches']:
        if node['type'] == SEED:
            resolved[node['id']] = _seed_players(node, seed_places)
            partial[node['id']] = list(resolved[node['id']])
            rankings[node['id']] = [player for player in resolved[node['id']] if player]

    standings = plan.standings
    for _ in range(len(schema['matches']) + 2):
        changed = False

        for node in schema['matches']:
            if node['type'] != MATCH or node['id'] in resolved:
                continue
            players = []
            for source in node['sources']:
                if source.get('type') == 'player':
                    players.append(Player(None, str(source.get('name') or '')))
                    continue
                if source.get('type') != MATCH:
                    players.append(None)
                    continue
                source_id = str(source.get('matchId') or '')
                if source_id not in rankings:
                    players.append(None)
                    continue
                try:
                    place = int(source.get('place') or 0)
                except (TypeError, ValueError):
                    place = 0
                ranking = rankings[source_id]
                players.append(ranking[place - 1] if 1 <= place <= len(ranking) else None)

            partial[node['id']] = players

            # Результат — факт: ранжирование сыгранного боя доступно зависимым
            # боям, даже если сам состав восстановить из правил не удалось.
            scores = results_map.get(int(node['id']))
            if scores:
                rankings[node['id']] = _scores_to_players(scores)

            if any(player is None for player in players):
                continue

            resolved[node['id']] = players
            changed = True

        for node in schema['matches']:
            if node['type'] != AGGREGATOR or node['id'] in standings:
                continue
            aggregate = _aggregate(node, results_map, nodes)
            if aggregate is None:
                continue
            ranking = aggregate['ranking'][:node['participantSlots']]
            standings[node['id']] = ranking
            plan.ties[node['id']] = aggregate['ties']
            plan.totals[node['id']] = aggregate['totals']
            rankings[node['id']] = ranking
            resolved[node['id']] = ranking + [None] * max(0, node['participantSlots'] - len(ranking))
            changed = True

        if not changed:
            break

    # Стадии
    stage_numbers = sorted({node['round'] for node in schema['matches']})
    for number in stage_numbers:
        group = [node for node in schema['matches'] if node['round'] == number]
        if any(node['type'] == SEED for node in group):
            kind = 'seed'
        elif any(node['type'] == AGGREGATOR for node in group):
            kind = 'aggregator'
        else:
            kind = 'games'
        first = group[0]
        # У стадии боёв нет своего имени: название первого боя путало бы (Игра 1).
        title = 'Бои' if kind == 'games' else first['name']
        plan.stages.append(Stage(number, kind, first['id'], title))

    # Бои
    for index, node in enumerate(item for item in schema['matches'] if item['type'] == MATCH):
        gameindex = int(node['id'])
        expected = list(
            resolved.get(node['id'])
            or partial.get(node['id'])
            or [None] * node['participantSlots']
        )
        scores = results_map.get(gameindex) or []
        actual = _scores_to_players(scores) if scores else []

        game = Game(
            gameindex=gameindex,
            match_id=node['id'],
            stage=node['round'],
            table=int(node.get('table') or 1),
            order=int(node.get('order') or index + 1),
            name=node['name'],
            expected=expected,
            scores=scores,
        )

        if scores:
            game.participants = actual
            game.status = DONE
            # Равные места в сыгранном бое делают победителя неоднозначным:
            # дальше по сетке его выбирает порядок в базе, а не правила.
            positions = [score.get('position') for score in scores]
            game.tie = len(set(positions)) < len(positions)
            if any(player is None for player in expected):
                game.warning = WARN_UNPLANNED
            elif sorted(player.key for player in expected) != sorted(player.key for player in actual):
                # Порядок в правилах — это посадка за стол, а не занятые места,
                # поэтому сравниваем состав, а не последовательность.
                game.warning = WARN_ROSTER
        else:
            game.participants = expected
            game.status = READY if all(player is not None for player in expected) else PLANNED

        plan.games.append(game)

    plan.games.sort(key=lambda item: (item.stage, item.order, item.gameindex))

    # Один игрок в двух боях одной стадии. Понятия круга у нас нет, правила
    # этого не запрещают, поэтому только предупреждаем, но не мешаем играть.
    for stage in plan.stages:
        seats = {}
        for game in plan.games:
            if game.stage != stage.number:
                continue
            for player in game.participants:
                if player is None:
                    continue
                name, gameindexes = seats.get(player.key, (player.name, []))
                seats[player.key] = (name, gameindexes + [game.gameindex])
        for name, gameindexes in seats.values():
            if len(gameindexes) > 1:
                plan.conflicts.append({
                    'stage': stage.number,
                    'name': name,
                    'games': gameindexes,
                })

    # Чего именно ждёт неготовый бой: номера боёв, а не абстрактные места.
    for game in plan.games:
        if game.done:
            continue
        node = nodes.get(game.match_id) or {}
        blockers = []
        for source in node.get('sources') or []:
            if source.get('type') != MATCH:
                continue
            source_id = str(source.get('matchId') or '')
            if not source_id.isdigit():
                continue
            source_game = plan.game(int(source_id))
            if source_game is not None and not source_game.done:
                blockers.append(source_game.gameindex)
        game.blockers = sorted(set(blockers))

    for game in plan.games:
        plan.queues.setdefault(game.table, []).append(game.gameindex)
    for table in plan.queues:
        plan.queues[table].sort(key=lambda gameindex: (
            plan.game(gameindex).order,
            gameindex,
        ))

    # Финал: явный final_id важнее, иначе берём узел с пометкой final.
    final_id = schema.get('final_id')
    if final_id and str(final_id).isdigit():
        plan.final_gameindex = int(final_id)
    else:
        for node in schema['matches']:
            if node['type'] == MATCH and node.get('final') and node['id'].isdigit():
                plan.final_gameindex = int(node['id'])
                break

    return plan
