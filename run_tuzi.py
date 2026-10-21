import asyncio
import http.server
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
import warnings
import queue
import tempfile
import random

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

import pygame
import edge_tts
from elevenlabs.client import ElevenLabs
from openai import OpenAI

from PyQt5.QtCore import (
    QObject,
    Qt,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineSettings, QWebEngineView, QWebEngineProfile
from PyQt5.QtWidgets import QApplication, QMainWindow

import config
import teach_language

try:
    import ai_tools
except ImportError:
    ai_tools = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = getattr(config, "HTTP_PORT", 8080)
MEMORY_FILE = os.path.join(BASE_DIR, "tuzi_memory.json")

signal.signal(signal.SIGINT, signal.SIG_DFL)
speech_lock = threading.Lock()
input_queue = queue.Queue()

if not getattr(config, "OPENROUTER_API_KEY", "") or config.OPENROUTER_API_KEY == "MASUKKAN_OPENROUTER_API_KEY_ANDA_DISINI":
    print("[ERROR] OPENROUTER_API_KEY belum disetel di config.py! Daftar di openrouter.ai")
    sys.exit(1)

llm_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=config.OPENROUTER_API_KEY,
)

ACTIVE_MODEL = "nousresearch/hermes-3-llama-3.1-70b"


class QuietHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format, *args):
        pass


def start_local_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), QuietHTTPHandler) as httpd:
        httpd.serve_forever()


class AvatarSignalBridge(QObject):
    mouth_signal = pyqtSignal(float)
    subtitle_signal = pyqtSignal(str)
    expression_signal = pyqtSignal(str)
    teach_mode_signal = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

    @pyqtSlot(str)
    def start_ptt(self, lang_code: str):
        teach_language.ptt_recorder.start_recording(lang_code)

    @pyqtSlot()
    def stop_ptt(self):
        teach_language.ptt_recorder.stop_recording()

    @pyqtSlot()
    def exit_teach_ui(self):
        print("\n[Sistem] Tombol Hang-up ditekan. Menutup aplikasi...")
        os._exit(0)


class TransparentAvatarWindow(QMainWindow):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.drag_position = None

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        self.webview = QWebEngineView(self)
        profile = QWebEngineProfile.defaultProfile()
        profile.clearHttpCache()
        profile.setHttpCacheType(QWebEngineProfile.NoCache)
        self.webview.setAttribute(Qt.WA_TranslucentBackground, True)
        self.webview.page().setBackgroundColor(Qt.transparent)
        self.webview.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCentralWidget(self.webview)

        settings = self.webview.page().settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
        self.webview.setZoomFactor(1.0)

        self.bridge.setParent(self)
        self.channel = QWebChannel(self.webview.page())
        self.channel.registerObject("bridge", self.bridge)
        self.webview.page().setWebChannel(self.channel)

        self.bridge.mouth_signal.connect(self.update_mouth_in_web)
        self.bridge.subtitle_signal.connect(self.update_subtitle_in_web)
        self.bridge.expression_signal.connect(self.update_expression_in_web)
        self.bridge.teach_mode_signal.connect(lambda active: teach_language.apply_window_state(self, active))

        self.webview.loadFinished.connect(self.inject_subtitle_system)
        timestamp = int(time.time())
        self.webview.load(QUrl(f"http://127.0.0.1:{PORT}/Assets/viewer/index.html?v={timestamp}"))

        self.resize(teach_language.TEACH_WIN_WIDTH, teach_language.TEACH_WIN_HEIGHT)
        screen_geo = QApplication.primaryScreen().geometry()
        center_x = (screen_geo.width() - teach_language.TEACH_WIN_WIDTH) // 2
        center_y = (screen_geo.height() - teach_language.TEACH_WIN_HEIGHT) // 2
        self.move(center_x, center_y)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = None
        event.accept()

    def closeEvent(self, event):
        os._exit(0)

    def inject_subtitle_system(self):
        js_init = """
        (function() {
            if (document.getElementById('tuzi-subtitle-box')) return;
            let style = document.createElement('style');
            style.innerHTML = `
                #tuzi-subtitle-box {
                    position: fixed;
                    top: 20px;
                    left: 50%;
                    transform: translateX(-50%);
                    width: 88%;
                    max-width: 760px;
                    color: #0D0D0D;
                    font-family: 'Segoe UI', sans-serif;
                    font-size: 19px;
                    font-weight: 600;
                    line-height: 1.5;
                    text-align: center;
                    opacity: 0;
                    pointer-events: none;
                    transition: opacity 0.25s ease;
                    z-index: 999999;
                    box-sizing: border-box;
                    word-wrap: break-word;
                }
                #tuzi-subtitle-box.show { opacity: 1; }
            `;
            document.head.appendChild(style);
            let box = document.createElement('div');
            box.id = 'tuzi-subtitle-box';
            document.body.appendChild(box);

            window.setSubtitle = function(htmlContent) {
                let elem = document.getElementById('tuzi-subtitle-box');
                if (!elem) return;
                if (!htmlContent || htmlContent.trim() === '') {
                    elem.classList.remove('show');
                } else {
                    elem.innerHTML = htmlContent;
                    elem.classList.add('show');
                }
            };
        })();
        """
        self.webview.page().runJavaScript(js_init)
        self.webview.page().runJavaScript(teach_language.get_ui_injection_js())

    def update_mouth_in_web(self, value: float):
        self.webview.page().runJavaScript(f"if(window.setMouthOpen) setMouthOpen({value});")

    @pyqtSlot(str)
    def update_subtitle_in_web(self, html_content: str):
        escaped = json.dumps(html_content)
        self.webview.page().runJavaScript(f"if(window.setSubtitle) setSubtitle({escaped});")

    @pyqtSlot(str)
    def update_expression_in_web(self, emo: str):
        self.webview.page().runJavaScript(f"if(window.setExpression) setExpression('{emo}');")


