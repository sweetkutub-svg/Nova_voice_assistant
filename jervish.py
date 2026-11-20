"""
jervish.py — Server (Flask/web mode) aur CLI voice-loop setup.

IMPORTANT: Isme koi command-logic NAHI hai. Har command "commands.py"
file me hai. Naya command add karna ho to commands.py khologe,
jervish.py ko chhedne ki zaroorat nahi padegi.
"""

import os
import sys
import json
import base64
import logging
from pathlib import Path

import speech_recognition as sr

from client_elevenlabs import speak, get_audio_bytes, synthesize_stream
from client_deepseek import reset_conversation

# 👇👇👇 YAHI LINE HAI jisse jervish.py ko saari commands milti hain 👇👇👇
# processCommand()  -> CLI (mic) mode ke commands
# get_text_reply()  -> web mode (nova.html/buddy.html) ke commands
# Dono commands.py me define hain — wahi file kholna hai jab bhi koi
# naya command add/edit karna ho.
from commands import processCommand, get_text_reply, get_reply_stream, return_to_nova

# 🔹 Flask (web frontend, nova.html/buddy.html, isse baat karta hai)
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from flask_cors import CORS

log = logging.getLogger("jervish")

# ============================================================
# CONFIG (naam, wake words, port) — ek jagah
# ============================================================
ASSISTANT_NAME = "Nova"
WAKE_WORDS = ("nova", "jervish")      # inme se kuch bhi bolo to jaag jaata hai
HOST = "127.0.0.1"
PORT = 5000
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = os.environ.get("FRONTEND_FILE", "jarvis.html")


# ============================================================
# WEB MODE — frontend (nova.html/buddy.html) isse call karta hai
# ============================================================
app = Flask(__name__)

# 🔒 SECURITY: sirf apna hi frontend (isi server se khula page) API ko call
# kar sakta hai. Pehle CORS(app) sab websites ko allow karta tha, to koi bhi
# khuli website tumhare laptop pe commands (camera, notepad...) chala sakti thi.
# Frontend kholne ka sahi tarika:  http://127.0.0.1:5000  (HTML double-click mat karo).
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
ALLOWED_ORIGINS |= {o.strip() for o in os.environ.get("EXTRA_ALLOWED_ORIGINS", "").split(",") if o.strip()}
CORS(app, origins=sorted(ALLOWED_ORIGINS))

STATIC_EXTENSIONS = {".mp4", ".webm", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
                     ".css", ".js", ".woff", ".woff2"}


@app.before_request
def _block_foreign_origins():
    """Kisi dusri website se aaya POST (Origin header alag) seedha 403."""
    if request.method == "POST" and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({"error": "Origin not allowed."}), 403


@app.route("/")
def index():
    """Frontend isi server se serve hota hai (isse CORS ki zaroorat hi nahi)."""
    if not (BASE_DIR / FRONTEND_FILE).exists():
        return f"{FRONTEND_FILE} not found next to jervish.py", 404
    return send_from_directory(BASE_DIR, FRONTEND_FILE)


@app.route("/<path:filename>")
def static_assets(filename):
    """Frontend ke saath wali media/css/js files (jaise nova-bg.mp4). Python/.env files kabhi nahi."""
    if Path(filename).suffix.lower() not in STATIC_EXTENSIONS:
        return jsonify({"error": "Not found."}), 404
    return send_from_directory(BASE_DIR, filename)


