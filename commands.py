"""
commands.py — Nova ka SAARA command logic yahan hai.

Naya command add karna ho to bas ek function likho aur uske upar
@command(...) laga do (neeche "COMMAND REGISTRY" section dekho).
jervish.py ko chhedne ki zaroorat nahi.

Har command ek alag function hai, aur har function try/except me chalta hai:
agar kisi ek command ka code galat nikla to SIRF wahi command error dega
(terminal me poora traceback dikhega), baaki assistant chalta rahega.

CLI (mic) aur Web dono isi registry ko use karte hain, isliye ab koi command
do jagah likhna nahi padta:
    processCommand()   -> CLI mode (bolta hai)
    get_text_reply()   -> web mode, poora reply text
    get_reply_stream() -> web mode, tukdon me (streaming)
"""

import os
import re
import sys
import time
import difflib
import logging
import queue
import subprocess
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests

import intent_router
import musicLibeary
from client_elevenlabs import speak, speak_stream
import client_deepseek
from client_deepseek import ask_ai, ask_ai_stream, generate_content, reset_conversation

log = logging.getLogger("jervish.commands")

BASE_DIR = Path(__file__).resolve().parent
SCREENSHOT_DIR = BASE_DIR / "screenshots"
GENERATED_SITES_DIR = BASE_DIR / "generated_sites"
IS_WINDOWS = sys.platform.startswith("win")

# 🔹 Desktop automation (Notepad, screenshot, volume, camera) ke liye. Install
# na ho (ya display na ho) to ye features gracefully band ho jaate hain,
# baaki sab chalta rahega.
try:
    import pyautogui
    import pygetwindow as gw
    import pyperclip
    AUTOMATION_AVAILABLE = True
except Exception:
    AUTOMATION_AVAILABLE = False
NOTEPAD_AUTOMATION_AVAILABLE = AUTOMATION_AVAILABLE  # purana naam (write_in_notepad use karta hai)

# 🔹 Progress: koi lamba command beech me kuch bol sake ("wait, soch raha hu").
#    Handler me bas say("...") likho. CLI me wo turant bolta hai, web streaming me
#    turant bhejta hai; baaki jagah ignore hota hai.
_progress = threading.local()


def say(text):
    callback = getattr(_progress, "callback", None)
    if callback and text:
        callback(text)


AUTOMATION_MISSING_MSG = (
    "I can't do that because 'pyautogui', 'pygetwindow' or 'pyperclip' isn't "
    "installed. Run: pip install pyautogui pygetwindow pyperclip"
)


def _is_devanagari(text):
    """Check karta hai ki text me Hindi (Devanagari script) characters hain."""
    return any("\u0900" <= ch <= "\u097F" for ch in text)


