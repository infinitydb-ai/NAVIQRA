"""Microbenchmark local v0.5.0 primitives; not a production or end-to-end benchmark."""
from __future__ import annotations
import json, statistics, tempfile, time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from naviqra.platform.core import PlatformStore
from naviqra.providers import ProviderRegistry, SandboxProvider


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, int((len(ordered)-1)*p))]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = PlatformStore(Path(tmp) / "bench.sqlite")
        registry = ProviderRegistry(); registry.register(SandboxProvider())
        store.put_memory("bench", "traveler", "profile", "home", "Bengaluru")
        samples = {"memory_read_ms": [], "provider_search_ms": [], "audit_write_ms": []}
        for _ in range(1000):
            start=time.perf_counter(); store.get_memories("bench", "traveler")
            samples["memory_read_ms"].append((time.perf_counter()-start)*1000)
            start=time.perf_counter(); registry.get("sandbox").search({"destination":"Lisbon"})
            samples["provider_search_ms"].append((time.perf_counter()-start)*1000)
            start=time.perf_counter(); store.audit("bench","benchmark","benchmark.event","test","1")
            samples["audit_write_ms"].append((time.perf_counter()-start)*1000)
        result={"benchmark":"naviqra-v0.5.0-local-microbenchmark","iterations":1000,
                "environment":"local SQLite + synthetic in-process provider",
                "metrics":{k:{"p50_ms":round(percentile(v,.50),4),
                              "p95_ms":round(percentile(v,.95),4),
                              "mean_ms":round(statistics.mean(v),4)} for k,v in samples.items()},
                "limitations":["not a production deployment","no network or LLM latency",
                               "synthetic inventory only","single-process local measurements"]}
        out=Path(__file__).resolve().parents[1]/"benchmark-results-v0.5.0.json"
        out.write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps(result,indent=2))

if __name__ == "__main__": main()
