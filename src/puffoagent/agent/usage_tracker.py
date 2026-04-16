import json
import logging
import os
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger(__name__)

USAGE_FILE = "token_usage.json"


class UsageTracker:
    def __init__(self, memory_dir: str):
        os.makedirs(memory_dir, exist_ok=True)
        self.path = os.path.join(memory_dir, USAGE_FILE)
        self.records: list[dict] = []
        self._load()
        logger.info(f"UsageTracker initialised — {len(self.records)} existing records at {self.path}")

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load usage records from {self.path}: {e}")
                self.records = []

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f)
        except Exception as e:
            logger.error(f"Failed to save usage records to {self.path}: {e}")

    def record(self, input_tokens: int, output_tokens: int):
        self.records.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": input_tokens,
            "output": output_tokens,
        })
        self._save()
        logger.info(f"Tokens recorded — input: {input_tokens}, output: {output_tokens} (total calls: {len(self.records)})")

    def stats(self) -> dict:
        buckets = {
            "hourly":  defaultdict(lambda: {"input": 0, "output": 0}),
            "daily":   defaultdict(lambda: {"input": 0, "output": 0}),
            "weekly":  defaultdict(lambda: {"input": 0, "output": 0}),
            "monthly": defaultdict(lambda: {"input": 0, "output": 0}),
        }

        for r in self.records:
            ts = datetime.fromisoformat(r["ts"])
            inp, out = r["input"], r["output"]
            for key, bucket in [
                (ts.strftime("%Y-%m-%dT%H:00"), "hourly"),
                (ts.strftime("%Y-%m-%d"),        "daily"),
                (ts.strftime("%G-W%V"),           "weekly"),
                (ts.strftime("%Y-%m"),            "monthly"),
            ]:
                buckets[bucket][key]["input"]  += inp
                buckets[bucket][key]["output"] += out

        def to_list(d):
            return [
                {"period": k, "input": v["input"], "output": v["output"], "total": v["input"] + v["output"]}
                for k, v in sorted(d.items())
            ]

        return {
            "hourly":   to_list(buckets["hourly"]),
            "daily":    to_list(buckets["daily"]),
            "weekly":   to_list(buckets["weekly"]),
            "monthly":  to_list(buckets["monthly"]),
            "all_time": {
                "input":  sum(r["input"]  for r in self.records),
                "output": sum(r["output"] for r in self.records),
                "total":  sum(r["input"] + r["output"] for r in self.records),
                "calls":  len(self.records),
            },
        }
