"""
test_commands.py — commands.py aur jervish.py ke tests (internet, mic, API key kuch nahi chahiye).

Chalane ke liye (jervish folder me):
    pip install pytest
    pytest -q
"""
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ---------- Asli TTS/AI/mic modules ki jagah nakli (taaki test offline chale) ----------
class FakeAI:
    def __init__(self):
        self.history = ["old question", "old answer"]
        self.next_reply = "ok"
        self.calls = []

    def ask_ai(self, prompt):
        self.calls.append(prompt)
        self.history.append(prompt)            # asli ask_ai bhi history badhata hai
        self.history.append(self.next_reply)
        return self.next_reply


@pytest.fixture()
def env(monkeypatch, tmp_path):
    fake = FakeAI()
    spoken, streamed, opened = [], [], []

    el = types.ModuleType("client_elevenlabs")
    el.speak = lambda t: spoken.append(t)
    el.speak_stream = lambda gen: streamed.append(list(gen))
    el.get_audio_bytes = lambda t: (b"audio", "wav")
    el.synthesize_stream = lambda gen: ((s, b"a", "wav") for s in gen)

    ds = types.ModuleType("client_deepseek")
    ds.conversation_history = fake.history
    ds.ask_ai = fake.ask_ai
    ds.ask_ai_stream = lambda p: iter([f"stream:{p}"])
    ds.generate_content = lambda t: "content"
    ds.reset_conversation = lambda: fake.history.clear()

    sr = types.ModuleType("speech_recognition")
    sr.Recognizer = object

    for name, mod in {"client_elevenlabs": el, "client_deepseek": ds, "speech_recognition": sr}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("commands", "jervish", "intent_router"):
        sys.modules.pop(name, None)

    import commands
    router = types.SimpleNamespace(calls=[], reply=None)

    def fake_to_command(text, usages):
        router.calls.append((text, usages))
        return router.reply

    monkeypatch.setattr(commands.intent_router, "to_command", fake_to_command)   # AI router (network nahi)
    monkeypatch.setattr(commands, "_youtube_first_video", lambda q: None)        # YouTube scraping (network nahi)
    monkeypatch.setattr(commands.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(commands, "GENERATED_SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(commands, "SCREENSHOT_DIR", tmp_path / "shots")
    return types.SimpleNamespace(commands=commands, ai=fake, spoken=spoken, streamed=streamed,
                                 opened=opened, tmp=tmp_path, monkeypatch=monkeypatch, router=router)


# ---------- Commands ----------
def test_open_site(env):
    assert env.commands.get_text_reply("open youtube") == "Opening Youtube."
    assert env.opened == ["https://www.youtube.com"]


def test_screenshot_and_open_is_not_dead_code(env):
    """'take a screenshot and open' pehle kabhi nahi chalta tha (dead branch)."""
    c = env.commands
    shots = []
    fake_pg = types.SimpleNamespace(screenshot=lambda: types.SimpleNamespace(save=lambda p: shots.append(p)))
    env.monkeypatch.setattr(c, "pyautogui", fake_pg, raising=False)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(c, "_open_file", lambda p: None)
    for phrase in ("take a screenshot", "Take screenshot and open", "take screenshort"):
        shots.clear()
        reply = c.get_text_reply(phrase)
        assert reply.startswith("Screenshot taken") and len(shots) == 1


def test_search_is_url_encoded(env):
    env.commands.get_text_reply("search for cats & dogs #1")
    assert env.opened == ["https://www.google.com/search?q=cats+%26+dogs+%231"]


def test_play_known_song_and_multiword(env):
    env.commands.get_text_reply("play kalank song")
    assert "Grr0FlC8SQA" in env.opened[0]
    assert env.commands.get_text_reply("Play Wolf") .startswith("Playing wolf")


def test_play_alone_does_not_crash(env):
    assert env.commands.get_text_reply("play") == "Which song should I play?"


def test_play_unknown_song_falls_back_to_youtube(env):
    reply = env.commands.get_text_reply("play some random thing xyz")
    assert "playing it from YouTube" in reply
    assert env.opened == ["https://www.youtube.com/results?search_query=some+random+thing+xyz"]


def test_open_website_rejects_unsafe_urls(env):
    env.ai.next_reply = "javascript:alert(1)"
    assert "couldn't find a safe" in env.commands.get_text_reply("open website evil")
    for bad in ("file:///C:/Windows/System32/cmd.exe", "ftp://example.com/x", "data:text/html,<script>1</script>"):
        env.ai.next_reply = bad
        assert "couldn't find a safe" in env.commands.get_text_reply("open website evil")
    assert env.opened == []


def test_open_website_accepts_https_in_markdown(env):
    env.ai.next_reply = "`https://example.com/page`."
    env.commands.get_text_reply("open website example")
    assert env.opened == ["https://example.com/page"]
    env.ai.next_reply = "example.org"
    env.commands.get_text_reply("open website example")
    assert env.opened[-1] == "https://example.org"


def test_one_shot_calls_do_not_pollute_memory(env):
    env.ai.next_reply = "https://example.com"
    env.commands.get_text_reply("open website example")
    assert env.ai.history == ["old question", "old answer"]


def test_website_builder_strips_markdown_fences(env):
    env.ai.next_reply = "```html\n<!DOCTYPE html><html><body>hi</body></html>\n```"
    reply = env.commands.get_text_reply("website about my cat/dog: friends")
    saved = list((env.tmp / "sites").glob("*.html"))
    assert len(saved) == 1 and "/" not in saved[0].name
    assert saved[0].read_text(encoding="utf-8") == "<!DOCTYPE html><html><body>hi</body></html>"
    assert saved[0].name in reply and env.opened[0].startswith("file://")


def test_forget_clears_memory(env):
    env.commands.get_text_reply("please forget everything")
    assert env.ai.history == []


def test_windows_only_command_message_on_other_os(env):
    c = env.commands
    env.monkeypatch.setattr(c, "IS_WINDOWS", False)
    assert "only works on Windows" in c.get_text_reply("write about cats")


def test_automation_missing_message(env):
    c = env.commands
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", False)
    assert "pip install pyautogui" in c.get_text_reply("turn up volume")


def test_volume_down_exists(env):
    c = env.commands
    presses = []
    env.monkeypatch.setattr(c, "pyautogui", types.SimpleNamespace(press=lambda *a, **k: presses.append(a[0])), raising=False)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    c.get_text_reply("turn down volume")
    c.get_text_reply("turn up volume")
    assert presses == ["volumedown", "volumeup"]


# ---------- Error isolation: ek command toote to baaki chalte rahein ----------
def test_one_broken_command_does_not_break_others(env):
    c = env.commands

    @c.command("broken", r"^\s*break it\b")
    def broken(m):
        raise RuntimeError("boom")

    c.COMMANDS.insert(0, c.COMMANDS.pop())   # sabse upar rakho
    reply = c.get_text_reply("break it")
    assert "broken command ran into a problem" in reply
    assert c.get_text_reply("open google") == "Opening Google."


# ---------- CLI / web entry points ----------
def test_chat_goes_to_ai_stream(env):
    env.commands.processCommand("what is the capital of France")
    assert env.streamed == [["stream:what is the capital of France"]]
    assert env.commands.get_text_reply("hello there", ai_fallback=False) is None


def test_say_progress_in_cli_and_web_stream(env):
    """Handler beech me say() se bol sakta hai: CLI me bolta hai, web stream me turant yield hota hai."""
    c = env.commands

    @c.command("slow", r"^\s*do slow thing\b")
    def slow(m):
        c.say("thinking")
        return "finished"

    c.COMMANDS.insert(0, c.COMMANDS.pop())
    c.processCommand("do slow thing")
    assert env.spoken == ["thinking", "finished"]
    assert list(c.get_reply_stream("do slow thing")) == ["thinking", "finished"]
    assert c.get_text_reply("do slow thing") == "finished"      # non-stream: bas final reply
    assert c._progress.callback is None
    c.say("no callback set, kuch nahi hona chahiye")


def test_stream_still_yields_ack_first(env):
    c = env.commands
    cmd, _ = c.find_command("open camera and capture image")
    env.monkeypatch.setattr(c, "IS_WINDOWS", True)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(cmd, "handler", lambda m: "Image captured.")   # ASLI camera/pyautogui kabhi nahi chalna chahiye
    chunks = list(c.get_reply_stream("open camera and capture image"))
    assert chunks == ["Opening camera to capture an image.", "Image captured."]


# ---------- Notepad: focus-safe automation ----------
class FakeWin:
    def __init__(self, desk, title):
        self.desk, self.title, self.visible, self.isMinimized = desk, title, True, False

    def activate(self):
        self.desk.activate_calls += 1
        if self.desk.never or self is self.desk.chrome:
            return
        if self.desk.activate_calls >= self.desk.works_after:
            self.desk.active = self

    def restore(self):
        pass


class FakeDesktop:
    def __init__(self):
        self.events, self.activate_calls, self.works_after, self.never = [], 0, 3, False
        self.chrome = FakeWin(self, "Notepad++ download - Google Chrome")   # naam me 'Notepad' hai, par ye Notepad nahi
        self.notepad = FakeWin(self, "Untitled - Notepad")
        self.active = self.chrome                      # shuru me Chrome front me hai (bug wali halat)
        self.on_type = None

    def active_title(self):
        return self.active.title


@pytest.fixture()
def notepad(env):
    c, desk = env.commands, FakeDesktop()
    gw = types.SimpleNamespace(
        getWindowsWithTitle=lambda t: [w for w in (desk.chrome, desk.notepad) if t.lower() in w.title.lower()],
        getActiveWindow=lambda: desk.active,
    )
    pg = types.SimpleNamespace(
        press=lambda k, **kw: desk.events.append(("press", k, desk.active_title())),
        hotkey=lambda *keys: desk.events.append(("hotkey", keys, desk.active_title())),
        typewrite=lambda text, interval=0: (desk.events.append(("type", text, desk.active_title())),
                                            desk.on_type and desk.on_type()),
    )
    env.monkeypatch.setattr(c, "gw", gw, raising=False)
    env.monkeypatch.setattr(c, "pyautogui", pg, raising=False)
    env.monkeypatch.setattr(c, "pyperclip", types.SimpleNamespace(copy=lambda t: desk.events.append(("copy", t, desk.active_title()))), raising=False)
    env.monkeypatch.setattr(c, "NOTEPAD_AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(c, "IS_WINDOWS", True)
    env.monkeypatch.setattr(c.subprocess, "Popen", lambda *a, **k: desk.events.append(("popen", a[0], "")))
    env.monkeypatch.setattr(c.time, "sleep", lambda s: None)
    desk.content = "Hello world."
    desk.on_generate = None

    def fake_generate(topic):
        desk.events.append(("generate", topic, desk.active_title()))
        if desk.on_generate:
            desk.on_generate()
        return desk.content

    env.monkeypatch.setattr(c, "generate_content", fake_generate)
    return types.SimpleNamespace(c=c, desk=desk, run=lambda: c.run_command(*c.find_command("write about cats"),
                                 say_fn=lambda t: desk.events.append(("say", t, desk.active_title()))))


def _kinds(desk):
    return [e[0] for e in desk.events]


def test_notepad_ctrl_n_only_after_notepad_is_in_front(notepad):
    """Bug: Chrome front me tha, to Ctrl+N se Chrome me naya tab khula. Ab Notepad front me aane ke BAAD hi."""
    n, desk = notepad, notepad.desk
    reply = n.run()
    assert reply.startswith("Here's what I wrote about cats")
    for kind, detail, active in desk.events:
        if kind in ("hotkey", "type"):
            assert active == "Untitled - Notepad", (kind, detail, active)
    ctrl_n = [e for e in desk.events if e[0] == "hotkey" and e[1] == ("ctrl", "n")]
    assert len(ctrl_n) == 1
    assert desk.activate_calls >= 3                     # Windows ne pehli koshishon me focus nahi diya tha


def test_notepad_order_open_ctrl_n_say_think_type_save(notepad):
    n, desk = notepad, notepad.desk
    n.run()
    order = [(e[0], e[1]) for e in desk.events if e[0] in ("popen", "hotkey", "say", "generate", "type")]
    kinds = [k for k, _ in order]
    assert kinds[0] == "popen"
    assert order[1] == ("hotkey", ("ctrl", "n"))
    assert order[2] == ("say", n.c.NOTEPAD_THINKING_LINE)       # Notepad khulne ke BAAD, likhne se pehle
    assert kinds[3] == "generate" and kinds[4] == "type"
    assert ("hotkey", ("ctrl", "s")) in order
    assert n.c.NOTEPAD_THINKING_LINE.startswith("Please wait")


def test_notepad_never_gets_focus_types_nothing(notepad):
    n, desk = notepad, notepad.desk
    desk.never = True
    reply = n.run()
    assert "couldn't bring it to the front" in reply
    assert not any(k in ("hotkey", "type", "copy", "say", "generate") for k in _kinds(desk))


def test_notepad_focus_stolen_while_ai_thinks_is_regained(notepad):
    n, desk = notepad, notepad.desk
    desk.content = "x" * 300
    desk.on_generate = lambda: setattr(desk, "active", desk.chrome)      # user ne Chrome click kar diya
    reply = n.run()
    assert reply.startswith("Here's what I wrote")
    typed = [e for e in desk.events if e[0] == "type" and e[1] == "x" * 120 or (e[0] == "type" and e[1] == "x" * 60)]
    assert len(typed) == 3
    assert all(e[2] == "Untitled - Notepad" for e in desk.events if e[0] == "type")


def test_notepad_focus_stolen_and_not_regained_types_nothing(notepad):
    n, desk = notepad, notepad.desk

    def steal():
        desk.active = desk.chrome
        desk.never = True

    desk.on_generate = steal
    reply = n.run()
    assert "no longer in front" in reply
    assert "type" not in _kinds(desk)


def test_notepad_focus_lost_mid_typing_stops(notepad):
    n, desk = notepad, notepad.desk
    desk.content = "y" * 400

    def steal_after_first_chunk():
        desk.active = desk.chrome
        desk.never = True

    desk.on_type = steal_after_first_chunk
    reply = n.run()
    assert "lost focus while I was writing" in reply
    assert _kinds(desk).count("type") == 1


def test_notepad_hindi_content_is_pasted_only_into_notepad(notepad):
    n, desk = notepad, notepad.desk
    desk.content = "नमस्ते दुनिया"
    reply = n.run()
    assert reply.startswith("Here's what I wrote")
    assert [e for e in desk.events if e[0] == "copy"][0][2] == "Untitled - Notepad"
    assert [e for e in desk.events if e[0] == "hotkey" and e[1] == ("ctrl", "v")][0][2] == "Untitled - Notepad"


def test_chrome_tab_with_notepad_in_title_is_not_mistaken_for_notepad(env):
    c = env.commands
    assert c._is_notepad_title("Untitled - Notepad") and c._is_notepad_title("*notes.txt - Notepad")
    assert not c._is_notepad_title("Notepad++ download - Google Chrome")


# ---------- Server security ----------
@pytest.fixture()
def client(env):
    import jervish
    jervish.app.config["TESTING"] = True
    return jervish.app.test_client()


def test_foreign_origin_is_blocked(client):
    r = client.post("/api/command", json={"text": "open google"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_own_origin_is_allowed(client, env):
    r = client.post("/api/command", json={"text": "open google"}, headers={"Origin": "http://127.0.0.1:5000"})
    assert r.status_code == 200 and r.get_json()["reply"] == "Opening Google."


def test_cors_header_not_sent_to_foreign_site(client):
    r = client.options("/api/command", headers={"Origin": "https://evil.example",
                                                "Access-Control-Request-Method": "POST"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_text_plain_post_is_rejected(client):
    r = client.post("/api/command", data='{"text":"open google"}', content_type="text/plain")
    assert r.status_code == 400


def test_python_and_env_files_are_not_served(client):
    for path in ("/jervish.py", "/commands.py", "/.env", "/requirements.txt"):
        assert client.get(path).status_code == 404


def test_debug_is_off():
    src = (HERE / "jervish.py").read_text(encoding="utf-8")
    assert "debug=True" not in src


# ---------- Direction 1: naye commands ----------
def test_time_and_date(env):
    c = env.commands
    assert c.get_text_reply("what time is it").startswith("It's ")
    assert c.get_text_reply("What's the time?").startswith("It's ")
    assert c.get_text_reply("what is today's date").startswith("Today is ")
    assert c.get_text_reply("what day is it").startswith("Today is ")


def test_time_pattern_does_not_hijack_normal_questions(env):
    c = env.commands
    for q in ("what is the time complexity of quicksort", "what is the date of independence day in india"):
        assert c.find_command(q) is None


def test_weather_with_and_without_city(env):
    c, asked = env.commands, []

    def fake_fetch(city):
        asked.append(city)
        return {"place": city, "temp": 31, "humidity": 62, "desc": "partly cloudy"}

    env.monkeypatch.setattr(c, "_fetch_weather", fake_fetch)
    env.monkeypatch.delenv("DEFAULT_CITY", raising=False)
    reply = c.get_text_reply("what's the weather in New York?")
    assert asked == ["New York"] and "31 degrees" in reply and "partly cloudy" in reply and "62 percent" in reply
    c.get_text_reply("weather")
    assert asked[-1] == "Kolkata"
    env.monkeypatch.setenv("DEFAULT_CITY", "Durgapur")
    c.get_text_reply("temperature")
    assert asked[-1] == "Durgapur"


def test_weather_errors_are_friendly(env):
    c = env.commands
    env.monkeypatch.setattr(c, "_fetch_weather", lambda city: None)
    assert "couldn't find a place called Atlantis" in c.get_text_reply("weather in Atlantis")

    def boom(city):
        raise c.requests.exceptions.ConnectionError("no internet")

    env.monkeypatch.setattr(c, "_fetch_weather", boom)
    assert "check your internet" in c.get_text_reply("weather in Delhi")


def test_fetch_weather_parses_open_meteo(env):
    c = env.commands

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_get(url, params=None, **kw):
        if "geocoding" in url:
            return Resp({"results": [{"name": "Durgapur", "latitude": 23.5, "longitude": 87.3}]})
        return Resp({"current": {"temperature_2m": 28.6, "relative_humidity_2m": 70, "weather_code": 63}})

    env.monkeypatch.setattr(c.requests, "get", fake_get)
    assert c._fetch_weather("Durgapur") == {"place": "Durgapur", "temp": 29, "humidity": 70, "desc": "rain"}


def test_youtube_play_opens_first_video_or_search(env):
    c = env.commands
    env.monkeypatch.setattr(c, "_youtube_first_video", lambda q: "dQw4w9WgXcQ")
    assert c.get_text_reply("play arijit singh songs on youtube") == "Playing arijit singh songs on YouTube."
    assert env.opened[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    env.monkeypatch.setattr(c, "_youtube_first_video", lambda q: None)
    c.get_text_reply("play lofi beats on youtube")
    assert env.opened[-1] == "https://www.youtube.com/results?search_query=lofi+beats"
    c.get_text_reply("youtube search python tutorial")
    assert env.opened[-1] == "https://www.youtube.com/results?search_query=python+tutorial"


def test_library_song_still_wins_over_youtube(env):
    env.commands.get_text_reply("play kalank")
    assert "Grr0FlC8SQA" in env.opened[0]


# ---------- Direction 1: smart (AI) routing ----------
def test_casual_hinglish_is_routed_to_write_command(env):
    c = env.commands
    env.monkeypatch.setattr(c, "IS_WINDOWS", True)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(c, "write_in_notepad", lambda topic: f"done {topic}")
    env.router.reply = "write about Shah Rukh Khan"
    reply = c.get_text_reply("yaar notepad khol ke sarukhan ke liye kuch likh de")
    assert reply == "done Shah Rukh Khan"
    text, usages = env.router.calls[0]
    assert text == "yaar notepad khol ke sarukhan ke liye kuch likh de"
    assert any(u.startswith("write about <topic>") for u in usages)


def test_direct_commands_never_call_the_router(env):
    env.commands.get_text_reply("open youtube")
    env.commands.get_text_reply("what time is it")
    assert env.router.calls == []


def test_plain_chat_never_calls_the_router(env):
    for q in ("what is the capital of France", "tum kaise ho", "explain how a CPU works"):
        assert env.commands.get_text_reply(q, ai_fallback=False) is None
    assert env.router.calls == []


def test_router_says_chat_or_returns_garbage(env):
    c = env.commands
    for reply in (None, "rm -rf /", "open https://evil.example", "format c:"):
        env.router.reply = reply
        assert c.get_text_reply("youtube pe kuch chala do please", ai_fallback=False) is None
    assert len(env.router.calls) == 4


def test_router_result_in_cli_and_stream(env):
    c = env.commands
    env.router.reply = "open youtube"
    c.processCommand("youtube chala do")
    assert env.opened[-1] == "https://www.youtube.com" and env.spoken[-1] == "Opening Youtube."
    assert list(c.get_reply_stream("youtube chala do")) == ["Opening Youtube."]


def test_every_command_has_usage_for_router(env):
    c = env.commands
    u = c.usages()
    assert "turn up volume" in u and "take a screenshot" in u and "play <song name>" in u
    assert any(x.startswith("weather in <city>") for x in u)
    assert any(x.startswith("open <youtube|") for x in u)
    assert all(cmd.usage for cmd in c.COMMANDS), [cmd.name for cmd in c.COMMANDS if not cmd.usage]


def test_explain_describes_routing(env):
    c = env.commands
    assert "direct match" in c.explain("open google")
    assert "normal chat" in c.explain("what is photosynthesis")
    env.router.reply = "open youtube"
    assert "smart -> 'open youtube'" in c.explain("youtube chala do")


# ---------- intent_router.py khud ----------
def test_looks_like_command_gate():
    import intent_router as ir
    assert ir.looks_like_command("yaar notepad khol ke sarukhan ke liye kuch likh de")
    assert ir.looks_like_command("mausam kaisa hai")
    assert ir.looks_like_command("आवाज़ बढ़ाओ और यूट्यूब खोलो")
    assert not ir.looks_like_command("what is photosynthesis")
    assert not ir.looks_like_command("tum kaise ho")
    assert not ir.looks_like_command("please " + "open " * 30)         # bahut lambi = baatcheet
    assert not ir.looks_like_command("")


def _fake_post(monkeypatch, content=None, exc=None, sent=None):
    import intent_router as ir

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": content}}]}

    def post(url, headers=None, json=None, timeout=None):
        if sent is not None:
            sent.append(json)
        if exc:
            raise exc
        return Resp()

    monkeypatch.setattr(ir.requests, "post", post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return ir


def test_router_sends_usages_and_cleans_reply(monkeypatch):
    sent = []
    ir = _fake_post(monkeypatch, content='  "write about Shah Rukh Khan."\nextra line', sent=sent)
    out = ir.to_command("sarukhan ke liye likh de", ["write about <topic>", "open youtube"])
    assert out == "write about Shah Rukh Khan"
    body = sent[0]
    assert body["temperature"] == 0 and body["messages"][1]["content"] == "sarukhan ke liye likh de"
    assert "- write about <topic>" in body["messages"][0]["content"] and "- open youtube" in body["messages"][0]["content"]


def test_router_chat_error_and_missing_key_return_none(monkeypatch):
    ir = _fake_post(monkeypatch, content="CHAT")
    assert ir.to_command("what is love", ["open youtube"]) is None
    ir = _fake_post(monkeypatch, exc=ConnectionError("offline"))
    assert ir.to_command("youtube chala do", ["open youtube"]) is None
    ir = _fake_post(monkeypatch, content="open youtube")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert ir.to_command("youtube chala do", ["open youtube"]) is None


if __name__ == "__main__":
    # VS Code ke "Run Python File" button se bhi chale: python test_commands.py
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


# ============================================================
# Command ke baad Nova window par wapas aana
# ============================================================
class NovaDesk:
    """Nakli desktop: Chrome (YouTube) front me, Nova ki alag window peeche."""
    def __init__(self, nova_works_after=1):
        self.calls, self.works_after = 0, nova_works_after
        self.chrome = types.SimpleNamespace(title="YouTube - Google Chrome", visible=True, isMinimized=False)
        self.nova = types.SimpleNamespace(title="Nova - Voice assistant", visible=True, isMinimized=False,
                                          restore=lambda: None, activate=self._activate_nova)
        self.active = self.chrome

    def _activate_nova(self):
        self.calls += 1
        if self.calls >= self.works_after:
            self.active = self.nova


@pytest.fixture()
def nova_desk(env):
    c, desk = env.commands, NovaDesk()
    gw = types.SimpleNamespace(
        getWindowsWithTitle=lambda t: [w for w in (desk.chrome, desk.nova) if t.lower() in w.title.lower()],
        getActiveWindow=lambda: desk.active,
    )
    env.monkeypatch.setattr(c, "gw", gw, raising=False)
    env.monkeypatch.setattr(c, "pyautogui", types.SimpleNamespace(press=lambda k, **kw: None), raising=False)
    env.monkeypatch.setattr(c, "AUTOMATION_AVAILABLE", True)
    env.monkeypatch.setattr(c, "RETURN_TO_NOVA", True)
    env.monkeypatch.setattr(c.time, "sleep", lambda s: None)
    env.monkeypatch.setattr(c.webbrowser, "open", lambda url: True)
    return types.SimpleNamespace(c=c, desk=desk, mp=env.monkeypatch)


def _run_and_join(c, text):
    reply = c.run_command(*c.find_command(text))
    for t in threading_enumerate_return(c):
        t.join(2)
    return reply


def threading_enumerate_return(c):
    import threading
    return [t for t in threading.enumerate() if t.name == "return-to-nova"]


def _add_returning_command(c):
    @c.command("demo window", r"^\s*demo window\b", return_delay=0.0)
    def demo(m):
        return "ok"
    c.COMMANDS.insert(0, c.COMMANDS.pop())


def test_return_to_nova_after_command_with_return_delay(nova_desk):
    n = nova_desk
    _add_returning_command(n.c)
    _run_and_join(n.c, "demo window")
    assert n.desk.active is n.desk.nova


def test_return_to_nova_retries_until_windows_allows(nova_desk):
    n = nova_desk
    n.desk.works_after = 3                      # Windows pehli 2 koshishon me focus nahi deta
    assert n.c.return_to_nova() is True
    assert n.desk.calls == 3


def test_return_to_nova_can_be_disabled(nova_desk):
    n = nova_desk
    n.mp.setattr(n.c, "RETURN_TO_NOVA", False)
    _add_returning_command(n.c)
    _run_and_join(n.c, "demo window")
    assert n.desk.active is n.desk.chrome and n.desk.calls == 0


def test_return_to_nova_skips_when_no_nova_window(nova_desk):
    n = nova_desk
    n.mp.setattr(n.c, "gw", types.SimpleNamespace(getWindowsWithTitle=lambda t: [], getActiveWindow=lambda: n.desk.chrome),
                 raising=False)
    assert n.c.return_to_nova() is False        # crash nahi, bas False


def test_commands_without_new_window_do_not_return(nova_desk):
    n = nova_desk
    _run_and_join(n.c, "what is the date")
    assert n.desk.calls == 0


def test_new_command_cancels_pending_return(nova_desk):
    n = nova_desk
    n.mp.setattr(n.c.time, "sleep", lambda s: None)
    n.c.cancel_pending_return()
    n.c.run_command(*n.c.find_command("what is the date"))     # naya command -> pehle wala token invalid
    # token purana ho chuka: worker chalay to bhi wapas nahi jaata
    import threading
    gen_before = n.c._return_generation
    t = n.c.schedule_return_to_nova(0)
    n.c.cancel_pending_return()
    t.join(2)
    assert n.c._return_generation > gen_before


def test_write_note_and_camera_and_pages_have_return_delay(env):
    c = env.commands
    by_name = {cmd.name: cmd for cmd in c.COMMANDS}
    for name in ("write note", "open camera"):       # sirf jab kuch save ho (note / photo)
        assert by_name[name].return_delay is not None, name
    for name in ("time", "date", "weather", "forget", "volume up", "screenshot", "open website", "open site",
                 "search", "youtube play", "website builder", "open app", "type text", "press key", "new tab",
                 "minimize", "scroll"):
        assert by_name[name].return_delay is None, name


def test_failed_command_does_not_schedule_return(nova_desk):
    n = nova_desk
    def boom(url): raise RuntimeError("x")
    n.mp.setattr(n.c.webbrowser, "open", boom)
    n.c.COMMANDS.insert(0, n.c.Command("demo", n.c.re.compile("^demo"), lambda m: boom(1), None, False, False, None, 0.0))
    reply = _run_and_join(n.c, "demo")
    assert "problem" in reply and n.desk.calls == 0


def test_open_nova_window_uses_app_mode(env):
    import jervish
    launched = []
    env.monkeypatch.setattr(jervish, "_find_browser_exe", lambda: r"C:\chrome.exe")
    import subprocess
    env.monkeypatch.setattr(subprocess, "Popen", lambda args, **k: launched.append(args))
    env.monkeypatch.delenv("NOVA_APP_WINDOW", raising=False)
    jervish.open_nova_window()
    assert launched == [[r"C:\chrome.exe", "--app=http://127.0.0.1:5000"]]


def test_open_nova_window_env_off_or_no_browser_falls_back_to_tab(env):
    import jervish, webbrowser
    opened = []
    env.monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))
    env.monkeypatch.setenv("NOVA_APP_WINDOW", "0")
    jervish.open_nova_window()
    env.monkeypatch.delenv("NOVA_APP_WINDOW")
    env.monkeypatch.setattr(jervish, "_find_browser_exe", lambda: None)
    jervish.open_nova_window()
    assert opened == ["http://127.0.0.1:5000"] * 2


# ============================================================
# Camera fix + computer control commands
# ============================================================
class CamDesk:
    """Nakli camera: window kuch der baad dikhti hai, pehle background me hoti hai."""
    def __init__(self, appears_after=2, focus_works=True, saves_photo=True):
        self.polls, self.appears_after, self.focus_works, self.saves_photo = 0, appears_after, focus_works, saves_photo
        self.events, self.active_title, self.photos = [], "Nova - Voice assistant", 0
        self.cam = types.SimpleNamespace(title="Camera", visible=True, isMinimized=False,
                                         restore=lambda: None, activate=self._activate)

    def _activate(self):
        if self.focus_works:
            self.active_title = "Camera"

    def windows(self, title):
        self.polls += 1
        return [self.cam] if (self.polls > self.appears_after and title.lower() in "camera") else []


def _cam_env(env, **kw):
    c, desk = env.commands, CamDesk(**kw)
    mp = env.monkeypatch
    mp.setattr(c, "gw", types.SimpleNamespace(getWindowsWithTitle=desk.windows,
               getActiveWindow=lambda: types.SimpleNamespace(title=desk.active_title)), raising=False)

    def press(k, **kw2):
        desk.events.append(("press", k, desk.active_title))
        if k == "space" and desk.active_title == "Camera" and desk.saves_photo:
            desk.photos += 1
    mp.setattr(c, "pyautogui", types.SimpleNamespace(press=press, hotkey=lambda *k: desk.events.append(("hotkey", k, desk.active_title))), raising=False)
    mp.setattr(c, "AUTOMATION_AVAILABLE", True)
    mp.setattr(c, "IS_WINDOWS", True)
    mp.setattr(c, "RETURN_TO_NOVA", False)
    mp.setattr(c.subprocess, "Popen", lambda *a, **k: desk.events.append(("popen", a[0], desk.active_title)))
    mp.setattr(c.time, "sleep", lambda s: desk.events.append(("sleep", s, desk.active_title)))
    mp.setattr(c, "_newest_photo_time", lambda: float(desk.photos))
    return c, desk


def test_camera_waits_for_window_focuses_then_captures(env):
    c, desk = _cam_env(env)
    reply = c.run_command(*c.find_command("open camera and capture image"))
    assert reply.startswith("Image captured")
    space = [e for e in desk.events if e[:2] == ("press", "space")]
    assert len(space) == 1 and space[0][2] == "Camera"          # Space tabhi, jab Camera front me
    assert any(e[0] == "sleep" and e[1] == c.CAMERA_WARMUP for e in desk.events)   # preview warmup


def test_camera_never_presses_space_if_it_cannot_get_focus(env):
    c, desk = _cam_env(env, focus_works=False)
    reply = c.run_command(*c.find_command("take a photo"))
    assert "background" in reply
    assert not [e for e in desk.events if e[:2] == ("press", "space")]


def test_camera_reports_honestly_if_window_never_appears_or_photo_not_saved(env):
    c, desk = _cam_env(env, appears_after=10**6)
    assert "window didn't appear" in c.run_command(*c.find_command("open camera and capture image"))
    c, desk = _cam_env(env, saves_photo=False)
    reply = c.run_command(*c.find_command("open camera and capture image"))
    assert "couldn't confirm" in reply and "captured and saved" not in reply


class CtlDesk:
    def __init__(self, nova_active=True, alt_tab_works=True):
        self.events, self.nova_active, self.alt_tab_works, self.clip = [], nova_active, alt_tab_works, None


@pytest.fixture()
def ctl(env):
    c, d = env.commands, CtlDesk()
    mp = env.monkeypatch

    def hotkey(*k):
        d.events.append(("hotkey", k, "nova" if d.nova_active else "app"))
        if k == ("alt", "tab") and d.alt_tab_works:
            d.nova_active = not d.nova_active
    mp.setattr(c, "gw", types.SimpleNamespace(getWindowsWithTitle=lambda t: [],
               getActiveWindow=lambda: types.SimpleNamespace(title="Nova - Voice assistant" if d.nova_active else "Untitled - Notepad")), raising=False)
    mp.setattr(c, "pyautogui", types.SimpleNamespace(
        press=lambda k, **kw: d.events.append(("press", k, "nova" if d.nova_active else "app")),
        hotkey=hotkey, scroll=lambda n: d.events.append(("scroll", n, "nova" if d.nova_active else "app"))), raising=False)
    mp.setattr(c, "pyperclip", types.SimpleNamespace(copy=lambda t: setattr(d, "clip", t)), raising=False)
    mp.setattr(c, "AUTOMATION_AVAILABLE", True)
    mp.setattr(c, "IS_WINDOWS", True)
    mp.setattr(c, "RETURN_TO_NOVA", False)
    mp.setattr(c.time, "sleep", lambda s: None)
    ran = []
    mp.setattr(c.subprocess, "Popen", lambda args, **k: ran.append(list(args)))
    mp.setattr(c.subprocess, "run", lambda args, **k: (ran.append(list(args)), types.SimpleNamespace(returncode=0))[1])
    return types.SimpleNamespace(c=c, d=d, ran=ran, run=lambda text: c.run_command(*c.find_command(text)))


def test_open_and_close_apps_use_fixed_table(ctl):
    assert ctl.run("open calculator") == "Opening calculator."
    assert ctl.ran[-1] == ["cmd", "/c", "start", "", "calc.exe"]
    assert ctl.run("close notepad") == "Closing notepad."
    assert ctl.ran[-1] == ["taskkill", "/IM", "notepad.exe"]            # /F nahi: save dialog aane do
    assert ctl.run("open vs code") == "Opening vs code."


def test_app_commands_cannot_be_used_for_injection(ctl):
    for evil in ("open calculator & del *", "open calc.exe && whoami", "close notepad; shutdown /s", "open cmd /c calc"):
        found = ctl.c.find_command(evil)
        assert found is None or found[0].name not in ("open app", "close app"), evil


def test_type_goes_to_previous_app_not_nova(ctl):
    reply = ctl.run("type hello world")
    assert reply == "" and ctl.d.clip == "hello world"
    assert ctl.d.events[0] == ("hotkey", ("alt", "tab"), "nova")         # pehle pichli app par
    assert ctl.d.events[-1] == ("hotkey", ("ctrl", "v"), "app")          # paste app me hua, Nova me nahi


def test_control_refuses_to_type_into_nova_if_cannot_switch(ctl):
    ctl.d.alt_tab_works = False
    assert "couldn't switch" in ctl.run("type hello")
    assert not [e for e in ctl.d.events if e[1] == ("ctrl", "v")]
    assert "couldn't switch" in ctl.run("close tab")                     # Nova ka apna tab band nahi hona chahiye
    assert not [e for e in ctl.d.events if e[1] == ("ctrl", "w")]


def test_hotkey_and_press_commands(ctl):
    ctl.d.nova_active = False
    ctl.run("press enter"); ctl.run("new tab"); ctl.run("select all"); ctl.run("scroll down"); ctl.run("refresh")
    kinds = [(e[0], e[1]) for e in ctl.d.events]
    assert ("press", "enter") in kinds and ("hotkey", ("ctrl", "t")) in kinds
    assert ("hotkey", ("ctrl", "a")) in kinds and ("scroll", -600) in kinds and ("press", "f5") in kinds


def test_media_and_lock_commands(ctl):
    ctl.run("pause music"); ctl.run("next song"); ctl.run("mute"); ctl.run("previous song")
    assert [e[1] for e in ctl.d.events] == ["playpause", "nexttrack", "volumemute", "prevtrack"]
    ctl.run("lock the screen")
    assert ctl.ran[-1][0] == "rundll32.exe"


def test_go_to_nova_command(nova_desk):
    n = nova_desk
    n.mp.setattr(n.c, "IS_WINDOWS", True)
    assert n.c.run_command(*n.c.find_command("go to nova screen")) == "I'm here."
    assert n.desk.active is n.desk.nova
    for phrase in ("show nova", "come back", "nova window"):
        assert n.c.find_command(phrase)[0].name == "go to nova", phrase


def test_new_commands_do_not_steal_normal_chat(env):
    c = env.commands
    for chat in ("what type of model are you", "type of food", "how do I copy a file in python", "tell me about paste",
                 "what is a good song to play", "who is nova"):
        found = c.find_command(chat)
        assert found is None or found[0].name in ("play",), (chat, found and found[0].name)


def test_non_windows_gets_polite_message_for_new_commands(env):
    c = env.commands
    env.monkeypatch.setattr(c, "IS_WINDOWS", False)
    assert "only works on Windows" in c.run_command(*c.find_command("open calculator"))


def test_api_focus_endpoint_calls_return_to_nova_and_blocks_foreign_origin(env):
    import jervish
    env.monkeypatch.setattr(jervish, "return_to_nova", lambda: True)
    client = jervish.app.test_client()
    ok = client.post("/api/focus", headers={"Origin": "http://127.0.0.1:5000"})
    assert ok.status_code == 200 and ok.get_json() == {"focused": True}
    bad = client.post("/api/focus", headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403


# ---------- Naya app background me khule to front me aana ----------
class AppDesk:
    def __init__(self, front_works_after=2):
        self.calc = types.SimpleNamespace(title="Calculator", visible=True, isMinimized=False, _hWnd=42,
                                          restore=lambda: None, activate=self._act)
        self.old = types.SimpleNamespace(title="Old Notepad", visible=True, isMinimized=False, _hWnd=1,
                                         restore=lambda: None, activate=lambda: None)
        self.nova = types.SimpleNamespace(title="Nova - Voice assistant", visible=True, isMinimized=False, _hWnd=7,
                                          restore=lambda: None, activate=lambda: None)
        self.active, self.launched, self.n, self.works_after = self.nova, False, 0, front_works_after

    def _act(self):
        self.n += 1
        if self.n >= self.works_after:
            self.active = self.calc

    def all(self):
        return [self.old, self.nova] + ([self.calc] if self.launched else [])


def test_open_app_brings_new_window_to_front(env):
    c, d, mp = env.commands, AppDesk(), env.monkeypatch
    mp.setattr(c, "gw", types.SimpleNamespace(getAllWindows=d.all, getActiveWindow=lambda: d.active), raising=False)
    mp.setattr(c, "pyautogui", types.SimpleNamespace(press=lambda k, **kw: None), raising=False)
    mp.setattr(c, "AUTOMATION_AVAILABLE", True); mp.setattr(c, "IS_WINDOWS", True)
    mp.setattr(c.time, "sleep", lambda s: None)
    mp.setattr(c.subprocess, "Popen", lambda *a, **k: setattr(d, "launched", True))
    c.run_command(*c.find_command("open calculator"))
    for t in [t for t in __import__("threading").enumerate() if t.name == "front-window"]:
        t.join(3)
    assert d.active is d.calc and d.n >= 2          # Windows ne pehli baar focus nahi diya, dobara koshish hui
    assert d.nova.title == "Nova - Voice assistant"


def test_front_new_app_never_picks_nova_window(env):
    c, d, mp = env.commands, AppDesk(), env.monkeypatch
    mp.setattr(c, "gw", types.SimpleNamespace(getAllWindows=lambda: [d.nova, d.old], getActiveWindow=lambda: d.nova), raising=False)
    mp.setattr(c, "pyautogui", types.SimpleNamespace(press=lambda k, **kw: None), raising=False)
    mp.setattr(c.time, "sleep", lambda s: None)
    assert c.front_new_app({1, 7}, "Calculator") is False      # nayi window nahi mili -> Nova ko nahi uthata


def test_open_url_fronts_browser_on_windows_only(env):
    c, mp = env.commands, env.monkeypatch
    calls = []
    mp.setattr(c.webbrowser, "open", lambda u: calls.append(u))
    mp.setattr(c, "front_browser", lambda: calls.append("front"))
    mp.setattr(c, "IS_WINDOWS", False)
    c._open_url("http://x")
    assert calls == ["http://x"]
    mp.setattr(c, "IS_WINDOWS", True); mp.setattr(c, "AUTOMATION_AVAILABLE", True)
    t = c._open_url("http://y"); 
    for th in [th for th in __import__("threading").enumerate() if th.name == "front-window"]:
        th.join(3)
    assert calls == ["http://x", "http://y", "front"]


def test_singular_and_aliases_and_your_article(ctl):
    """Screenshot bug: 'close setting' / 'open your setting' AI chat me chale gaye the."""
    assert ctl.run("close setting") == "Closing settings."
    assert ctl.ran[-1] == ["taskkill", "/IM", "SystemSettings.exe"]
    assert ctl.run("open your setting") == "Opening settings."
    assert ctl.run("open the calc") == "Opening calculator."
    assert ctl.run("close ms word") == "Closing word."
    assert ctl.run("open google chrome") == "Opening chrome."


def test_router_gate_lets_hinglish_close_and_media_commands_through(env):
    import intent_router
    for text in ("setting ko close karo", "setting ko band karo", "gaana roko", "agla gaana lagao",
                 "chrome minimize kar do", "ye tab band kar do", "screen lock kar do", "calculator chalu kar do"):
        assert intent_router.looks_like_command(text), text
    assert not intent_router.looks_like_command("what is photosynthesis")


class CloseDesk:
    def __init__(self):
        self.closed = []
        mk = lambda t: types.SimpleNamespace(title=t, visible=True, isMinimized=False, close=lambda t=t: self.closed.append(t))
        self.wins = [mk("Settings"), mk("Nova - Voice assistant"), mk("Settings - Google Chrome"), mk("Untitled - Notepad")]


@pytest.fixture()
def closer(ctl):
    d = CloseDesk()
    ctl.c.gw = types.SimpleNamespace(getAllWindows=lambda: d.wins, getActiveWindow=lambda: None)
    ctl.desk = d
    return ctl


def test_close_settings_closes_the_window_not_only_the_process(closer):
    """Screenshot bug: 'Closing settings.' bola par Settings khuli rahi (taskkill UWP window nahi band karta)."""
    assert closer.run("close settings") == "Closing settings."
    assert closer.desk.closed == ["Settings"]            # Chrome ka 'Settings - Google Chrome' tab/window nahi
    assert not any(a[0] == "taskkill" for a in closer.ran)


def test_close_app_never_closes_nova_and_falls_back_honestly(closer):
    closer.desk.wins = [w for w in closer.desk.wins if "Notepad" not in w.title and "Chrome" not in w.title]
    assert "couldn't find" in closer.run("close chrome")       # Nova ya browser process ko taskkill nahi
    assert closer.desk.closed == []
    assert closer.run("close notepad") == "Closing notepad."  # window nahi: taskkill fallback
    assert closer.ran[-1] == ["taskkill", "/IM", "notepad.exe"]


def test_close_this_window_closes_top_non_nova_window(closer):
    closer.desk.wins = closer.desk.wins[1:]                    # Nova upar, phir Chrome, phir Notepad
    for phrase in ("close karo", "close this window", "band karo", "close it"):
        closer.desk.closed.clear()
        assert closer.run(phrase).startswith("Closing"), phrase
        assert closer.desk.closed == ["Settings - Google Chrome"], phrase
    closer.desk.wins = [closer.desk.wins[0]]                   # sirf Nova bachi
    assert "couldn't find" in closer.run("close karo")