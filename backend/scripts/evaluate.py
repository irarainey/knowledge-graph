"""Offline evaluation of the ``/ask`` knowledge-graph pipeline.

Runs each question from a pre-baked ground-truth file through the in-process
:class:`KnowledgeGraphAgent` (the same code path the API uses), then scores the
result with deterministic metrics — no LLM-as-judge:

* **Retrieval** — the rows the agent retrieved (its generated Cypher result) are
  compared as a set against the rows of a hand-written *gold* Cypher query:
  precision / recall / F1 / Jaccard / exact-match.
* **Answer** — expected fact values are string-matched against the generated
  answer (**coverage**) and against the retrieved rows (**groundedness**).
* **Operational** — Cypher validity, empty-retrieval rate, and the token/latency
  cost taken from the pipeline's own ``stats`` debug event.

Results are written as a single JSON document (per-question detail + run-level
and per-tag aggregates) and a summary is logged.

Usage:
    uv run poe evaluate
    uv run python scripts/evaluate.py --ground-truth eval/ground_truth.json
    uv run python scripts/evaluate.py --tag aggregation --f1-threshold 0.7
    uv run python scripts/evaluate.py --question-id flight-count --output -
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# This script lives in backend/scripts, outside the ``src`` package. Add ``src`` to
# the path so the shared modules import when the script is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import AzureOpenAISettings, KnowledgeGraphAgent
from common.config import ENV_LOG_LEVEL, LOG_LEVEL_DEFAULT
from common.env import load_env
from common.logging_config import get_logger, setup_logging
from neo4j_client import Neo4jClient, Neo4jSettings

logger = get_logger(__name__)

# Repo layout: <repo>/backend/scripts/evaluate.py -> <repo>/backend/eval/ground_truth.json
BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROUND_TRUTH = BACKEND_ROOT / "eval" / "ground_truth.json"
DEFAULT_RESULTS_DIR = BACKEND_ROOT / "eval" / "results"
DEFAULT_F1_THRESHOLD = 0.5


# ── Deterministic metrics ───────────────────────────────────────
# Pure, side-effect-free scoring (no LLM-as-judge). The *primary* retrieval metric
# is value-based: it compares the set of cell *values* the agent retrieved against
# the gold Cypher result, ignoring column names and tolerating float formatting, so
# a correct answer scores well even when the generated Cypher aliases columns
# differently or returns an unrounded number. A *strict* whole-row comparison is
# also reported as a secondary diagnostic. Answer metrics string-match expected fact
# values against the answer text and the retrieved rows.

DEFAULT_ROUND_DIGITS = 3


def _canonical(value: Any) -> Any:
    """Recursively normalise a value into order-independent, JSON-ready data."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonicalize_row(row: Any) -> str:
    """Return a stable, hashable JSON string for a record (used for strict matching).

    Keys are sorted so that two records with the same fields in a different order
    compare equal; non-JSON scalars fall back to ``str`` so canonicalisation never
    raises on unexpected driver types.
    """
    return json.dumps(_canonical(row), sort_keys=True, ensure_ascii=False, default=str)


