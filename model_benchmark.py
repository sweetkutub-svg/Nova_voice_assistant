"""
model_benchmark.py — alag-alag AI models ka SPEED naapta hai (OpenRouter pe), taaki
sabse tez wala choose kar sako. Har model ko 3 baar try karta hai (latency har
baar badalti hai), aur batata hai:
   TTFT  = pehla shabd aane me kitna time (ye hi "delay" feel hota hai)
   TOTAL = poora jawab aane me kitna time

jervish folder me chalao (.env me OPENROUTER_API_KEY honi chahiye):
    python model_benchmark.py
Apne models try karne ke liye:
    python model_benchmark.py openai/gpt-4o-mini google/gemini-2.0-flash-001

Har model ke 3 chhote sawaal = kuch paise ka bhi nahi.
"""
import json
import os
import statistics
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

URL = os.environ.get("BENCH_URL", "https://openrouter.ai/api/v1/chat/completions")
KEY = os.environ.get("OPENROUTER_API_KEY")
PROMPT = "In two short sentences, what is a CPU?"
RUNS = 3

DEFAULT_MODELS = [
    ("deepseek/deepseek-chat", None),                                   # abhi wala
    ("deepseek/deepseek-chat", {"provider": {"sort": "latency"}}),      # same model, sabse tez provider
    ("google/gemini-2.0-flash-001", None),
    ("openai/gpt-4o-mini", None),
]


def one_run(model, extra):
    body = {"model": model, "stream": True, "max_tokens": 80,
            "messages": [{"role": "user", "content": PROMPT}]}
    if extra:
        body.update(extra)
    t0 = time.time()
    ttft = None
    with requests.post(URL, headers={"Authorization": f"Bearer {KEY}"}, json=body, stream=True, timeout=(10, 60)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        for raw in r.iter_lines():
            line = raw.decode("utf-8", "ignore") if raw else ""
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, ValueError):
                continue
            if delta and ttft is None:
                ttft = time.time() - t0
    return ttft, time.time() - t0


def main():
    if not KEY:
        sys.exit("OPENROUTER_API_KEY nahi mili (.env check karo).")

    if len(sys.argv) > 1:
        models = [(m, None) for m in sys.argv[1:]]
    else:
        models = DEFAULT_MODELS

    rows = []
    for model, extra in models:
        label = model + ("  [provider sort=latency]" if extra else "")
        print(f"\n{label}")
        ttfts, totals = [], []
        for i in range(RUNS):
            try:
                a, b = one_run(model, extra)
                if a is None:
                    raise RuntimeError("koi text nahi aaya")
                ttfts.append(a); totals.append(b)
                print(f"   run {i + 1}: TTFT {a:.2f}s | total {b:.2f}s")
            except Exception as e:
                print(f"   run {i + 1}: ERROR {e}")
                break
        if ttfts:
            rows.append((statistics.median(ttfts), min(ttfts), max(ttfts), statistics.median(totals), label))

    print("\n===== RESULT (TTFT median, chhota = tez) =====")
    for med, lo, hi, tot, label in sorted(rows):
        print(f"  {med:.2f}s  (min {lo:.2f} / max {hi:.2f})  total {tot:.2f}s   {label}")
    if rows:
        print("\nSabse tez:", sorted(rows)[0][4])
        print('Isse use karne ke liye client_deepseek.py me MODEL = "..." badal do.')


if __name__ == "__main__":
    main()