def _normalize_for_typing(text):
    """
    AI kabhi kabhi "smart" typography use karta hai (em-dash —, curly
    quotes '' "", ellipsis …) jo technically non-ASCII hote hain, isse
    poora English content bhi galti se "non-ASCII" maan liya jaata tha
    aur turant paste mode me chala jaata tha. Inhe plain ASCII equivalents
    se replace karte hain taaki English/Hinglish content hamesha TYPE ho,
    paste nahi.
    """
    replacements = {
        "\u2014": "-", "\u2013": "-",       # em-dash, en-dash
        "\u2018": "'", "\u2019": "'",       # curly single quotes
        "\u201c": '"', "\u201d": '"',       # curly double quotes
        "\u2026": "...",                     # ellipsis
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


NOTEPAD_THINKING_LINE = "Please wait, I'm thinking about what to write."
_TYPE_CHUNK = 120   # itne characters type karne ke baad dobara check: Notepad abhi bhi front me hai?


def _is_notepad_title(title):
    """Notepad ki window ka title 'Untitled - Notepad' / 'notes.txt - Notepad' jaisa hota hai."""
    return (title or "").strip().lower().endswith("notepad")


def _is_notepad_active():
    try:
        active = gw.getActiveWindow()
        return bool(active and _is_notepad_title(active.title))
    except Exception:
        return False


def _find_notepad_window():
    try:
        found = [w for w in gw.getWindowsWithTitle("Notepad") if w.visible and _is_notepad_title(w.title)]
    except Exception:
        return None
    return found[-1] if found else None


def _focus_notepad(window=None, attempts=8):
    """
    Notepad ko sach me front me laata hai aur CONFIRM karta hai (True/False).
    Windows kabhi kabhi dusre app (jaise Chrome) se focus chheenne nahi deta, isliye
    alt-key trick aur kai baar koshish. Focus confirm na ho to caller ko kuch bhi
    type / Ctrl+N nahi karna chahiye — warna wo galat window me chala jaata hai.
    """
    for _ in range(attempts):
        if _is_notepad_active():
            return True
        try:
            win = _find_notepad_window() or window
            if win is not None:
                if getattr(win, "isMinimized", False):
                    win.restore()
                pyautogui.press("alt")      # Windows ka foreground-lock todne ki chhoti trick
                win.activate()
        except Exception:
            pass
        time.sleep(0.3)
    return _is_notepad_active()


# ---------- Nova window par wapas aana ----------
# Koi command naya tab/window khole (browser, Notepad, camera...) to kaam hone ke baad
# screen wapas Nova ki window par aa jaati hai. Band karna ho: .env me RETURN_TO_NOVA=0
NOVA_WINDOW_TITLE = os.environ.get("NOVA_WINDOW_TITLE", "Nova - Voice assistant")
RETURN_TO_NOVA = os.environ.get("RETURN_TO_NOVA", "1").strip().lower() not in ("0", "false", "no", "off")
try:
    RETURN_TO_NOVA_DELAY = float(os.environ.get("RETURN_TO_NOVA_DELAY", "2.5"))
except ValueError:
    RETURN_TO_NOVA_DELAY = 2.5
PAGE_RETURN = RETURN_TO_NOVA_DELAY      # browser page khulne ke baad itna ruko, phir Nova par wapas
_return_generation = 0          # naya command aaye to purana pending "wapas jao" cancel ho jaata hai
_return_lock = threading.Lock()


def _find_nova_window():
    """Nova ki window (browser tab ka title 'Nova - Voice assistant ...' hota hai)."""
    try:
        found = [w for w in gw.getWindowsWithTitle(NOVA_WINDOW_TITLE)
                 if w.visible and NOVA_WINDOW_TITLE.lower() in (w.title or "").lower()]
    except Exception:
        return None
    return found[0] if found else None


def _is_nova_active():
    try:
        active = gw.getActiveWindow()
        return bool(active and NOVA_WINDOW_TITLE.lower() in (active.title or "").lower())
    except Exception:
        return False


def return_to_nova(attempts=6):
    """
    Nova window ko front me laata hai, CONFIRM karta hai (True/False).
    Limit: Windows sirf poori window activate kar sakta hai, browser ka specific TAB nahi.
    Isliye Nova ko alag app-window me chalana best hai (jervish.py web ye khud kholta hai).
    """
    if not (AUTOMATION_AVAILABLE and RETURN_TO_NOVA):
        return False
    for _ in range(attempts):
        if _is_nova_active():
            return True
        try:
            win = _find_nova_window()
            if win is None:
                return False            # Nova window mili hi nahi (CLI mode, band hai...) — chup-chaap chhodo
            if getattr(win, "isMinimized", False):
                win.restore()
            pyautogui.press("alt")      # foreground-lock trick
            win.activate()
        except Exception:
            pass
        time.sleep(0.3)
    return _is_nova_active()


def schedule_return_to_nova(delay):
    """Background me: delay ke baad Nova par wapas. Naya command aaye to ye cancel."""
    global _return_generation
    if delay is None or not (AUTOMATION_AVAILABLE and RETURN_TO_NOVA):
        return None
    with _return_lock:
        _return_generation += 1
        token = _return_generation

    def worker():
        time.sleep(max(0.0, delay))
        with _return_lock:
            if token != _return_generation:
                return
        try:
            return_to_nova()
        except Exception:
            log.exception("return_to_nova failed")

    t = threading.Thread(target=worker, name="return-to-nova", daemon=True)
    t.start()
    return t


def cancel_pending_return():
    global _return_generation
    with _return_lock:
        _return_generation += 1


# ---------- Naya khula app/browser background me reh jaye to use front me laana ----------
# Windows kabhi kabhi naye app ko peeche hi rakhta hai (foreground-lock). Isliye launch ke baad
# uski window dhoondh ke alt-key trick se front me laate hain (background thread me, reply nahi rukta).
BROWSER_TITLE_ENDINGS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera")


def _all_windows():
    try:
        return [w for w in gw.getAllWindows() if w.visible and (w.title or "").strip()]
    except Exception:
        return []


def _win_key(w):
    return getattr(w, "_hWnd", None) or w.title


def _is_nova_title(title):
    return NOVA_WINDOW_TITLE.lower() in (title or "").lower()


def _bring_to_front(win, attempts=6):
    """win ko front me laata hai; True agar sach me front me aa gayi."""
    for _ in range(attempts):
        try:
            active = gw.getActiveWindow()
            if active is not None and active.title == win.title:
                return True
            if getattr(win, "isMinimized", False):
                win.restore()
            pyautogui.press("alt")
            win.activate()
        except Exception:
            pass
        time.sleep(0.3)
    try:
        active = gw.getActiveWindow()
        return bool(active and active.title == win.title)
    except Exception:
        return False


def _front_when_ready(finder, timeout=8.0):
    """finder() window return kare to use front me laao. timeout tak 0.5s ke gap se poochta rehta hai."""
    waited = 0.0
    while waited <= timeout:
        try:
            win = finder()
        except Exception:
            win = None
        if win is not None:
            return _bring_to_front(win)
        time.sleep(0.5)
        waited += 0.5
    return False


def front_new_app(before_keys, hint):
    """Launch ke baad: nayi window (ya title me hint wali) ko front me laao. Nova window ko kabhi nahi."""
    def finder():
        wins = [w for w in _all_windows() if not _is_nova_title(w.title)]
        new = [w for w in wins if _win_key(w) not in before_keys]
        if new:
            return new[0]
        if hint:
            hit = [w for w in wins if hint.lower() in w.title.lower()]
            if hit:
                return hit[0]
        return None
    return _front_when_ready(finder)


def front_browser():
    def finder():
        for w in _all_windows():             # z-order: upar wali pehle
            t = w.title.lower()
            if not _is_nova_title(w.title) and t.endswith(BROWSER_TITLE_ENDINGS):
                return w
        return None
    return _front_when_ready(finder, timeout=5.0)


def _in_background(fn, *args):
    if not (IS_WINDOWS and AUTOMATION_AVAILABLE):
        return None
    t = threading.Thread(target=fn, args=args, name="front-window", daemon=True)
    t.start()
    return t


def _open_url(url):
    """Browser me kholo aur (Windows par) browser window ko front me laao."""
    webbrowser.open(url)
    _in_background(front_browser)


def write_in_notepad(topic):
    """
    Order: Notepad kholo -> pakka front me aane do -> Ctrl+N (blank) -> bolo
    "wait, soch raha hu" -> AI se content -> visibly TYPE karo -> Ctrl+S se save.

    Safety: har important step se pehle check hota hai ki Notepad hi front me hai.
    Agar nahi (aur wapas laa nahi paaye) to kuch type nahi karta, warna text Chrome
    jaisi kisi aur window me chala jaata (aur Ctrl+N se wahan naya tab khul jaata).

    Sirf Windows pe kaam karta hai.
    """
    if not NOTEPAD_AUTOMATION_AVAILABLE:
        return (
            "I can't automate Notepad because 'pyautogui', 'pygetwindow', "
            "or 'pyperclip' isn't installed. Run: "
            "pip install pyautogui pygetwindow pyperclip"
        )

    safe_name = "".join(c if c.isalnum() or c in " _-" else "" for c in topic).strip()
    safe_name = safe_name.replace(" ", "_") or "notes"

    try:
        # 1. Sabse pehle Notepad khol do — AI ka wait mat karo
        subprocess.Popen(["notepad.exe"])
    except FileNotFoundError:
        return "I couldn't find Notepad on this system. This feature only works on Windows."

    # 2. Notepad ki window dikhne ka intezaar (max ~8 second)
    notepad_window = None
    for _ in range(32):
        time.sleep(0.25)
        notepad_window = _find_notepad_window()
        if notepad_window:
            break
    if not notepad_window:
        return "I opened Notepad but couldn't confirm its window — please check if it opened correctly."

    # 3. Notepad ko pakka front me lao. Ye confirm hone se pehle Ctrl+N NAHI dabana
    #    (pehle yahi galti thi: Chrome front me tha, to Ctrl+N se Chrome me naya tab khul gaya).
    if not _focus_notepad(notepad_window):
        return "I opened Notepad but couldn't bring it to the front, so I stopped instead of typing into another window."
    time.sleep(0.6)   # window ko input lene layak ho jaane do

    # 3b. Windows 11 ka Notepad tabs use karta hai aur purana content bhi la sakta hai —
    #     Ctrl+N se guaranteed blank document.
    pyautogui.hotkey("ctrl", "n")
    time.sleep(0.6)
    if not _focus_notepad(notepad_window):
        return "Notepad lost focus right after opening, so I stopped instead of typing into another window."

    # 4. Notepad khula, blank aur focused hai — ab bolo, aur AI ko sochne do
    say(NOTEPAD_THINKING_LINE)
    content = generate_content(topic)
    content = _normalize_for_typing(content)

    # 5. AI ke wait me user ne koi aur window click kar di ho sakti hai — dobara check
    if not _focus_notepad(notepad_window):
        return "Notepad was no longer in front when my text was ready, so I didn't type anything."

    # 6. Type karo (paste nahi) — dekhne me lagta hai jaise koi live likh raha hai. Sirf Hindi
    #    (Devanagari) content paste hota hai kyunki typewrite non-ASCII type nahi kar sakta.
    if not _is_devanagari(content):
        for i in range(0, len(content), _TYPE_CHUNK):
            if not _focus_notepad(notepad_window):
                return "Notepad lost focus while I was writing, so I stopped there."
            pyautogui.typewrite(content[i:i + _TYPE_CHUNK], interval=0.012)
    else:
        pyperclip.copy(content)
        pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)

    # 7. Ctrl+S se Save As dialog kholo aur naam de ke save karo
    if not _focus_notepad(notepad_window):
        return "I wrote the text, but Notepad wasn't in front to save it, so I left it unsaved."
    pyautogui.hotkey("ctrl", "s")
    time.sleep(0.7)  # Save As dialog khulne ka wait
    pyautogui.typewrite(safe_name, interval=0.02)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    # Isi naam ki file pehle se ho to Windows "overwrite?" puchta hai — usko bhi confirm kar do.
    pyautogui.press("enter")

    return f"Here's what I wrote about {topic} in Notepad, and saved it as {safe_name}.txt"


