"""单次只读轮询采样：世代前后夹住全部数据块，两次相同且为偶数才接受。"""
import time
from fractions import Fraction

from . import codec, exact

OPERATIONS = {'holding': 'ReadHolding', 'input': 'ReadInput'}
READ_FUNCTIONS = {'ReadHolding': 3, 'ReadInput': 4}
WRITE_FUNCTIONS = {5, 6, 15, 16, 22, 23}
EXCEPTION_NAMES = {
    1: 'illegal_function', 2: 'illegal_data_address', 3: 'illegal_data_value',
    4: 'server_device_failure', 5: 'acknowledge', 6: 'server_device_busy',
    8: 'memory_parity_error', 10: 'gateway_path_unavailable', 11: 'gateway_target_no_response',
}


class SourceError(RuntimeError):
    """设备异常码、传输失败或契约不符；保留 exception 字段，区别于解码诊断。"""

    def __init__(self, code, message, name=None, detail=None):
        super().__init__(message)
        self.code = code
        self.name = name if name is not None else EXCEPTION_NAMES.get(code, 'unknown')
        self.detail = detail

    def frozen(self):
        row = {'code': self.code, 'name': self.name, 'message': str(self)}
        if self.detail is not None:
            row['detail'] = self.detail
        return row


class Deadline:
    """采样墙钟预算；时钟可注入以便离线复演。"""

    def __init__(self, milliseconds, clock=None):
        self.milliseconds = milliseconds
        self.clock = clock if clock is not None else time.monotonic
        self.start = self.clock()

    def elapsed(self):
        return max(0, int(round((self.clock() - self.start) * 1000)))

    def fired(self):
        return self.elapsed() >= self.milliseconds


def _read(source, operation, arguments, deadline, order):
    if deadline.fired():
        raise SourceError(None, '采样时限已耗尽，未发出读取', 'time_budget_exhausted')
    try:
        response = source.call(operation, arguments)
    except SourceError:
        raise
    except RuntimeError as error:
        raise SourceError(None, str(error), 'transport_error') from None
    except ValueError as error:
        raise SourceError(None, str(error), 'source_contract') from None
    if not isinstance(response, dict):
        raise SourceError(None, '源响应必须为对象', 'bad_response')
    if 'exception' in response:
        if 'words' in response:
            raise SourceError(None, '异常响应不得同时携带 words', 'bad_response')
        code = response['exception']
        if type(code) is not int or not 1 <= code <= 0xFF:
            raise SourceError(None, 'exception 必须为 1 到 255 的整数', 'bad_response')
        # 保留异常现场，异常码不解释成正常值。
        order.append({'step': 'exception', 'operation': operation,
                      'arguments': dict(arguments), 'response': {'exception': code}})
        raise SourceError(code, f'设备异常 exception={code}')
    return response


def _words(response, expected, label):
    words = response.get('words')
    count = len(words) if isinstance(words, list) else '非列表'
    mismatch = not isinstance(words, list) or len(words) != expected
    if mismatch or any(type(word) is not int or not 0 <= word <= 0xFFFF for word in words):
        detail = {'label': label, 'expected_words': expected,
                  'actual_words': len(words) if isinstance(words, list) else None,
                  'returned_words': list(words) if isinstance(words, list) else None}
        if mismatch:
            raise SourceError(None, f'{label}返回字数不符：期望 {expected}，实际 {count}',
                              'word_count_mismatch', detail)
        raise SourceError(None, f'{label}寄存器字必须为 0 到 65535', 'bad_response', detail)
    return list(words)


def _generation(source, mapping, deadline, order, step):
    arguments = {'unit': mapping.identity.unit,
                 'address': mapping.generation.address, 'words': 1}
    operation = OPERATIONS[mapping.generation.area]
    words = _words(_read(source, operation, arguments, deadline, order), 1, step)
    order.append({'step': step, 'operation': operation, 'arguments': dict(arguments),
                  'response': {'words': list(words)}})
    # bits 声明小于 16 时只取低位；偶数/相等判据作用于这个声明宽度下的计数值。
    raw = words[0]
    return raw, raw & ((1 << mapping.generation.bits) - 1)


def _block(source, mapping, block, deadline, order):
    arguments = {'unit': mapping.identity.unit,
                 'address': block.address, 'words': block.words}
    operation = OPERATIONS[block.area]
    label = f'{operation}@{block.address}'
    words = _words(_read(source, operation, arguments, deadline, order), block.words, label)
    order.append({'step': 'data', 'operation': operation, 'arguments': dict(arguments),
                  'response': {'words': list(words)}})
    return words


