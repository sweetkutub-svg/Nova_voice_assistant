"""
apply_offline_patch.py — client_deepseek.py me AUTO FALLBACK add karta hai:
  Internet hai  -> OpenRouter (online, DeepSeek)
  Internet nahi -> local Ollama model

Isme ye bhi hai: Ollama sirf tab start hota hai jab local model ki zaroorat
padti hai, aur code band hote hi band ho jaata hai.

Kaise chalana hai (jervish folder me, jahan client_deepseek.py hai):
    python apply_offline_patch.py

Ye pehle client_deepseek.py.bak naam se backup banata hai. Dobara chalane
par kuch nahi badalta (already patched ho to bata deta hai).
"""
import re
import shutil
import sys

FILE = "client_deepseek.py"

BLOCK = '''

# ============================================================
# 🌐➡️💻 AUTO FALLBACK (apply_offline_patch.py ne add kiya)
#   Internet hai  -> OpenRouter (online)
#   Internet nahi -> local Ollama model
# .env me ye optional settings:
#   AI_MODE=auto      (default) online try karo, fail ho to local
#   AI_MODE=online    sirf online      |  AI_MODE=local  sirf local
#   LOCAL_MODEL=qwen2.5:3b   (ollama pull wala naam)
# ============================================================
import time

LOCAL_URL = os.environ.get("LOCAL_AI_URL", "http://localhost:11434/v1/chat/completions")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "qwen2.5:3b")
AI_MODE = os.environ.get("AI_MODE", "auto").lower()
ONLINE_RETRY_AFTER = 30      # online fail hone ke baad itne second seedha local use karo
_online_down_until = 0.0
_last_source = None


# OpenRouter ko bolo ki sabse kam latency wale provider se jawab do (DeepSeek ka
# response time provider ke hisaab se 1s se 10s tak badalta rehta hai).
# Band karna ho to .env me:  OPENROUTER_SORT=
OPENROUTER_SORT = os.environ.get("OPENROUTER_SORT", "latency").strip()


def _provider_pref():
    return {"provider": {"sort": OPENROUTER_SORT}} if OPENROUTER_SORT else {}


def _announce(source):
    global _last_source
    if source != _last_source:
        _last_source = source
        print(f"AI source: {'ONLINE (OpenRouter)' if source == 'online' else 'LOCAL (Ollama: ' + LOCAL_MODEL + ')'}")


def _post_with_fallback(data, timeout=20):
    """
    requests.post jaisa hi hai, par pehle online try karta hai aur fail
    hone par (internet off, timeout, quota/key error) local Ollama pe chala
    jaata hai. Response object return karta hai (stream=True bhi chalega).
    """
    global _online_down_until
    key = os.environ.get("OPENROUTER_API_KEY")
    stream = bool(data.get("stream"))

    try_online = (
        AI_MODE != "local"
        and bool(key)
        and (AI_MODE == "online" or time.time() >= _online_down_until)
    )

    if try_online:
        try:
            resp = requests.post(
                URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={**data, "model": MODEL, **_provider_pref()},
                stream=stream,
                timeout=(5, timeout),     # 5s me connect nahi hua = internet nahi hai
            )
            resp.raise_for_status()
            _announce("online")
            return resp
        except requests.exceptions.RequestException as e:
            if AI_MODE == "online":
                raise
            print(f"AI: online fail ({type(e).__name__}) — local model pe switch kar raha hu...")
            _online_down_until = time.time() + ONLINE_RETRY_AFTER

    try:
        _ensure_ollama_running()
        resp = requests.post(
            LOCAL_URL,
            json={**data, "model": LOCAL_MODEL},
            stream=stream,
            timeout=(3, max(timeout, 120)),   # local model pehli baar RAM me load hone me time leta hai
        )
        resp.raise_for_status()
        _announce("local")
        return resp
    except requests.exceptions.RequestException as e:
        raise requests.exceptions.RequestException(
            f"Internet nahi hai aur local Ollama bhi nahi chal raha. "
            f"Check karo: 'ollama list' me {LOCAL_MODEL} hai? ({type(e).__name__})"
        ) from e
'''

PREF_BLOCK = '''
# OpenRouter ko bolo ki sabse kam latency wale provider se jawab do (DeepSeek ka
# response time provider ke hisaab se 1s se 10s tak badalta rehta hai).
# Band karna ho to .env me:  OPENROUTER_SORT=
OPENROUTER_SORT = os.environ.get("OPENROUTER_SORT", "latency").strip()


def _provider_pref():
    return {"provider": {"sort": OPENROUTER_SORT}} if OPENROUTER_SORT else {}


'''