# ============================================================
# HELPERS
# ============================================================
SITES = {
    "youtube": "https://www.youtube.com",
    "facebook": "https://www.facebook.com",
    "google": "https://www.google.com",
    "linkedin": "https://www.linkedin.com",
    "github": "https://www.github.com",
    "gmail": "https://mail.google.com",
    "instagram": "https://www.instagram.com",
}

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>\"'`]*)?", re.IGNORECASE)


def _safe_http_url(text):
    """
    AI ke jawab me se ek SAFE http/https URL nikalta hai (ya None). 'javascript:',
    'file:' jaise schemes ya bina domain wali cheezein reject ho jaati hain.
    """
    if not text:
        return None
    # Koi bhi non-http scheme (javascript:, file://, data:, ftp:// ...) -> seedha reject.
    stripped = text.strip().strip("`'\" ")
    if re.match(r"(?i)(?:javascript|vbscript|data|file|blob|about|chrome|ftp)\s*:", stripped):
        return None
    for sm in re.finditer(r"(?i)\b([a-z][a-z0-9+.-]*)://", text):
        if sm.group(1).lower() not in ("http", "https"):
            return None
    m = _URL_RE.search(text)
    if m:
        candidate = m.group(0)
    else:
        d = _DOMAIN_RE.search(text)
        if not d:
            return None
        candidate = "https://" + d.group(0)
    candidate = candidate.rstrip(".,;:)]}")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        return None
    return candidate


def _strip_code_fences(text):
    """AI kabhi ```html ... ``` daal deta hai — wo hatake sirf asli code return karta hai."""
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _safe_filename(text, default="file"):
    name = re.sub(r"[^\w\-]+", "_", text.strip(), flags=re.UNICODE).strip("_")
    return name[:60] or default


def _one_shot(prompt):
    """
    AI se ek akela sawaal poochta hai bina conversation memory kharab kiye
    (URL ya website-code jaise kaam chat history me nahi jaane chahiye).
    """
    history = client_deepseek.conversation_history
    snapshot = list(history)
    try:
        return ask_ai(prompt)
    finally:
        history[:] = snapshot


def _open_file(path):
    """File ko default app me kholta hai (Windows / macOS / Linux)."""
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: only exists on Windows
    elif sys.platform.startswith("darwin"):
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _clean_song_query(query):
    q = query.lower().strip()
    q = re.sub(r"\b(the\s+)?song\b", " ", q)
    q = re.sub(r"^(the|a)\s+", "", q.strip())
    return re.sub(r"\s+", " ", q).strip()


def find_song(query):
    """Library me gaana dhoondhta hai: exact -> naam usme -> milta-julta. (naam, link) ya None."""
    q = _clean_song_query(query)
    library = {k.lower(): v for k, v in musicLibeary.music.items()}
    if not q:
        return None
    if q in library:
        return q, library[q]
    for name, link in library.items():
        if name in q or (len(q) >= 3 and q in name):
            return name, link
    close = difflib.get_close_matches(q, list(library), n=1, cutoff=0.6)
    if close:
        return close[0], library[close[0]]
    return None


