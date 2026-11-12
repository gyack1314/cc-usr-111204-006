"""精确有理数：只在最终工程量上使用，避免二进制浮点干扰结果。"""
from fractions import Fraction


def fraction(value, label):
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, str) and value:
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError(f'{label}必须为精确有理数字符串') from None
    raise ValueError(f'{label}必须为精确有理数字符串')


def quantity(value):
    """十进制工程量的JSON形状：整数用数字，其余用精确分数文本。"""
    if value.denominator == 1:
        return int(value.numerator)
    return f'{value.numerator}/{value.denominator}'