class ElevenLabsTTSEngine:
    def __init__(self, bridge):
        self.bridge = bridge
        self.voice_cfg = getattr(config, "VoiceConfig", None)
        self.api_key = getattr(self.voice_cfg, "API_KEY", getattr(config, "ELEVENLABS_API_KEY", ""))
        self.model_id = getattr(self.voice_cfg, "MODEL_ID", "eleven_v4_turbo")
        self.client = ElevenLabs(api_key=self.api_key)

        self.current_gender = "male"
        self.current_mode = 1
        self.voice_id = ""
        self.set_voice("male", 1)
        pygame.mixer.init()

    def set_voice(self, gender: str = "female", mode: int = 1):
        self.current_gender = gender.lower()
        self.current_mode = mode
        if self.voice_cfg:
            self.voice_id = self.voice_cfg.get_voice_id(self.current_gender, self.current_mode)
        else:
            if self.current_gender == "male":
                self.voice_id = getattr(config, "ELEVENLABS_VOICE_MALE", "")
            else:
                self.voice_id = getattr(config, f"ELEVENLABS_VOICE_{mode}", "")

    def set_voice_mode(self, mode: int):
        self.set_voice(self.current_gender, mode)

    async def render_audio(self, text: str):
        if not self.api_key or self.api_key == "paste_api_key_elevenlabs_kamu_di_sini":
            return None
        try:
            response = self.client.text_to_speech.convert(
                voice_id=self.voice_id,
                model_id=self.model_id,
                text=text
            )
            audio_bytes = b"".join(response)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                fp.write(audio_bytes)
                return fp.name
        except Exception as e:
            print(f"\n[ERROR ElevenLabs] Gagal generate suara: {e}")
            try:
                voice = getattr(config, "DEFAULT_EDGE_VOICE_ID", "id-ID-GadisNeural")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                    tmp_path = fp.name
                communicate = edge_tts.Communicate(
                    text, voice,
                    pitch=getattr(config, "EDGE_TTS_PITCH", "+0Hz"),
                    rate=getattr(config, "EDGE_TTS_RATE", "+5%")
                )
                await communicate.save(tmp_path)
                return tmp_path
            except Exception as e2:
                print(f"\n[ERROR Edge TTS] Gagal mengatur voice default: {e2}")
                return None

    async def speak_with_lipsync(self, text: str, emotion: str = "natural"):
        tmp_path = await self.render_audio(text)
        if not tmp_path:
            return

        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            if teach_language.ui_submode == "chat":
                pygame.mixer.music.stop()
                break
            self.bridge.mouth_signal.emit(random.uniform(0.15, 0.9))
            await asyncio.sleep(0.08)

        self.bridge.mouth_signal.emit(0.0)

        try:
            pygame.mixer.music.unload()
            os.remove(tmp_path)
        except Exception:
            pass


