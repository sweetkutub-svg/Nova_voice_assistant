"""
commands.py — Jervish ka SAARA command logic yahan hai.

Rule: koi bhi naya command yahin add hoga, jervish.py ko chhedne ki
zaroorat nahi. jervish.py sirf yahan se processCommand aur
get_text_reply import karke use karta hai (Flask server aur CLI
voice-loop wahan hai, actual command logic yahan hai).
"""

import sys
import os
import time
import subprocess
import webbrowser

import musicLibeary
from client_elevenlabs import speak, speak_stream
from client_deepseek import ask_ai, ask_ai_stream, generate_content, reset_conversation

# 🔹 Notepad automation ke liye (agar installed na ho to feature
# gracefully disable ho jaayega, baaki sab kaam karta rahega).
try:
    import pyautogui
    import pygetwindow as gw
    import pyperclip
    NOTEPAD_AUTOMATION_AVAILABLE = True
except ImportError:
    NOTEPAD_AUTOMATION_AVAILABLE = False


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


def write_in_notepad(topic):
    """
    Command sunte hi pehle Notepad khol deta hai (blank), uske baad AI se
    content generate karta hai, aur content ko visibly TYPE karta hai
    (paste nahi — dekhne me lagta hai jaise koi likh raha hai), phir
    Ctrl+S se topic ke naam se save karta hai.

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

    # 2. Naya khula hua Notepad window dhundo aur usko focus do — sirf
    #    "sleep karke ummeed lagana" ki jagah explicitly check karte hain,
    #    isse focus/timing wali gadbad nahi hoti.
    notepad_window = None
    for _ in range(20):  # max ~5 second wait
        time.sleep(0.25)
        candidates = [w for w in gw.getWindowsWithTitle("Notepad") if w.visible]
        if candidates:
            notepad_window = candidates[-1]  # sabse naya wala window
            break

    if not notepad_window:
        return "I opened Notepad but couldn't confirm its window — please check if it opened correctly."

    try:
        notepad_window.activate()
    except Exception:
        pass
    time.sleep(0.3)

    # 2b. Windows 11 ka Notepad ek hi instance me tabs use karta hai — agar
    #     pehle se Notepad khula tha, to upar wala Popen sirf ek naya tab
    #     khol sakta hai jisme purana content bhi ho sakta hai. Ctrl+N
    #     bhejke guaranteed blank document banate hain.
    pyautogui.hotkey("ctrl", "n")
    time.sleep(0.5)

    # 3. Ab AI se content generate karo — Notepad already khula, blank,
    #    aur focused hai, isliye is wait ke dauran kuch galat nahi hoga.
    content = generate_content(topic)
    content = _normalize_for_typing(content)

    # 4. Content ko type karo (paste nahi) — dekhne me lagta hai jaise
    #    koi live likh raha hai. Sirf Hindi (Devanagari) content ke liye
    #    paste karte hain kyunki pyautogui.typewrite non-ASCII characters
    #    (jaise Devanagari) type nahi kar sakta.
    if not _is_devanagari(content):
        pyautogui.typewrite(content, interval=0.012)
    else:
        pyperclip.copy(content)
        pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)

    # 5. Ctrl+S se Save As dialog kholo aur naam de ke save karo
    pyautogui.hotkey("ctrl", "s")
    time.sleep(0.7)  # Save As dialog khulne ka wait
    pyautogui.typewrite(safe_name, interval=0.02)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    # Agar isi naam ki file pehle se maujood hai to Windows "overwrite?"
    # confirmation maangta hai — usko bhi confirm kar do.
    pyautogui.press("enter")

    return f"Here's what I wrote about {topic} in Notepad, and saved it as {safe_name}.txt"


def processCommand(command):
    """
    Ek command (text) leta hai, action perform karta hai, aur speak() se
    bolta hai. Ye CLI (mic) mode ke liye hai.
    """
    command = command.lower()

    if "open youtube" in command:
        webbrowser.open("https://www.youtube.com")
        speak("Opening YouTube")

    elif "open facebook" in command:
        webbrowser.open("https://www.facebook.com")
        speak("Opening Facebook")

    elif "open google" in command:
        webbrowser.open("https://www.google.com")
        speak("Opening google")

    elif "open linkedin" in command:
        webbrowser.open("https://www.linkedin.com")
        speak("Opening linkedin")

    elif command.startswith("take screenshort") or command.startswith("take a screenshot"):
        screenshot = pyautogui.screenshot()
        screenshot_folder = os.path.join(os.getcwd(), "screenshort")
        os.makedirs(screenshot_folder, exist_ok=True)
        filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        filepath = os.path.join(screenshot_folder, filename)
        screenshot.save(filepath)
        os.startfile(filepath)
        speak(f"Screenshot taken and saved as {filename}")

    elif command.startswith("take screenshot and open") or command.startswith("take a screenshot and open"):
        screenshot = pyautogui.screenshot()
        screenshot_folder = os.path.join(os.getcwd(), "screenshort")
        os.makedirs(screenshot_folder, exist_ok=True)
        filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        filepath = os.path.join(screenshot_folder, filename)
        screenshot.save(filepath)
        os.startfile(filepath)
        speak(f"Screenshot taken and saved as {filename}")

    elif command.startswith("search for"):
        query = command.split("for", 1)[1].strip()
        url = f"https://www.google.com/search?q={query}"
        webbrowser.open(url)
        speak(f"Searching for {query}")

    elif command.startswith("open website"):
        site = command.split("website", 1)[1].strip()
        ai_response = ask_ai(f"What is the correct URL for {site}? Reply with only the URL, nothing else.")
        url = ai_response.strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        speak(f"Opening {site}")

    elif command.startswith("play"):
        song = command.split(" ")[1]
        link = musicLibeary.music.get(song)
        if link:
            webbrowser.open(link)
            speak(f"Playing {song}")
        else:
            speak("Song not found")

    elif "forget everything" in command or "clear your memory" in command or "forget our conversation" in command:
        reset_conversation()
        speak("Okay, I've cleared my memory. Let's start fresh.")

    elif command.startswith("write about") or command.startswith("write a note about") or command.startswith("note about"):
        topic = command.split("about", 1)[1].strip()
        speak(f"Writing about {topic} in Notepad, give me a moment.")
        reply = write_in_notepad(topic)
        speak(reply)

    elif command.startswith("turn up volume"):
        speak("Turning up the volume.")
        pyautogui.press("volumeup", presses=5, interval=0.1)

    elif command.startswith("open camera and capture image") or command.startswith("open camera and capture photo"):
        speak("Opening camera to capture an image.")
        if sys.platform.startswith("win"):
            subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
            # The Camera app does not capture automatically when it opens.
            # Give it time to load, then Space activates the shutter button.
            if NOTEPAD_AUTOMATION_AVAILABLE:
                time.sleep(3)
                pyautogui.press("space")
                speak("Image captured.")
            else:
                speak("Camera opened, but automatic capture requires pyautogui.")
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", "-a", "Photo Booth"])
            speak("Camera opened. Please press the capture button.")
        else:
            speak("Camera opening is not supported on this OS.")

    elif command.startswith("website about"):
        speak("Wait a minute, I am building the website. Then I will start the work.")
        topic = command.split("about", 1)[1].strip()
        ai_response = ask_ai(
            f"Create a complete HTML and CSS code for a website about {topic}. "
            f"Include inline CSS. Start with <!DOCTYPE html>, and write the full "
            f"code only, with content related to {topic}, without any explanations."
        )
        filename = f"{topic.replace(' ', '_')}.html"
        with open(filename, "w", encoding="utf-8") as file:
            file.write(ai_response)
        speak(f"Website about {topic} created and saved as {filename}")
        webbrowser.open(filename)

    else:
        # AI fallback (jab predefined command na ho)
        # Streaming pipeline: AI likhta jaata hai, saath hi saath bolta jaata hai
        speak_stream(ask_ai_stream(command))


def get_text_reply(command, ai_fallback=True):
    """
    Web mode ke liye: text command leta hai, koi bhi window/browser action
    perform karta hai (agar zaroori ho), aur sirf REPLY TEXT return karta
    hai — koi local audio play nahi karta (wo Flask endpoint ka kaam hai).
    """
    command_lower = command.lower()

    if "open youtube" in command_lower:
        webbrowser.open("https://www.youtube.com")
        return "Opening YouTube."

    if "open facebook" in command_lower:
        webbrowser.open("https://www.facebook.com")
        return "Opening Facebook."

    if "open google" in command_lower:
        webbrowser.open("https://www.google.com")
        return "Opening Google."

    if "open linkedin" in command_lower:
        webbrowser.open("https://www.linkedin.com")
        return "Opening LinkedIn."

    if command_lower.startswith("search for"):
        query = command_lower.split("for", 1)[1].strip()
        webbrowser.open(f"https://www.google.com/search?q={query}")
        return f"Searching for {query}."

    if command_lower.startswith("open website"):
        site = command_lower.split("website", 1)[1].strip()
        ai_response = ask_ai(f"What is the correct URL for {site}? Reply with only the URL, nothing else.")
        url = ai_response.strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opening {site}."

    if command_lower.startswith("open camera and capture image") or command_lower.startswith("open camera and capture photo"):
        if sys.platform.startswith("win"):
            subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
            if NOTEPAD_AUTOMATION_AVAILABLE:
                time.sleep(3)
                pyautogui.press("space")
                return "Image captured."
            return "Camera opened, but automatic capture requires pyautogui."
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", "-a", "Photo Booth"])
            return "Camera opened. Please press the capture button."
        return "Camera opening is not supported on this OS."

    if command_lower.startswith("play"):
        song = command_lower.split(" ")[1]
        link = musicLibeary.music.get(song)
        if link:
            webbrowser.open(link)
            return f"Playing {song}."
        return "Song not found in the library."

    if "forget everything" in command_lower or "clear your memory" in command_lower or "forget our conversation" in command_lower:
        reset_conversation()
        return "Okay, I've cleared my memory. Let's start fresh."

    if command_lower.startswith("write about") or command_lower.startswith("write a note about") or command_lower.startswith("note about"):
        topic = command_lower.split("about", 1)[1].strip()
        return write_in_notepad(topic)

    if command_lower.startswith("website about"):
        topic = command_lower.split("about", 1)[1].strip()
        ai_response = ask_ai(
            f"Create a complete HTML and CSS code for a website about {topic}. "
            f"Include inline CSS. Start with <!DOCTYPE html>, and write the full "
            f"code only, with content related to {topic}, without any explanations."
        )
        filename = f"{topic.replace(' ', '_')}.html"
        with open(filename, "w", encoding="utf-8") as file:
            file.write(ai_response)
        webbrowser.open(filename)
        return f"Website about {topic} created and saved as {filename}."

    # AI fallback (ai_fallback=False ho to None return -> caller streaming use karega)
    if not ai_fallback:
        return None
    return ask_ai(command)


def get_reply_stream(command):
    """
    Web streaming ke liye: generator jo reply ko tukdon (sentences) me deta
    hai. Predefined command ho to ek hi tukda, warna AI ka streaming jawab.
    """
    reply = get_text_reply(command, ai_fallback=False)
    if reply is not None:
        yield reply
    else:
        yield from ask_ai_stream(command)