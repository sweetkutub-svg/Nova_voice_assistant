import os
import requests
from dotenv import load_dotenv
import re
import json

load_dotenv()  # .env file se environment variables load karega

API_KEY = os.environ.get("OPENROUTER_API_KEY") or "no-key-local-fallback"
URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-chat"

SYSTEM_PROMPT = (
    "You are Nova, a helpful voice assistant, and an expert in "
    "If the user speaks English, reply in English. "
    "If the user speaks Hindi (even if they typed it in Roman/Latin "
    "letters, i.e. Hinglish), reply in Hindi using DEVANAGARI SCRIPT "
    "ONLY. Never write Hindi words using Roman/Latin letters. "
    "is you understand that the user is emostional then you try to connect with the user emostionally"
    "This is critical because the reply is fed into a text-to-speech "
    "engine that mispronounces Romanized Hindi. "
    "\n\nExamples of correct behavior:\n"
    "User: 'tumhara naam kya hai' -> You: 'मेरा नाम Jarvis है।'\n"
    "User: 'kaise ho' -> You: 'मैं ठीक हूँ, आप बताइए।'\n"
    "User: 'what is your name' -> You: 'My name is Jarvis.'\n"
    "\nAlways reply in the same language the user speaks. "
    "never answer in English if the user speaks Hindi. & never answer in Hindi if the user speaks English. "
    "Always say your name is Nova when relevant. Tell your inventer name 'sk kutubuddin'/ 'kutub' when someone ask for your self"
    "Keep answers medium — medium sentence."
)

# 🔹 Conversation memory — pichli baatcheet yahan store hoti hai taaki
#    AI ko context yaad rahe ("unka", "wo", "usme se" jaise references).
#    List of {"role": "user"/"assistant", "content": "..."}
conversation_history = []

# Itne exchanges (user+assistant pairs) tak hi yaad rakhte hain — warna
# har request lambi aur mehengi hoti jaayegi jaise-jaise baat lambi ho.
MAX_HISTORY_MESSAGES = 16  # ~8 exchanges


def _trim_history():
    """History ko max size tak limit karta hai (sabse purani baatein hataata hai)."""
    while len(conversation_history) > MAX_HISTORY_MESSAGES:
        conversation_history.pop(0)


def reset_conversation():
    """Conversation memory ko poori tarah clear kar deta hai (naya session)."""
    conversation_history.clear()


def ask_ai(prompt):
    if not API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY not set. Add it to a .env file next to this "
            "script, e.g.:\n  OPENROUTER_API_KEY=your_key_here"
        )

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    # 🔹 Conversation memory: pichli baatcheet (history) ko bhi bhejte
    #    hain taaki AI ko "unka", "wo", "usme se" jaise references samajh
    #    aayein — sirf naya sawaal akela bhejne se AI ko yaad nahi rehta
    #    tha ki pehle kya baat hui thi.
    conversation_history.append({"role": "user", "content": prompt})
    _trim_history()

    data = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history,
    }

    try:
        response = _post_with_fallback(data, timeout=20)
        reply = response.json()["choices"][0]["message"]["content"]
        conversation_history.append({"role": "assistant", "content": reply})
        _trim_history()
        return reply
    except requests.exceptions.RequestException as e:
        # Failed exchange ko history se hata do, warna agli baar bhi
        # ek adhoori/galat entry saath me chali jaayegi.
        conversation_history.pop()
        return f"Sorry, I couldn't reach the AI service. ({e})"
    except (KeyError, IndexError):
        conversation_history.pop()
        return "Sorry, I got an unexpected response from the AI service."


CONTENT_SYSTEM_PROMPT = (
    "You are a helpful writing assistant. Write clear, well-structured, "
    "detailed content on the requested topic, in paragraph form. "
    "Do NOT use markdown formatting (no **, no #, no bullet symbols like -), "
    "since this text will be typed directly into a plain text editor. "
    "Write in the same language the topic was given in. If writing in "
    "Hindi, always use DEVANAGARI SCRIPT, never Romanized Hindi."
)