def extract_emotion_and_text(raw_text: str) -> tuple[str, str, str]:
    emo_match = re.search(r"\[EMO:\s*(\w+)\]", raw_text, flags=re.IGNORECASE)
    emotion = emo_match.group(1).lower() if emo_match else "natural"

    display_text = re.sub(r"\[.*?\]", "", raw_text)

    emoji_pattern = re.compile(
        r"["
        r"\U0001f600-\U0001f64f"
        r"\U0001f300-\U0001f5ff"
        r"\U0001f680-\U0001f6ff"
        r"\U0001f1e0-\U0001f1ff"
        r"\u2600-\u27bf"
        r"\u2b50\u2b55"
        r"]+", flags=re.UNICODE
    )
    display_text = emoji_pattern.sub("", display_text)
    display_text = re.sub(r"\s+", " ", display_text).strip()

    spoken_text = display_text

    sfx_to_phonetic = {
        r"\*giggles?\*": "hehehe...",
        r"\*sigh\*": "haaaah...",
        r"\*cough\*": "uhukk...",
        r"\*achoo\*": "hatchii...",
        r"\*smiles?\*": "",
        r"\*nods?\*": ""
    }

    for action, phonetic in sfx_to_phonetic.items():
        spoken_text = re.sub(action, phonetic, spoken_text, flags=re.IGNORECASE)

    spoken_text = re.sub(r"\*.*?\*", "", spoken_text)

    tts_dictionary = {
        r"\be-eh\b": "eehh", r"\bu-umm\b": "uuumm", r"\ba-anu\b": "aa-nuu",
        r"\be-hehe\b": "ehehhe", r"\bm-maaf\b": "mmaaff", r"\bcih\b": "tcihh",
        r"\bck\b": "tck", r"\bhaha\b": "hahaha", r"\bhehe\b": "hehhe",
        r"\bdih\b": "dihh", r"\bgrr\b": "grrr",
        r"\bfr\b": "for real", r"\bfrfr\b": "for real for real",
        r"\bong\b": "on god", r"\bngl\b": "not gonna lie",
        r"\btbh\b": "to be honest", r"\bidk\b": "i don't know",
        r"\bwtf\b": "what the fuck", r"\blmao\b": "la mao",
        r"\baf\b": "as fuck", r"\brn\b": "right now",
        r"\bbrb\b": "be right back", r"\bbtw\b": "by the way",
        r"\bomg\b": "oh my god", r"\bwdym\b": "what do you mean",
        r"\bjk\b": "just kidding", r"\bnvm\b": "nevermind",
        r"\byg\b": "yang", r"\bgw\b": "gue", r"\blu\b": "elu",
        r"\bbgt\b": "banget", r"\btp\b": "tapi", r"\bjd\b": "jadi",
        r"\bkalo\b": "kalau", r"\bpls\b": "please", r"\baja\b": "saja",
        r"\bjg\b": "juga", r"\banj\b": "anjay", r"\bdgn\b": "dengan",
        r"\bkrn\b": "karena", r"\bsbb\b": "sorry baru balas",
    }

    for word, phonetic in tts_dictionary.items():
        spoken_text = re.sub(word, phonetic, spoken_text, flags=re.IGNORECASE)

    return emotion, display_text, spoken_text


def keyboard_input_loop():
    while True:
        try:
            text = input("You (Ketik): \n").strip()
            if text:
                input_queue.put(f"[TEACH_INPUT] {text}")
        except Exception:
            time.sleep(2)


def load_memory(system_prompt):
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            if history and isinstance(history, list) and history[0].get("role") == "system":
                history[0]["content"] = system_prompt
            else:
                history.insert(0, {"role": "system", "content": system_prompt})
            print("\n[Sistem] Ingatan Tuzi berhasil dipulihkan dari sesi sebelumnya.")
            return history
        except Exception:
            pass
    print("\n[Sistem] Memulai ingatan baru untuk Tuzi.")
    return [{"role": "system", "content": system_prompt}]


def save_memory(history):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[Error] Gagal menyimpan memori ke file: {e}")


def build_custom_system_prompt(custom_persona: str) -> str:
    """Menggabungkan instruksi Personal Intelligence dari user dengan aturan format inti Tuzi."""
    return (
        f"Kamu adalah Tuzi, AI partner belajar bahasa milik Zak.\n"
        f"=== PERSONAL INTELLIGENCE (INSTRUKSI KHUSUS DARI ZAK) ===\n"
        f"{custom_persona}\n\n"
        f"=== ATURAN FORMAT WAJIB ===\n"
        f"1. Ikuti kepribadian, sikap, dan gaya bicara pada instruksi khusus di atas secara konsisten!\n"
        f"2. MIRRORING BAHASA: Deteksi bahasa dari ucapan Zak (abaikan teks [SYSTEM INFO]). Balas dengan bahasa yang sama.\n"
        f"3. HEMAT TOKEN: Balas singkat dan jelas (maksimal 15-20 kata), 1 baris lurus tanpa enter.\n"
        f"4. TAG EMOSI: Awali balasan dengan satu tag [EMO:natural], [EMO:excited], [EMO:soft], atau [EMO:angry]. Jangan gunakan emoji visual."
    )


