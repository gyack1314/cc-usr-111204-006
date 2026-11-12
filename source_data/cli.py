"""离线复演虚拟寄存器样本并显示只读采样结果。"""
import argparse
import json
from pathlib import Path

from . import io, mapping, sampling


def main():
    parser = argparse.ArgumentParser(description='只读轮询采样（虚拟寄存器源，不连接真实设备）')
    parser.add_argument('command', choices=['inspect', 'sample'])
    parser.add_argument('file')
    args = parser.parse_args()
    value, digest = io.load(args.file)
    if args.command == 'inspect':
        from .domain import summarize
        summary = summarize(value, Path(args.file).parent)
        print(json.dumps({'source_sha256': digest, 'summary': summary}, ensure_ascii=False, indent=2))
        return
    from .replay import Replay
    if 'steps' not in value:
        raise ValueError('资料缺少虚拟源响应的 steps')
    plan = mapping.parse(value)
    result = sampling.sample(Replay(value['steps']), plan)
    result['source_sha256'] = digest
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