@app.route("/api/command", methods=["POST"])
def api_command():
    """
    Web version: text leta hai, reply text + audio (base64) dono deta
    hai. Frontend audio ko play karega — browser ki apni TTS use nahi
    hogi, isliye sirf ek hi awaaz sunayi degi.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()

    if not text:
        return jsonify({"error": "No 'text' field provided."}), 400

    try:
        # 👇 Yahan commands.py se import ki hui get_text_reply() use ho rahi hai
        reply = get_text_reply(text)
    except Exception:
        log.exception("api_command failed")
        reply = "Sorry, something went wrong while handling that."

    audio_base64 = None
    audio_format = None
    try:
        audio_bytes, audio_format = get_audio_bytes(reply)
        if audio_bytes:
            audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
    except Exception:
        log.exception("TTS generation failed")  # audio fail ho to bhi text reply to chala jaayega

    return jsonify({"reply": reply, "audio_base64": audio_base64, "audio_format": audio_format})


@app.route("/api/stream", methods=["POST"])
def api_stream():
    """
    Streaming (pipeline) version: reply sentence-by-sentence aata hai,
    har line ek JSON hai {text, audio_base64, audio_format}. Frontend pehli
    line aate hi bolna shuru kar deta hai, baaki peeche se aati rehti hai.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No 'text' field provided."}), 400

    def generate():
        try:
            # 👇 commands.py ka get_reply_stream() + client_elevenlabs ka synthesize_stream()
            for sentence, audio, fmt in synthesize_stream(get_reply_stream(text)):
                b64 = base64.b64encode(audio).decode("utf-8") if audio else None
                yield json.dumps({"text": sentence, "audio_base64": b64, "audio_format": fmt}) + "\n"
        except Exception:
            log.exception("api_stream failed")
            yield json.dumps({"text": "Sorry, something went wrong.", "audio_base64": None, "audio_format": None}) + "\n"
        yield json.dumps({"done": True}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/focus", methods=["POST"])
def api_focus():
    """Wake word ("Nova") sunte hi frontend isse bolta hai: Nova window ko screen par aage laao."""
    try:
        ok = bool(return_to_nova())
    except Exception:
        log.exception("api_focus failed")
        ok = False
    return jsonify({"focused": ok})


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "openrouter_key_loaded": bool(os.environ.get("OPENROUTER_API_KEY")),
        "elevenlabs_key_loaded": bool(os.environ.get("ELEVENLABS_API_KEY")),
    })


# ============================================================
# CLI MODE — mic se wake-word bol ke wake karo
# ============================================================
def run_voice_loop():
    print(f"Initializing {ASSISTANT_NAME}.....")
    speak(f"Hi, I'm {ASSISTANT_NAME}. How can I help you?")

    recognizer = sr.Recognizer()
    recognizer.pause_threshold = 1.5
    recognizer.non_speaking_duration = 0.5

    # IMPORTANT: dynamic_energy_threshold (default True) background noise
    # ke hisaab se "kitni awaaz = bolna" wala level LIVE adjust karta
    # rehta hai — agar recording ke beech me thodi bhi noise badhe, ye
    # level bhi badha deta hai, aur phir tumhari normal awaaz bhi "chup"
    # lagne lagti hai, isliye beech vaakya me hi kaat deta hai. Isse band
    # karke sirf shuru me kiya gaya calibration hi fix rakhte hain.
    recognizer.dynamic_energy_threshold = False

    # Background noise (fan, room hum, etc.) ko ek baar calibrate kar lo
    # taaki chhoti awaazein "kuch bola gaya" na samjhi jaayein.
    with sr.Microphone() as source:
        print("Calibrating for background noise, please stay quiet for a moment...")
        recognizer.adjust_for_ambient_noise(source, duration=1.5)

    # Wake-word mode: sirf "nova"/"jervish" sunta hai, chota timeout
    # hai taaki CPU zyada busy na rahe jab tak koi bulaye nahi.
    while True:
        try:
            print(f"Waiting for wake word ({' / '.join(WAKE_WORDS)})...")
            with sr.Microphone() as source:
                audio = recognizer.listen(source, timeout=4, phrase_time_limit=3)

            word = recognizer.recognize_google(audio)
            print(f"Heard: {word}")

            if any(w in word.lower() for w in WAKE_WORDS):
                speak("Yes? Go ahead, I'm listening.")
                have_continuous_conversation(recognizer)

        except sr.WaitTimeoutError:
            pass  # koi awaaz nahi aayi, wapas wake-word sunna jari rakho
        except sr.UnknownValueError:
            pass  # awaaz aayi par samajh nahi aayi
        except Exception as e:
            print(f"Error: {str(e)}")