AUTO_BLOCK = '''

# ============================================================
# 🔌 OLLAMA AUTO START/STOP — Ollama sirf tab chalta hai jab code ko
#    local model ki zaroorat padti hai (internet nahi), aur code band
#    hote hi band ho jaata hai. Jo Ollama pehle se chal raha tha use
#    ye band NAHI karta (sirf wahi band karta hai jo isne khud start kiya).
# ============================================================
import sys
import atexit
import shutil
import subprocess

_ollama_proc = None


def _ollama_up():
    try:
        requests.get(LOCAL_URL.split("/v1")[0], timeout=1)
        return True
    except requests.exceptions.RequestException:
        return False


def _stop_ollama():
    global _ollama_proc
    p = _ollama_proc
    _ollama_proc = None
    if p is None or p.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            # /T = uske child (model runner) processes bhi band
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("Local Ollama stopped.")
    except Exception as e:
        print(f"Ollama stop error: {e}")


def _ensure_ollama_running():
    """Ollama nahi chal raha to 'ollama serve' background me start karta hai."""
    global _ollama_proc
    if _ollama_up():
        return True
    exe = shutil.which("ollama")
    if not exe:
        print("Ollama install/PATH me nahi mila (ollama.com se install karo).")
        return False

    print("Starting local Ollama (internet nahi hai)...")
    env = os.environ.copy()
    env.setdefault("OLLAMA_KEEP_ALIVE", "10m")   # model 10 min idle rahe to RAM se hata do
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    try:
        _ollama_proc = subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, creationflags=flags,
        )
    except Exception as e:
        print(f"Ollama start nahi hua: {e}")
        return False
    atexit.register(_stop_ollama)

    for _ in range(60):           # max ~30 second wait
        if _ollama_up():
            return True
        if _ollama_proc.poll() is not None:
            break
        time.sleep(0.5)
    print("Ollama start hone me problem aayi.")
    return False
'''


def main():
    try:
        src = open(FILE, encoding="utf-8").read()
    except FileNotFoundError:
        sys.exit(f"{FILE} nahi mila. Ye script usi folder me chalao jahan {FILE} hai.")

    new = src
    changed = []

    if "_post_with_fallback" not in new:
        # ---------- Naya patch: online/local fallback + Ollama auto start/stop + latency routing ----------
        problems = []

        pat = re.compile(r'API_KEY = os\.environ\.get\("OPENROUTER_API_KEY"\)[ \t]*(?=\n)')
        if pat.search(new):
            new = pat.sub('API_KEY = os.environ.get("OPENROUTER_API_KEY") or "no-key-local-fallback"', new, count=1)
        else:
            problems.append('API_KEY line (API_KEY = os.environ.get("OPENROUTER_API_KEY"))')

        pat = re.compile(
            r'(?P<ind>[ \t]*)response = requests\.post\(URL, headers=headers, json=data, timeout=(?P<t>\d+)\)\n'
            r'[ \t]*response\.raise_for_status\(\)'
        )
        new, n = pat.subn(lambda m: f"{m.group('ind')}response = _post_with_fallback(data, timeout={m.group('t')})", new)
        if n == 0:
            problems.append("ask_ai / generate_content ka requests.post(...) wala hissa")
        print(f"normal calls patched: {n} (2 hone chahiye: ask_ai + generate_content)")

        pat = re.compile(
            r'with requests\.post\(URL, headers=headers, json=data, stream=True, timeout=\(\d+, *\d+\)\) as resp:\n'
            r'[ \t]*resp\.raise_for_status\(\)'
        )
        new, n2 = pat.subn("with _post_with_fallback(data, timeout=60) as resp:", new)
        print(f"streaming calls patched: {n2} (1 hone chahiye, agar ask_ai_stream add kiya hai)")

        if problems:
            sys.exit("Ye hissa nahi mila, isliye kuch nahi badla — file ka ye part mujhe bhejo:\n  - " + "\n  - ".join(problems))

        new = new.rstrip("\n") + "\n" + BLOCK + AUTO_BLOCK
        changed.append("online/local fallback + Ollama auto start/stop + latency routing")
    else:
        # ---------- Upgrade: pehle wala patch laga hai, bacha hua add karo ----------
        if "_ensure_ollama_running" not in new:
            old = "    try:\n        resp = requests.post(\n            LOCAL_URL,"
            if old not in new:
                sys.exit("Upgrade nahi ho paaya: local fallback wala hissa nahi mila. client_deepseek.py mujhe bhejo.")
            new = new.replace(old, "    try:\n        _ensure_ollama_running()\n        resp = requests.post(\n            LOCAL_URL,", 1)
            new = new.rstrip("\n") + "\n" + AUTO_BLOCK
            changed.append("Ollama auto start/stop")

        if "_provider_pref" not in new:
            old = 'json={**data, "model": MODEL},'
            if old not in new:
                sys.exit("Upgrade nahi ho paaya: online request wala hissa nahi mila. client_deepseek.py mujhe bhejo.")
            new = new.replace(old, 'json={**data, "model": MODEL, **_provider_pref()},', 1)
            new = new.rstrip("\n") + "\n\n\n" + PREF_BLOCK.strip("\n") + "\n"
            changed.append("OpenRouter latency routing")

    if not changed:
        print("Already patched — kuch nahi badla.")
        return

    shutil.copyfile(FILE, FILE + ".bak")
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(new)
    print("Done! Add hua: " + ", ".join(changed))
    print(f"Backup: {FILE}.bak")


if __name__ == "__main__":
    main()
