"""Test memory dedup — Safeguard #5: don't save duplicates."""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.dedup import jaccard_similarity, is_duplicate, compute_importance


def test_jaccard_identical():
    """Identical texts have similarity 1.0."""
    sim = jaccard_similarity("我喜欢喝咖啡", "我喜欢喝咖啡")
    assert sim == 1.0


def test_jaccard_different():
    """Completely different texts have low similarity."""
    sim = jaccard_similarity("我喜欢喝咖啡", "今天天气真好")
    assert sim < 0.3


def test_jaccard_similar():
    """Similar texts have moderate similarity."""
    sim = jaccard_similarity(
        "用户喜欢喝冰美式咖啡，不加糖",
        "用户喜欢喝咖啡，冰美式不加糖",
    )
    assert sim > 0.4


def test_is_duplicate_with_fixture():
    """Duplicate detection with fixture data."""
    fixture_path = Path(__file__).parent / "fixtures" / "memory_dedup_input.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    existing = data["existing"]

    # Similar content should be detected as duplicate (with fixture threshold)
    threshold = data.get("threshold", 0.75)
    assert is_duplicate(data["new_similar"], existing, threshold) == data["expected_duplicate"]

    # Different content should not be detected as duplicate
    assert is_duplicate(data["new_different"], existing, threshold) == data["expected_different"]


def test_is_duplicate_empty_inputs():
    """Empty inputs are never duplicates."""
    assert is_duplicate("", [{"content": "something"}]) is False
    assert is_duplicate("something", []) is False
    assert is_duplicate("", []) is False


def test_compute_importance_preference():
    """Preference keywords increase importance."""
    score = compute_importance("用户喜欢喝冰美式，不加糖", existing_count=0)
    assert score > 0.5  # Should be boosted by "喜欢" keyword


def test_compute_importance_low_value():
    """Very short, non-factual content gets low importance."""
    score = compute_importance("好的", existing_count=0)
    assert score < 0.5


def test_compute_importance_decay():
    """Time decay reduces importance."""
    fresh = compute_importance("用户喜欢喝咖啡", existing_count=0, days_since_created=0)
    old = compute_importance("用户喜欢喝咖啡", existing_count=0, days_since_created=35)
    assert old < fresh


def test_compute_importance_clamped():
    """Importance stays within [0.1, 0.95]."""
    score = compute_importance("x", existing_count=0)
    assert 0.1 <= score <= 0.95

    score = compute_importance("用户特别喜欢喝冰美式不加糖每天三杯", existing_count=10)
    assert 0.1 <= score <= 0.95


if __name__ == "__main__":
    test_jaccard_identical()
    test_jaccard_different()
    test_jaccard_similar()
    test_is_duplicate_with_fixture()
    test_is_duplicate_empty_inputs()
    test_compute_importance_preference()
    test_compute_importance_low_value()
    test_compute_importance_decay()
    test_compute_importance_clamped()
    print("[OK] All memory tests passed")
