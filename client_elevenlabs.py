import time
import os
import io
import re
import shutil
import subprocess
import queue
import threading
import wave
import pygame
import tempfile
import pyttsx3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from gtts import gTTS

load_dotenv()  # .env file se environment variables load karega

pygame.mixer.init()

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")

if not ELEVENLABS_API_KEY:
    raise ValueError(
        "ELEVENLABS_API_KEY not set. Add it to a .env file next to this "
        "script, e.g.:\n  ELEVENLABS_API_KEY=your_key_here"
    )

client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

VOICE_ID = "pNInz6obpgDQGcFmaJgB"   # Adam (Male)
MODEL_ID = "eleven_turbo_v2_5"      # fast/low-latency model

TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "elevenlabs").lower()

# Online TTS (ElevenLabs/gTTS) ko itna time dete hain, warna turant
# Piper (local) pe fallback kar jaate hain.
TTS_TIMEOUT_SECONDS = 8
PIPER_TIMEOUT_SECONDS = 10
PLAYBACK_MAX_SECONDS = 60

# 🔹 Piper voice models — .onnx files jervish.py wale hi folder me
#    hone chahiye (jaha se "python jervish.py" chalate ho). Naam .env
#    se override bhi kar sakte ho agar alag jagah rakhi hon.
PIPER_EN_MODEL = os.environ.get("PIPER_EN_MODEL", "en_US-ryan-medium.onnx")
PIPER_HI_MODEL = os.environ.get("PIPER_HI_MODEL", "hi_IN-rohan-medium.onnx")
PIPER_AVAILABLE = shutil.which("piper") is not None

# 🔹 Piper ko IN-PROCESS chalao: voice model ek baar memory me load hota hai
#    aur har sentence pe dobara load nahi hota (pehle har sentence pe
#    "piper" command naya process banata tha aur ~2.5s sirf model load me
#    jaate the). Band karna ho to .env me PIPER_INPROCESS=0.
PIPER_INPROCESS = os.environ.get("PIPER_INPROCESS", "1") != "0"
_piper_voices = {}
_piper_load_lock = threading.Lock()
_piper_synth_lock = threading.Lock()
_piper_inprocess_warned = False

_executor = ThreadPoolExecutor(max_workers=2)


def _is_devanagari(text):
    """Check karta hai ki text me Hindi (Devanagari script) characters hain."""
    return any("\u0900" <= ch <= "\u097F" for ch in text)


def _generate_gtts_bytes(text):
    buf = io.BytesIO()
    tts = gTTS(text=text, lang="en")
    tts.write_to_fp(buf)
    return buf.getvalue(), "mp3"


def _generate_elevenlabs_bytes(text):
    audio_stream = client.text_to_speech.convert(
        voice_id=VOICE_ID,
        model_id=MODEL_ID,
        text=text
    )
    return b"".join(audio_stream), "mp3"


def _load_piper_voice(model):
    """Voice ko ek baar load karke cache me rakhta hai."""
    with _piper_load_lock:
        voice = _piper_voices.get(model)
        if voice is None:
            from piper import PiperVoice
            voice = PiperVoice.load(model)   # config = model + ".json" khud dhoondh leta hai
            _piper_voices[model] = voice
        return voice


def _generate_piper_inprocess(text, model):
    """Piper Python API se (CLI process banaye bina) wav bytes banata hai."""
    voice = _load_piper_voice(model)
    buf = io.BytesIO()
    with _piper_synth_lock:
        with wave.open(buf, "wb") as wf:
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(text, wf)      # piper-tts 1.3+
            else:
                voice.synthesize(text, wf)          # purana piper-tts (1.2.x)
    data = buf.getvalue()
    if len(data) < 100:
        raise RuntimeError("Piper ne khali audio banaya")
    return data, "wav"


def _warm_up_piper():
    """Background me dono voices load + ek chhota test — taaki pehle hi command pe delay na aaye."""
    for model, sample in ((PIPER_EN_MODEL, "Ready."), (PIPER_HI_MODEL, "तैयार।")):
        try:
            if os.path.exists(model):
                _generate_piper_inprocess(sample, model)
        except Exception as e:
            print(f"Piper warm-up skipped ({os.path.basename(model)}): {e}")


