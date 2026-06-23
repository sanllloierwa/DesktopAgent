"Human-like operation simulation: random delays, mouse trajectories"

from __future__ import annotations

import random
import time
import math


def random_delay(min_sec: float = 0.1, max_sec: float = 1.5) -> None:
    """模拟人类操作间隔"""
    time.sleep(random.uniform(min_sec, max_sec))


def human_typing_interval(text: str, base_cpm: int = 300) -> list[float]:
    """为字符串中每个字符生成拟人化输入间隔（毫秒）。
    cpm = characters per minute，基准 300 约为普通打字速度。
    """
    intervals: list[float] = []
    for ch in text:
        delay = (60.0 / base_cpm) * random.uniform(0.7, 1.5)
        # 对于标点符号和特殊字符，增加额外停顿
        if ch in ",.;:!?，。；：！？":
            delay += random.uniform(0.05, 0.2)
        intervals.append(delay)
    return intervals


def generate_mouse_path(
    start: tuple[int, int],
    end: tuple[int, int],
    steps: int | None = None,
) -> list[tuple[int, int]]:
    """生成带贝塞尔曲线的人类鼠标轨迹"""
    if steps is None:
        dist = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(10, int(dist / 5))

    # 随机控制点，让路径弯曲
    cx1 = start[0] + (end[0] - start[0]) * random.uniform(0.2, 0.4) + random.randint(-50, 50)
    cy1 = start[1] + (end[1] - start[1]) * random.uniform(0.1, 0.3) + random.randint(-30, 30)
    cx2 = start[0] + (end[0] - start[0]) * random.uniform(0.6, 0.8) + random.randint(-50, 50)
    cy2 = start[1] + (end[1] - start[1]) * random.uniform(0.7, 0.9) + random.randint(-30, 30)

    path: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 3 * start[0] + 3 * (1 - t) ** 2 * t * cx1 + 3 * (1 - t) * t ** 2 * cx2 + t ** 3 * end[0]
        y = (1 - t) ** 3 * start[1] + 3 * (1 - t) ** 2 * t * cy1 + 3 * (1 - t) * t ** 2 * cy2 + t ** 3 * end[1]
        path.append((round(x), round(y)))
    return path
