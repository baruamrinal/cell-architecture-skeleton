"""Injectable time source, so time-dependent logic is testable without sleeping."""

import time
from collections.abc import Callable

Clock = Callable[[], float]

monotonic: Clock = time.monotonic