# ============================================================
# COMMAND REGISTRY
# ============================================================
class Command:
    __slots__ = ("name", "pattern", "handler", "ack", "windows_only", "needs_automation", "usage", "return_delay")

    def __init__(self, name, pattern, handler, ack, windows_only, needs_automation, usage=None, return_delay=None):
        self.return_delay = return_delay   # None = Nova par wapas nahi; warna itne second baad wapas
        self.usage = usage          # AI router ko dikhane wali ek line, jaise "open <youtube|google>"
        self.name = name
        self.pattern = pattern
        self.handler = handler
        self.ack = ack
        self.windows_only = windows_only
        self.needs_automation = needs_automation


COMMANDS = []   # order matters: pehle match hone wala command chalta hai


def command(name, pattern, *, ack=None, windows_only=False, needs_automation=False, usage=None, return_delay=None):
    r"""
    Naya command register karta hai.

        @command("weather", r"^\s*weather in (?P<city>.+)", ack="Checking the weather.")
        def weather(m):
            return f"It is sunny in {m.group('city')}."

    - handler(m) reply TEXT return karta hai (bolne ka kaam CLI/web khud karte hain).
      Kuch aur bolna na ho to "" return karo.
    - ack: kaam shuru hone se PEHLE turant bolne wali line (string ya function(m)).
    - return_delay: command naya tab/window kholta hai to itne second baad screen wapas
      Nova window par aa jaati hai (None = wapas nahi aana).
    - usage: AI router ke liye ek line ("weather in <city>"). Dene se casual baat
      ("durgapur me mausam kaisa hai") bhi is command tak pahunch jaati hai.
    """
    rx = re.compile(pattern, re.IGNORECASE)

    def decorator(fn):
        COMMANDS.append(Command(name, rx, fn, ack, windows_only, needs_automation, usage, return_delay))
        return fn
    return decorator


def find_command(text):
    """(Command, match) return karta hai, ya None agar koi predefined command nahi mila."""
    text = (text or "").strip()
    for cmd in COMMANDS:
        m = cmd.pattern.search(text)
        if m:
            return cmd, m
    return None


def usages():
    """Router ko dikhane ke liye saare commands ki usage lines (duplicate hata ke)."""
    seen, out = set(), []
    for c in COMMANDS:
        if c.usage and c.usage not in seen:
            seen.add(c.usage)
            out.append(c.usage)
    return out


def resolve_command(text):
    """
    Text ko command me badalta hai:
      1. fixed pattern (turant, bina AI)
      2. nahi mila + baat command jaisi lagti hai -> AI router (casual Hinglish)
    (Command, match) ya None (matlab: normal AI baatcheet).
    """
    found = find_command(text)
    if found:
        return found
    if not intent_router.looks_like_command(text):
        return None
    canonical = intent_router.to_command(text, usages())
    if not canonical:
        return None
    found = find_command(canonical)       # AI ka jawab verify: sirf registered command hi chalega
    if found:
        print(f"[smart] '{text}' -> '{canonical}'")
    return found


def explain(text):
    """Debug: ye text kaunsa command banega? (chalata nahi)."""
    found = find_command(text)
    if found:
        return f"direct match -> {found[0].name}"
    if not intent_router.looks_like_command(text):
        return "normal chat (command jaisa nahi laga, AI router nahi bulaya)"
    canonical = intent_router.to_command(text, usages())
    if not canonical:
        return "normal chat (router ne CHAT kaha)"
    found = find_command(canonical)
    return f"smart -> '{canonical}' -> {found[0].name}" if found else f"router ne '{canonical}' diya par wo koi command nahi, normal chat"


def ack_text(cmd, m):
    if cmd.ack is None:
        return ""
    return cmd.ack(m) if callable(cmd.ack) else cmd.ack


def run_command(cmd, m, say_fn=None):
    """
    Ek command chalata hai. Kuch bhi galat ho to sirf is command ka error aata hai.
    say_fn: handler ke andar say("...") bulane par ye function (text) ke saath chalta hai.
    """
    if cmd.windows_only and not IS_WINDOWS:
        return f"Sorry, the {cmd.name} command only works on Windows."
    if cmd.needs_automation and not AUTOMATION_AVAILABLE:
        return AUTOMATION_MISSING_MSG
    _progress.callback = say_fn
    cancel_pending_return()             # purana pending "wapas jao" naye command ke saath na takraye
    try:
        reply = cmd.handler(m)
        if cmd.return_delay is not None:
            schedule_return_to_nova(cmd.return_delay)
        return reply if reply is not None else ""
    except Exception:
        log.exception("Command '%s' failed", cmd.name)
        return f"Sorry, the {cmd.name} command ran into a problem. Please check the terminal for details."
    finally:
        _progress.callback = None


# ============================================================
# COMMANDS  (naya command yahan add karo)
# ============================================================
@command("open website", r"^\s*open\s+website\s+(?P<site>.+?)\s*$")
def open_website(m):
    site = m.group("site")
    answer = _one_shot(f"What is the correct URL for {site}? Reply with only the URL, nothing else.")
    url = _safe_http_url(answer)
    if not url:
        return f"Sorry, I couldn't find a safe web address for {site}."
    _open_url(url)
    return f"Opening {site}."


CAMERA_WARMUP = float(os.environ.get("CAMERA_WARMUP", "3.0") or 3.0)   # preview ready hone ka wait (second)


def _camera_roll_dir():
    return Path.home() / "Pictures" / "Camera Roll"


def _newest_photo_time():
    """Camera Roll ki sabse nayi file ka time (0 agar folder/file nahi). Photo ban gayi ya nahi, isse pata chalta hai."""
    try:
        times = [p.stat().st_mtime for p in _camera_roll_dir().iterdir() if p.is_file()]
        return max(times) if times else 0.0
    except Exception:
        return 0.0


def _find_camera_window():
    try:
        found = [w for w in gw.getWindowsWithTitle("Camera")
                 if w.visible and (w.title or "").strip().lower() == "camera"]
    except Exception:
        return None
    return found[-1] if found else None


def _is_camera_active():
    try:
        active = gw.getActiveWindow()
        return bool(active and (active.title or "").strip().lower() == "camera")
    except Exception:
        return False


def _focus_camera(attempts=8):
    for _ in range(attempts):
        if _is_camera_active():
            return True
        try:
            win = _find_camera_window()
            if win is not None:
                if getattr(win, "isMinimized", False):
                    win.restore()
                pyautogui.press("alt")
                win.activate()
        except Exception:
            pass
        time.sleep(0.3)
    return _is_camera_active()


