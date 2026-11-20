"""
intent_router.py — casual baat ko ek SAHI command me badalta hai.

"yaar notepad khol ke sarukhan ke liye kuch likh de"  ->  "write about Shah Rukh Khan"
"youtube chala do"                                    ->  "open youtube"
"kalank bajao"                                        ->  "play kalank"

Kaise kaam karta hai:
  1. commands.py pehle apne fixed patterns try karta hai (turant, bina AI ke).
  2. Match na ho, aur baat "command jaisi" lage (looks_like_command), to ye file
     AI se bolti hai: "is baat ko in commands me se ek me badlo, ya CHAT bolo."
  3. AI ka jawab wapas commands.py ke patterns se verify hota hai. Match na hua
     ya kuch bhi gadbad hui -> normal AI baatcheet. AI sirf registered commands hi
     trigger kar sakta hai, kuch aur nahi.

Seedhe test karne ke liye (jervish folder me, .env me key ke saath):
    python intent_router.py
    python intent_router.py "yaar notepad khol ke sarukhan ke liye kuch likh de"
"""

import os
import re
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ROUTER_URL = os.environ.get("ROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "openai/gpt-4o-mini")
ROUTER_TIMEOUT = 6          # second — itne me jawab na aaye to normal baatcheet pe laut jao
MAX_WORDS = 25              # isse lambi baat command nahi, baatcheet hoti hai

# Gate: in shabdon me se koi bhi ho tabhi AI router bulaya jaata hai. Sirf baatcheet
# wale sawaalon ("what is photosynthesis") me extra delay nahi aata.
GATE_WORDS = {
    # English
    "open", "launch", "start", "play", "search", "google", "find", "write", "note", "notes",
    "notepad", "type", "screenshot", "capture", "camera", "photo", "volume", "louder", "quieter",
    "mute", "weather", "temperature", "time", "date", "day", "website", "site", "youtube",
    "facebook", "linkedin", "github", "gmail", "instagram", "forget",
    "close", "quit", "exit", "minimize", "maximize", "desktop", "tab", "refresh", "reload", "scroll", "pause",
    "resume", "next", "previous", "skip", "lock", "paste", "copy", "undo", "save", "fullscreen", "press",
    "switch", "window", "music", "song", "calculator", "calc", "settings", "setting", "chrome", "edge", "paint",
    "word", "excel", "powerpoint", "spotify", "whatsapp", "discord", "telegram", "vlc", "terminal", "explorer",
    "nova", "unmute",
    # Hinglish / Hindi
    "khol", "kholo", "khol", "chala", "chalao", "chalado", "bajao", "baja", "laga", "lagao",
    "dhoondh", "dhundh", "khoj", "likh", "likho", "mausam", "baje", "samay", "tarikh", "awaaz",
    "awaz", "badha", "badhao", "ghata", "ghatao", "bhool", "bhul",
    "band", "bandh", "bund", "rok", "roko", "ruko", "agla", "pichla", "chalu", "shuru", "neeche", "upar", "kam",
    "खोल", "खोलो", "चला", "चलाओ", "बजाओ", "लिख", "लिखो", "मौसम", "समय", "आवाज़",
}

SYSTEM_TEMPLATE = """You turn a voice-assistant user's request into ONE command.
The user speaks casually in English, Hindi or Hinglish, and speech recognition can mis-spell names.

Available commands (reply with EXACTLY one of these forms, filling the <placeholders>):
{usages}

Rules:
- Reply with the command text only: one line, no quotes, no explanation.
- Fix obvious spelling of names/topics (for example "sarukhan" -> "Shah Rukh Khan").
- If the user is NOT asking the assistant to do one of these things (it is a question,
  a chat, or anything else), reply exactly: CHAT

Examples:
yaar notepad khol ke sarukhan ke liye kuch likh de -> write about Shah Rukh Khan
youtube chala do -> open youtube
kalank bajao -> play kalank
arijit singh ke gaane youtube pe chalao -> play arijit singh songs on youtube
durgapur me mausam kaisa hai -> weather in Durgapur
abhi kitne baje hain -> what time is it
awaaz thodi badha do -> turn up volume
screen ka photo le lo -> take a screenshot
python tutorial google pe dhoondh do -> search for python tutorial
setting ko band karo -> close settings
calculator chalu kar do -> open calculator
chrome minimize kar do -> minimize
gaana roko -> pause music
agla gaana lagao -> next song
ye tab band kar do -> close tab
screen lock kar do -> lock the screen
what is photosynthesis -> CHAT
tum kaise ho -> CHAT
explain how a CPU works -> CHAT"""


def looks_like_command(text):
    """Sasta check: kya ye baat command jaisi lagti hai? (AI bulane layak)"""
    if not text:
        return False
    words = text.split()
    if len(words) > MAX_WORDS:
        return False
    tokens = set(re.findall(r"[a-zऀ-ॿ']+", text.lower()))
    return bool(tokens & GATE_WORDS)


def _clean_reply(raw):
    line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    line = line.strip("`'\" ").rstrip(".")
    return line


def to_command(text, usages, timeout=ROUTER_TIMEOUT):
    """
    AI se poochta hai ki `text` kaunsa command hai. Command ka text return karta
    hai, ya None (CHAT / koi bhi error). Ye kabhi exception nahi uthata.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key or not usages:
        return None
    system = SYSTEM_TEMPLATE.format(usages="\n".join(f"- {u}" for u in usages))
    try:
        resp = requests.post(
            ROUTER_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": ROUTER_MODEL,
                "temperature": 0,
                "max_tokens": 40,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=(4, timeout),
        )
        resp.raise_for_status()
        reply = _clean_reply(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:  # network, HTTP, JSON — kuch bhi ho, baatcheet pe laut jao
        print(f"[smart] router unavailable ({type(e).__name__}), normal chat use ho raha hai")
        return None

    if not reply or reply.upper().startswith("CHAT"):
        return None
    return reply


if __name__ == "__main__":
    # Self-test: dekho AI kya samajhta hai (command chalata NAHI hai, sirf dikhata hai).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import commands

    samples = sys.argv[1:] or [
        "yaar notepad khol ke sarukhan ke liye kuch likh de",
        "youtube chala do",
        "kalank bajao",
        "durgapur me mausam kaisa hai",
        "abhi kitne baje hain",
        "awaaz thodi badha do",
        "what is photosynthesis",
        "tum kaise ho",
    ]
    for s in samples:
        print(f"\n> {s}")
        print("   ", commands.explain(s))