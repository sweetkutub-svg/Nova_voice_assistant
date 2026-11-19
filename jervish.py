"""
jervish.py — Server (Flask/web mode) aur CLI voice-loop setup.

IMPORTANT: Isme koi command-logic NAHI hai. Har command "commands.py"
file me hai. Naya command add karna ho to commands.py khologe,
jervish.py ko chhedne ki zaroorat nahi padegi.
"""

import sys
import json
import base64
import speech_recognition as sr

from client_elevenlabs import speak, get_audio_bytes, synthesize_stream
from client_deepseek import reset_conversation

# 👇👇👇 YAHI LINE HAI jisse jervish.py ko saari commands milti hain 👇👇👇
# processCommand()  -> CLI (mic) mode ke commands
# get_text_reply()  -> web mode (nova.html/buddy.html) ke commands
# Dono commands.py me define hain — wahi file kholna hai jab bhi koi
# naya command add/edit karna ho.
from commands import processCommand, get_text_reply, get_reply_stream

# 🔹 Flask (web frontend, nova.html/buddy.html, isse baat karta hai)
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS


# ============================================================
# WEB MODE — frontend (nova.html/buddy.html) isse call karta hai
# ============================================================
app = Flask(__name__)
CORS(app)


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
    except Exception as e:
        reply = f"Sorry, I couldn't reach the AI service. ({e})"

    audio_base64 = None
    audio_format = None
    try:
        audio_bytes, audio_format = get_audio_bytes(reply)
        if audio_bytes:
            audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        print(f"TTS generation failed: {e}")  # audio fail ho to bhi text reply to chala jaayega

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
        except Exception as e:
            yield json.dumps({"text": f"Sorry, something went wrong. ({e})", "audio_base64": None, "audio_format": None}) + "\n"
        yield json.dumps({"done": True}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/health", methods=["GET"])
def api_health():
    import os
    return jsonify({
        "status": "ok",
        "openrouter_key_loaded": bool(os.environ.get("OPENROUTER_API_KEY")),
        "elevenlabs_key_loaded": bool(os.environ.get("ELEVENLABS_API_KEY")),
    })


# ============================================================
# CLI MODE — mic se wake-word bol ke wake karo
# ============================================================
def run_voice_loop():
    print("Initializing Nexus.....")
    speak("Hey Buddy How are you?.....")
    speak("How can I help you?.....")

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
            print("Waiting for wake word ('nova')...")
            with sr.Microphone() as source:
                audio = recognizer.listen(source, timeout=4, phrase_time_limit=3)

            word = recognizer.recognize_google(audio)
            print(f"Heard: {word}")

            if "nova" in word.lower() or "jervish" in word.lower():
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


if __name__ == "__main__":
    # "python jervish.py web"  -> Flask server (frontend ke liye)
    # "python jervish.py"      -> purana mic-wala CLI mode
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        print("Starting Jervish web backend on http://127.0.0.1:5000 ...")
        app.run(host="127.0.0.1", port=5000, debug=True)
    else:
        run_voice_loop()