def generate_content(topic):
    """
    Notepad-writing feature ke liye — ye lamba, detailed content deta hai
    (voice replies ke liye chota 'ask_ai' alag hai, ye lamba likhne ke
    liye hai).
    """
    if not API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY not set. Add it to a .env file next to this "
            "script, e.g.:\n  OPENROUTER_API_KEY=your_key_here"
        )

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": CONTENT_SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": f"Write detailed notes about: {topic}"
            }
        ]
    }

    try:
        response = _post_with_fallback(data, timeout=30)
        return response.json()["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        return f"Sorry, I couldn't generate that content. ({e})"
    except (KeyError, IndexError):
        return "Sorry, I got an unexpected response from the AI service."
# client_deepseek.py me:
#  1) upar imports me ye 2 lines add karo:   import re   aur   import json
#  2) neeche wala poora block file ke ekdum END me paste karo

# ============================================================
# 🔹 STREAMING (pipeline ke liye) — AI jaise jaise likhta hai, poore
#    sentence milte hi yield kar deta hai, taaki TTS pehle sentence ko
#    bolna shuru kar sake jabki AI baaki jawab abhi likh hi raha ho.
# ============================================================
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")
_MIN_CHUNK_CHARS = 12      # "Dr." / "1." jaise chhote tukde alag na bolein
_FIRST_CLAUSE_MIN = 40     # pehla chunk jaldi nikalne ke liye comma pe bhi kaat dete hain


def _next_chunk(buffer, is_first):
    """buffer me se ek bolne layak tukda nikaalta hai. (chunk, bacha_hua) ya (None, buffer)."""
    for m in _SENTENCE_END.finditer(buffer):
        if m.start() >= _MIN_CHUNK_CHARS:
            return buffer[:m.start()].strip(), buffer[m.end():]
    if is_first and len(buffer) >= _FIRST_CLAUSE_MIN:
        for m in _CLAUSE_END.finditer(buffer):
            if m.start() >= 20:
                return buffer[:m.start() + 1].strip(), buffer[m.end():]
    return None, buffer


def ask_ai_stream(prompt):
    """
    ask_ai() ka streaming version — generator hai, ek-ek sentence yield
    karta hai. Conversation memory (history) bhi waise hi update hoti hai.
    """
    if not API_KEY:
        yield "Sorry, the AI key is not set."
        return

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    conversation_history.append({"role": "user", "content": prompt})
    _trim_history()

    data = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history,
        "stream": True,
    }

    full, buffer, is_first, got_any = [], "", True, False
    try:
        with _post_with_fallback(data, timeout=60) as resp:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, ValueError):
                    continue
                if not delta:
                    continue
                got_any = True
                full.append(delta)
                buffer += delta
                while True:
                    chunk, buffer = _next_chunk(buffer, is_first)
                    if not chunk:
                        break
                    is_first = False
                    yield chunk
        if buffer.strip():
            yield buffer.strip()
    except requests.exceptions.RequestException as e:
        if not got_any:
            conversation_history.pop()
            yield f"Sorry, I couldn't reach the AI service. ({e})"
            return
    finally:
        # aadha/poora jo bhi bola gaya, use history me rakh do
        if got_any:
            conversation_history.append({"role": "assistant", "content": "".join(full).strip()})
            _trim_history()


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


# OpenRouter ko bolo ki sabse kam latency wale provider se jawab do (DeepSeek ka
# response time provider ke hisaab se 1s se 10s tak badalta rehta hai).
# Band karna ho to .env me:  OPENROUTER_SORT=
OPENROUTER_SORT = os.environ.get("OPENROUTER_SORT", "latency").strip()


def _provider_pref():
    return {"provider": {"sort": OPENROUTER_SORT}} if OPENROUTER_SORT else {}