def capture_photo():
    """
    Camera app kholo -> uski window dikhne ka wait -> front me laao (confirm) -> preview ready
    hone do -> Space (shutter) -> Camera Roll me nayi photo aayi ya nahi, check karo.
    Pehle ye 3 second ke baad blind Space dabata tha: camera background me khulta to photo nahi
    banti thi. Ab har step verify hota hai, aur jhooth nahi bolta ("captured" tabhi jab file bani).
    """
    before = _newest_photo_time()
    subprocess.Popen(["cmd", "/c", "start", "", "microsoft.windows.camera:"])
    for _ in range(24):                          # max ~12 second window ka intezaar
        if _find_camera_window() is not None:
            break
        time.sleep(0.5)
    else:
        return "I opened the camera, but its window didn't appear, so I couldn't take the photo."
    if not _focus_camera():
        return "The camera opened in the background and I couldn't bring it to the front, so I didn't take the photo."
    time.sleep(CAMERA_WARMUP)                    # sensor + preview ko ready hone do
    if not _focus_camera(attempts=3):            # beech me focus kisi aur ne le liya to space galat jagah jaata
        return "The camera lost focus, so I didn't take the photo."
    pyautogui.press("space")
    for _ in range(10):                          # nayi photo ka intezaar (~5 second)
        time.sleep(0.5)
        if _newest_photo_time() > before:
            return "Image captured and saved in your Camera Roll."
    return "I pressed capture, but I couldn't confirm that the photo was saved. Please check your Camera Roll."


@command("open camera",
         r"^\s*(?:open\s+camera\s+and\s+capture\s+(?:an?\s+)?(?:image|photo)\b|(?:take|click|capture)\s+(?:a\s+)?(?:photo|picture|selfie|pic)\b)",
         ack="Opening camera to capture an image.", windows_only=True, needs_automation=True, return_delay=1.0)
def open_camera(m):
    return capture_photo()


@command("open site", r"\bopen\s+(?P<site>" + "|".join(SITES) + r")\b")
def open_site(m):
    site = m.group("site").lower()
    _open_url(SITES[site])
    return f"Opening {site.capitalize()}."


@command("screenshot", r"^\s*(?:take|capture)\s+(?:a\s+)?(?:screenshot|screenshort)\b",
         needs_automation=True)
def take_screenshot(m):
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
    filepath = SCREENSHOT_DIR / filename
    pyautogui.screenshot().save(str(filepath))
    _open_file(filepath)
    return f"Screenshot taken and saved as {filename}"


@command("search", r"^\s*search\s+for\s+(?P<query>.+?)\s*$")
def web_search(m):
    query = m.group("query")
    _open_url(f"https://www.google.com/search?q={quote_plus(query)}")
    return f"Searching for {query}."


# ---------- YouTube / time / date / weather ----------
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _youtube_first_video(query):
    """YouTube search page se pehle video ka ID nikalta hai (nahi mila ya error to None)."""
    try:
        r = requests.get("https://www.youtube.com/results", params={"search_query": query},
                         headers=_BROWSER_HEADERS, timeout=5)
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', r.text)
        return m.group(1) if m else None
    except requests.exceptions.RequestException:
        return None


def _play_on_youtube(query):
    """Pehla result seedha chalata hai; ID na mile to search results khol deta hai."""
    video_id = _youtube_first_video(query)
    if video_id:
        _open_url(f"https://www.youtube.com/watch?v={video_id}")
    else:
        _open_url(f"https://www.youtube.com/results?search_query={quote_plus(query)}")


WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "snow showers",
    95: "a thunderstorm", 96: "a thunderstorm with hail", 99: "a thunderstorm with hail",
}


def _fetch_weather(city):
    """Open-Meteo (free, bina API key) se mausam. dict ya None (shehar nahi mila)."""
    geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                       params={"name": city, "count": 1, "language": "en"}, timeout=6)
    geo.raise_for_status()
    results = geo.json().get("results")
    if not results:
        return None
    place = results[0]
    wx = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "current": "temperature_2m,relative_humidity_2m,weather_code",
    }, timeout=6)
    wx.raise_for_status()
    cur = wx.json()["current"]
    return {
        "place": place.get("name", city),
        "temp": round(cur["temperature_2m"]),
        "humidity": round(cur["relative_humidity_2m"]),
        "desc": WEATHER_CODES.get(cur.get("weather_code"), "mixed conditions"),
    }


@command("youtube play", r"^\s*play\s+(?P<q>.+?)\s+on\s+youtube\s*$",
         usage="play <song or video> on youtube")
def play_on_youtube(m):
    q = m.group("q")
    _play_on_youtube(q)
    return f"Playing {q} on YouTube."


@command("youtube search", r"^\s*(?:youtube\s+search|search\s+youtube\s+for)\s+(?P<q>.+?)\s*$",
         usage="youtube search <query>")
def youtube_search(m):
    q = m.group("q")
    _open_url(f"https://www.youtube.com/results?search_query={quote_plus(q)}")
    return f"Searching YouTube for {q}."


@command("time", r"^\s*(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|(?:tell\s+me\s+)?the\s+(?:current\s+)?time|current\s+time|time\s+please)(?:\s+(?:now|right\s+now))?\s*[?.!]?\s*$",
         usage="what time is it")
def tell_time(m):
    now = datetime.now()
    return f"It's {now.strftime('%I:%M %p').lstrip('0')}."


@command("date", r"^\s*(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:today'?s\s+)?date(?:\s+today)?|what\s+day\s+is\s+(?:it|today)|today'?s\s+date|(?:tell\s+me\s+)?the\s+date)\s*[?.!]?\s*$",
         usage="what is today's date")
def tell_date(m):
    now = datetime.now()
    return f"Today is {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}."


@command("weather", r"^\s*(?:(?:what(?:'s|\s+is)|how(?:'s|\s+is))\s+the\s+)?(?:weather|temperature)(?:\s+(?:like\s+)?(?:in|at|of|for)\s+(?P<city>[\w\s.,-]+?))?\s*[?.!]?\s*$",
         usage="weather in <city>  (or just: weather)")
