"""映射校验与只读读取计划：零基地址、批次不跨禁读洞、字段证据不跨块。"""
import hashlib
import json
import struct
from dataclasses import dataclass
from fractions import Fraction

from . import codec, exact, io

AREAS = ('holding', 'input')
OPERATIONS = {'holding': 'ReadHolding', 'input': 'ReadInput'}
IEEE_POLICIES = ('error', 'preserve', 'null', 'sentinel')
IDENTITY_KEYS = ('device', 'unit', 'map_revision')


@dataclass(frozen=True)
class Identity:
    device: str
    unit: int
    map_revision: str
    areas: tuple

    def frozen(self):
        return {'device': self.device, 'unit': self.unit,
                'map_revision': self.map_revision, 'areas': list(self.areas)}


@dataclass(frozen=True)
class Field:
    id: str
    area: str
    address: int
    words: int
    type: str
    byte_order: str
    word_order: str
    scale: Fraction
    offset: Fraction
    lsb: int | None = None
    width: int | None = None
    signed: bool = False
    ieee_special: str = 'error'
    sentinel_bits: int | None = None

    def frozen(self):
        row = dict(self.__dict__)
        row['scale'] = str(self.scale)
        row['offset'] = str(self.offset)
        return row


@dataclass(frozen=True)
class Generation:
    area: str
    address: int
    bits: int

    def frozen(self):
        return {'area': self.area, 'address': self.address, 'bits': self.bits}


@dataclass(frozen=True)
class Block:
    area: str
    address: int
    words: int

    @property
    def end(self):
        return self.address + self.words

    def frozen(self):
        return {'operation': OPERATIONS[self.area], 'area': self.area,
                'address': self.address, 'words': self.words}


@dataclass(frozen=True)
class Mapping:
    identity: Identity
    revision: str
    max_words: int
    allowed: dict
    generation: Generation
    fields: tuple
    blocks: tuple
    max_attempts: int
    max_sample_duration_ms: int
    counter_wrap_min_ms: int

    def frozen(self):
        return {
            'identity': self.identity.frozen(),
            'revision': self.revision,
            'max_words': self.max_words,
            'allowed_ranges': {area: [[lo, hi] for lo, hi in self.allowed[area]] for area in AREAS},
            'generation': self.generation.frozen(),
            'read_plan': [block.frozen() for block in self.blocks],
            'fields': [field.frozen() for field in self.fields],
            'max_attempts': self.max_attempts,
            'max_sample_duration_ms': self.max_sample_duration_ms,
            'counter_wrap_min_ms': self.counter_wrap_min_ms,
        }

    def digest(self):
        """映射身份（设备、unit、寄存器区、map_revision、地址与解码规则）的稳定摘要。"""
        text = json.dumps(self.frozen(), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(text.encode('utf8')).hexdigest()


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f'{label}必须为布尔值')
    return value


def _allowed(value):
    rows = value.get('allowed_ranges')
    if not isinstance(rows, dict) or set(rows) - set(AREAS):
        raise ValueError('允许地址段必须只含 holding 与 input')
    if set(rows) != set(AREAS):
        raise ValueError('holding 与 input 都必须声明允许地址段')
    out = {}
    for area in AREAS:
        spans = rows[area]
        if not isinstance(spans, list) or not spans:
            raise ValueError(f'{area}缺少允许地址段')
        parsed = []
        for span in spans:
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError('地址段必须为起止二元组')
            lo, hi = io.integer(span[0], 0), io.integer(span[1], 0)
            if hi < lo or hi > 0xFFFF:
                raise ValueError('地址段端点越界')
            parsed.append((lo, hi))
        parsed.sort()
        for (_, previous_hi), (next_lo, _) in zip(parsed, parsed[1:]):
            if next_lo <= previous_hi:
                raise ValueError('允许地址段不得重叠')
        out[area] = tuple(parsed)
    return out


def _segment(allowed, area, address, words):
    """字段必须整体落在单个允许地址段内，跨段即跨禁读洞。"""
    for lo, hi in allowed[area]:
        if lo <= address and address + words - 1 <= hi:
            return (lo, hi)
    raise ValueError(f'{area}地址 {address} 起 {words} 字跨出允许地址段')


def _sentinel_bits(text):
    if not isinstance(text, str) or not text:
        raise ValueError('sentinel 必须为非空十进制字符串')
    try:
        value = exact.fraction(text, 'sentinel')
        packed = struct.pack('>f', float(value))
        if struct.unpack('>f', packed)[0] in (float('inf'), float('-inf')):
            raise ValueError
        if not value == Fraction(struct.unpack('>f', packed)[0]):
            raise ValueError('sentinel 不能精确表示为 float32')
    except (ValueError, OverflowError, TypeError):
        raise ValueError('sentinel 不是有限且可精确表示的 float32') from None
    return int.from_bytes(packed, 'big')