def _round_floats(value: Any, ndigits: int) -> Any:
    """Recursively round floats so e.g. ``6.5999999`` and ``6.6`` compare equal."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, ndigits)
        return int(rounded) if rounded.is_integer() else rounded
    if isinstance(value, Mapping):
        return {str(key): _round_floats(value[key], ndigits) for key in value}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, ndigits) for item in value]
    return value


def _canonicalize_value(value: Any, ndigits: int) -> str:
    """Canonical, hashable string for a single cell value (float-tolerant)."""
    return json.dumps(_round_floats(_canonical(value), ndigits), sort_keys=True, ensure_ascii=False, default=str)


def value_set(rows: Iterable[Any], *, answer_key: str | None, ndigits: int) -> set[str]:
    """Collect the set of canonical cell values across rows, ignoring column names.

    When ``answer_key`` is present on a row, only that column's value is taken
    (used to pin the gold side to the column the question actually asks about);
    otherwise every cell value in the row is included. This asymmetry lets a gold
    query declare its answer column without requiring the generated query — whose
    aliases differ run-to-run — to reproduce it.
    """
    values: set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            cells = [row[answer_key]] if (answer_key and answer_key in row) else list(row.values())
        else:
            cells = [row]
        for cell in cells:
            values.add(_canonicalize_value(cell, ndigits))
    return values


@dataclass
class RetrievalMetrics:
    """Value-based comparison of predicted rows against gold rows.

    The primary fields (precision/recall/F1/Jaccard/exact_match) compare *cell
    values* ignoring column names and tolerating float formatting. ``strict_*``
    fields compare whole rows verbatim (column names, all columns and exact value
    formatting) and are reported only as a secondary diagnostic.
    """

    gold_count: int
    predicted_count: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    jaccard: float
    exact_match: bool
    strict_exact_match: bool
    strict_f1: float


def _prf(predicted: set[str], gold: set[str]) -> tuple[int, int, int, float, float, float, float, bool]:
    """Shared precision/recall/F1/Jaccard/exact-match over two string sets."""
    tp = len(predicted & gold)
    fp = len(predicted - gold)
    fn = len(gold - predicted)
    if predicted or gold:
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / len(gold) if gold else 1.0
    else:
        precision = recall = 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    union = len(predicted | gold)
    jaccard = (tp / union) if union else 1.0
    return tp, fp, fn, precision, recall, f1, jaccard, predicted == gold


def retrieval_metrics(
    predicted: Iterable[Any],
    gold: Iterable[Any],
    *,
    answer_key: str | None = None,
    ndigits: int = DEFAULT_ROUND_DIGITS,
) -> RetrievalMetrics:
    """Score retrieved rows against gold rows (value-based primary, strict secondary).

    When both sets are empty the result is a perfect match (the agent correctly
    retrieved nothing); when only the gold set is empty, recall is defined as 1.0
    and precision as 0.0.
    """
    predicted_rows = list(predicted)
    gold_rows = list(gold)

    predicted_values = value_set(predicted_rows, answer_key=answer_key, ndigits=ndigits)
    gold_values = value_set(gold_rows, answer_key=answer_key, ndigits=ndigits)
    tp, fp, fn, precision, recall, f1, jaccard, exact = _prf(predicted_values, gold_values)

    predicted_strict = {canonicalize_row(row) for row in predicted_rows}
    gold_strict = {canonicalize_row(row) for row in gold_rows}
    _, _, _, _, _, strict_f1, _, strict_exact = _prf(predicted_strict, gold_strict)

    return RetrievalMetrics(
        gold_count=len(gold_values),
        predicted_count=len(predicted_values),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        jaccard=jaccard,
        exact_match=exact,
        strict_exact_match=strict_exact,
        strict_f1=strict_f1,
    )


def _value_variants(value: Any) -> set[str]:
    """Surface forms a fact value might take in free text (e.g. ``1157`` / ``1,157``)."""
    variants = {str(value)}
    if isinstance(value, bool):
        return variants
    if isinstance(value, float) and value.is_integer():
        variants.add(str(int(value)))
    if isinstance(value, int):
        variants.add(f"{value:,}")
    return {variant for variant in variants if variant}


def value_in_text(value: Any, text: str) -> bool:
    """Case-insensitive check that any surface form of ``value`` appears in ``text``."""
    haystack = text.casefold()
    return any(variant.casefold() in haystack for variant in _value_variants(value))


@dataclass
class AnswerMetrics:
    """How well the generated answer reflects the expected facts.

    ``coverage`` and ``groundedness`` are ``None`` when not applicable (no expected
    values, or no expected value found in the answer respectively), so aggregates
    can skip them rather than averaging in a misleading zero.
    """

    expected_count: int
    matched: list[Any]
    missing: list[Any]
    coverage: float | None
    grounded: list[Any]
    ungrounded: list[Any]
    groundedness: float | None


def answer_metrics(answer: str, expected_values: Sequence[Any], records: Iterable[Any]) -> AnswerMetrics:
    """Score the answer text against expected fact values and the retrieved rows."""
    text = answer or ""
    record_text = canonicalize_row(list(records))

    matched = [value for value in expected_values if value_in_text(value, text)]
    missing = [value for value in expected_values if value not in matched]
    grounded = [value for value in matched if value_in_text(value, record_text)]
    ungrounded = [value for value in matched if value not in grounded]

    coverage = (len(matched) / len(expected_values)) if expected_values else None
    groundedness = (len(grounded) / len(matched)) if matched else None

    return AnswerMetrics(
        expected_count=len(expected_values),
        matched=matched,
        missing=missing,
        coverage=coverage,
        grounded=grounded,
        ungrounded=ungrounded,
        groundedness=groundedness,
    )


def mean(values: Iterable[float | None]) -> float | None:
    """Mean of the non-``None`` values, or ``None`` when there are none."""
    present = [value for value in values if value is not None]
    return (sum(present) / len(present)) if present else None


def _normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) into single spaces and strip.

    Applied to the generated/gold Cypher and the answer text in the report so they are
    stored as single, readable lines free of the model's formatting and line breaks.
    """
    return " ".join(text.split())