def have_continuous_conversation(recognizer):
    """
    Wake word ke baad ye loop chalta hai — jab tak user bolta rahega,
    ye sunta rahega aur reply deta rahega. Do tarike se ye sleep mode
    me wapas jaata hai:
      1. Pure silence (6 second tak kuch bolna shuru hi na ho), ya
      2. Lagatar kai baar sirf background noise/mumble aaye jo samajh
         na aaye (isse background noise hamesha awake nahi rakh sakta).
    """
    SILENCE_TIMEOUT = 6
    MAX_CONSECUTIVE_UNKNOWN = 3  # itni baar "samajh nahi aaya" ke baad sula do

    consecutive_unknown = 0

    while True:
        try:
            print("Listening (conversation active)...")
            with sr.Microphone() as source:
                audio = recognizer.listen(source, timeout=SILENCE_TIMEOUT, phrase_time_limit=15)

            command = recognizer.recognize_google(audio)
            print(f"Command: {command}")
            consecutive_unknown = 0  # asli command mila, counter reset
            # 👇 Yahan commands.py se import ki hui processCommand() use ho rahi hai
            processCommand(command)
            # command process ho gaya, ab loop wapas top pe jaake agla
            # command sunega — user ko dobara "nova" bolne ki zaroorat nahi

        except sr.WaitTimeoutError:
            # Silence detect hui — conversation khatam, wapas sleep mode.
            print("Silence detected — going back to sleep. Say 'nova' to wake me again.")
            speak("Going to sleep. Say nova to wake me again.")
            reset_conversation()  # naya session — purani baatein bhula do
            return

        except sr.UnknownValueError:
            # Kuch awaaz aayi (shayad background noise) par samajh nahi
            # aayi. Agar ye baar baar ho raha hai (asli baat-cheet nahi
            # ho rahi), to bhi sleep kar do — warna noise hamesha jagaye
            # rakhega.
            consecutive_unknown += 1
            print(f"Didn't catch that ({consecutive_unknown}/{MAX_CONSECUTIVE_UNKNOWN})")
            if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
                print("Too much background noise / no real speech — going back to sleep.")
                speak("I'll go to sleep for now. Say nova to wake me again.")
                reset_conversation()  # naya session — purani baatein bhula do
                return
            continue

        except Exception as e:
            print(f"Error: {str(e)}")
            return


# ============================================================
# NOVA ko alag "app window" me kholna (Chrome/Edge --app mode)
# Fayda: Nova ki apni window hoti hai, to command ke baad Windows us window ko
# seedha front me la sakta hai. Normal browser tab me Windows sirf poori browser
# window laa sakta hai, specific tab nahi. Band karna ho: .env me NOVA_APP_WINDOW=0
# ============================================================
def _find_browser_exe():
    import shutil
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("chrome") or shutil.which("msedge")


def open_nova_window():
    """Nova UI ko app-window me kholta hai. Browser na mile to normal tab me."""
    import subprocess, webbrowser
    url = f"http://{HOST}:{PORT}"
    if os.environ.get("NOVA_APP_WINDOW", "1").strip().lower() in ("0", "false", "no", "off"):
        webbrowser.open(url)
        return
    exe = _find_browser_exe()
    try:
        if exe:
            subprocess.Popen([exe, f"--app={url}"])
            return
    except Exception as e:
        print(f"App window nahi khuli ({e}); normal tab me khol raha hu.")
    webbrowser.open(url)


if __name__ == "__main__":
    # "python jervish.py web"  -> Flask server (frontend ke liye)
    # "python jervish.py"      -> purana mic-wala CLI mode
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        print(f"Starting {ASSISTANT_NAME} web backend...")
        print(f"Browser me kholo:  http://{HOST}:{PORT}   (HTML file double-click mat karo)")
        # debug=False: debug mode me Werkzeug debugger arbitrary code run kar sakta hai,
        # aur reloader server ko do baar start kar deta hai.
        import threading
        threading.Timer(1.5, open_nova_window).start()     # server ready hone ke baad Nova window kholo
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    else:
        run_voice_loop()