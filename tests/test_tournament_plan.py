"""Тесты плана турнира: раскладка по столам и разрешение боёв."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tournament_plan as tp


def load_rules(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as handle:
        return json.load(handle)


def roster(names):
    """Игроки с устойчивыми id: имя P7 -> playerid 7."""
    return [tp.Player(int(name[1:]), name) for name in names]


def seed_of(names):
    return {index + 1: player for index, player in enumerate(roster(names))}


def scores_of(players, points=(4.0, 3.0, 2.0, 1.0)):
    prepared = []
    for player in players:
        if isinstance(player, str):
            prepared.append(tp.Player(int(player[1:]), player))
        else:
            prepared.append(player)
    return [
        {
            'gameindex': 0,
            'playerid': player.playerid,
            'name': player.name,
            'score': 0,
            'position': index + 1,
            'points': points[index],
        }
        for index, player in enumerate(prepared)
    ]


def play(schema, seed, results, gameindex, players=None):
    """Записывает результат боя и пересобирает план."""
    plan = tp.build_plan(schema, seed, results)
    game = plan.game(gameindex)
    if game is None:
        raise AssertionError('боя %s нет в плане' % gameindex)
    chosen = players if players is not None else [item for item in game.participants if item is not None]
    results[gameindex] = scores_of(chosen)
    return tp.build_plan(schema, seed, results)


AZART_NAMES = ['P%d' % index for index in range(1, 13)]


class AzartLayoutTest(unittest.TestCase):
    def setUp(self):
        self.raw = load_rules('12-2-azart.json')

    def test_three_tables(self):
        schema = tp.prepare(self.raw, tables=3)
        plan = tp.build_plan(schema)
        self.assertEqual([game.gameindex for game in plan.games], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([game.table for game in plan.games], [1, 2, 3, 1, 2, 3, 1])
        self.assertEqual([game.order for game in plan.games], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(plan.queue(1), [1, 4, 7])
        self.assertEqual(plan.queue(2), [2, 5])
        self.assertEqual(plan.queue(3), [3, 6])

    def test_one_table(self):
        schema = tp.prepare(self.raw, tables=1)
        plan = tp.build_plan(schema)
        self.assertEqual([game.table for game in plan.games], [1] * 7)
        self.assertEqual([game.order for game in plan.games], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(plan.queue(1), [1, 2, 3, 4, 5, 6, 7])

    def test_stages(self):
        schema = tp.prepare(self.raw, tables=3)
        plan = tp.build_plan(schema)
        self.assertEqual(
            [(stage.number, stage.kind) for stage in plan.stages],
            [(0, 'seed'), (1, 'games'), (2, 'aggregator'), (3, 'games'), (4, 'games')],
        )
        self.assertEqual(plan.final_gameindex, 7)

    def test_both_wrapper_formats(self):
        flat = self.raw['brackets'] if 'brackets' in self.raw else self.raw
        first = tp.prepare(self.raw, tables=3)
        second = tp.prepare(flat, tables=3)
        self.assertEqual(first, second)

    def test_prepared_schema_is_json_serializable(self):
        schema = tp.prepare(self.raw, tables=3)
        again = json.loads(json.dumps(schema, ensure_ascii=False))
        self.assertEqual(tp.build_plan(again).queue(1), [1, 4, 7])


class AzartResolutionTest(unittest.TestCase):
    def setUp(self):
        self.raw = load_rules('12-2-azart.json')
        self.schema = tp.prepare(self.raw, tables=3)
        self.seed = seed_of(AZART_NAMES)

    def plan(self, results=None):
        return tp.build_plan(self.schema, self.seed, results or {})

    def test_initial_readiness(self):
        plan = self.plan()
        ready = [game.gameindex for game in plan.ready_games()]
        self.assertEqual(ready, [1, 2, 3])
        self.assertEqual(plan.game(7).status, tp.PLANNED)
        self.assertEqual(plan.standings, {})

    def test_first_round_reveals_aggregator(self):
        results = {}
        for gameindex in (1, 2, 3):
            plan = play(self.schema, self.seed, results, gameindex)
        self.assertEqual(plan.game(1).status, tp.DONE)
        self.assertEqual([game.gameindex for game in plan.ready_games()], [4, 5, 6])

        ranking = [player.name for player in plan.standings['v1']]
        self.assertEqual(
            ranking,
            ['P1', 'P5', 'P9', 'P2', 'P6', 'P10', 'P3', 'P7', 'P11', 'P4', 'P8', 'P12'],
        )

        self.assertEqual(plan.game(4).roster(), ['P1', 'P5', 'P9', 'P2'])
        self.assertEqual(plan.game(5).roster(), ['P6', 'P10', 'P3', 'P7'])
        self.assertEqual(plan.game(6).roster(), ['P11', 'P4', 'P8', 'P12'])

        self.assertEqual(plan.totals['v1'][('id', 1)], 4.0)
        self.assertEqual(plan.totals['v1'][('id', 12)], 1.0)

    def test_current_game_follows_table_order(self):
        plan = self.plan()
        # На столе 1 очередь 1 -> 4 -> 7: первым играют бой 1.
        self.assertEqual(plan.current_game(1).gameindex, 1)
        self.assertEqual(plan.game(1).blockers, [])

        results = {}
        plan = play(self.schema, self.seed, results, 1)
        # Бой 4 ещё не готов: он ждёт 2 и 3, а они за другими столами.
        current = plan.current_game(1)
        self.assertEqual(current.gameindex, 4)
        self.assertEqual(current.status, tp.PLANNED)
        self.assertEqual(current.blockers, [2, 3])

        plan = play(self.schema, self.seed, results, 2)
        plan = play(self.schema, self.seed, results, 3)
        self.assertEqual(plan.current_game(1).gameindex, 4)
        self.assertEqual(plan.game(4).blockers, [])

        plan = play(self.schema, self.seed, results, 4)
        self.assertEqual(plan.current_game(1).gameindex, 7)
        self.assertEqual(plan.game(7).blockers, [5, 6])

    def test_current_game_is_none_when_table_is_done(self):
        # На столе 2 играют бои 2 и 5: после них очередь стола пуста.
        results = {}
        plan = self.plan()
        for gameindex in (1, 2, 3, 4, 5, 6):
            plan = play(self.schema, self.seed, results, gameindex)
        self.assertIsNone(plan.current_game(2))
        self.assertEqual(plan.current_game(1).gameindex, 7)

    def test_ties_are_exposed(self):
        results = {}
        for gameindex in (1, 2, 3):
            plan = play(self.schema, self.seed, results, gameindex)
        groups = [[player.name for player in group] for group in plan.ties['v1']]
        self.assertIn(['P1', 'P5', 'P9'], groups)

    def test_final_and_winner(self):
        results = {}
        for gameindex in (1, 2, 3, 4, 5, 6):
            plan = play(self.schema, self.seed, results, gameindex)
        self.assertEqual(plan.game(7).status, tp.READY)
        self.assertEqual(plan.game(7).roster(), ['P1', 'P5', 'P6', 'P11'])

        plan = play(self.schema, self.seed, results, 7)
        self.assertEqual(plan.game(7).status, tp.DONE)
        winner = plan.winner()
        self.assertIsNotNone(winner)
        self.assertEqual(winner.name, 'P1')

    def test_out_of_order_play(self):
        results = {}
        for gameindex in (1, 2, 3):
            plan = play(self.schema, self.seed, results, gameindex)

        # Бой 5 играем раньше 4: оба готовы, порядок в очереди не меняется.
        plan = play(self.schema, self.seed, results, 5)
        self.assertEqual(plan.game(5).status, tp.DONE)
        self.assertEqual(plan.game(4).status, tp.READY)
        self.assertEqual(plan.queue(1), [1, 4, 7])
        self.assertEqual(plan.queue(2), [2, 5])
        self.assertIsNone(plan.game(5).warning)

    def test_freeze_after_play(self):
        results = {}
        for gameindex in (1, 2, 3, 4, 5, 6, 7):
            plan = play(self.schema, self.seed, results, gameindex)
        final_before = plan.game(7).roster()

        # Меняем результат первого боя задним числом.
        results[1] = scores_of(['P4', 'P3', 'P2', 'P1'])
        plan = tp.build_plan(self.schema, self.seed, results)

        self.assertEqual(plan.game(7).roster(), final_before)
        self.assertEqual(plan.game(1).roster(), ['P4', 'P3', 'P2', 'P1'])

    def test_warning_roster_mismatch(self):
        results = {}
        for gameindex in (1, 2, 3):
            plan = play(self.schema, self.seed, results, gameindex)

        # Ведущий вводит бой 4 не тем составом, который ожидался.
        plan = play(self.schema, self.seed, results, 4, players=roster(['P12', 'P11', 'P10', 'P8']))
        game = plan.game(4)
        self.assertEqual(game.status, tp.DONE)
        self.assertEqual(game.warning, tp.WARN_ROSTER)
        self.assertEqual([player.name for player in game.expected], ['P1', 'P5', 'P9', 'P2'])
        self.assertEqual(game.roster(), ['P12', 'P11', 'P10', 'P8'])

    def test_warning_unplanned(self):
        # Финал играют, хотя полуфиналы не сыграны: состав задан вручную.
        results = {}
        plan = play(self.schema, self.seed, results, 7, players=roster(['P1', 'P5', 'P6', 'P11']))
        game = plan.game(7)
        self.assertEqual(game.status, tp.DONE)
        self.assertEqual(game.warning, tp.WARN_UNPLANNED)

    def test_partial_seed_keeps_games_planned(self):
        plan = tp.build_plan(self.schema, seed_of(['P1', 'P2', 'P3']))
        self.assertEqual(plan.game(1).status, tp.PLANNED)
        self.assertEqual(plan.game(1).missing_slots, [4])

    def test_tie_in_played_game_is_marked(self):
        # Равные места в сыгранном бое: победителя правила не задают.
        results = {
            1: [
                {'name': 'P1', 'playerid': 1, 'score': 0, 'position': 2.5, 'points': 2.5},
                {'name': 'P2', 'playerid': 2, 'score': 0, 'position': 2.5, 'points': 2.5},
                {'name': 'P3', 'playerid': 3, 'score': 0, 'position': 2.5, 'points': 2.5},
                {'name': 'P4', 'playerid': 4, 'score': 0, 'position': 2.5, 'points': 2.5},
            ],
        }
        plan = tp.build_plan(self.schema, self.seed, results)
        self.assertTrue(plan.game(1).tie)

        clean = {}
        plan = tp.build_plan(self.schema, self.seed, clean)
        self.assertFalse(plan.game(1).tie)

    def test_duplicate_player_in_stage_is_reported(self):
        # Один игрок попал в два боя одной стадии: повод предупредить ведущего.
        results = {
            1: [
                {'name': 'P1', 'playerid': 1, 'score': 5, 'position': 1, 'points': 5},
                {'name': 'P2', 'playerid': 2, 'score': 4, 'position': 2, 'points': 4},
                {'name': 'P3', 'playerid': 3, 'score': 3, 'position': 3, 'points': 3},
                {'name': 'P4', 'playerid': 4, 'score': 2, 'position': 4, 'points': 2},
            ],
            2: [
                {'name': 'P1', 'playerid': 1, 'score': 5, 'position': 1, 'points': 5},
                {'name': 'P2', 'playerid': 2, 'score': 4, 'position': 2, 'points': 4},
                {'name': 'P3', 'playerid': 3, 'score': 3, 'position': 3, 'points': 3},
                {'name': 'P4', 'playerid': 4, 'score': 2, 'position': 4, 'points': 2},
            ],
        }
        plan = tp.build_plan(self.schema, self.seed, results)
        conflicts = {(item['name'], item['stage']) for item in plan.conflicts}
        self.assertIn(('P1', 1), conflicts)

        fresh = tp.build_plan(self.schema, self.seed, {})
        self.assertEqual(fresh.conflicts, [])


class De8Test(unittest.TestCase):
    def setUp(self):
        self.raw = load_rules('DE8_with_consolation.json')
        # В файле нет final_id — помечаем финал вручную, как это сделают правила.
        for node in self.raw['matches']:
            if str(node.get('id')) == '7':
                node['final'] = True
        self.schema = tp.prepare(self.raw, tables=2)
        self.seed = seed_of(['P%d' % index for index in range(1, 9)])

    def test_layout(self):
        plan = tp.build_plan(self.schema, self.seed)
        self.assertEqual(len(plan.games), 7)
        self.assertEqual(plan.final_gameindex, 7)
        self.assertEqual(len(plan.stages), 5)

    def test_file_without_final_marker(self):
        raw = load_rules('DE8_with_consolation.json')
        plan = tp.build_plan(tp.prepare(raw, tables=2), self.seed)
        self.assertIsNone(plan.final_gameindex)

    def test_full_run(self):
        results = {}
        plan = tp.build_plan(self.schema, self.seed, results)
        guard = 0
        while True:
            ready = [game for game in plan.ready_games() if not game.done]
            if not ready:
                break
            for game in ready:
                plan = play(self.schema, self.seed, results, game.gameindex)
            guard += 1
            if guard > 10:
                raise AssertionError('план не сходится')

        self.assertTrue(all(game.done for game in plan.games))
        self.assertEqual(plan.winner().name, 'P1')


class RoomRulesTest(unittest.TestCase):
    def setUp(self):
        self.raw = load_rules('12-2-azart.json')

    def test_writes_explicit_tables(self):
        schema, params = tp.room_rules(self.raw, tables=3, parameters={'basic_game_number': 3})
        self.assertEqual(params['tables'], 3)
        self.assertEqual(params['basic_game_number'], 3)
        plan = tp.build_plan(schema)
        self.assertEqual({game.table for game in plan.games}, {1, 2, 3})
        self.assertEqual(json.loads(json.dumps(schema, ensure_ascii=False)), schema)

    def test_single_table_snapshot(self):
        schema, params = tp.room_rules(self.raw, tables=1)
        self.assertEqual(params, {'tables': 1})
        plan = tp.build_plan(schema)
        self.assertEqual({game.table for game in plan.games}, {1})
        self.assertEqual(plan.queue(1), [1, 2, 3, 4, 5, 6, 7])

    def test_capacity(self):
        base = tp.normalize_schema(self.raw)
        self.assertEqual(tp.tables_capacity(base), 3)
        three = tp.prepare(self.raw, tables=3)
        self.assertEqual(tp.tables_capacity(three), 3)
        two = tp.prepare(self.raw, tables=2)
        self.assertEqual(tp.tables_capacity(two), 2)

    def test_prepare_is_idempotent(self):
        once = tp.prepare(self.raw, tables=3)
        twice = tp.prepare(once, tables=3)
        self.assertEqual(once, twice)


class EdgeCaseTest(unittest.TestCase):
    def test_legacy_schema_without_tables(self):
        schema = tp.prepare(load_rules('12-2-azart.json'), tables=1)
        for node in schema['matches']:
            if node['type'] == tp.MATCH:
                node.pop('table', None)
        collapsed = tp.assign_tables(schema, tables=1)
        plan = tp.build_plan(collapsed)
        self.assertEqual({game.table for game in plan.games}, {1})

    def test_collapse_explicit_tables(self):
        schema = tp.prepare(load_rules('12-2-azart.json'), tables=3)
        collapsed = tp.assign_tables(schema, tables=2)
        plan = tp.build_plan(collapsed)
        self.assertEqual([game.table for game in plan.games], [1, 2, 1, 1, 2, 1, 1])
        self.assertEqual({1: [1, 3, 4, 6, 7], 2: [2, 5]}, plan.queues)

    def test_broken_reference(self):
        raw = {
            'matches': [
                {'id': '1', 'round': 1, 'participantSlots': 4,
                 'sources': [{'slot': 1, 'type': 'match', 'matchId': 'zz', 'place': 1}]},
            ]
        }
        schema = tp.prepare(raw, tables=1)
        plan = tp.build_plan(schema)
        self.assertTrue(plan.errors)
        self.assertEqual(plan.game(1).status, tp.PLANNED)

    def test_unknown_type(self):
        raw = {'matches': [{'id': '1', 'type': 'magic', 'round': 1}]}
        with self.assertRaises(tp.PlanError):
            tp.normalize_schema(raw)

    def test_duplicate_id(self):
        raw = {'matches': [{'id': '1'}, {'id': '1'}]}
        with self.assertRaises(tp.PlanError):
            tp.normalize_schema(raw)

    def test_empty_rules(self):
        with self.assertRaises(tp.PlanError):
            tp.normalize_schema({'matches': []})

    def test_tables_validation(self):
        with self.assertRaises(tp.PlanError):
            tp.assign_tables(tp.normalize_schema(load_rules('12-2-azart.json')), tables=0)


if __name__ == '__main__':
    unittest.main()