def _parse_field(raw, allowed, gen_area, gen_address):
    if not isinstance(raw, dict):
        raise ValueError('字段必须为对象')
    field_id = raw.get('id')
    if not isinstance(field_id, str) or not field_id:
        raise ValueError('字段身份为空')
    area = raw.get('area')
    if area not in AREAS:
        raise ValueError('未知寄存器区')
    type_name = raw.get('type')
    if type_name not in codec.TYPES:
        raise ValueError('未知字段类型')
    if raw.get('byte_order') not in codec.ORDERS or raw.get('word_order') not in codec.ORDERS:
        raise ValueError('字节或字顺序未知')
    address = io.integer(raw.get('address'), 0)
    if type_name in codec.WORD_TYPES:
        words = io.integer(raw.get('words'), 1)
        if codec.WORD_TYPES[type_name] != words:
            raise ValueError('字段字数与声明类型不符')
    else:
        if 'words' in raw and (type(raw['words']) is not int or raw['words'] != 1):
            raise ValueError('位段只允许一个字')
        words = 1
    if address + words > 0x10000:
        raise ValueError('字段越界')
    lsb = width = None
    signed = False
    if type_name == 'bits':
        lsb = io.integer(raw.get('lsb'), 0)
        width = io.integer(raw.get('width'), 1)
        if lsb + width > 16:
            raise ValueError('位段越界')
        signed = _boolean(raw.get('signed', False), 'signed')
    scale = exact.fraction(raw.get('scale', '1'), 'scale')
    offset = exact.fraction(raw.get('offset', '0'), 'offset')
    if scale == 0:
        raise ValueError('scale 不得为零，否则工程量恒定且与读到的原值无关')
    policy = raw.get('ieee_special', 'error')
    if type_name != 'float32':
        if 'ieee_special' in raw or 'sentinel' in raw:
            raise ValueError('仅 float32 允许 IEEE 特殊值策略')
        policy = 'error'
    elif policy not in IEEE_POLICIES:
        raise ValueError('IEEE 特殊值策略未知')
    sentinel = None
    if policy == 'sentinel':
        sentinel = _sentinel_bits(raw.get('sentinel'))
    elif 'sentinel' in raw:
        raise ValueError('仅 sentinel 策略允许声明哨兵')
    _segment(allowed, area, address, words)
    if area == gen_area and address <= gen_address <= address + words - 1:
        # 字段与世代计数共用原字时无法静态判定期内值是否属于同一更新周期。
        raise ValueError(f'字段 {field_id} 与世代寄存器地址重叠')
    return Field(field_id, area, address, words, type_name, raw['byte_order'],
                 raw['word_order'], scale, offset, lsb, width, signed, policy, sentinel)


def _plan(allowed, fields, max_words):
    """计划只读字段所需地址：同一允许段内的相邻需求可合并，批次不超过最大字数。

    合并只在同一允许地址段内进行；每个批次本身连续，因此永远不会跨越禁读空洞。
    """
    blocks = []
    for area in AREAS:
        wanted = sorted((field.address, field.address + field.words - 1)
                        for field in fields if field.area == area)
        merged = []
        for lo, hi in wanted:
            segment = _segment(allowed, area, lo, hi - lo + 1)
            # 同一允许段内且合起来不超过设备上限时可共用一个批次；
            # 中间虽是未声明的地址，但都在允许段内，没有跨越禁读洞。
            if (merged and merged[-1][2] == segment
                    and max(merged[-1][1], hi) - merged[-1][0] + 1 <= max_words):
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi), segment)
            else:
                merged.append((lo, hi, segment))
        for lo, hi, _ in merged:
            cursor = lo
            while cursor <= hi:
                end = min(cursor + max_words - 1, hi)
                # 不切断任何字段：把批次末端退到包含它的字段起点之前。
                for f_lo, f_hi in wanted:
                    if f_lo <= end < f_hi:
                        end = f_lo - 1
                if end < cursor:
                    raise ValueError('字段无法在不跨禁读洞的批读中容纳')
                blocks.append(Block(area, cursor, end - cursor + 1))
                cursor = end + 1
    return tuple(blocks)


def parse(value):
    """解析访问配置；任何不能形成整组同代采样的声明都直接拒绝。"""
    if not isinstance(value, dict):
        raise ValueError('映射必须为对象')
    for key in IDENTITY_KEYS:
        if key not in value:
            raise ValueError(f'缺少映射身份 {key}')
    device, revision = value['device'], value['map_revision']
    if not isinstance(device, str) or not device:
        raise ValueError('device 必须为非空字符串')
    if not isinstance(revision, str) or not revision:
        raise ValueError('map_revision 必须为非空字符串')
    unit = io.integer(value.get('unit'), 1)
    max_words = min(io.integer(value.get('max_words'), 1), 0x10000)
    allowed = _allowed(value)

    raw_generation = value.get('generation')
    if not isinstance(raw_generation, dict):
        raise ValueError('缺少世代声明')
    gen_area = raw_generation.get('area')
    if gen_area not in AREAS:
        raise ValueError('世代寄存器区未知')
    gen_address = io.integer(raw_generation.get('address'), 0)
    gen_bits = io.integer(raw_generation.get('bits'), 1)
    if gen_bits > 16 or gen_address + 1 > 0x10000:
        raise ValueError('世代位宽或地址越界')
    generation = Generation(gen_area, gen_address, gen_bits)
    _segment(allowed, gen_area, gen_address, 1)

    raw_fields = value.get('fields')
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError('字段表为空')
    io.unique(raw_fields, 'id')

    attempts = io.integer(value.get('max_attempts', 3), 1)
    duration = io.integer(value.get('max_sample_duration_ms', 1000), 1)
    wrap = io.integer(value.get('counter_wrap_min_ms', 100000), 1)
    if duration >= wrap:
        raise ValueError('采样时限必须严格短于计数器完整回绕周期')

    fields = tuple(_parse_field(raw, allowed, gen_area, gen_address) for raw in raw_fields)
    for field in fields:
        if field.words > max_words:
            raise ValueError(f'字段 {field.id} 字数超过设备最大字数')

    blocks = _plan(allowed, fields, max_words)
    identity = Identity(device, unit, revision, tuple(sorted({field.area for field in fields})))
    return Mapping(identity, revision, max_words, allowed, generation,
                   fields, blocks, attempts, duration, wrap)
