"""Small, deterministic retrieval evaluation harness.

Datasets are JSONL records with ``query`` and ``expected_paths``.  Optional
``answer`` fields are scored for claim grounding against retrieved evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
from typing import Any, Awaitable, Callable

from rag_enterprise import GroundingVerifier


Retriever = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise ValueError(f"Invalid evaluation record on line {line_number}")
        expected = item.get("expected_paths") or []
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"expected_paths is required on line {line_number}")
        records.append(item)
    return records


async def evaluate(records: list[dict[str, Any]], retrieve: Retriever, *, limit: int = 8) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for record in records:
        started = time.perf_counter()
        results = await retrieve(str(record["query"]), limit)
        latency_ms = (time.perf_counter() - started) * 1000
        actual = [str(item.get("path") or "") for item in results]
        expected = {str(item) for item in record["expected_paths"]}
        matched_ranks = [index + 1 for index, path in enumerate(actual) if path in expected]
        claims = GroundingVerifier.verify(str(record.get("answer") or ""), results) if record.get("answer") else []
        rows.append({
            "query": record["query"], "actual_paths": actual, "expected_paths": sorted(expected),
            "hit": bool(matched_ranks), "reciprocal_rank": 1 / min(matched_ranks) if matched_ranks else 0,
            "latency_ms": round(latency_ms, 2),
            "grounded_claims": sum(1 for claim in claims if claim.status == "supported"),
            "claims": len(claims),
        })
    latencies = sorted(float(row["latency_ms"]) for row in rows)
    p95_index = max(0, int(len(latencies) * 0.95 + 0.999) - 1) if latencies else 0
    claim_count = sum(int(row["claims"]) for row in rows)
    grounded = sum(int(row["grounded_claims"]) for row in rows)
    return {
        "cases": len(rows),
        f"recall_at_{limit}": round(sum(bool(row["hit"]) for row in rows) / len(rows), 4) if rows else 0,
        "mean_reciprocal_rank": round(statistics.fmean(row["reciprocal_rank"] for row in rows), 4) if rows else 0,
        "grounded_claim_rate": round(grounded / claim_count, 4) if claim_count else None,
        "latency_p95_ms": latencies[p95_index] if latencies else None,
        "results": rows,
    }


async def _main(args: argparse.Namespace) -> None:
    from team import AgentTeam

    root = Path(args.workspace).expanduser().resolve()
    team = AgentTeam(root)

    async def retrieve(query: str, limit: int) -> list[dict[str, Any]]:
        return await team.search_project_knowledge(args.project_id, query, limit=limit)

    report = await evaluate(load_dataset(args.dataset), retrieve, limit=args.limit)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate a published project-knowledge index")
    parser.add_argument("dataset", help="JSONL evaluation dataset")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--limit", type=int, default=8)
    asyncio.run(_main(parser.parse_args()))