def _generate_piper_bytes(text):
    """
    Piper se local, offline, fast audio generate karta hai. Text me
    Devanagari (Hindi) characters hain to Hindi voice use karta hai,
    warna English voice.

    IMPORTANT: Windows pe Piper ka andar wala Python UTF-8 ki jagah
    system ka purana codepage (cp1252) use kar leta hai jo Devanagari
    text ko corrupt kar deta hai — isliye PYTHONUTF8=1 env variable
    zaroori hai (verified fix).
    """
    model = PIPER_HI_MODEL if _is_devanagari(text) else PIPER_EN_MODEL

    if not os.path.exists(model):
        raise RuntimeError(f"Piper voice model not found: {model}")

    # 1) Tez raasta: in-process (model memory me loaded rehta hai)
    global _piper_inprocess_warned
    if PIPER_INPROCESS:
        try:
            return _generate_piper_inprocess(text, model)
        except Exception as e:
            if not _piper_inprocess_warned:
                _piper_inprocess_warned = True
                print(f"Piper in-process fail ({e}) — purane CLI tareeke pe ja raha hu (slow).")

    # 2) Purana raasta: "piper" command (har baar naya process)
    if not PIPER_AVAILABLE:
        raise RuntimeError("Piper is not installed (pip install piper-tts)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        filename = tmp.name

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            ["piper", "--model", model, "--output_file", filename],
            input=text.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=PIPER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Piper failed: {result.stderr.decode(errors='ignore')}")

        with open(filename, "rb") as f:
            data = f.read()
        return data, "wav"
    finally:
        try:
            os.remove(filename)
        except OSError:
            pass


def _generate_offline_bytes(text):
    """
    Windows SAPI (pyttsx3) — 100% offline, kabhi fail/timeout nahi hota.
    Final safety net hai — sabse robotic, par hamesha kaam karta hai.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        filename = tmp.name
    try:
        engine = pyttsx3.init()
        engine.save_to_file(text, filename)
        engine.runAndWait()
        engine.stop()
        with open(filename, "rb") as f:
            data = f.read()
        return data, "wav"
    finally:
        try:
            os.remove(filename)
        except OSError:
            pass


def _try_online_provider(text):
    generator = _generate_gtts_bytes if TTS_PROVIDER == "gtts" else _generate_elevenlabs_bytes
    future = _executor.submit(generator, text)
    return future.result(timeout=TTS_TIMEOUT_SECONDS)


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002700-\U000027BF"  # dingbats
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U00002600-\U000026FF"  # misc symbols
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A
    "]+",
    flags=re.UNICODE,
)


def _clean_text_for_speech(text):
    """
    TTS ko bolne se pehle text saaf karta hai — markdown symbols
    (**bold**, *italic*, # headers, `code`) aur emoji hata deta hai,
    warna TTS unhe literally bolne ki koshish karta hai ("asterisk
    asterisk...") jo bahut awkward sunayi deta hai.
    """
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)   # **bold** -> bold
    text = re.sub(r"\*(.*?)\*", r"\1", text)       # *italic* -> italic
    text = re.sub(r"__(.*?)__", r"\1", text)       # __bold__ -> bold
    text = re.sub(r"_(.*?)_", r"\1", text)         # _italic_ -> italic
    text = re.sub(r"`(.*?)`", r"\1", text)         # `code` -> code
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)  # # Headers
    text = _EMOJI_PATTERN.sub("", text)            # emoji hatao
    text = re.sub(r"\s+", " ", text).strip()       # extra spaces saaf karo
    return text


def _generate_audio(text):
    """
    Fallback chain (Piper primary — fast, free, local):
      1. Piper — local, fast, English + Hindi dono, no cost/quota.
      2. Online provider (ElevenLabs/gTTS) — sirf tab agar Piper na chale
         (model missing, ya piper install na ho).
      3. pyttsx3 — 100% offline, guaranteed kaam karega (final safety net).
    Returns: (audio_bytes, format) jahan format 'mp3' ya 'wav' hai.
    """
    text = _clean_text_for_speech(text)

    try:
        return _generate_piper_bytes(text)
    except Exception as e:
        print(f"TTS: Piper failed ({e}), trying online provider...")

    try:
        return _try_online_provider(text)
    except FutureTimeoutError:
        print(f"TTS: online provider took longer than {TTS_TIMEOUT_SECONDS}s, using offline voice instead.")
    except Exception as e:
        print(f"TTS: online provider failed ({e}), using offline voice instead.")

    return _generate_offline_bytes(text)


def _play_audio_bytes(audio_bytes, fmt):
    """Audio bytes ko speaker pe bajata hai (aur khatam hone tak ruka rehta hai)."""
    filename = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            filename = tmp.name
        with open(filename, "wb") as f:
            f.write(audio_bytes)

        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
        pygame.mixer.music.load(filename)
        pygame.mixer.music.play()

        waited = 0.0
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
            waited += 0.05
            if waited > PLAYBACK_MAX_SECONDS:
                print("TTS Error: playback exceeded max wait, stopping.")
                pygame.mixer.music.stop()
                break

        pygame.mixer.music.unload()
    except Exception as e:
        print("TTS Error:", e)
    finally:
        if filename:
            try:
                os.remove(filename)
            except OSError:
                pass


def speak(text):
    """CLI mode ke liye — server pe hi audio play karta hai."""
    try:
        audio_bytes, fmt = _generate_audio(text)
    except Exception as e:
        print("TTS Error (all fallbacks failed):", e)
        return
    _play_audio_bytes(audio_bytes, fmt)


# ============================================================
# 🔹 PIPELINE: AI sentence-by-sentence likhta hai -> TTS har sentence ka
#    audio banata hai -> playback chalta hai. Teeno ek saath, alag
#    threads me, isliye pehli awaaz jaldi aati hai.
# ============================================================
_DONE = object()


def synthesize_stream(sentences):
    """
    `sentences` (text ka iterator/generator) leta hai aur (text, audio_bytes,
    format) tuples ORDER me yield karta hai. Background me AI aur TTS dono
    chalte rehte hain jab tak aap pichla audio play kar rahe ho.
    """
    sentence_q = queue.Queue()
    audio_q = queue.Queue(maxsize=3)  # TTS bahut aage na nikal jaye

    def reader():
        try:
            for s in sentences:
                if s and s.strip():
                    sentence_q.put(s)
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            sentence_q.put(_DONE)

    def tts_worker():
        while True:
            s = sentence_q.get()
            if s is _DONE:
                audio_q.put(_DONE)
                return
            try:
                audio, fmt = _generate_audio(s)
            except Exception as e:
                print(f"TTS Error (all fallbacks failed): {e}")
                audio, fmt = None, None
            audio_q.put((s, audio, fmt))

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=tts_worker, daemon=True).start()

    while True:
        item = audio_q.get()
        if item is _DONE:
            return
        yield item


def speak_stream(sentences):
    """CLI mode: AI ka streaming jawab bolta jaata hai. Poora text return karta hai."""
    spoken = []
    for text, audio, fmt in synthesize_stream(sentences):
        print(f"Nova: {text}")
        spoken.append(text)
        if audio:
            _play_audio_bytes(audio, fmt)
    return " ".join(spoken)


def get_audio_bytes(text):
    """
    Web mode ke liye — (audio_bytes, format) return karta hai. format
    'mp3' ya 'wav' ho sakta hai, taaki frontend sahi mime type se play
    kare.
    """
    try:
        return _generate_audio(text)
    except Exception as e:
        print(f"TTS Error (all fallbacks failed): {e}")
        return None, None


# Import hote hi background me Piper ko garam kar do (startup block nahi hota)
if PIPER_INPROCESS:
    threading.Thread(target=_warm_up_piper, daemon=True).start()