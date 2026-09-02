"""Accuracy benchmark for `gramvault.ai.classifier` against the 1814
hand-labelled saved items from the reels-workflow run.

Skipped by default (the `eval` marker is filtered out in `pyproject.toml`)
and skipped entirely unless the local dataset is present — see
`loader.py`. Run it with:

    pytest -m eval -s                       # keyword pass only
    GRAMVAULT_EVAL_LLM=1 pytest -m eval -s   # also the (paid) LLM pass

`-s` shows the per-category report; the asserts only guard against a gross
regression, the numbers in the captured output are the point.
"""

from __future__ import annotations

import os

import pytest

from gramvault.ai.classifier import CategoryResult, keyword_vote, llm_classify
from gramvault.config import get_config
from gramvault.db.session import DEFAULT_CATEGORIES

from .loader import EVAL_DATA_AVAILABLE, load_eval_set, missing_reason
from .metrics import evaluate

pytestmark = pytest.mark.eval

CATEGORIES = list(DEFAULT_CATEGORIES)
CATEGORY_NAMES = [name for name, _ in CATEGORIES]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def eval_set():
    if not EVAL_DATA_AVAILABLE:
        pytest.skip(missing_reason())
    pairs = load_eval_set()
    if not pairs:
        pytest.skip("eval dataset loaded but empty (id mismatch between labels and items?)")
    return pairs


def test_keyword_vote_accuracy(eval_set) -> None:
    preds = [
        (gold, keyword_vote(item, CATEGORY_NAMES).category) for item, gold in eval_set
    ]
    report = evaluate(preds)
    print("\n" + report.format(f"keyword vote  (n={report.total})"))

    # The keyword vote is only the cheap floor; on this multilingual, meme-heavy
    # corpus it lands around 55-65%. Assert well below that so a real regression
    # trips but noise doesn't.
    assert report.accuracy >= 0.45, f"keyword accuracy {report.accuracy:.1%} — regression?"


def test_keyword_vote_flags_its_weak_guesses(eval_set) -> None:
    """`needs_review` (confidence < REVIEW_CONFIDENCE) should be where the
    wrong answers concentrate — otherwise the review queue is pointless."""
    confident_pairs: list[tuple[str, str]] = []
    review_pairs: list[tuple[str, str]] = []
    for item, gold in eval_set:
        result = keyword_vote(item, CATEGORY_NAMES)
        bucket = review_pairs if result.confidence < 0.6 else confident_pairs
        bucket.append((gold, result.category))

    confident = evaluate(confident_pairs)
    review = evaluate(review_pairs)
    print(
        f"\nconfident guesses: {confident.total} @ {confident.accuracy:.1%}"
        f"   |   needs-review: {review.total} @ {review.accuracy:.1%}"
    )
    if confident.total and review.total:
        assert confident.accuracy > review.accuracy


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("GRAMVAULT_EVAL_LLM") != "1",
    reason="set GRAMVAULT_EVAL_LLM=1 to run the paid LLM classifier pass",
)
async def test_llm_classify_accuracy(eval_set) -> None:
    config = get_config()
    items = [item for item, _ in eval_set]
    gold_by_id = {item.id: gold for item, gold in eval_set}
    keyword_guesses: dict[int, CategoryResult] = {
        item.id: keyword_vote(item, CATEGORY_NAMES) for item in items
    }

    results = await llm_classify(items, keyword_guesses, CATEGORIES, config)

    preds = [
        (gold_by_id[item_id], results[item_id].category)
        for item_id in gold_by_id
        if item_id in results
    ]
    report = evaluate(preds)
    print("\n" + report.format(f"LLM re-label  (n={report.total})"))
    assert report.accuracy >= 0.60
