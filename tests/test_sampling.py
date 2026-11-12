"""只读轮询采样：世代夹取、批读计划、解码证据与失败预算。"""
import struct
import unittest
from fractions import Fraction
from pathlib import Path

from source_data import codec, mapping, sampling
from source_data.io import load
from source_data.replay import Replay


def config(**changes):
    """以 normal.json 为底，按需替换字段。"""
    value, _ = load('data/normal.json')
    value = dict(value)
    value.update(changes)
    return value


def clock_at(*offsets):
    """返回按采样读取次数推进的假时钟；超出的读取停在最后一个时刻。"""
    state = {'i': 0,
             'stamps': [offset / 1000.0 for offset in offsets]}

    def clock():
        i = min(state['i'], len(state['stamps']) - 1)
        state['i'] += 1
        return state['stamps'][i]
    return clock


def response(words):
    return {'words': list(words)}


def gen(value):
    return {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 0, 'words': 1},
            'response': response([value])}


def data(address, words):
    return {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': address, 'words': len(words)},
            'response': response(words)}


def config_unit(unit, **changes):
    """改写 unit，并同步调整虚拟源步骤中的 unit 参数。"""
    value = config(**changes)
    value['unit'] = unit
    for step in value['steps']:
        if 'arguments' in step and 'unit' in step['arguments']:
            step['arguments']['unit'] = unit
    return value