def weather(m):
    city = (m.group("city") or os.environ.get("DEFAULT_CITY", "Kolkata")).strip()
    try:
        data = _fetch_weather(city)
    except (requests.exceptions.RequestException, KeyError, ValueError):
        return "Sorry, I couldn't reach the weather service. Please check your internet."
    if not data:
        return f"Sorry, I couldn't find a place called {city}."
    return (f"In {data['place']} it's {data['temp']} degrees Celsius with {data['desc']}. "
            f"Humidity is {data['humidity']} percent.")


@command("play", r"^\s*play\s*$")
def play_prompt(m):
    return "Which song should I play?"


@command("play", r"^\s*play\s+(?P<song>.+?)\s*$")
def play_song(m):
    found = find_song(m.group("song"))
    if not found:
        # Library me nahi hai -> YouTube se chalao
        song = _clean_song_query(m.group("song")) or m.group("song")
        _play_on_youtube(song)
        return f"I don't have {song} in my library, so I'm playing it from YouTube."
    name, link = found
    _open_url(link)
    return f"Playing {name}."


@command("forget", r"forget everything|clear your memory|forget our conversation")
def forget_memory(m):
    reset_conversation()
    return "Okay, I've cleared my memory. Let's start fresh."


@command("write note", r"^\s*(?:write\s+(?:a\s+note\s+)?about|note\s+about)\s+(?P<topic>.+?)\s*$",
         windows_only=True, needs_automation=True, return_delay=0.5)   # ack nahi: pehle Notepad khulna chahiye, bolna baad me
def write_note(m):
    return write_in_notepad(m.group("topic"))


@command("volume up", r"^\s*(?:(?:turn\s+up|increase|raise)\s+(?:the\s+)?volume|volume\s+up)\b",
         ack="Turning up the volume.", needs_automation=True)
def volume_up(m):
    pyautogui.press("volumeup", presses=5, interval=0.1)
    return ""


@command("volume down", r"^\s*(?:(?:turn\s+down|decrease|lower|reduce)\s+(?:the\s+)?volume|volume\s+down)\b",
         ack="Turning down the volume.", needs_automation=True)
def volume_down(m):
    pyautogui.press("volumedown", presses=5, interval=0.1)
    return ""


@command("website builder", r"^\s*website\s+about\s+(?P<topic>.+?)\s*$",
         ack="Wait a minute, I am building the website. Then I will start the work.")
def build_website(m):
    topic = m.group("topic")
    code = _one_shot(
        f"Create a complete HTML and CSS code for a website about {topic}. "
        f"Include inline CSS. Start with <!DOCTYPE html>, and write the full "
        f"code only, with content related to {topic}, without any explanations."
    )
    html = _strip_code_fences(code)
    GENERATED_SITES_DIR.mkdir(exist_ok=True)
    path = GENERATED_SITES_DIR / f"{_safe_filename(topic, 'website')}.html"
    path.write_text(html, encoding="utf-8")
    _open_url(path.resolve().as_uri())
    return f"Website about {topic} created and saved as {path.name}"



# ============================================================
# COMPUTER CONTROL: apps kholna/band karna, keyboard/window control, Nova par wapas bulana
# (sirf Windows). Saare commands fixed table se chalte hain: user ki bolayi hui cheez kabhi
# shell command nahi banti, isliye safe.
# ============================================================
# naam -> (kholne ka target, band karne ke liye process (ya None))
APPS = {
    "notepad": ("notepad.exe", "notepad.exe"),
    "calculator": ("calc.exe", "CalculatorApp.exe"),
    "paint": ("mspaint.exe", "mspaint.exe"),
    "command prompt": ("cmd.exe", "cmd.exe"),
    "cmd": ("cmd.exe", "cmd.exe"),
    "powershell": ("powershell.exe", "powershell.exe"),
    "terminal": ("wt.exe", "WindowsTerminal.exe"),
    "file explorer": ("explorer.exe", None),
    "explorer": ("explorer.exe", None),
    "task manager": ("taskmgr.exe", "Taskmgr.exe"),
    "settings": ("ms-settings:", "SystemSettings.exe"),
    "control panel": ("control.exe", None),
    "word": ("winword.exe", "WINWORD.EXE"),
    "excel": ("excel.exe", "EXCEL.EXE"),
    "powerpoint": ("powerpnt.exe", "POWERPNT.EXE"),
    "onenote": ("onenote:", "ONENOTE.EXE"),
    "outlook": ("outlook.exe", "OUTLOOK.EXE"),
    "teams": ("msteams:", "ms-teams.exe"),
    "vs code": ("code", "Code.exe"),
    "vscode": ("code", "Code.exe"),
    "chrome": ("chrome.exe", "chrome.exe"),
    "edge": ("msedge.exe", "msedge.exe"),
    "firefox": ("firefox.exe", "firefox.exe"),
    "spotify": ("spotify:", "Spotify.exe"),
    "whatsapp": ("whatsapp:", "WhatsApp.exe"),
    "telegram": ("telegram.exe", "Telegram.exe"),
    "discord": ("discord.exe", "Discord.exe"),
    "vlc": ("vlc.exe", "vlc.exe"),
    "snipping tool": ("snippingtool.exe", "SnippingTool.exe"),
    "clock": ("ms-clock:", None),
    "store": ("ms-windows-store:", None),
    "photos": ("ms-photos:", None),
    "maps": ("bingmaps:", None),
    "camera": ("microsoft.windows.camera:", "WindowsCamera.exe"),
}

APP_TITLE_HINT = {
    "notepad": "Notepad", "calculator": "Calculator", "paint": "Paint", "command prompt": "Command Prompt",
    "cmd": "Command Prompt", "powershell": "PowerShell", "terminal": "Terminal", "task manager": "Task Manager",
    "settings": "Settings", "control panel": "Control Panel", "word": "Word", "excel": "Excel",
    "powerpoint": "PowerPoint", "onenote": "OneNote", "outlook": "Outlook", "teams": "Teams",
    "vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "chrome": "Google Chrome",
    "edge": "Microsoft Edge", "firefox": "Firefox", "spotify": "Spotify", "whatsapp": "WhatsApp",
    "telegram": "Telegram", "discord": "Discord", "vlc": "VLC", "snipping tool": "Snipping Tool",
    "clock": "Clock", "store": "Microsoft Store", "photos": "Photos", "maps": "Maps", "camera": "Camera",
}
_APP_RX = "|".join(re.escape(a) for a in sorted(APPS, key=len, reverse=True))


