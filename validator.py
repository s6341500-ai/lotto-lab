"""당첨 데이터의 유일한 검증 경계."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
FIRST_DRAW = date(2002, 12, 7)


def today_kst():
    return datetime.now(KST).date()


@dataclass(frozen=True)
class Draw:
    round: int
    draw_date: str
    numbers: tuple[int, ...]
    bonus: int


def validate_draw(draw: Draw, today=None) -> Draw:
    if type(draw.round) is not int or draw.round < 1:
        raise ValueError('회차는 양의 정수여야 합니다.')
    day = date.fromisoformat(draw.draw_date)
    if day != FIRST_DRAW + timedelta(weeks=draw.round - 1):
        raise ValueError('회차와 추첨일이 일치하지 않습니다.')
    if day > (today or today_kst()):
        raise ValueError('미래 추첨 데이터입니다.')
    values = (*draw.numbers, draw.bonus)
    if len(draw.numbers) != 6 or any(type(n) is not int or not 1 <= n <= 45 for n in values):
        raise ValueError('당첨번호 6개와 보너스는 1~45의 정수여야 합니다.')
    if len(set(values)) != 7:
        raise ValueError('당첨번호와 보너스는 서로 달라야 합니다.')
    return Draw(draw.round, day.isoformat(), tuple(sorted(draw.numbers)), draw.bonus)
