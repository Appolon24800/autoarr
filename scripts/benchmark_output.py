"""Measure stdout size against the former pretty/full JSON, without API calls."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from autoarr.cli import parse
from autoarr.output import format_result, render_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path, help='saved native release array')
    args = parser.parse_args()
    data = json.loads(args.file.read_text())
    options = parse(['result', '--file', str(args.file)], {'apps': {}})
    before = len(format_result(data).encode())
    after = len(render_result(data, options, 'abcdef123456', 'releases').encode())
    print(json.dumps({'records': len(data), 'previousPrettyBytes': before, 'compactBytes': after,
                      'reductionPercent': round(100 * (1 - after / before), 2),
                      'measurement': 'UTF-8 stdout bytes, not tokenizer counts; native data retained in private cache'}))


if __name__ == '__main__':
    main()