def _launch(target):
    subprocess.Popen(["cmd", "/c", "start", "", target])


@command("open app", r"^\s*(?:open|launch|start)\s+(?:the\s+)?(?P<app>" + _APP_RX + r")(?:\s+app)?\s*$",
         windows_only=True,
         usage="open <app>   (notepad, calculator, paint, word, excel, powerpoint, vs code, chrome, spotify, "
               "task manager, settings, file explorer, command prompt, whatsapp, discord ...)")
def open_app(m):
    app = m.group("app").lower()
    before = {_win_key(w) for w in _all_windows()} if (IS_WINDOWS and AUTOMATION_AVAILABLE) else set()
    _launch(APPS[app][0])
    _in_background(front_new_app, before, APP_TITLE_HINT.get(app))    # background me na reh jaye
    return f"Opening {app}."


@command("close app", r"^\s*(?:close|quit|exit|kill)\s+(?:the\s+)?(?P<app>" + _APP_RX + r")(?:\s+app)?\s*$",
         windows_only=True, usage="close <app>")
def close_app(m):
    app = m.group("app").lower()
    proc = APPS[app][1]
    if not proc:
        return f"Sorry, I can't close {app} safely."
    # /F nahi: app apna "save karna hai?" dialog dikha sakta hai, kaam khoya nahi jaata.
    result = subprocess.run(["taskkill", "/IM", proc], capture_output=True, text=True)
    return f"Closing {app}." if result.returncode == 0 else f"{app.capitalize()} doesn't seem to be running."


@command("go to nova",
         r"^\s*(?:(?:go|come)\s+back(?:\s+to\s+nova)?|(?:go\s+to|show|switch\s+to|open|bring\s+up|focus)\s+nova(?:\s+(?:screen|window))?|"
         r"nova\s+(?:screen|window)|bring\s+nova\s+(?:here|to\s+(?:the\s+)?front))\s*$",
         windows_only=True, needs_automation=True, usage="go to nova screen  (brings the Nova window to the front)")
def go_to_nova(m):
    return "I'm here." if return_to_nova() else "I couldn't find my window. Is Nova open in a browser window?"


# ---- Keyboard / window control: "jis app me tum the, usme kaam karo" ----
def _to_previous_app():
    """
    Nova front me ho to Alt+Tab se pichli app par jao (jo tum use kar rahe the). Agar phir bhi
    Nova hi front me hai to None (kuch type/click mat karo — warna Nova ke andar chala jaata).
    Return: None = fail, True = ready.
    """
    if _is_nova_active():
        pyautogui.hotkey("alt", "tab")
        time.sleep(0.7)
        if _is_nova_active():
            return None
    return True


_NOT_READY = "I couldn't switch to your other app. Click the app you want to control first, then ask me again."


def _do_keys(*keys):
    if not _to_previous_app():
        return _NOT_READY
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return ""


KEY_NAMES = {
    "enter": "enter", "escape": "esc", "esc": "esc", "tab": "tab", "space": "space", "backspace": "backspace",
    "delete": "delete", "up": "up", "down": "down", "left": "left", "right": "right", "home": "home",
    "end": "end", "page up": "pageup", "page down": "pagedown", "f5": "f5", "f11": "f11",
}
_KEY_RX = "|".join(re.escape(k) for k in sorted(KEY_NAMES, key=len, reverse=True))


@command("press key", r"^\s*press\s+(?:the\s+)?(?P<key>" + _KEY_RX + r")(?:\s+key)?\s*$",
         windows_only=True, needs_automation=True,
         usage="press <enter|escape|tab|space|backspace|delete|up|down|left|right|page up|page down>")
def press_key(m):
    return _do_keys(KEY_NAMES[m.group("key").lower()])


@command("type text", r"^\s*type\s+(?!of\b)(?P<text>.+?)\s*$", windows_only=True, needs_automation=True,
         usage="type <text>   (types the text into the app you were using)")
def type_text(m):
    if not _to_previous_app():
        return _NOT_READY
    pyperclip.copy(m.group("text"))          # clipboard se paste: har language/symbol sahi jaata hai
    pyautogui.hotkey("ctrl", "v")
    return ""


# naam -> (pattern, keys, ack/reply)
_HOTKEY_COMMANDS = {
    "minimize": (r"(?:minimi[sz]e)(?:\s+(?:this|the)\s+(?:window|app))?", ("win", "down"), "usage: minimize"),
    "maximize": (r"(?:maximi[sz]e)(?:\s+(?:this|the)\s+(?:window|app))?", ("win", "up"), "usage: maximize"),
    "show desktop": (r"(?:show\s+(?:the\s+)?desktop|minimi[sz]e\s+all(?:\s+windows)?)", ("win", "d"), "usage: show desktop"),
    "switch window": (r"(?:switch\s+(?:the\s+)?(?:window|app)|next\s+window)", ("alt", "tab"), "usage: switch window"),
    "new tab": (r"(?:open\s+a?\s*new\s+tab|new\s+tab)", ("ctrl", "t"), "usage: new tab"),
    "close tab": (r"close\s+(?:this\s+|the\s+|current\s+)?tab", ("ctrl", "w"), "usage: close tab"),
    "refresh": (r"(?:refresh|reload)(?:\s+(?:the\s+)?page)?", ("f5",), "usage: refresh"),
    "go back": (r"go\s+back\s+(?:a\s+)?page|navigate\s+back", ("alt", "left"), "usage: go back a page"),
    "copy": (r"copy(?:\s+(?:this|that|it|selection))?", ("ctrl", "c"), "usage: copy"),
    "paste": (r"paste(?:\s+(?:this|that|it))?", ("ctrl", "v"), "usage: paste"),
    "select all": (r"select\s+all", ("ctrl", "a"), "usage: select all"),
    "undo": (r"undo(?:\s+(?:that|it))?", ("ctrl", "z"), "usage: undo"),
    "save file": (r"save\s+(?:this|the\s+file|it|file)", ("ctrl", "s"), "usage: save this"),
    "fullscreen": (r"(?:full\s*screen|toggle\s+full\s*screen)", ("f11",), "usage: fullscreen"),
}