def _sample_once(source, mapping, deadline, order):
    """一次尝试：先读世代，再按计划读全部数据块，最后再读世代。"""
    raw_start, start = _generation(source, mapping, deadline, order, 'generation_start')
    if start % 2 == 1:
        # 奇数表示更新进行中，不继续读取可能被拼接的数据块。
        raise SourceError(None, f'起始世代为奇数（{start}）：采样落在更新周期内', 'generation_unstable')
    evidence = {}
    for block in mapping.blocks:
        evidence[(block.area, block.address)] = _block(source, mapping, block, deadline, order)

    raw_end, end = _generation(source, mapping, deadline, order, 'generation_end')
    if end % 2 == 1:
        raise SourceError(None, f'结束世代为奇数（{end}）：采样落在更新周期内', 'generation_unstable')
    if start != end:
        raise SourceError(None, f'世代在采样期间变化（{start} 到 {end}）', 'generation_changed')
    return {'value': start,
            'start': {'raw_word': raw_start, 'value': start},
            'end': {'raw_word': raw_end, 'value': end}}, evidence, order


def _decode_field(field, words):
    raw = codec.raw_value(words, field.type, field.byte_order, field.word_order)
def _field_words(field, mapping, evidence):
    """按计划块逐字收集字段原字；计划保证字段不跨块，这里再校验完整。"""
    pieces = []
    next_address = field.address
    for block in mapping.blocks:
        if block.area != field.area or block.address > next_address or block.end <= next_address:
            continue
        words = evidence[(block.area, block.address)]
        lo = max(next_address, block.address)
        hi = min(field.address + field.words, block.end)
        pieces.extend(words[lo - block.address:hi - block.address])
        next_address = hi
        if next_address == field.address + field.words:
            return pieces
    raise ValueError(f'字段 {field.id} 的原字证据不完整')


def _hex(raw, words):
    return f'0x{raw:0{4 * words}x}'


def _special(field, raw, ieee):
    """IEEE 特殊值与厂商哨兵按字段策略保留证据，绝不伪装成零或正常值。"""
    if ieee in ('nan', 'inf'):
        if field.ieee_special == 'error':
            raise ValueError(f'字段 {field.id} 出现 IEEE {ieee.upper()}，按 error 策略拒绝整组')
        if field.ieee_special == 'null':
            return {'presence': 'read_ieee_special', 'state': 'ieee_null', 'null': True,
                    'preserved': {'ieee': ieee, 'bits': raw, 'hex': _hex(raw, 2)}}
        return {'presence': 'read_ieee_special', 'state': f'ieee_{ieee}_preserved', 'null': False,
                'preserved': {'ieee': ieee, 'bits': raw, 'hex': _hex(raw, 2)}}
    if field.ieee_special == 'sentinel' and raw == field.sentinel_bits:
        return {'presence': 'read_vendor_sentinel', 'state': 'vendor_sentinel',
                'sentinel': True, 'null': False,
                'preserved': {'bits': raw, 'hex': _hex(raw, 2)}}
    return None


def _decode_field(field, words):
    raw = codec.raw_value(words, field.type, field.byte_order, field.word_order)
    entry = {
        'id': field.id,
        'area': field.area,
        'address': field.address,
        'words': list(words),
        'raw_bits': raw,
        'raw_hex': _hex(raw, len(words)),
        'type': field.type,
        'byte_order': field.byte_order,
        'word_order': field.word_order,
        'presence': 'read',
        'scale': str(field.scale),
        'offset': str(field.offset),
    }
    if field.type != 'bits':
        entry['canonical_bytes_hex'] = codec.canonical_bytes(
            words, field.byte_order, field.word_order).hex()
    if field.type == 'bits':
        extracted = (raw >> field.lsb) & ((1 << field.width) - 1)
        entry['raw_word'] = raw
        entry.update({'lsb': field.lsb, 'width': field.width,
                      'signed': field.signed, 'extracted_unsigned': extracted})
    value, ieee = codec.interpret(field.type, raw, field.signed, field.lsb, field.width)
    if field.type == 'float32':
        entry['ieee'] = ieee
        special = _special(field, raw, ieee)
        if special is not None:
            entry.update(special)
            entry['value'] = None
            entry['engineering'] = None
            entry['null'] = special.get('null', False)
            return entry
    entry['value'] = exact.quantity(Fraction(value))
    entry['engineering'] = exact.quantity(codec.engineering(value, field.scale, field.offset))
    return entry


