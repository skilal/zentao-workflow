"""复用精确行比较，并一次统计同长度片段的出现顺序。"""

from collections import defaultdict
from difflib import SequenceMatcher
from functools import lru_cache


@lru_cache(maxsize=32)
def _line_opcodes(before, after):
    if before == after:
        return (("equal", 0, len(before), 0, len(after)),) if before else ()
    return tuple(SequenceMatcher(None, before, after, autojunk=False).get_opcodes())


def line_opcodes(before, after):
    return _line_opcodes(tuple(before), tuple(after))


def occurrence_counts(source, windows):
    """结果与逐片段扫描此前所有位置相同，同一长度只扫描到最远起点一次。"""
    grouped = defaultdict(lambda: defaultdict(list))
    for index, (start, end) in enumerate(windows):
        grouped[end - start][start].append(index)
    result = [0] * len(windows)
    for width, starts in grouped.items():
        counts = defaultdict(int)
        for position in range(max(starts) + 1):
            key = (tuple(source[position:position + width]), tuple(source[max(0, position - 2):position]),
                   tuple(source[position + width:position + width + 2]))
            for index in starts.get(position, ()):
                result[index] = counts[key]
            counts[key] += 1
    return result