def _register_hotkey(name, pat, keys, usage):
    @command(name, r"^\s*" + pat + r"\s*[.!]?\s*$", windows_only=True, needs_automation=True,
             usage=usage.replace("usage: ", ""))
    def _run(m, _keys=keys):
        if _keys == ("alt", "tab"):          # Nova front me ho to seedha Alt+Tab
            pyautogui.hotkey("alt", "tab")
            return ""
        return _do_keys(*_keys)
    return _run


for _n, (_p, _k, _u) in _HOTKEY_COMMANDS.items():
    _register_hotkey(_n, _p, _k, _u)


@command("scroll", r"^\s*scroll\s+(?P<dir>up|down)(?:\s+a\s+(?:little|lot))?\s*$", windows_only=True,
         needs_automation=True, usage="scroll <up|down>")
def scroll(m):
    if not _to_previous_app():
        return _NOT_READY
    pyautogui.scroll(600 if m.group("dir").lower() == "up" else -600)
    return ""


# ---- Media / system (focus ki zaroorat nahi) ----
@command("pause media", r"^\s*(?:pause|resume|play\s*/\s*pause)(?:\s+(?:the\s+)?(?:music|song|video|playback|media))?\s*[.!]?\s*$",
         windows_only=True, needs_automation=True, usage="pause music  (or: resume)")
def pause_media(m):
    pyautogui.press("playpause")
    return ""


@command("next track", r"^\s*(?:next\s+(?:song|track)|skip(?:\s+(?:this\s+)?(?:song|track))?)\s*[.!]?\s*$",
         windows_only=True, needs_automation=True, usage="next song")
def next_track(m):
    pyautogui.press("nexttrack")
    return ""


@command("previous track", r"^\s*(?:previous\s+(?:song|track)|last\s+song)\s*[.!]?\s*$",
         windows_only=True, needs_automation=True, usage="previous song")
def previous_track(m):
    pyautogui.press("prevtrack")
    return ""


@command("mute", r"^\s*(?:mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio))?\s*[.!]?\s*$",
         windows_only=True, needs_automation=True, usage="mute  (or: unmute)")
def mute(m):
    pyautogui.press("volumemute")
    return ""


@command("lock screen", r"^\s*lock\s+(?:the\s+)?(?:screen|computer|pc|laptop)\s*[.!]?\s*$", windows_only=True,
         ack="Locking the screen.", usage="lock the screen")
def lock_screen(m):
    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
    return ""



# AI router ko in commands ke "form" dikhane ke liye (naye commands apne @command me usage= dete hain)
_USAGE = {
    "open website": "open website <site name>",
    "open camera": "open camera and capture image   (also: take a photo)",
    "open site": "open <" + "|".join(SITES) + ">",
    "screenshot": "take a screenshot",
    "search": "search for <query>",
    "play": "play <song name>",
    "forget": "forget everything",
    "write note": "write about <topic>   (writes notes about the topic in Notepad)",
    "volume up": "turn up volume",
    "volume down": "turn down volume",
    "website builder": "website about <topic>   (builds an HTML website)",
}
for _c in COMMANDS:
    if _c.usage is None:
        _c.usage = _USAGE.get(_c.name)

# ============================================================
# ENTRY POINTS  (jervish.py yahi 3 functions import karta hai)
# ============================================================
def processCommand(command):
    """CLI (mic) mode: command chalata hai aur reply bolta hai."""
    command = (command or "").strip()
    if not command:
        return
    found = resolve_command(command)
    if not found:
        # Predefined command nahi mila -> AI se baat, streaming (likhta jaata hai, bolta jaata hai)
        speak_stream(ask_ai_stream(command))
        return
    cmd, m = found
    ack = ack_text(cmd, m)
    if ack:
        speak(ack)

    # Beech ki lines (say) alag thread me bolo, taaki kaam (jaise AI ka sochna) ruke nahi.
    talkers = []

    def say_async(text):
        t = threading.Thread(target=speak, args=(text,), daemon=True)
        t.start()
        talkers.append(t)

    reply = run_command(cmd, m, say_fn=say_async)
    for t in talkers:           # pehle ki line poori bol chuki ho, tabhi aakhri reply (warna kat jaati)
        t.join(timeout=20)
    if reply:
        speak(reply)


def get_text_reply(command, ai_fallback=True):
    """
    Web mode: command chalata hai aur poora reply TEXT return karta hai (audio
    banana Flask ka kaam hai). ai_fallback=False ho aur koi predefined command
    na mile to None return karta hai.
    """
    found = resolve_command(command)
    if found:
        cmd, m = found
        reply = run_command(cmd, m)
        return reply or ack_text(cmd, m) or "Done."
    if not ai_fallback:
        return None
    return ask_ai(command)


def get_reply_stream(command):
    """
    Web streaming: reply ko tukdon me yield karta hai. Pehle chhota 'ack' (agar
    hai) taaki kaam chalte waqt awaaz pehle se bajne lage, phir asli reply.
    """
    found = resolve_command(command)
    if not found:
        yield from ask_ai_stream(command)
        return
    cmd, m = found
    ack = ack_text(cmd, m)
    if ack:
        yield ack

    # Handler worker thread me chalta hai; uski say() lines turant yahan se yield hoti hain.
    lines, finished, result = queue.Queue(), object(), {}

    def worker():
        try:
            result["reply"] = run_command(cmd, m, say_fn=lines.put)
        finally:
            lines.put(finished)

    threading.Thread(target=worker, daemon=True).start()
    said_something = bool(ack)
    while True:
        item = lines.get()
        if item is finished:
            break
        said_something = True
        yield item

    reply = result.get("reply")
    if reply:
        yield reply
    elif not said_something:
        yield "Done."