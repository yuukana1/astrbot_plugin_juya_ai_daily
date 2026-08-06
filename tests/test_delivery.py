import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery import (  # noqa: E402
    CircuitBreaker,
    PushResult,
    TargetResult,
    is_delivery_success,
    pending_targets,
    retry_delays,
)


class DeliveryTests(unittest.TestCase):
    def test_retry_delays_are_exponential_and_capped(self):
        self.assertEqual(retry_delays(1, 2), [])
        self.assertEqual(retry_delays(5, 2, cap=10), [2.0, 4.0, 8.0, 10])

    def test_pending_targets_only_returns_failed_or_unknown_targets(self):
        record = {
            "targets": {
                "group:a": {"success": True, "mode": "image_url"},
                "group:b": {"success": False, "mode": "failed"},
                # success=True alone is insufficient: mode must be completed.
                "group:c": {"success": True, "mode": "failed"},
            }
        }
        pending = pending_targets(
            record, {"group:a", "group:b", "group:c", "group:d"}
        )
        self.assertEqual(pending, ["group:b", "group:c", "group:d"])
        self.assertTrue(is_delivery_success(record["targets"]["group:a"]))

    def test_recently_deduped_image_counts_as_success(self):
        record = {
            "targets": {
                "group:a": {"success": True, "mode": "image_deduped"},
            }
        }
        self.assertEqual(pending_targets(record, {"group:a"}), [])

    def test_circuit_breaker_opens_and_half_opens(self):
        breaker = CircuitBreaker(threshold=2, cooldown_seconds=30)
        breaker.record_failure(100)
        self.assertTrue(breaker.allow(101))
        breaker.record_failure(110)
        self.assertFalse(breaker.allow(139))
        self.assertTrue(breaker.allow(140))
        breaker.record_success()
        self.assertEqual(breaker.failures, 0)
        self.assertEqual(breaker.open_until, 0)

    def test_push_result_requires_every_attempted_target_to_succeed(self):
        success = TargetResult("a", True, "image_url", 1)
        failure = TargetResult("b", False, "failed", 3, "network")
        partial = PushResult(results={"a": success, "b": failure})
        self.assertEqual(partial.succeeded, 1)
        self.assertEqual(partial.failed, 1)
        self.assertFalse(partial.complete)

        self.assertTrue(PushResult(results={"a": success}).complete)
        self.assertTrue(PushResult(no_targets=True).complete)
        self.assertFalse(PushResult().complete)


if __name__ == "__main__":
    unittest.main()
