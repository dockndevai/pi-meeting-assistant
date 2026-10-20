"""
arm/brain.py — Laya decides which gesture a command means.

Loads the fine-tuned checkpoint (ARM_MODEL_DIR, default arm/model) if present,
otherwise Laya's stock multilingual checkpoint (works, but ~44% vs the tuned
model's accuracy — see arm/model/eval.json).

Decision: one forward pass answers gesture + speed together
  Both questions share the encoded command, so asking two costs barely more
  than one.

Decision: several transcripts in, one decision out
  Voice runs two recognisers (Indian English, Hindi). Both transcripts go to
  Laya in one batch and the most confident non-"none" decision wins — Laya,
  not a heuristic, chooses which hearing of the audio to trust.

Decision: confidence gate
  Below ARM_MIN_CONFIDENCE (0.45) the arm stays still. A wrong gesture is
  worse than none, and Laya's confidences are calibrated, so the threshold
  means what it says.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from arm.schema import QUESTIONS

MIN_CONFIDENCE = float(os.environ.get("ARM_MIN_CONFIDENCE", "0.45"))


class ArmBrain:
    def __init__(self):
        self.model_dir = Path(os.environ.get("ARM_MODEL_DIR", Path(__file__).parent / "model"))
        self.kind = "fine-tuned" if (self.model_dir / "rl_agent_config.json").exists() else "stock multilingual"
        self.status = "loading"
        self.error: Optional[str] = None
        self._agent = None
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, dict] = OrderedDict()

    def load(self) -> None:
        """Blocking: load weights and run one warm-up pass. Call from a thread."""
        try:
            import torch
            import laya

            torch.set_num_threads(int(os.environ.get("ARM_TORCH_THREADS", os.cpu_count() or 4)))
            t = time.time()
            if self.kind == "fine-tuned":
                self._agent = laya.load(str(self.model_dir), device="cpu")
            else:
                self._agent = laya.load("multilingual", device="cpu")
            self._agent.predict({"command": "wave hello"}, QUESTIONS)
            self.status = "ready"
            print(f"[arm] Laya {self.kind} ready in {time.time() - t:.1f}s")
        except Exception as e:
            self.status, self.error = "error", str(e)
            print(f"[arm] Laya failed to load: {e}")

    def decide(self, texts: list[str]) -> dict:
        """Blocking. Best decision across candidate transcripts of one utterance."""
        texts = [t.strip() for t in texts if t and t.strip()]
        if not texts:
            return {"gesture": "none", "confidence": 1.0, "text": "", "laya_ms": 0}
        if self.status != "ready":
            raise RuntimeError(f"Laya is {self.status}" + (f": {self.error}" if self.error else ""))

        key = "␞".join(t.lower() for t in texts)
        if key in self._cache:
            self._cache.move_to_end(key)
            return {**self._cache[key], "cached": True, "laya_ms": 0}

        t0 = time.perf_counter()
        with self._lock:   # torch on CPU: one forward pass at a time is fastest anyway
            states = [{"command": t} for t in texts]
            results = (self._agent.predict_batch(states, QUESTIONS) if len(states) > 1
                       else [self._agent.predict(states[0], QUESTIONS)])
        laya_ms = round((time.perf_counter() - t0) * 1000)

        candidates = []
        for text, res in zip(texts, results):
            g, s = res["answers"]["gesture"], res["answers"]["speed"]
            probs = g.get("probabilities", {})
            candidates.append({
                "text": text,
                "gesture": g["choice"],
                "confidence": round(float(g["confidence"]), 3),
                "top": sorted(probs.items(), key=lambda kv: -kv[1])[:3],
                "speed_score": round(float(s["score"]), 2),
                "language_route": (res.get("routing") or {}).get("model"),
            })
        acting = [c for c in candidates if c["gesture"] != "none"]
        best = max(acting or candidates, key=lambda c: c["confidence"])
        decision = {**best, "candidates": candidates, "laya_ms": laya_ms,
                    "gated": best["gesture"] != "none" and best["confidence"] < MIN_CONFIDENCE}

        self._cache[key] = decision
        if len(self._cache) > 256:
            self._cache.popitem(last=False)
        return decision