# ── Ground truth + orchestration ────────────────────────────────


@dataclass(frozen=True)
class GoldQuestion:
    """One evaluation case: a question, its gold Cypher, and expected answer facts."""

    id: str
    question: str
    gold_cypher: str
    tags: list[str] = field(default_factory=list)
    expected_answer_values: list[Any] = field(default_factory=list)
    answer_key: str | None = None


def load_ground_truth(path: Path) -> list[GoldQuestion]:
    """Load and minimally validate the ground-truth file."""
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    raw_questions = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError(f"{path} does not contain a non-empty 'questions' list")

    questions: list[GoldQuestion] = []
    seen: set[str] = set()
    for entry in raw_questions:
        for key in ("id", "question", "gold_cypher"):
            if not entry.get(key):
                raise ValueError(f"Ground-truth entry is missing required field '{key}': {entry!r}")
        if entry["id"] in seen:
            raise ValueError(f"Duplicate question id in ground truth: {entry['id']!r}")
        seen.add(entry["id"])
        questions.append(
            GoldQuestion(
                id=entry["id"],
                question=entry["question"],
                gold_cypher=entry["gold_cypher"],
                tags=list(entry.get("tags", [])),
                expected_answer_values=list(entry.get("expected_answer_values", [])),
                answer_key=entry.get("answer_key"),
            )
        )
    return questions


async def _drive_agent(agent: KnowledgeGraphAgent, question: str) -> dict[str, Any]:
    """Consume the agent's streamed events into the pieces the metrics need."""
    answer_parts: list[str] = []
    cypher_used: list[str] = []
    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    error: str | None = None

    async for event in agent.ask(question):
        kind = event.get("type")
        if kind == "metadata":
            cypher_used = event.get("cypher_used", [])
            records = event.get("records", [])
        elif kind == "token":
            answer_parts.append(event.get("text", ""))
        elif kind == "stats":
            stats = event
        elif kind == "error":
            error = event.get("message")

    return {
        "answer": "".join(answer_parts),
        "cypher_used": cypher_used,
        "records": records,
        "stats": stats,
        "error": error,
    }


