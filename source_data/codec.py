"""纯解码：保留位模式与寄存器证据，工程量用精确有理数表示。"""
import struct
from fractions import Fraction

ORDERS = ('big', 'little')
WORD_TYPES = {'uint16': 1, 'int16': 1, 'uint32': 2, 'int32': 2, 'float32': 2}
TYPES = WORD_TYPES.keys() | {'bits'}


def canonical_bytes(words, byte_order, word_order):
    """先按字顺序排列字，再按字节内顺序形成规范（大端）字节。"""
    ordered = words if word_order == 'big' else list(reversed(words))
    out = bytearray()
    for word in ordered:
        out += bytes((word >> 8, word & 0xFF)) if byte_order == 'big' else bytes((word & 0xFF, word >> 8))
    return bytes(out)


def raw_value(words, type_name, byte_order, word_order):
    """无符号原模式：位段直接取单个原字，其余取规范字节的无符号整数。
    浮点按 IEC 60559 大端位模式处理，字节序只影响寄存器到规范字节的搬运。"""
    if type_name == 'bits':
        return words[0]
    return int.from_bytes(canonical_bytes(words, byte_order, word_order), 'big')


def interpret(type_name, raw, signed=False, lsb=None, width=None):
    """按声明类型与位宽解释符号，返回 (值, IEEE 类别)。"""
    if type_name == 'bits':
        value = (raw >> lsb) & ((1 << width) - 1)
        if signed and value & (1 << (width - 1)):
            value -= 1 << width
        return value, None
    if type_name in ('uint16', 'uint32'):
        return raw, None
    if type_name in ('int16', 'int32'):
        bits = 16 if type_name == 'int16' else 32
        return raw if raw < 1 << (bits - 1) else raw - (1 << bits), None
    if type_name == 'float32':
        if raw in (0x7F800000, 0xFF800000):
            return None, 'inf'
        if raw & 0x7F800000 == 0x7F800000 and raw & 0x007FFFFF:
            return None, 'nan'
        return Fraction(struct.unpack('>f', raw.to_bytes(4, 'big'))[0]), 'finite'
    raise ValueError('未知字段类型')


def engineering(value, scale, offset):
    if value is None:
        raise ValueError('IEEE 特殊值没有有限工程量')
    return Fraction(value) * scale + offset
