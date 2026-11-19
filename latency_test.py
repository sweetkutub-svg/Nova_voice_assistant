"""
latency_test.py — ye batata hai ki delay KAHAN ho raha hai (AI me, awaaz me, ya
frontend me). jervish folder me chalao:

    python latency_test.py

Output (poora) mujhe paste kar do.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def timed(label, fn):
    s = time.time()
    try:
        r = fn()
        print(f"  {label}: {time.time() - s:.2f}s")
        return r, time.time() - s
    except Exception as e:
        print(f"  {label}: ERROR -> {type(e).__name__}: {e}")
        return None, None


print("== 0) Kaun sa code laga hai ==")
import client_deepseek as ai
import client_elevenlabs as tts

has_stream = hasattr(ai, "ask_ai_stream") and hasattr(tts, "synthesize_stream")
print("  streaming code  :", "HAAN" if has_stream else "NAHI (STREAM_BLOCK / naya client_elevenlabs.py nahi laga)")
print("  online/local fallback:", "HAAN" if hasattr(ai, "_post_with_fallback") else "NAHI")
print("  Piper CLI mila  :", "HAAN" if getattr(tts, "PIPER_AVAILABLE", False) else "NAHI")

print("\n== 1) Awaaz (Piper) — ek chhota sentence ==")
sentence = "Hello, this is a short test sentence."
timed("Piper call #1", lambda: tts._generate_piper_bytes(sentence))
_, warm = timed("Piper call #2", lambda: tts._generate_piper_bytes(sentence))
timed("poora _generate_audio (jo Nova use karta hai)", lambda: tts._generate_audio(sentence))

print("\n== 2) AI ka jawab (non-streaming, poora jawab aane tak) ==")
results = {}
for mode in ["online", "local"]:
    if not hasattr(ai, "AI_MODE"):
        mode = "online"
    ai.AI_MODE = mode
    print(f"  -- mode: {mode}")
    ai.reset_conversation()
    _, total = timed(f"ask_ai ({mode}) poora jawab", lambda: ai.ask_ai("Say hello in one short sentence."))
    results[mode] = total
    if not hasattr(ai, "AI_MODE"):
        break

if has_stream:
    print("\n== 3) Streaming: pehla sentence kitni der me aata hai ==")
    for mode in ["online", "local"]:
        ai.AI_MODE = mode
        ai.reset_conversation()
        print(f"  -- mode: {mode}")

        def first_sentence():
            gen = ai.ask_ai_stream("Explain what a CPU is in three short sentences.")
            first = next(gen)
            for _ in gen:
                pass
            return first

        timed(f"ask_ai_stream ({mode}) pehla sentence + poora", first_sentence)

        ai.reset_conversation()
        s = time.time()
        gen = tts.synthesize_stream(ai.ask_ai_stream("Explain what a CPU is in three short sentences."))
        try:
            next(gen)
            print(f"  PEHLI AWAAZ aane me ({mode}): {time.time() - s:.2f}s   <-- user ko itna wait karna padta hai")
            for _ in gen:
                pass
        except StopIteration:
            print("  koi audio nahi bana")
        except Exception as e:
            print(f"  ERROR: {e}")

    ai.AI_MODE = "auto"

print("\n== Matlab ==")
if warm and warm > 1.0:
    print(f"  * Piper har sentence pe ~{warm:.1f}s le raha hai (model har baar dobara load hota hai) -> yehi slow hai.")
elif warm:
    print(f"  * Piper theek hai (~{warm:.1f}s).")
if not has_stream:
    print("  * Streaming code laga hi nahi, isliye poora jawab banne ke baad hi awaaz shuru hoti hai.")
print("  * Web me delay ho to ye bhi check karo: nova.html abhi /api/command use kar rahi hai (streaming wali nahi).")