class PlanTests(unittest.TestCase):
    def test_normal_plan_is_two_non_touching_blocks(self):
        plan = mapping.parse(config())
        self.assertEqual([(b.area, b.address, b.words) for b in plan.blocks],
                         [('holding', 1, 2), ('holding', 8, 1)])
        self.assertTrue(all(
            any(lo <= b.address and b.end - 1 <= hi for lo, hi in plan.allowed[b.area])
            for b in plan.blocks))

    def test_zero_based_declaration_never_added_by_one(self):
        plan = mapping.parse(config())
        self.assertEqual(plan.generation.address, 0)
        self.assertEqual([b.address for b in plan.blocks if b.words == 1], [8])

    def test_hole_is_not_crossed(self):
        value = config(fields=[{'id': 'current', 'area': 'holding', 'address': 3, 'words': 2,
                                'type': 'float32', 'byte_order': 'big', 'word_order': 'little'}])
        with self.assertRaises(ValueError):
            mapping.parse(value)

    def test_adjacent_fields_merge_into_one_read(self):
        value = config(fields=[
            {'id': 'a', 'area': 'holding', 'address': 1, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'},
            {'id': 'b', 'area': 'holding', 'address': 2, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'}])
        plan = mapping.parse(value)
        self.assertEqual([(b.address, b.words) for b in plan.blocks], [(1, 2)])

    def test_reads_fill_allowed_segment_without_crossing_hole(self):
        # 地址 9 未声明但属于允许段 [8,10]，可一并读取；地址 4..7 是禁读洞。
        value = config(fields=[
            {'id': 'a', 'area': 'holding', 'address': 8, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'},
            {'id': 'b', 'area': 'holding', 'address': 10, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'}])
        self.assertEqual([(b.address, b.words) for b in mapping.parse(value).blocks], [(8, 3)])

    def test_hole_gap_forces_separate_reads(self):
        # max_words 限制为 2 时不能再合并 8..10，仍可各自读取，均不跨洞。
        value = config(max_words=2, fields=[
            {'id': 'a', 'area': 'holding', 'address': 8, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'},
            {'id': 'b', 'area': 'holding', 'address': 10, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'}])
        self.assertEqual([b.address for b in mapping.parse(value).blocks], [8, 10])

    def test_max_words_respected(self):
        value = config(max_words=1, fields=[
            {'id': 'a', 'area': 'holding', 'address': 1, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'},
            {'id': 'b', 'area': 'holding', 'address': 2, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big'}])
        self.assertEqual([(b.address, b.words) for b in mapping.parse(value).blocks], [(1, 1), (2, 1)])

    def test_invalid_area_rejected(self):
        value = config(fields=[{'id': 'x', 'area': 'coils', 'address': 8, 'words': 1, 'type': 'bits',
                                'lsb': 0, 'width': 1, 'byte_order': 'big', 'word_order': 'big'}])
        with self.assertRaises(ValueError):
            mapping.parse(value)

    def test_identity_is_part_of_map(self):
        base = mapping.parse(config())
        moved = mapping.parse(config(unit=2))
        revised = mapping.parse(config(map_revision='vendor-A-r3'))
        self.assertNotEqual(base.digest(), moved.digest())
        self.assertNotEqual(base.digest(), revised.digest())
        self.assertEqual(mapping.parse(config()).digest(), base.digest())

    def test_limits_are_enforced(self):
        with self.assertRaises(ValueError):
            mapping.parse(config(max_sample_duration_ms=100000, counter_wrap_min_ms=100000))
        with self.assertRaises(ValueError):
            mapping.parse(config(max_attempts=0))
        with self.assertRaises(ValueError):
            mapping.parse(config(fields=[{'id': 'zero', 'area': 'holding', 'address': 1, 'words': 1,
                                          'type': 'uint16', 'scale': '0',
                                          'byte_order': 'big', 'word_order': 'big'}]))
        with self.assertRaises(ValueError):
            mapping.parse(config(fields=[{'id': 'current', 'area': 'holding', 'address': 1, 'words': 2,
                                          'type': 'float32', 'byte_order': 'big', 'word_order': 'little'},
                                         {'id': 'alias', 'area': 'holding', 'address': 0, 'words': 1,
                                          'type': 'uint16', 'byte_order': 'big', 'word_order': 'big'}]))

    def test_field_over_generation_address_rejected(self):
        with self.assertRaises(ValueError):
            mapping.parse(config(fields=[{'id': 'alias', 'area': 'holding', 'address': 0, 'words': 1,
                                          'type': 'uint16',
                                          'byte_order': 'big', 'word_order': 'big'}]))

    def test_non_16_generation_bit_width_is_masked(self):
        value = config(generation={'area': 'holding', 'address': 0, 'bits': 8},
                       steps=[gen(0x100), data(1, [0, 17224]), data(8, [20]), gen(0x100)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['sample']['generation']['start'],
                         {'raw_word': 0x100, 'value': 0})


class OrderTests(unittest.TestCase):
    def test_byte_and_word_orders(self):
        cases = {
            ('big', 'big'): 0x12345678,
            ('little', 'big'): 0x34127856,
            ('big', 'little'): 0x56781234,
            ('little', 'little'): 0x78563412,
        }
        for (byte_order, word_order), expected in cases.items():
            with self.subTest(byte=byte_order, word=word_order):
                raw = codec.raw_value([0x1234, 0x5678], 'uint32', byte_order, word_order)
                self.assertEqual(raw, expected)
                self.assertEqual(len(codec.canonical_bytes([0x1234, 0x5678], byte_order, word_order)), 4)

    def test_all_four_orders_are_decoded_in_a_sample(self):
        fields = []
        for index, (byte_order, word_order) in enumerate(
                (('big', 'big'), ('little', 'big'), ('big', 'little'), ('little', 'little'))):
            fields.append({'id': f'u{index}', 'area': 'holding', 'address': 1, 'words': 2,
                           'type': 'uint32', 'byte_order': byte_order, 'word_order': word_order})
        value = config(fields=fields, steps=[
            gen(4), data(1, [0x1234, 0x5678]), gen(4)])
        source = Replay(value['steps'])
        result = sampling.sample(source, mapping.parse(value))
        self.assertTrue(result['accepted'])
        values = {field['id']: field['value'] for field in result['sample']['fields']}
        self.assertEqual(values, {'u0': 0x12345678, 'u1': 0x34127856,
                                  'u2': 0x56781234, 'u3': 0x78563412})
        # 四个字段共享同一次读取的原始字，但各自记录字节证据。
        self.assertEqual(len(result['sample']['request_order']), 3)
        self.assertEqual({field['bytes_word_evidence'] if 'bytes_word_evidence' in field else 0
                          for field in result['sample']['fields']}, {0})
        for field in result['sample']['fields']:
            self.assertEqual(field['words'], [0x1234, 0x5678])

    def test_bitfield_on_unsigned_word(self):
        value = config(steps=[
            gen(2), data(1, [0, 17224]), data(8, [0xABCD]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        field = next(f for f in result['sample']['fields'] if f['id'] == 'state')
        # 位段先在无符号原字上提取：0xABCD 的 bit2 起 3 位为 011，即 3。
        self.assertEqual(field['raw_word'], 0xABCD)
        self.assertEqual(field['extracted_unsigned'], 3)
        self.assertEqual(field['value'], 3)
        self.assertEqual(field['engineering'], 3)

    def test_signed_width_interprets_sign_only_after_extraction(self):
        state = {'id': 'state', 'area': 'holding', 'address': 8, 'words': 1, 'type': 'bits',
                 'lsb': 4, 'width': 3, 'signed': True, 'byte_order': 'big', 'word_order': 'big'}
        value = config(fields=[
            {'id': 'current', 'area': 'holding', 'address': 1, 'words': 2, 'type': 'float32',
             'byte_order': 'big', 'word_order': 'little'}, state],
            steps=[gen(2), data(1, [0, 17224]), data(8, [0xABCD]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        field = next(f for f in result['sample']['fields'] if f['id'] == 'state')
        # 0xABCD 的 bit4 起 3 位为 100：无符号 4，按声明宽度解释为 -4。
        self.assertEqual(field['extracted_unsigned'], 4)
        self.assertEqual(field['value'], -4)

    def test_uint_and_int_and_exact_scale_offset(self):
        fields = [
            {'id': 'u', 'area': 'holding', 'address': 1, 'words': 1, 'type': 'uint16',
             'byte_order': 'big', 'word_order': 'big', 'scale': '1/4', 'offset': '10'},
            {'id': 'i', 'area': 'holding', 'address': 2, 'words': 1, 'type': 'int16',
             'byte_order': 'big', 'word_order': 'big', 'scale': '1', 'offset': '0'}]
        value = config(fields=fields, steps=[gen(2), data(1, [0xFFFF, 0x8000]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        out = {f['id']: f for f in result['sample']['fields']}
        self.assertEqual(out['u']['value'], 65535)
        self.assertEqual(out['u']['engineering'], '65575/4')
        self.assertEqual(out['i']['value'], -32768)


class SpecialValueTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(config()['fields'][0])

    def float_field(self, **changes):
        field = dict(self.base)
        field.update(changes)
        return field

    def run_with(self, field, words):
        value = config(fields=[field], steps=[gen(2), data(1, words), gen(2)])
        return sampling.sample(Replay(value['steps']), mapping.parse(value))

    def test_nan_is_rejected_by_default(self):
        # 按 float32 大端位模式构造 NaN：0x7FC00000 在 little 字序下拆为 [0x0000, 0x7FC0]。
        nan = [0x0000, 0x7FC0]
        result = self.run_with(self.float_field(), nan)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason']['name'], 'decode_error')

    def test_nan_preserved_with_bits(self):
        nan = [0x0000, 0x7FC0]
        result = self.run_with(self.float_field(ieee_special='preserve'), nan)
        entry = result['sample']['fields'][0]
        self.assertEqual(entry['value'], None)
        self.assertEqual(entry['ieee'], 'nan')
        self.assertEqual(entry['preserved'], {'ieee': 'nan', 'bits': 0x7FC00000, 'hex': '0x7fc00000'})

    def test_inf_null_policy(self):
        result = self.run_with(self.float_field(ieee_special='null'), [0x0000, 0x7F80])
        entry = result['sample']['fields'][0]
        self.assertTrue(entry['null'])
        self.assertEqual(entry['state'], 'ieee_null')
        self.assertIsNone(entry['engineering'])

    def test_vendor_sentinel_distinguished_from_missing(self):
        # 厂商写 -9999 表示“无测量”，这是读到的值，不是缺读。
        # float32 大端位模式 0xC61C3C00 在 little 字序下拆为 [0x3C00, 0xC61C]。
        result = self.run_with(self.float_field(ieee_special='sentinel', sentinel='-9999'),
                               [0x3C00, 0xC61C])
        entry = result['sample']['fields'][0]
        self.assertEqual(entry['presence'], 'read_vendor_sentinel')
        self.assertEqual((entry['sentinel'], entry['null'], entry['value']), (True, False, None))
        self.assertEqual(entry['preserved'], {'bits': 0xC61C3C00, 'hex': '0xc61c3c00'})

    def test_missing_block_is_distinct_from_special(self):
        value = config(max_attempts=1,
                       fields=[self.float_field(ieee_special='preserve')],
                       steps=[gen(2), {'operation': 'ReadHolding',
                                       'arguments': {'unit': 1, 'address': 1, 'words': 2},
                                       'exception': 4}])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason']['name'], 'server_device_failure')
        self.assertNotIn('sample', result)

    def test_sentinel_must_be_finite_float32(self):
        for text in ('nan', 'inf', '1e300', ''):
            with self.subTest(text=text), self.assertRaises(ValueError):
                mapping.parse(config(fields=[self.float_field(ieee_special='sentinel', sentinel=text)]))


class GenerationTests(unittest.TestCase):
    def test_accepts_equal_even_generation(self):
        value = config(steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'] and result['published'])
        self.assertEqual(result['sample']['generation']['value'], 2)
        self.assertEqual([c['step'] for c in result['sample']['request_order']],
                         ['generation_start', 'data', 'data', 'generation_end'])
        self.assertEqual(result['sample']['request_order'][1]['arguments'],
                         {'unit': 1, 'address': 1, 'words': 2})
        self.assertEqual(result['sample']['not_a_snapshot'].find('快照') >= 0, True)

    def test_rejects_change_between_blocks(self):
        value = config(max_attempts=1, steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(4)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertFalse(result['published'])
        self.assertNotIn('sample', result)
        self.assertEqual(result['attempts'][0]['reason']['name'], 'generation_changed')
        self.assertEqual(len(result['attempts'][0]['request_order']), 4)

    def test_even_generation_changed_later_but_both_odd_rejected(self):
        value = config(max_attempts=1, steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(3)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertEqual(result['reason']['name'], 'generation_unstable')

    def test_rejects_odd_generation_without_reading_data(self):
        value = config(steps=[gen(3), gen(3), gen(3)])
        source = Replay(value['steps'])
        result = sampling.sample(source, mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason']['name'], 'generation_unstable')
        self.assertEqual(len(result['attempts']), 3)
        # 起始世代为奇数时不继续读取数据块，避免拼接更新中的值。
        self.assertEqual([c['arguments']['address'] for c in source.calls], [0, 0, 0])

    def test_retry_repeats_the_whole_group(self):
        value = config(steps=[
            gen(2), data(1, [0, 17224]), data(8, [20]), gen(4),
            gen(6), data(1, [0, 17224]), data(8, [20]), gen(6)])
        source = Replay(value['steps'])
        result = sampling.sample(source, mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual(len(result['attempts']), 2)
        self.assertFalse(result['attempts'][0]['accepted'])
        self.assertEqual(result['sample']['request_order'][0]['step'], 'generation_start')
        self.assertEqual(len(source.calls), 8)

    def test_partial_failure_does_not_publish(self):
        value = config(steps=[
            gen(2),
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 1, 'words': 2},
             'exception': 2},
        ])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['attempts'][0]['request_order'][-1]['response'], {'exception': 2})

    def test_short_word_count_invalidates_group(self):
        value = config(max_attempts=1, steps=[
            gen(2),
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 1, 'words': 2},
             'response': response([0])},
            gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason']['name'], 'word_count_mismatch')
        self.assertEqual(result['reason']['detail']['expected_words'], 2)
        self.assertEqual(result['reason']['detail']['actual_words'], 1)

    def test_long_word_count_invalidates_group(self):
        value = config(max_attempts=1, steps=[
            gen(2),
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 1, 'words': 2},
             'response': response([0, 1, 2])},
            gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertEqual(result['reason']['name'], 'word_count_mismatch')

    def test_budget_stops_retries(self):
        value = config(max_sample_duration_ms=1, steps=[gen(2), data(1, [0, 17224]),
                                                       gen(2), data(1, [0, 17224]),
                                                       data(8, [20]), gen(2)])
        # 第 1 次读取前时钟为 0；读取数据时已过去 10 毫秒，预算 1 毫秒耗尽。
        result = sampling.sample(Replay(value['steps']), mapping.parse(value),
                                 clock=clock_at(0, 0, 10, 20, 30, 40))
        self.assertFalse(result['accepted'])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['reason']['name'], 'time_budget_exhausted')

    def test_wide_word_count_and_scaled_float(self):
        value = config(steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        field = next(f for f in result['sample']['fields'] if f['id'] == 'current')
        self.assertEqual(field['raw_bits'], 0x43480000)
        self.assertEqual(field['raw_hex'], '0x43480000')
        self.assertEqual(field['canonical_bytes_hex'], '43480000')

    def test_generation_word_wrap_is_not_silently_merged(self):
        value = config(max_attempts=1,
                       steps=[gen(65534), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason']['name'], 'generation_changed')

    def test_even_generation_wrap_requires_wrap_guarantee(self):
        # 相等偶数判据只有在采样时限短于完整回绕周期时才可靠；
        # 不满足的配置在解析阶段被拒绝，而不会在运行期静默接受可能回绕过的值。
        with self.assertRaises(ValueError):
            mapping.parse(config(max_sample_duration_ms=100000, counter_wrap_min_ms=100000))
        with self.assertRaises(ValueError):
            mapping.parse(config(max_sample_duration_ms=200000, counter_wrap_min_ms=100000))
        self.assertEqual(mapping.parse(config(max_sample_duration_ms=99999,
                                              counter_wrap_min_ms=100000)).counter_wrap_min_ms,
                         100000)

    def test_attempts_are_capped(self):
        value = config(max_attempts=2, steps=[gen(3), gen(3), gen(3)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertEqual(len(result['attempts']), 2)

    def test_exception_code_preserved(self):
        value = config(max_attempts=1, steps=[
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 0, 'words': 1},
             'exception': 11},
        ])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertEqual(result['reason'], {'code': 11, 'name': 'gateway_target_no_response',
                                            'message': '设备异常 exception=11'})
        self.assertNotIn('words', result['attempts'][0]['request_order'][0]['response'])

    def test_busy_is_retried(self):
        value = config(steps=[
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 0, 'words': 1},
             'exception': 6},
            gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['attempts'][0]['reason']['name'], 'server_device_busy')

    def test_illegal_address_is_not_retried(self):
        value = config(steps=[
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 0, 'words': 1},
             'exception': 2},
            gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertFalse(result['accepted'])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['reason'], {'code': 2, 'name': 'illegal_data_address',
                                            'message': '设备异常 exception=2'})

    def test_transient_block_failure_retries_whole_group(self):
        value = config(max_attempts=2, steps=[
            gen(2),
            {'operation': 'ReadHolding', 'arguments': {'unit': 1, 'address': 1, 'words': 2},
             'exception': 4},
            gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        result = sampling.sample(Replay(value['steps']), mapping.parse(value))
        self.assertTrue(result['accepted'])
        self.assertEqual([a['accepted'] for a in result['attempts']], [False, True])
        # 第二次尝试重新读取世代和全部块；第一组读取痕迹留在记录里。
        self.assertEqual(result['sample']['request_order'][0]['step'], 'generation_start')
        self.assertEqual(result['attempts'][0]['request_order'][0]['response'], {'words': [2]})
        self.assertEqual(result['attempts'][0]['request_order'][-1]['response'], {'exception': 4})

    def test_only_read_functions_used(self):
        value = config(steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        source = Replay(value['steps'])
        sampling.sample(source, mapping.parse(value))
        self.assertEqual({c['function'] for c in source.calls}, {3})


class ReplayGuardTests(unittest.TestCase):
    def test_write_operations_rejected_without_side_effects(self):
        source = Replay([])
        for operation in ('Write', 'WriteHolding', 'Coil'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                source.call(operation, {'unit': 1, 'address': 0})
        self.assertEqual(source.calls, [])
        self.assertEqual(source.remaining(), 0)

    def test_write_function_code_rejected(self):
        source = Replay([])
        with self.assertRaises(ValueError):
            source.call('ReadHolding', {'unit': 1, 'address': 0, 'words': 1, 'function': 16})

    def test_no_write_function_in_any_attempt_record(self):
        value = config_unit(7, steps=[gen(2), data(1, [0, 17224]), data(8, [20]), gen(2)])
        source = Replay(value['steps'])
        result = sampling.sample(source, mapping.parse(value))
        self.assertTrue(result['accepted'])
        for step in result['sample']['request_order']:
            self.assertIn(step['operation'], ('ReadHolding', 'ReadInput'))
        self.assertEqual(result['sample']['request_order'][1]['arguments']['unit'], 7)
        self.assertEqual({c['function'] for c in source.calls}, {3})
