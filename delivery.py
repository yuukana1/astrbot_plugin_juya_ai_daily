"""投递重试、熔断和目标状态的纯逻辑工具。"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List


SUCCESS_MODES = {
    "image_url",
    "image_file",
    "image_deduped",
    "render_failed_notice",
    "send_failed_notice",
}


def retry_delays(max_attempts: int, base_delay: float, cap: float = 30.0) -> List[float]:
    """返回每次失败后的指数退避时间；最后一次失败后无需再等待。"""
    attempts = max(1, int(max_attempts))
    base = max(0.0, float(base_delay))
    return [min(cap, base * (2**index)) for index in range(attempts - 1)]


def is_delivery_success(entry: Dict) -> bool:
    return bool(entry.get("success") and entry.get("mode") in SUCCESS_MODES)


def pending_targets(record: Dict, targets: Iterable[str]) -> List[str]:
    """只返回尚未成功的目标，保证轮询重试不会重复投递。"""
    deliveries = record.get("targets", {}) if isinstance(record, dict) else {}
    return sorted(
        target
        for target in set(targets)
        if not is_delivery_success(deliveries.get(target, {}))
    )


@dataclass
class CircuitBreaker:
    """轻量的进程内熔断器，避免渲染服务异常时持续阻塞。"""

    threshold: int = 3
    cooldown_seconds: float = 1800.0
    failures: int = 0
    open_until: float = 0.0

    def allow(self, now: float) -> bool:
        if self.open_until and now >= self.open_until:
            # 半开：允许下一次探测，但保留失败计数供状态展示。
            self.open_until = 0.0
            return True
        return not self.open_until

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= max(1, self.threshold):
            self.open_until = now + max(1.0, self.cooldown_seconds)


@dataclass
class TargetResult:
    target: str
    success: bool
    mode: str
    attempts: int
    error: str = ""


@dataclass
class PushResult:
    """一次推送的逐目标结果。"""

    results: Dict[str, TargetResult] = field(default_factory=dict)
    no_targets: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for result in self.results.values() if result.success)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results.values() if not result.success)

    @property
    def complete(self) -> bool:
        return self.no_targets or bool(self.results) and self.failed == 0