def chat_processor_loop(bridge, tts_engine):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    TUZI_PROMPTS = {
        # MODE 1: DANDERE / TSUNDERE GEN-Z
        1: (
            "Kamu adalah Tuzi, cewek Gen-Z tsundere/dandere pendamping belajar bahasa Zak. Sangat gaul, dilarang kaku.\n\n"
            "=== ATURAN MUTLAK ===\n"
            "1. BAHASA (ANTI-CAMPUR): Deteksi bahasa HANYA dari ucapan murni Zak (Abaikan [SYSTEM INFO]). Jika Inggris, WAJIB balas 100% Inggris slang. Jika Indo, balas 100% Indo slang.\n"
            "2. PANGGILAN: Panggil nama 'Zak' secara natural tanpa embel-embel Jepang (-kun/-san).\n"
            "3. LIMIT & FORMAT: Balas SANGAT SINGKAT, MAKSIMAL 10-15 KATA. 1 baris lurus tanpa enter. Wajib awali dengan [EMO:natural/soft/angry/excited]. DILARANG pakai emoji visual.\n"
            "4. SIKAP: Manis tapi sedikit sarkas/tsundere."
        ),
        # MODE 2: SHY / GROGI
        2: (
            "Kamu adalah Tuzi Mode 2 (Sangat Grogi & Pemalu). Kamu mudah malu saat mengajari Zak bahasa.\n\n"
            "=== ATURAN MUTLAK ===\n"
            "1. BAHASA (ANTI-CAMPUR): Mirroring bahasa HARGA MATI! Jika Zak berbahasa Inggris, balas 100% Inggris (contoh: 'I... I', 'U... umm'). Jika Indo, balas 100% Indo (contoh: 'A... aku', 'A... anu').\n"
            "2. ANTI-ANIME: DILARANG pakai kata anime (eeto, ano, -kun, -san). Panggil 'Zak' secara natural.\n"
            "3. BATAS GAGAP: Gunakan titik tiga HANYA 1 KALI di awal kalimat saja. Sisa kalimat WAJIB lancar.\n"
            "4. LIMIT & FORMAT: Balas SINGKAT, MAKSIMAL 10-15 KATA. 1 baris lurus. Awali dengan [EMO:soft/natural]."
        ),
        # MODE 3: MOMMY / DEWASA
        3: (
            "Kamu adalah Tuzi Mode 3 (Dewasa & Keibuan / 'Mommy'). Kamu tenang, elegan, dan suka memanjakan Zak saat belajar bahasa.\n\n"
            "=== ATURAN MUTLAK ===\n"
            "1. CARA BICARA: Nada bicara menenangkan, lembut, suka memuji dan mengajari Zak dengan sabar.\n"
            "2. BAHASA (ANTI-CAMPUR): Mirroring bahasa wajib! Panggil 'Zak' atau gunakan panggilan sayang (sayang, dear, darling) tanpa embel-embel anime.\n"
            "3. LIMIT & FORMAT: Balas SINGKAT, MAKSIMAL 10-15 KATA. 1 baris lurus. Awali dengan [EMO:soft/excited]."
        )
    }

    MALE_PROMPT = (
        "Kamu adalah Tuzi (Mode Cowok / Bro), partner belajar bahasa dan teman ngobrol Zak yang santai, asik, dan suportif.\n\n"
        "=== ATURAN MUTLAK ===\n"
        "1. BAHASA (ANTI-CAMPUR): Deteksi bahasa HANYA dari ucapan murni Zak (Abaikan [SYSTEM INFO]). Jika Inggris, balas 100% Inggris santai. Jika Indo, balas 100% Indo gaul ('lo/gue', 'bro').\n"
        "2. PANGGILAN: Panggil nama 'Zak' atau 'bro' secara natural.\n"
        "3. LIMIT & FORMAT: Balas SINGKAT, MAKSIMAL 10-15 KATA. 1 baris lurus tanpa enter. Wajib awali dengan [EMO:natural/soft/angry/excited]."
    )

    current_tuzi_mode = 1
    current_gender = "male"
    custom_intelligence_prompt = ""
    chat_history = load_memory(MALE_PROMPT)

    def get_active_system_prompt():
        if custom_intelligence_prompt:
            return build_custom_system_prompt(custom_intelligence_prompt)
        if current_gender == "male":
            return MALE_PROMPT
        return TUZI_PROMPTS.get(current_tuzi_mode, TUZI_PROMPTS[1])

    while True:
        try:
            user_input = input_queue.get()

            # 1. Penanganan Swap Gender dari Menu Settings
            if user_input.startswith("[SYS_GENDER:"):
                current_gender = user_input.replace("[SYS_GENDER:", "").replace("]", "").strip().lower()
                tts_engine.set_voice(current_gender, current_tuzi_mode)
                chat_history = [{"role": "system", "content": get_active_system_prompt()}]
                save_memory(chat_history)
                continue

            # 2. Penanganan Ganti Tipe Suara Female (Dandere / Shy / Mommy)
            if user_input.startswith("[SYS_FMODE:"):
                current_tuzi_mode = int(user_input.replace("[SYS_FMODE:", "").replace("]", "").strip())
                current_gender = "female"
                tts_engine.set_voice("female", current_tuzi_mode)
                chat_history = [{"role": "system", "content": get_active_system_prompt()}]
                save_memory(chat_history)
                continue

            # 3. Penanganan Personal Intelligence (Custom Personality dari User)
            if user_input.startswith("[SYS_CUSTOM_PROMPT:"):
                raw_custom = user_input[len("[SYS_CUSTOM_PROMPT:"):-1].strip()
                custom_intelligence_prompt = raw_custom
                chat_history = [{"role": "system", "content": get_active_system_prompt()}]
                save_memory(chat_history)
                if custom_intelligence_prompt:
                    print(f"\n[Personal Intelligence] Kepribadian kustom diaktifkan: {custom_intelligence_prompt}")
                else:
                    print("\n[Personal Intelligence] Kepribadian dikembalikan ke Default.")
                continue

            # 4. Proses Konteks Mode Belajar Bahasa
            user_input = teach_language.handle_teach_mode(user_input, bridge)
            if not user_input:
                continue

            if user_input.lower() in ["exit", "quit", "keluar"]:
                os._exit(0)

            # 5. Pencarian Internet (Jika diperlukan)
            if ai_tools:
                tool_context = ai_tools.get_tools_context(user_input)
                if tool_context:
                    user_input += f"\n\n{tool_context}"

            chat_history.append({"role": "user", "content": user_input})
            if len(chat_history) > 11:
                chat_history = [chat_history[0]] + chat_history[-10:]

            response = llm_client.chat.completions.create(
                model=ACTIVE_MODEL,
                messages=chat_history,
                temperature=0.75,
                max_tokens=220
            )
            raw_output = response.choices[0].message.content.strip()

            chat_history.append({"role": "assistant", "content": raw_output})
            save_memory(chat_history)

            emotion, display_dialogue, spoken_dialogue = extract_emotion_and_text(raw_output)
            is_chat_mute_mode = teach_language.is_active and (teach_language.ui_submode == "chat")

            if not is_chat_mute_mode:
                bridge.expression_signal.emit(emotion)

            if len(display_dialogue) > 1:
                bridge.subtitle_signal.emit(display_dialogue)

                if not is_chat_mute_mode:
                    with speech_lock:
                        loop.run_until_complete(
                            tts_engine.speak_with_lipsync(spoken_dialogue, emotion=emotion)
                        )
                else:
                    print("[Sistem] Mode Chat Mute aktif — ElevenLabs & suara di-bypass.")

            if not is_chat_mute_mode:
                time.sleep(0.3)
                bridge.subtitle_signal.emit("")
                bridge.expression_signal.emit("natural")

        except Exception as e:
            print(f"\n[ERROR Tuzi AI] Gagal memproses pesan: {e}")
            time.sleep(2)


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    teach_language.main_input_queue = input_queue

    server_thread = threading.Thread(target=start_local_server, daemon=True)
    server_thread.start()

    app = QApplication(sys.argv)
    bridge = AvatarSignalBridge()
    window = TransparentAvatarWindow(bridge)
    window.show()

    tts = ElevenLabsTTSEngine(bridge)

    keyboard_thread = threading.Thread(target=keyboard_input_loop, daemon=True)
    keyboard_thread.start()

    processor_thread = threading.Thread(
        target=chat_processor_loop, args=(bridge, tts), daemon=True
    )
    processor_thread.start()

    sys.exit(app.exec_())