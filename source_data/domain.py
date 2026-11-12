"""清点寄存器映射及字数，不合并读取或解码字段。"""
from .io import unique,integer

def summarize(v,root):
    unique(v['fields'],'id');integer(v['max_words'],1);integer(v['unit'],1)
    total=0
    for f in v['fields']:
        integer(f['address']);total+=integer(f['words'],1)
        if f['area'] not in ('holding','input'):raise ValueError('未知寄存器区')
        if f['byte_order'] not in ('big','little') or f['word_order'] not in ('big','little'):raise ValueError('字序未知')
    return {'fields':len(v['fields']),'declared_field_words':total,'unit':v['unit'],'map_revision':v['map_revision'],'steps':len(v['steps'])}
