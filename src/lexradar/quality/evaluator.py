"""Finite labeled-universe metrics; no text similarity or model self-scoring."""

from .models import Metric, Prediction, Reference


def ratio(n: int, d: int) -> Metric:
    return Metric(
        numerator=n,
        denominator=d,
        value=n / d if d else None,
        status="measured" if d else "not_applicable",
    )


def key(item):
    return (
        item.topic.strip().lower(),
        item.claim_code.strip().lower(),
        tuple(sorted(item.evidence_ids)),
    )


def match(reference: Reference, predictions: list[Prediction]):
    matches, ambiguous, unmatched = {}, [], []
    for p in predictions:
        candidates = [u for u in reference.units if key(u) == key(p)]
        if len(candidates) != 1:
            (ambiguous if candidates else unmatched).append(p.id)
            continue
        uid = candidates[0].id
        if uid in matches:
            ambiguous.extend([matches[uid].id, p.id])
        else:
            matches[uid] = p
    for uid, p in list(matches.items()):
        if p.id in ambiguous:
            del matches[uid]
    return matches, sorted(set(ambiguous)), unmatched


def measure(reference: Reference, predictions: list[Prediction]):
    matches, ambiguous, unmatched = match(reference, predictions)
    found, missed, fp = [], [], []
    tp = tn = fn = false_positive = 0
    for unit in reference.units:
        p = matches.get(unit.id)
        positive = bool(p and p.asserted)
        if unit.expected_problem:
            if positive:
                tp += 1
                found.append(unit.id)
            else:
                fn += 1
                missed.append(unit.id)
        elif positive:
            false_positive += 1
            fp.append(unit.id + ": " + unit.rationale)
        else:
            tn += 1
    # Out-of-universe assertions affect precision but cannot invent FPR negatives.
    extra = sum(p.asserted for p in predictions if p.id in unmatched)
    fp.extend(
        p.id + ": no labeled equivalent; expert review required"
        for p in predictions
        if p.id in unmatched and p.asserted
    )
    counts = {"tp": tp, "tn": tn, "fp": false_positive, "fn": fn, "extra_fp": extra}
    metrics = classification(counts)
    metrics["factual_detection"] = ratio(
        sum(
            any(key(p) == key(u) and p.fact_grounded for p in predictions)
            for u in reference.units
            if u.expected_problem
        ),
        sum(u.expected_problem for u in reference.units),
    )
    applicable = [u for u in reference.units if u.expected_applicability is not None]
    metrics["applicability_accuracy"] = ratio(
        sum(
            bool(matches.get(u.id) and matches[u.id].applicability == u.expected_applicability)
            for u in applicable
        ),
        len(applicable),
    )
    operators = [u for u in reference.units if u.expected_operator_ref is not None]
    metrics["operator_identification_accuracy"] = ratio(
        sum(
            bool(matches.get(u.id) and matches[u.id].operator_ref == u.expected_operator_ref)
            for u in operators
        ),
        len(operators),
    )
    return matches, ambiguous + unmatched, found, missed, fp, counts, metrics


def classification(c):
    return {
        "precision": ratio(c["tp"], c["tp"] + c["fp"] + c["extra_fp"]),
        "recall": ratio(c["tp"], c["tp"] + c["fn"]),
        "false_positive_rate": ratio(c["fp"], c["fp"] + c["tn"]),
        "false_negative_rate": ratio(c["fn"], c["fn"] + c["tp"]),
    }


def aggregate(cases):
    counts = {k: sum(c.counts[k] for c in cases) for k in ("tp", "tn", "fp", "fn", "extra_fp")}
    result = classification(counts)
    for name in cases[0].metrics if cases else []:
        if name not in result:
            result[name] = ratio(
                sum(c.metrics[name].numerator for c in cases),
                sum(c.metrics[name].denominator for c in cases),
            )
    return result
