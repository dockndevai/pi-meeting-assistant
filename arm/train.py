"""
arm/train.py — fine-tune Laya's multilingual checkpoint on arm commands, then
report held-out accuracy per language against the untuned model.

    python -m arm.dataset --out arm/data      # (first) build train/test JSONL
    python -m arm.train                        # → arm/model/  (+ arm/model/eval.json)

Decision: start from the multilingual checkpoint
  Commands arrive in English, Hinglish and Devanagari Hindi. The English
  checkpoint can't read Devanagari at all; one multilingual model also halves
  the Pi's RAM versus keeping both resident.

Decision: fine-tune on a laptop/GPU, ship the checkpoint to the Pi
  Training a 322M-parameter encoder on a Pi CPU would take hours. The saved
  checkpoint is fp16 (~0.65 GB) and only needs inference on the Pi.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from arm.schema import QUESTIONS


def evaluate(agent_predict, rows: list[dict]) -> dict:
    by_lang = defaultdict(lambda: [0, 0])
    confusions = Counter()
    times = []
    for r in rows:
        t = time.perf_counter()
        ans = agent_predict(r["state"])["answers"]
        times.append((time.perf_counter() - t) * 1000)
        got, want = ans["gesture"]["choice"], r["expected"]["gesture"]
        lang = r.get("meta", {}).get("lang", "?")
        by_lang[lang][1] += 1
        if got == want:
            by_lang[lang][0] += 1
        else:
            confusions[f"{want}->{got}"] += 1
    total_ok = sum(v[0] for v in by_lang.values())
    total = sum(v[1] for v in by_lang.values())
    times.sort()
    return {
        "accuracy": round(total_ok / total, 3),
        "by_lang": {k: {"correct": v[0], "total": v[1], "accuracy": round(v[0] / v[1], 3)} for k, v in by_lang.items()},
        "top_confusions": confusions.most_common(8),
        "latency_ms": {"median": round(times[len(times) // 2]), "p95": round(times[int(len(times) * 0.95)])},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="arm/data")
    ap.add_argument("--out", default="arm/model")
    ap.add_argument("--base", default="multilingual")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    import laya
    from laya.train import TrainConfig, finetune, read_jsonl, resolve_checkpoint_dir

    test_rows = read_jsonl(f"{args.data}/test.jsonl")
    report = {}

    if not args.skip_baseline:
        base = laya.Router(device="cpu")
        report["baseline"] = evaluate(lambda s: base.predict(s, QUESTIONS, model=args.base), test_rows)
        print("baseline (untuned):", json.dumps(report["baseline"], ensure_ascii=False), flush=True)
        del base

    base_dir = resolve_checkpoint_dir(args.base)
    config = TrainConfig(
        epochs=args.epochs,
        micro_batch=8,
        grad_accum=2,                 # small dataset: more, smaller updates
        loss="soft-ce",               # #741: no gain from the RL term on typed decisions
        shuffle_options=("choice",),  # stop it learning "the 3rd option is usually right"
        log_every=25,
    )
    t = time.time()
    summary = finetune(f"{args.data}/train.jsonl", base_dir, args.out, config=config, device=args.device)
    report["train_seconds"] = round(time.time() - t)
    report["finetune_summary"] = {k: v for k, v in summary.items() if isinstance(v, (int, float, str, bool))}

    tuned = laya.load(args.out, device="cpu")
    report["finetuned"] = evaluate(lambda s: tuned.predict(s, QUESTIONS), test_rows)
    print("fine-tuned:", json.dumps(report["finetuned"], ensure_ascii=False), flush=True)
    Path(args.out, "eval.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