async def evaluate_question(
    agent: KnowledgeGraphAgent,
    client: Neo4jClient,
    question: GoldQuestion,
    *,
    f1_threshold: float,
    round_digits: int = DEFAULT_ROUND_DIGITS,
) -> dict[str, Any]:
    """Run one question through the pipeline and score it."""
    logger.info("Evaluating '%s': %s", question.id, question.question)

    _, gold_records = await client.run_query(question.gold_cypher)
    outcome = await _drive_agent(agent, question.question)
    answer_text = _normalize_whitespace(outcome["answer"])

    retrieval = retrieval_metrics(outcome["records"], gold_records, answer_key=question.answer_key, ndigits=round_digits)
    answer = answer_metrics(answer_text, question.expected_answer_values, outcome["records"])

    generated_cypher = _normalize_whitespace(outcome["cypher_used"][0]) if outcome["cypher_used"] else None
    cypher_valid = generated_cypher is not None and outcome["error"] is None
    empty_retrieval = len(outcome["records"]) == 0
    passed = retrieval.f1 >= f1_threshold

    stats = outcome["stats"]
    cost = {
        "model": stats.get("model"),
        "llm_calls": stats.get("llm_calls"),
        "tokens": stats.get("tokens", {}),
        "durations_ms": stats.get("durations_ms", {}),
    }

    logger.info(
        "  %s | F1=%.2f P=%.2f R=%.2f exact=%s strict_exact=%s valid=%s coverage=%s",
        "PASS" if passed else "FAIL",
        retrieval.f1,
        retrieval.precision,
        retrieval.recall,
        retrieval.exact_match,
        retrieval.strict_exact_match,
        cypher_valid,
        "n/a" if answer.coverage is None else f"{answer.coverage:.2f}",
    )

    return {
        "id": question.id,
        "question": question.question,
        "tags": question.tags,
        "passed": passed,
        "cypher_valid": cypher_valid,
        "empty_retrieval": empty_retrieval,
        "error": outcome["error"],
        "gold_cypher": _normalize_whitespace(question.gold_cypher),
        "generated_cypher": generated_cypher,
        "gold_record_count": len(gold_records),
        "predicted_record_count": len(outcome["records"]),
        "retrieval": asdict(retrieval),
        "answer": {"text": answer_text, **asdict(answer)},
        "cost": cost,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-question metrics up into a summary block."""
    count = len(results)
    if count == 0:
        return {"question_count": 0}

    retrievals = [r["retrieval"] for r in results]
    answers = [r["answer"] for r in results]
    token_totals = [r["cost"]["tokens"].get("total") for r in results]
    latency_totals = [r["cost"]["durations_ms"].get("total") for r in results]

    total_tokens = sum(value for value in token_totals if value is not None)
    total_latency_ms = sum(value for value in latency_totals if value is not None)

    return {
        "question_count": count,
        "pass_rate": sum(r["passed"] for r in results) / count,
        "mean_precision": mean(r["precision"] for r in retrievals),
        "mean_recall": mean(r["recall"] for r in retrievals),
        "mean_f1": mean(r["f1"] for r in retrievals),
        "mean_jaccard": mean(r["jaccard"] for r in retrievals),
        "exact_match_rate": sum(r["exact_match"] for r in retrievals) / count,
        "strict_exact_match_rate": sum(r["strict_exact_match"] for r in retrievals) / count,
        "cypher_valid_rate": sum(r["cypher_valid"] for r in results) / count,
        "empty_retrieval_rate": sum(r["empty_retrieval"] for r in results) / count,
        "mean_answer_coverage": mean(a["coverage"] for a in answers),
        "mean_answer_groundedness": mean(a["groundedness"] for a in answers),
        "total_tokens": total_tokens,
        "total_latency_ms": round(total_latency_ms, 1),
        "mean_latency_ms": round(total_latency_ms / count, 1),
    }


def _aggregate_by_tag(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-tag aggregates so weak query classes stand out."""
    tags = sorted({tag for r in results for tag in r["tags"]})
    return {tag: _aggregate([r for r in results if tag in r["tags"]]) for tag in tags}


def build_report(results: list[dict[str, Any]], *, ground_truth: Path, f1_threshold: float) -> dict[str, Any]:
    """Assemble the full evaluation report document."""
    models = {r["cost"].get("model") for r in results if r["cost"].get("model")}
    return {
        "run": {
            "timestamp": datetime.now(UTC).isoformat(),
            "ground_truth": str(ground_truth),
            "f1_threshold": f1_threshold,
            "model": next(iter(models)) if len(models) == 1 else sorted(models),
            "question_count": len(results),
        },
        "summary": _aggregate(results),
        "by_tag": _aggregate_by_tag(results),
        "questions": results,
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def log_summary(report: dict[str, Any]) -> None:
    """Log a compact, human-readable summary of the run."""
    summary = report["summary"]
    logger.info("=" * 60)
    logger.info("Evaluation summary (%d questions)", summary.get("question_count", 0))
    logger.info("  pass rate           : %s", _fmt(summary.get("pass_rate")))
    logger.info("  mean F1 (value)     : %s", _fmt(summary.get("mean_f1")))
    logger.info("  mean precision      : %s", _fmt(summary.get("mean_precision")))
    logger.info("  mean recall         : %s", _fmt(summary.get("mean_recall")))
    logger.info("  exact-match (value) : %s", _fmt(summary.get("exact_match_rate")))
    logger.info("  exact-match (strict): %s", _fmt(summary.get("strict_exact_match_rate")))
    logger.info("  cypher valid rate   : %s", _fmt(summary.get("cypher_valid_rate")))
    logger.info("  empty retrieval rate: %s", _fmt(summary.get("empty_retrieval_rate")))
    logger.info("  answer coverage     : %s", _fmt(summary.get("mean_answer_coverage")))
    logger.info("  answer groundedness : %s", _fmt(summary.get("mean_answer_groundedness")))
    logger.info("  total tokens        : %s", summary.get("total_tokens"))
    logger.info("  mean latency (ms)   : %s", summary.get("mean_latency_ms"))
    logger.info("=" * 60)


def _write_report(report: dict[str, Any], output: str | None) -> None:
    """Write the report to a file (or stdout when ``output`` is ``-``)."""
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if output == "-":
        print(payload)
        return
    if output:
        path = Path(output)
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = DEFAULT_RESULTS_DIR / f"eval-{timestamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    logger.info("Wrote evaluation report to %s", path)


async def run(args: argparse.Namespace) -> int:
    questions = load_ground_truth(args.ground_truth)
    if args.question_id:
        questions = [q for q in questions if q.id in set(args.question_id)]
    if args.tag:
        questions = [q for q in questions if args.tag in q.tags]
    if not questions:
        logger.error("No questions matched the given filters")
        return 1

    neo4j_settings = Neo4jSettings.from_env()
    client = Neo4jClient(neo4j_settings)
    await client.verify_connectivity()
    agent = KnowledgeGraphAgent.from_settings(AzureOpenAISettings.from_env(), neo4j_settings)

    try:
        results = [
            await evaluate_question(agent, client, question, f1_threshold=args.f1_threshold, round_digits=args.round_digits)
            for question in questions
        ]
    finally:
        agent.close()
        await client.close()

    report = build_report(results, ground_truth=args.ground_truth, f1_threshold=args.f1_threshold)
    log_summary(report)
    _write_report(report, args.output)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline evaluation of the /ask knowledge-graph pipeline.")
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH, help="Path to the ground-truth JSON file")
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for the JSON report; '-' writes to stdout (default: eval/results/eval-<timestamp>.json)",
    )
    parser.add_argument("--f1-threshold", type=float, default=DEFAULT_F1_THRESHOLD, help="Retrieval F1 at/above which a question passes")
    parser.add_argument(
        "--round-digits",
        type=int,
        default=DEFAULT_ROUND_DIGITS,
        help="Decimal places to round floats to before value comparison (float tolerance)",
    )
    parser.add_argument("--tag", default=None, help="Only evaluate questions carrying this tag")
    parser.add_argument("--question-id", action="append", default=None, help="Only evaluate this question id (repeatable)")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to a .env file with Neo4j/Azure OpenAI settings")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env(args.env_file)
    setup_logging(level=os.getenv(ENV_LOG_LEVEL, LOG_LEVEL_DEFAULT))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
