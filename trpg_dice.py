"""CoC 7 dice; randomness stays on the server and is injectable in tests."""
import re
import secrets


def loss(expression, randbelow=secrets.randbelow):
    if re.fullmatch(r'\d+', expression):
        return int(expression)
    match = re.fullmatch(r'(\d*)d(\d+)', expression.lower())
    if not match:
        raise ValueError('invalid SAN loss')
    count, sides = int(match[1] or 1), int(match[2])
    if not 1 <= count <= 100 or not 1 <= sides <= 1000:
        raise ValueError('SAN dice out of range')
    return sum(randbelow(sides) + 1 for _ in range(count))


def roll(target, difficulty='regular', bonus=0, penalty=0, randbelow=secrets.randbelow):
    if type(target) is not int or not 0 <= target <= 100:
        raise ValueError('target must be 0..100')
    if difficulty not in ('regular', 'hard', 'extreme'):
        raise ValueError('invalid difficulty')
    if any(type(n) is not int or not 0 <= n <= 2 for n in (bonus, penalty)):
        raise ValueError('bonus/penalty must be 0..2')
    units = randbelow(10)
    tens = [randbelow(10) for _ in range(1 + abs(bonus - penalty))]
    candidates = [t * 10 + units or 100 for t in tens]
    value = (max if penalty > bonus else min)(candidates)
    if value == 1:
        grade = 'critical'
    elif value == 100 or (target < 50 and value >= 96):
        grade = 'fumble'
    elif value <= target // 5:
        grade = 'extreme'
    elif value <= target // 2:
        grade = 'hard'
    elif value <= target:
        grade = 'regular'
    else:
        grade = 'failure'
    ranks = {'fumble': -1, 'failure': 0, 'regular': 1, 'hard': 2, 'extreme': 3, 'critical': 4}
    return dict(value=value, units=units, tens=tens, candidates=candidates, target=target,
                difficulty=difficulty, grade=grade, success=ranks[grade] >= ranks[difficulty])
