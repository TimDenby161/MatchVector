"""Detailed playing roles from a line-up's formation and grid position.

API-Football gives each starter a grid "row:col" (row 1 = goalkeeper, then one row per line;
col 1 = the left side) and the team's formation, e.g. "4-2-3-1". From those:

    back line      3 -> CB CB CB        4 -> LB CB CB RB        5 -> LWB CB CB CB RWB
    front line     1 -> ST              2 -> ST ST              3 -> LW ST RW
    single middle  2/3 -> CM            4 -> LM CM CM RM        5 -> LWB CM CM CM RWB
    first of two+  1/2 -> DM            3 -> CM                 4 -> LM/LWB CM CM RM/RWB
    later middles  1/2 -> AM (CM if another line is in front)  3 -> LW AM RW  4 -> LM CM CM RM

(wide players in a 4-man first midfield line are wing-backs behind a back three.)
Roles are grouped for comparing players: left and right versions share a group.
"""

GROUPS = {
    "GK": "GK", "CB": "CB",
    "LB": "FB", "RB": "FB", "LWB": "FB", "RWB": "FB",
    "DM": "DM", "CM": "CM", "AM": "AM",
    "LW": "W", "RW": "W", "LM": "W", "RM": "W",
    "ST": "ST",
}
# Broad API position -> group, for players never seen starting with a grid
FALLBACK = {"G": "GK", "D": "CB", "M": "CM", "F": "ST"}
GROUP_LABELS = {"GK": "Goalkeeper", "CB": "Centre-back", "FB": "Full-back", "DM": "Defensive mid",
                "CM": "Central mid", "AM": "Attacking mid", "W": "Winger", "ST": "Striker"}


def _wide(n, col, left, middle, right):
    return left if col == 1 else right if col == n else middle


def role(formation, grid):
    """Role label ('LB', 'CM', 'RW', ...) or None if it can't be worked out."""
    try:
        lines = [int(x) for x in formation.split("-")]
        row, col = (int(x) for x in grid.split(":"))
    except (AttributeError, ValueError):
        return None
    if row == 1:
        return "GK"
    idx = row - 2                       # 0 = back line
    if idx < 0 or idx >= len(lines) or not 1 <= col <= lines[idx]:
        return None
    n, last = lines[idx], len(lines) - 1
    if idx == 0:                        # back line
        if n == 4:
            return _wide(n, col, "LB", "CB", "RB")
        if n == 5:
            return _wide(n, col, "LWB", "CB", "RWB")
        return "CB"
    if idx == last:                     # front line
        if n >= 3:
            return _wide(n, col, "LW", "ST", "RW")
        return "ST"
    back_three = lines[0] == 3
    middles = last - 1                  # number of midfield lines
    if middles == 1:
        if n == 4:
            return _wide(n, col, "LWB", "CM", "RWB") if back_three else _wide(n, col, "LM", "CM", "RM")
        if n == 5:
            return _wide(n, col, "LWB", "CM", "RWB")
        return "CM"
    if idx == 1:                        # first of several midfield lines
        if n <= 2:
            return "DM"
        if n == 4:
            return _wide(n, col, "LWB", "CM", "RWB") if back_three else _wide(n, col, "LM", "CM", "RM")
        if n == 5:
            return _wide(n, col, "LWB", "CM", "RWB")
        return "CM"
    # later midfield lines (a pair with another line in front is central, e.g. a diamond)
    if n <= 2:
        return "AM" if idx == last - 1 else "CM"
    if n == 3:
        return _wide(n, col, "LW", "AM", "RW")
    if n == 4:
        return _wide(n, col, "LM", "CM", "RM")
    return _wide(n, col, "LW", "AM", "RW")


def group(role_label, broad_position=None):
    return GROUPS.get(role_label) or FALLBACK.get(broad_position)
