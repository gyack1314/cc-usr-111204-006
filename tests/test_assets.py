"""配置样例：跨禁读空洞的字段被拒绝，计划不自动合并。"""
import json
import unittest
from pathlib import Path

from source_data import mapping, sampling
from source_data.io import load
from source_data.replay import Replay

FIELD = {'id': 'voltage', 'area': 'holding', 'address': 1, 'words': 1, 'type': 'uint16',
         'byte_order': 'big', 'word_order': 'big', 'scale': '1', 'offset': '0'}


def asset(path):
    return (Path('data') / path).resolve()


class FixtureTests(unittest.TestCase):
    def test_all_assets_plan_or_reject_consistently(self):
        for path in sorted(Path('data').glob('*.json')):
            value, _ = load(path)
            with self.subTest(file=path.name):
                if path.name.startswith('invalid-'):
                    with self.assertRaises(ValueError):
                        mapping.parse(value)
                else:
                    plan = mapping.parse(value)
                    self.assertTrue(plan.blocks)

    def test_invalid_rejected_before_polling(self):
        value, _ = load('data/invalid-domain.json')
        with self.assertRaises(ValueError):
            mapping.parse(value)

    def test_normal_accepts(self):
        value, _ = load('data/normal.json')
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['sample']['fields'][0]['value'], 200)

    def test_torn_rejects_without_publishing(self):
        value, _ = load('data/torn-generation.json')
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertNotIn('sample', result)
        self.assertEqual(result['attempts'][0]['reason']['name'], 'generation_changed')

    def test_samples_never_claim_cross_device_snapshot(self):
        value, _ = load('data/normal.json')
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        text = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result['sample']['claim'], 'single_unit_same_generation')
        self.assertIn('不构成跨设备同时快照', text)
        self.assertNotIn('snapshot": true', text)


class MachineReadableTests(unittest.TestCase):
    @staticmethod
    def stepping(*seconds):
        state = {'i': 0}

        def clock():
            value = seconds[min(state['i'], len(seconds) - 1)]
            state['i'] += 1
            return value
        return clock

    def test_clock_can_stretch_elapsed(self):
        value, _ = load('data/normal.json')
        result = sampling.sample(Replay(value['steps']), mapping.parse(value),
                                 clock=self.stepping(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.001, 0.001))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['sample']['elapsed_ms'], 1)

    def test_deadline_before_start_skips_reads(self):
        value, _ = load('data/normal.json')
        source = Replay(value['steps'])
        result = sampling.sample(source, mapping.parse(value),
                                 clock=self.stepping(0.0, 10.0))
        self.assertFalse(result['accepted'])
        self.assertTrue(result['budget_exhausted_before_start'])
        self.assertEqual(source.calls, [])

    def test_request_order_matches_plan(self):
        value, _ = load('data/normal.json')
        plan = mapping.parse(value)
        result = sampling.sample(Replay(value['steps']), plan)
        expected = [(mapping.OPERATIONS[b.area], b.address, b.words) for b in plan.blocks]
        observed = [(s['operation'], s['arguments']['address'], s['arguments']['words'])
                    for s in result['sample']['request_order'] if s['step'] == 'data']
        self.assertEqual(observed, expected)

    def test_unit_isolation(self):
        value, _ = load('data/normal.json')
        value['unit'] = 9
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual({s['arguments']['unit'] for s in result['sample']['request_order']}, {9})

    def test_same_words_different_identity_different_digest(self):
        a, _ = load('data/normal.json')
        b = json.loads(json.dumps(a))
        b['unit'] = 2
        self.assertNotEqual(mapping.parse(a).digest(), mapping.parse(b).digest())
        self.assertEqual(mapping.parse(a).digest(), mapping.parse(copy_of(a)).digest())


def copy_of(value):
    return json.loads(json.dumps(value))