def _decode(mapping, evidence):
    return [_decode_field(field, _field_words(field, mapping, evidence)) for field in mapping.fields]


def _assert_read_only(source):
    calls = getattr(source, 'calls', None)
    if calls is None:
        return
    allowed = set(READ_FUNCTIONS)
    for call in calls:
        operation = call.get('operation')
        function = call.get('function')
        if operation not in allowed:
            raise RuntimeError(f'只读适配器出现非读取调用：{operation!r}')
        if type(function) is int and function in WRITE_FUNCTIONS:
            raise RuntimeError(f'只读适配器出现写功能码：{function}')


def _retryable(error):
    """瞬时故障与竞态重试整组；配置或契约错误重试不会改变结果，直接终止。

    重试的是整组（世代 + 全部数据块 + 世代），绝不只补失败字段。
    """
    if isinstance(error, SourceError):
        if error.code in (4, 6, 8, 10, 11):
            return True
        if error.code in (1, 2, 3, 5):
            return False
        return error.name in ('generation_unstable', 'generation_changed', 'word_count_mismatch')
    return False


def sample(source, mapping, clock=None):
    """执行只读轮询采样；只有同代整组才发布，否则保留全部原始尝试。"""
    deadline = Deadline(mapping.max_sample_duration_ms, clock)
    if deadline.fired():
        # 预算在开始前已耗尽：不发出任何读取，也不伪造尝试。
        return {
            'accepted': False, 'published': False,
            'map': {'identity': mapping.identity.frozen(), 'map_sha256': mapping.digest(),
                    'generation_register': mapping.generation.frozen(),
                    'read_plan': [block.frozen() for block in mapping.blocks]},
            'generation_rule': _rule(mapping), 'attempts': [], 'budget_exhausted_before_start': True,
            'reason': {'code': None, 'name': 'time_budget_exhausted',
                       'message': '采样时限在开始前已耗尽，未发出读取'},
        }
    attempts = []
    accepted = None
    for index in range(1, mapping.max_attempts + 1):
        order = []
        try:
            generation, evidence, order = _sample_once(source, mapping, deadline, order)
            fields = _decode(mapping, evidence)
        except SourceError as error:
            attempts.append({'attempt': index, 'accepted': False, 'reason': error.frozen(),
                             'request_order': order, 'elapsed_ms': deadline.elapsed()})
            if not _retryable(error) or deadline.fired() or index == mapping.max_attempts:
                break
            continue
        except ValueError as error:
            attempts.append({'attempt': index, 'accepted': False,
                             'reason': {'code': None, 'name': 'decode_error', 'message': str(error)},
                             'request_order': order, 'elapsed_ms': deadline.elapsed()})
            break
        attempts.append({'attempt': index, 'accepted': True, 'elapsed_ms': deadline.elapsed()})
        accepted = {
            'sampling': 'read_only_polled',
            'claim': 'single_unit_same_generation',
            'not_a_snapshot': '只证明本 unit 这组值来自同一更新周期，不构成跨设备同时快照',
            'generation': generation,
            'map': {'identity': mapping.identity.frozen(), 'map_sha256': mapping.digest()},
            'request_order': order,
            'fields': fields,
            'elapsed_ms': deadline.elapsed(),
        }
        break
    _assert_read_only(source)
    result = {
        'accepted': accepted is not None,
        'published': accepted is not None,
        'map': {'identity': mapping.identity.frozen(), 'map_sha256': mapping.digest(),
                'generation_register': mapping.generation.frozen(),
                'read_plan': [block.frozen() for block in mapping.blocks]},
        'generation_rule': _rule(mapping),
        'attempts': attempts,
    }
    if accepted is None:
        result['reason'] = attempts[-1]['reason'] if attempts else {
            'code': None, 'name': 'time_budget_exhausted',
            'message': '采样时限耗尽，未开始新的尝试'}
    else:
        result['sample'] = accepted
    return result


def _rule(mapping):
    return {'accepted_when': 'start == end 且两次均为偶数',
            'counter_wrap_min_ms': mapping.counter_wrap_min_ms,
            'max_sample_duration_ms': mapping.max_sample_duration_ms,
            'wrap_guarantee_checked': True}
