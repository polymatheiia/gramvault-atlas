"""Accuracy / precision / recall / confusion reporting for the classifier
eval. No dependency on the rest of the suite — plain stdlib so it stays
easy to eyeball."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class CategoryScore:
    support: int = 0  # gold items in this category
    predicted: int = 0  # times the classifier chose it
    correct: int = 0  # true positives

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.correct / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class EvalReport:
    total: int = 0
    correct: int = 0
    per_category: dict[str, CategoryScore] = field(default_factory=dict)
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        scores = [s.f1 for s in self.per_category.values() if s.support]
        return sum(scores) / len(scores) if scores else 0.0

    def format(self, title: str) -> str:
        lines = [
            f"=== {title} ===",
            f"items: {self.total}   accuracy: {self.accuracy:.1%}   macro-F1: {self.macro_f1:.3f}",
            "",
            f"  {'category':<16} {'support':>7} {'prec':>6} {'recall':>7} {'f1':>6}",
        ]
        for name, score in sorted(
            self.per_category.items(), key=lambda kv: kv[1].support, reverse=True
        ):
            lines.append(
                f"  {name:<16} {score.support:>7} {score.precision:>6.2f} "
                f"{score.recall:>7.2f} {score.f1:>6.2f}"
            )
        worst = [((g, p), n) for (g, p), n in self.confusion.most_common() if g != p][:12]
        if worst:
            lines += ["", "  top confusions (gold -> predicted):"]
            lines += [f"    {g:<16} -> {p:<16} {n:>4}" for (g, p), n in worst]
        return "\n".join(lines)


def evaluate(pairs: list[tuple[str, str]]) -> EvalReport:
    """`pairs` is a list of `(gold_category, predicted_category)`."""
    report = EvalReport(total=len(pairs))
    for gold, pred in pairs:
        report.per_category.setdefault(gold, CategoryScore())
        report.per_category.setdefault(pred, CategoryScore())
        report.per_category[gold].support += 1
        report.per_category[pred].predicted += 1
        report.confusion[(gold, pred)] += 1
        if gold == pred:
            report.correct += 1
            report.per_category[gold].correct += 1
    return report
