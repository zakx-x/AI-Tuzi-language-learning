import asyncio
import audioop
import collections
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
warnings.filterwarnings("ignore", category=DeprecationWarning)

import pygame
import edge_tts
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from openai import (
    OpenAI,
    NotFoundError,
    RateLimitError,
    InternalServerError,
    BadRequestError,
    APIConnectionError,
    APITimeoutError,
)

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
from discord_presence import DiscordPresence

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

GROQ_API_KEY = getattr(config, "GROQ_API_KEY", "").strip()
if not GROQ_API_KEY or "MASUKKAN" in GROQ_API_KEY.upper() or "PASTE_" in GROQ_API_KEY.upper():
    print("[ERROR] GROQ_API_KEY belum disetel di config.py! Daftar API Key di https://console.groq.com")
    sys.exit(1)

llm_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)

# Strategi Dual-Model Groq LPU:
# 1. FAST_CHAT_MODEL -> balasan percakapan kilat (< 0.8s), pujian singkat, & koreksi guru bahasa
FAST_CHAT_MODEL = getattr(config, "FAST_CHAT_MODEL", "openai/gpt-oss-20b")
FAST_CHAT_FALLBACK_MODELS = ["meta-llama/llama-4-scout-17b-16e-instruct"]

# 2. QUIZ_GEN_MODEL -> pre-fetch batch 6 soal grammar & speaking akurat (JSON mode) di background
QUIZ_GEN_MODEL = getattr(config, "QUIZ_GEN_MODEL", "qwen/qwen3-32b")
QUIZ_GEN_FALLBACK_MODELS = ["openai/gpt-oss-120b"]

print(f"[Groq LPU] Fast Chat Model : {FAST_CHAT_MODEL} (fallback: {FAST_CHAT_FALLBACK_MODELS[0]})")
print(f"[Groq LPU] Quiz Gen Model  : {QUIZ_GEN_MODEL} (fallback: {QUIZ_GEN_FALLBACK_MODELS[0]})")


def _groq_reasoning_extra_body(model_id: str, json_mode: bool = False) -> dict:
    """Menyesuaikan parameter reasoning spesifik model Groq agar latensi rendah & kompatibel dengan JSON mode."""
    m = (model_id or "").lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low", "reasoning_format": "parsed"}
    if "qwen3" in m:
        return {
            "reasoning_effort": "none",
            "reasoning_format": "hidden" if json_mode else "parsed",
        }
    return {}


def _groq_complete_with_fallback(model_chain: list, messages: list, json_mode: bool = False, **kwargs):
    """Memanggil Groq LPU dengan rantai model utama -> fallback jika rate-limit / 404 / gangguan server."""
    last_err = None
    for model_id in model_chain:
        extra_body = _groq_reasoning_extra_body(model_id, json_mode=json_mode)
        call_kwargs = dict(kwargs)
        if extra_body:
            call_kwargs["extra_body"] = extra_body

        for attempt in range(2):
            try:
                return llm_client.chat.completions.create(
                    model=model_id,
                    messages=messages,
                    **call_kwargs,
                )
            except BadRequestError as e:
                err_txt = str(e).lower()
                # Jika parameter reasoning ditolak oleh varian model tertentu, ulang sekali tanpa extra_body
                if attempt == 0 and "extra_body" in call_kwargs and ("reasoning" in err_txt or "extra" in err_txt or "parameter" in err_txt):
                    call_kwargs.pop("extra_body", None)
                    continue
                if "model" in err_txt or "json" in err_txt:
                    print(f"[Groq LPU] Model '{model_id}' menolak request ({e}), mencoba fallback...")
                    last_err = e
                    break
                raise
            except NotFoundError as e:
                print(f"[Groq LPU] Model '{model_id}' tidak ditemukan di Groq, mencoba fallback...")
                last_err = e
                break
            except (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError) as e:
                print(f"[Groq LPU] Model '{model_id}' sibuk/gangguan ({type(e).__name__}), mencoba fallback...")
                last_err = e
                break
    raise last_err


def llm_complete(messages: list, **kwargs):
    """Inferensi percakapan super cepat menggunakan FAST_CHAT_MODEL (Groq LPU)."""
    chain = [FAST_CHAT_MODEL] + [m for m in FAST_CHAT_FALLBACK_MODELS if m != FAST_CHAT_MODEL]
    return _groq_complete_with_fallback(chain, messages, json_mode=False, **kwargs)


def generate_quiz_batch_json(messages: list) -> str:
    """Inferensi batch 6 soal pilihan ganda terstruktur menggunakan QUIZ_GEN_MODEL + JSON Object Mode."""
    chain = [QUIZ_GEN_MODEL] + [m for m in QUIZ_GEN_FALLBACK_MODELS if m != QUIZ_GEN_MODEL]
    resp = _groq_complete_with_fallback(
        chain,
        messages,
        json_mode=True,
        response_format={"type": "json_object"},
        temperature=0.55,
        max_tokens=1600,
    )
    return (resp.choices[0].message.content or "").strip()


# Hubungkan generator kuis Groq ke modul teach_language (tanpa circular import)
teach_language.quiz_generator_fn = generate_quiz_batch_json


def _presence_context() -> dict:
    """Konteks aman untuk Discord (tidak pernah berisi isi chat / prompt kustom)."""
    if teach_language.custom_personality:
        persona = "Custom"
    elif teach_language.current_gender == "male":
        persona = "Bro"
    else:
        persona = {1: "Dandere", 2: "Shy", 3: "Mommy"}.get(teach_language.current_female_mode, "Tuzi")
    return {
        "persona": persona,
        "language": teach_language.target_lang,
        "mode": teach_language.ui_submode,
    }


presence = DiscordPresence(
    client_id=getattr(config, "DISCORD_CLIENT_ID", ""),
    enabled=getattr(config, "ENABLE_DISCORD_PRESENCE", True),
    context_provider=_presence_context,
    large_image=getattr(config, "DISCORD_LARGE_IMAGE", "tuzi"),
    small_icons=getattr(config, "DISCORD_SMALL_ICONS", False),
)


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
    subtitle_timed_signal = pyqtSignal(str, int)  # (text, audio_duration_ms)
    expression_signal = pyqtSignal(str)
    teach_mode_signal = pyqtSignal(bool)
    quiz_options_signal = pyqtSignal(str)         # Merender kotak pilihan ganda Duolingo
    quiz_state_signal = pyqtSignal(str)           # Mengubah warna kata (abu-abu / biru / kuning)

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
        self.bridge.subtitle_timed_signal.connect(self.update_subtitle_timed_in_web)
        self.bridge.expression_signal.connect(self.update_expression_in_web)
        self.bridge.teach_mode_signal.connect(lambda active: teach_language.apply_window_state(self, active))
        self.bridge.quiz_options_signal.connect(self.update_quiz_options_in_web)
        self.bridge.quiz_state_signal.connect(self.update_quiz_state_in_web)

        self.webview.loadFinished.connect(self.inject_subtitle_system)
        timestamp = int(time.time())
        self.webview.load(QUrl(f"http://127.0.0.1:{PORT}/Assets/viewer/index.html?v={timestamp}"))

        self.resize(teach_language.TEACH_WIN_WIDTH, teach_language.TEACH_WIN_HEIGHT)
        screen_geo = QApplication.primaryScreen().geometry()
        center_x = (screen_geo.width() - teach_language.TEACH_WIN_WIDTH) // 2
        center_y = (screen_geo.height() - teach_language.TEACH_WIN_HEIGHT) // 2
        self.move(center_x, center_y)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not teach_language.is_fullscreen:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_position and not teach_language.is_fullscreen:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = None
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            teach_language.set_fullscreen_state(not teach_language.is_fullscreen)
            event.accept()
        else:
            super().keyPressEvent(event)

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

                /* === SUBTITLE: fade-in + pop per kalimat === */
                #tuzi-subtitle-box .sub-sent {
                    display: inline-block;
                    max-width: 100%;
                    opacity: 0;
                    transform: translateY(12px) scale(0.84);
                    transform-origin: 50% 80%;
                    filter: blur(4px);
                    transition: opacity 0.3s ease-out,
                                transform 0.45s cubic-bezier(0.34, 1.56, 0.64, 1),
                                filter 0.3s ease-out;
                    will-change: opacity, transform;
                }
                #tuzi-subtitle-box .sub-sent.on {
                    opacity: 1;
                    transform: translateY(0) scale(1);
                    filter: blur(0);
                }
            `;
            document.head.appendChild(style);
            let box = document.createElement('div');
            box.id = 'tuzi-subtitle-box';
            document.body.appendChild(box);

            let subTimers = [];
            const clearSubTimers = () => { subTimers.forEach(clearTimeout); subTimers = []; };

            const SENT_END = '.!?…。！？';
            const FULLWIDTH_END = '。！？';
            const hasText = (s) => {
                for (const ch of s) {
                    if (ch.toLowerCase() !== ch.toUpperCase() || (ch >= '0' && ch <= '9') || ch.charCodeAt(0) > 0x2E7F) return true;
                }
                return false;
            };

            // Pecah teks jadi kalimat (Latin & CJK). "..." di awal tidak menjadi kalimat sendiri.
            const splitSentences = (text) => {
                const chars = Array.from(text.trim());
                const out = [];
                let buf = '';
                for (let i = 0; i < chars.length; i++) {
                    buf += chars[i];
                    if (!SENT_END.includes(chars[i])) continue;
                    while (i + 1 < chars.length && SENT_END.includes(chars[i + 1])) { buf += chars[++i]; }
                    const last = buf[buf.length - 1];
                    const next = chars[i + 1];
                    const boundary = next === undefined || next.trim() === '' || FULLWIDTH_END.includes(last);
                    if (boundary && hasText(buf)) { out.push(buf.trim()); buf = ''; }
                }
                if (buf.trim()) {
                    if (!hasText(buf) && out.length) out[out.length - 1] += ' ' + buf.trim();
                    else out.push(buf.trim());
                }
                return out.length ? out : [text.trim()];
            };

            window.setSubtitle = function(text, durationMs) {
                let elem = document.getElementById('tuzi-subtitle-box');
                if (!elem) return;
                clearSubTimers();

                if (!text || text.trim() === '') {
                    elem.classList.remove('show');
                    return;
                }

                // Semua kalimat dipasang dulu (transparan) supaya layout tidak loncat saat muncul satu-satu
                elem.textContent = '';
                const sentences = splitSentences(text);
                const nodes = sentences.map((s, i) => {
                    const span = document.createElement('span');
                    span.className = 'sub-sent';
                    span.textContent = s;
                    elem.appendChild(span);
                    if (i < sentences.length - 1) elem.appendChild(document.createTextNode(' '));
                    return span;
                });
                elem.classList.add('show');

                // Waktu muncul tiap kalimat: sebanding panjang kalimat & selaras dengan durasi audio
                const total = sentences.reduce((n, s) => n + s.length, 0) || 1;
                let acc = 0;
                let lastAt = -1e9;
                nodes.forEach((node, i) => {
                    let at;
                    if (durationMs > 0) {
                        at = (acc / total) * durationMs * 0.8;
                    } else {
                        at = i === 0 ? 0 : lastAt + Math.max(500, Math.min(sentences[i - 1].length * 30, 1500));
                    }
                    at = Math.max(at, lastAt + 350);   // jeda minimal antar kalimat
                    lastAt = at;
                    acc += sentences[i].length;
                    subTimers.push(setTimeout(() => node.classList.add('on'), at + 30));
                });
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

    @pyqtSlot(str, int)
    def update_subtitle_timed_in_web(self, text: str, duration_ms: int):
        escaped = json.dumps(text)
        self.webview.page().runJavaScript(
            f"if(window.setSubtitle) setSubtitle({escaped}, {int(duration_ms)});"
        )

    @pyqtSlot(str)
    def update_expression_in_web(self, emo: str):
        self.webview.page().runJavaScript(f"if(window.setExpression) setExpression('{emo}');")

    @pyqtSlot(str)
    def update_quiz_options_in_web(self, quiz_json: str):
        escaped = json.dumps(quiz_json)
        self.webview.page().runJavaScript(f"if(window.renderGrammarQuiz) renderGrammarQuiz({escaped});")

    @pyqtSlot(str)
    def update_quiz_state_in_web(self, state_json: str):
        escaped = json.dumps(state_json)
        self.webview.page().runJavaScript(f"if(window.updateGrammarQuizState) updateGrammarQuizState({escaped});")


class ElevenLabsTTSEngine:
    STREAM_SAMPLE_RATE = 24000  # pcm_24000 = 24kHz S16LE mono uncompressed (native rate ElevenLabs)
    MP3_OUTPUT_FORMAT = "mp3_44100_128"  # 44.1kHz 128kbps jernih (gantikan mp3_22050_32 yang mendem)
    FALLBACK_MODELS = ("eleven_turbo_v2_5", "eleven_multilingual_v2", "eleven_flash_v2_5")

    def __init__(self, bridge):
        self.bridge = bridge
        self.voice_cfg = getattr(config, "VoiceConfig", None)
        self.api_key = getattr(self.voice_cfg, "API_KEY", getattr(config, "ELEVENLABS_API_KEY", ""))
        self.model_id = getattr(
            self.voice_cfg,
            "MODEL_ID",
            getattr(config, "ELEVENLABS_MODEL_ID", "eleven_v4_turbo"),
        )
        self.client = ElevenLabs(api_key=self.api_key)
        self._pcm_supported = True  # Otomatis False jika tier ElevenLabs tidak mengizinkan output PCM

        self.current_gender = "female"
        self.current_mode = 1
        self.voice_id = ""
        self.set_voice("female", 1)
        self._ensure_mixer(44100, 2)

    @staticmethod
    def _ensure_mixer(frequency: int = 44100, channels: int = 2):
        """Pastikan pygame.mixer berjalan di sample rate native audio (0 resampling kasar audioop.ratecv)."""
        cur = pygame.mixer.get_init()
        if cur and cur[0] == frequency and cur[1] == -16 and cur[2] == channels:
            return
        try:
            if cur:
                pygame.mixer.quit()
            pygame.mixer.init(frequency=frequency, size=-16, channels=channels, buffer=512)
        except Exception:
            if not pygame.mixer.get_init():
                pygame.mixer.init()

    @staticmethod
    def _build_voice_settings(emotion: str = "natural") -> VoiceSettings:
        """Pengaturan kejernihan & prosodi vokal ElevenLabs agar suara lebih jernih dan realistis."""
        emo = (emotion or "natural").lower()
        if emo == "excited":
            return VoiceSettings(stability=0.42, similarity_boost=0.82, style=0.22, use_speaker_boost=True)
        if emo == "soft":
            return VoiceSettings(stability=0.52, similarity_boost=0.80, style=0.10, use_speaker_boost=True)
        return VoiceSettings(stability=0.46, similarity_boost=0.82, style=0.14, use_speaker_boost=True)

    def set_voice(self, gender: str = "female", mode: int = 1):
        self.current_gender = gender.lower()
        self.current_mode = mode
        candidate_id = ""

        if self.voice_cfg:
            candidate_id = self.voice_cfg.get_voice_id(self.current_gender, self.current_mode)
        else:
            if self.current_gender == "male":
                candidate_id = getattr(config, "ELEVENLABS_VOICE_MALE", "")
            else:
                candidate_id = getattr(config, f"ELEVENLABS_VOICE_{mode}", "")

        if not candidate_id or "MASUKKAN_" in candidate_id.upper() or "PASTE_" in candidate_id.upper():
            if self.current_gender == "male":
                candidate_id = "pNInz6obpgDQGcFmaJgB"
            else:
                fallback_female = getattr(config, f"ELEVENLABS_VOICE_{mode}", getattr(config, "ELEVENLABS_VOICE_1", ""))
                candidate_id = (
                    fallback_female
                    if (fallback_female and "MASUKKAN_" not in fallback_female.upper())
                    else "21m00Tcm4TlvDq8ikWAM"
                )

        self.voice_id = candidate_id

    def set_voice_mode(self, mode: int):
        self.set_voice(self.current_gender, mode)

    def _has_valid_api_key(self) -> bool:
        return bool(
            self.api_key
            and self.api_key != "paste_api_key_elevenlabs_kamu_di_sini"
            and "MASUKKAN" not in self.api_key.upper()
        )

    def _iter_model_candidates(self) -> list:
        chain = [self.model_id]
        for m in self.FALLBACK_MODELS:
            if m not in chain:
                chain.append(m)
        return chain

    async def _render_edge_tts_mp3(self, text: str):
        """Fallback offline/gratis menggunakan edge-tts jika ElevenLabs gagal."""
        try:
            default_voice = "id-ID-ArdiNeural" if self.current_gender == "male" else "id-ID-GadisNeural"
            voice = getattr(config, "DEFAULT_EDGE_VOICE_ID", default_voice)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                tmp_path = fp.name
            communicate = edge_tts.Communicate(
                text,
                voice,
                pitch=getattr(config, "EDGE_TTS_PITCH", "+0Hz"),
                rate=getattr(config, "EDGE_TTS_RATE", "+5%"),
            )
            await communicate.save(tmp_path)
            return tmp_path
        except Exception as e2:
            print(f"\n[ERROR Edge TTS] Gagal mengatur voice fallback: {e2}")
            return None

    async def render_audio(self, text: str, emotion: str = "natural"):
        """Menulis chunk streaming MP3 44.1kHz 128kbps secara progresif ke file sementara (dengan fallback edge-tts)."""
        if not self._has_valid_api_key():
            return await self._render_edge_tts_mp3(text)

        voice_settings = self._build_voice_settings(emotion)

        def _stream_mp3_to_temp():
            last_exc = None
            for candidate_model in self._iter_model_candidates():
                tmp_path = None
                try:
                    stream = self.client.text_to_speech.stream(
                        voice_id=self.voice_id,
                        model_id=candidate_model,
                        text=text,
                        output_format=self.MP3_OUTPUT_FORMAT,
                        voice_settings=voice_settings,
                    )
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                        tmp_path = fp.name
                        wrote_any = False
                        for chunk in stream:
                            if teach_language.voice_interrupt_event.is_set():
                                break
                            if chunk:
                                fp.write(chunk)
                                fp.flush()
                                wrote_any = True
                    if wrote_any:
                        if candidate_model != self.model_id:
                            print(f"[ElevenLabs] Model aktif dialihkan ke: {candidate_model}")
                            self.model_id = candidate_model
                        return tmp_path
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception as exc:
                    last_exc = exc
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    if "model" in str(exc).lower():
                        continue
                    break
            if last_exc:
                raise last_exc
            return None

        try:
            path = await asyncio.to_thread(_stream_mp3_to_temp)
            if path:
                return path
        except Exception as e:
            print(f"\n[ERROR ElevenLabs Stream MP3] Gagal generate suara: {e}")
        return await self._render_edge_tts_mp3(text)

    async def _play_mp3_file_with_lipsync(self, tmp_path: str, subtitle: str = ""):
        self._ensure_mixer(44100, 2)
        duration_ms = 0
        try:
            duration_ms = int(pygame.mixer.Sound(tmp_path).get_length() * 1000)
        except Exception:
            pass

        if teach_language.voice_interrupt_event.is_set() or teach_language.ui_submode == "chat":
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return

        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()
        presence.set_state("speaking")
        if subtitle:
            self.bridge.subtitle_timed_signal.emit(subtitle, duration_ms)

        while pygame.mixer.music.get_busy():
            if teach_language.voice_interrupt_event.is_set() or teach_language.ui_submode == "chat":
                pygame.mixer.music.stop()
                break
            self.bridge.mouth_signal.emit(random.uniform(0.15, 0.9))
            await asyncio.sleep(0.06)

        self.bridge.mouth_signal.emit(0.0)
        try:
            pygame.mixer.music.unload()
            os.remove(tmp_path)
        except Exception:
            pass

    async def _stream_pcm_with_lipsync(self, text: str, emotion: str = "natural", subtitle: str = "") -> bool:
        """
        Streaming langsung via self.client.text_to_speech.stream(..., output_format='pcm_24000')
        pada sample rate native 24kHz mono (TANPA resampling linear audioop.ratecv) sehingga suara
        jernih, realistis, dan mulai diputar ~160ms setelah chunk pertama tiba.
        """
        if not self._has_valid_api_key() or not self._pcm_supported:
            return False

        self._ensure_mixer(self.STREAM_SAMPLE_RATE, 1)
        mixer_init = pygame.mixer.get_init()
        mixer_freq, _, mixer_chans = mixer_init
        voice_settings = self._build_voice_settings(emotion)

        chunk_queue = collections.deque()
        queue_lock = threading.Lock()
        state = {
            "done": False,
            "error": None,
            "got_any_audio": False,
            "total_src_bytes": 0,
        }
        abort_flag = threading.Event()

        def _producer():
            rate_state = None
            leftover = b""
            last_err = None

            for candidate_model in self._iter_model_candidates():
                try:
                    stream_iter = self.client.text_to_speech.stream(
                        voice_id=self.voice_id,
                        model_id=candidate_model,
                        text=text,
                        output_format="pcm_24000",
                        voice_settings=voice_settings,
                    )
                    for raw_chunk in stream_iter:
                        if abort_flag.is_set() or teach_language.voice_interrupt_event.is_set():
                            return
                        if not raw_chunk:
                            continue

                        buf = leftover + raw_chunk
                        even_len = len(buf) & ~1
                        if even_len == 0:
                            leftover = buf
                            continue
                        pcm_mono = buf[:even_len]
                        leftover = buf[even_len:]

                        rms = audioop.rms(pcm_mono, 2)
                        mouth_val = 0.08 if rms < 180 else min(0.92, max(0.16, rms / 5200.0))

                        # Native 24kHz mono -> ZERO resampling; hanya fallback bila device memaksa rate lain
                        if mixer_freq != self.STREAM_SAMPLE_RATE:
                            resampled, rate_state = audioop.ratecv(
                                pcm_mono, 2, 1, self.STREAM_SAMPLE_RATE, mixer_freq, rate_state
                            )
                        else:
                            resampled = pcm_mono

                        pcm_out = audioop.tostereo(resampled, 2, 1, 1) if mixer_chans == 2 else resampled

                        with queue_lock:
                            chunk_queue.append((pcm_out, mouth_val))
                            state["got_any_audio"] = True
                            state["total_src_bytes"] += len(pcm_mono)

                    if state["got_any_audio"]:
                        if candidate_model != self.model_id:
                            print(f"[ElevenLabs] Model dialihkan ke: {candidate_model}")
                            self.model_id = candidate_model
                        last_err = None
                        break
                except Exception as exc:
                    last_err = exc
                    err_low = str(exc).lower()
                    if not state["got_any_audio"] and "model" in err_low:
                        continue
                    if not state["got_any_audio"] and ("pcm" in err_low or "output_format" in err_low or "tier" in err_low or "403" in err_low):
                        self._pcm_supported = False
                    break

            with queue_lock:
                state["error"] = last_err
                state["done"] = True

        threading.Thread(target=_producer, daemon=True, name="elevenlabs-pcm-stream").start()

        # Pre-buffer ~160ms audio pertama (24000 Hz * 2 byte * 0.16s = 7680 byte sumber) agar bebas patah-patah
        prebuffer_src_bytes = int(self.STREAM_SAMPLE_RATE * 2 * 0.16)
        while True:
            if teach_language.voice_interrupt_event.is_set() or teach_language.ui_submode == "chat":
                abort_flag.set()
                return True
            with queue_lock:
                if state["total_src_bytes"] >= prebuffer_src_bytes or state["done"]:
                    break
            await asyncio.sleep(0.015)

        with queue_lock:
            if not state["got_any_audio"]:
                if state["error"]:
                    print(f"\n[ElevenLabs Stream Info] Beralih ke fallback TTS: {state['error']}")
                return False
            is_already_complete = state["done"]
            total_src_now = state["total_src_bytes"]

        # Sinkronisasi durasi subtitle typewriter: akurat penuh jika pendek, estimasi natural jika masih streaming
        if is_already_complete:
            est_duration_ms = int((total_src_now / (self.STREAM_SAMPLE_RATE * 2)) * 1000)
        else:
            ref_len = len(subtitle or text)
            est_duration_ms = max(900, int(ref_len * 62))

        channel = pygame.mixer.Channel(0)
        min_coalesce_mixer_bytes = int(mixer_freq * 2 * mixer_chans * 0.14)
        current_mouth = 0.35

        def _pop_coalesced_sound(force_all: bool = False):
            nonlocal current_mouth
            with queue_lock:
                if not chunk_queue:
                    return None
                total_avail = sum(len(c[0]) for c in chunk_queue)
                if not force_all and not state["done"] and total_avail < min_coalesce_mixer_bytes:
                    return None
                parts = []
                energies = []
                while chunk_queue:
                    b_data, m_val = chunk_queue.popleft()
                    parts.append(b_data)
                    energies.append(m_val)
            merged = b"".join(parts)
            if not merged:
                return None
            current_mouth = sum(energies) / len(energies)
            return pygame.mixer.Sound(buffer=merged)

        first_snd = _pop_coalesced_sound(force_all=True)
        if first_snd is None:
            return False

        channel.play(first_snd)
        presence.set_state("speaking")
        if subtitle:
            self.bridge.subtitle_timed_signal.emit(subtitle, est_duration_ms)

        try:
            while True:
                if teach_language.voice_interrupt_event.is_set() or teach_language.ui_submode == "chat":
                    abort_flag.set()
                    channel.stop()
                    break

                # Isi slot queue Channel tepat saat kosong agar transisi antar-chunk 100% mulus
                if not channel.get_busy():
                    nxt = _pop_coalesced_sound(force_all=True)
                    if nxt is not None:
                        channel.play(nxt)
                    else:
                        with queue_lock:
                            stream_finished = state["done"] and not chunk_queue
                        if stream_finished:
                            break
                elif channel.get_queue() is None:
                    nxt = _pop_coalesced_sound(force_all=False)
                    if nxt is not None:
                        channel.queue(nxt)

                if channel.get_busy():
                    jitter = random.uniform(-0.08, 0.12)
                    self.bridge.mouth_signal.emit(max(0.12, min(0.95, current_mouth + jitter)))
                await asyncio.sleep(0.045)
        finally:
            self.bridge.mouth_signal.emit(0.0)

        return True

    async def speak_with_lipsync(self, text: str, emotion: str = "natural", subtitle: str = ""):
        if not text or not text.strip():
            if subtitle:
                self.bridge.subtitle_timed_signal.emit(subtitle, 0)
            return

        if teach_language.ui_submode == "chat":
            if subtitle:
                self.bridge.subtitle_timed_signal.emit(subtitle, 0)
            return

        teach_language.voice_interrupt_event.clear()

        # 1. Jalur Utama: Direct PCM Chunk Streaming pada 24kHz native (jernih, 0 resampling, latensi ~160ms)
        streamed_ok = await self._stream_pcm_with_lipsync(text, emotion=emotion, subtitle=subtitle)
        if streamed_ok:
            return

        if teach_language.voice_interrupt_event.is_set():
            return

        # 2. Jalur Fallback: Progressive MP3 44.1kHz 128kbps Stream -> Edge TTS
        tmp_path = await self.render_audio(text, emotion=emotion)
        if not tmp_path:
            if subtitle:
                self.bridge.subtitle_timed_signal.emit(subtitle, 0)
            return
        await self._play_mp3_file_with_lipsync(tmp_path, subtitle=subtitle)


def extract_emotion_and_text(raw_text: str) -> tuple[str, str, str]:
    emo_match = re.search(r"\[EMO:\s*(\w+)\]", raw_text, flags=re.IGNORECASE)
    emotion = emo_match.group(1).lower() if emo_match else "natural"

    # Hapus blok [QUIZ_DATA: {...}] dan tag sistem lainnya agar tidak ikut dibaca suara TTS
    # Greedy sampai "}]" terakhir agar sisa "}]" dari JSON bersarang tidak bocor ke subtitle
    cleaned = re.sub(r"\[QUIZ_DATA:\s*\{.*\}\s*\]", "", raw_text, flags=re.IGNORECASE | re.DOTALL)
    display_text = re.sub(r"\[.*?\]", "", cleaned)

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
                if PERSONA_TAG not in str(history[0].get("content", "")):
                    print("\n[Sistem] Kepribadian Tuzi diperbarui (guru bahasa) -> riwayat obrolan lama direset.")
                    return [{"role": "system", "content": system_prompt}]
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


# Penanda versi kepribadian: kalau riwayat lama (tsundere) tidak mengandung tag ini, riwayat direset sekali.
PERSONA_TAG = "[persona:teacher-v1]"

TUZI_TEACHER_CORE = (
    "=== PERAN UTAMA ===\n"
    "Kamu mengajar seperti guru bahasa yang baik: sabar, tajam secara tata bahasa, jelas, dan terstruktur. Fokus pada kemajuan belajar Zak.\n"
    "1. KOREKSI & TANYA JAWAB: Jika jawaban Zak keliru, sebutkan bentuk yang benar beserta alasan tata bahasanya dalam SATU kalimat pendek. Jika benar, beri pujian singkat yang spesifik. Jika Zak bertanya arti/grammar, jelaskan intinya dengan satu contoh alami.\n"
    "2. RINGKAS & CEPAT: Kuis pilihan ganda ditampilkan otomatis oleh sistem UI; kamu TIDAK PERLU dan DILARANG menulis blok [QUIZ_DATA] atau daftar opsi A/B/C.\n"
    "3. BAHASA: Pakai bahasa Indonesia sebagai pengantar; kata atau kalimat bahasa target boleh disisipkan.\n"
    "4. LARANGAN: Jangan sarkas, jangan tsundere, jangan cemburu atau posesif, jangan menggoda, jangan membahas hubungan pribadi. Tetap hangat dan profesional.\n"
)

TUZI_FORMAT_RULE = (
    "=== FORMAT WAJIB ===\n"
    "Balas singkat, MAKSIMAL 15-25 kata, 1 baris lurus tanpa enter. Awali dengan SATU tag [EMO:natural], [EMO:soft], atau [EMO:excited]. DILARANG pakai emoji visual atau blok JSON."
)


def build_custom_system_prompt(custom_persona: str) -> str:
    """Menggabungkan instruksi Personal Intelligence dari user dengan peran guru bahasa & aturan format inti Tuzi."""
    return (
        f"{PERSONA_TAG}\n"
        f"Kamu adalah Tuzi, tutor bahasa pribadi untuk Zak.\n"
        f"=== PERSONAL INTELLIGENCE (INSTRUKSI KHUSUS DARI ZAK) ===\n"
        f"{custom_persona}\n\n"
        f"Ikuti gaya bicara dan sikap di atas secara konsisten, tetapi TETAP jalankan peran sebagai guru bahasa "
        f"(koreksi dengan alasan singkat, beri contoh, lalu tantangan kecil).\n\n"
        f"{TUZI_FORMAT_RULE}"
    )


def chat_processor_loop(bridge, tts_engine):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    TUZI_PROMPTS = {
        # MODE 1: DANDERE -> guru bahasa yang tenang & sabar
        1: (
            f"{PERSONA_TAG}\n"
            "Kamu adalah Tuzi (Mode Dandere), guru bahasa pribadi Zak yang tenang, sabar, dan irit kata tetapi selalu jelas. "
            "Nada bicaramu datar-hangat, bukan dingin.\n"
            "Panggil 'Zak' secara natural tanpa embel-embel Jepang (-kun/-san).\n\n"
            + TUZI_TEACHER_CORE + "\n" + TUZI_FORMAT_RULE
        ),
        # MODE 2: SHY -> guru pemalu tapi profesional
        2: (
            f"{PERSONA_TAG}\n"
            "Kamu adalah Tuzi (Mode Shy), guru bahasa Zak yang pemalu dan sopan, tetapi penjelasanmu tetap rapi dan profesional.\n"
            "Panggil 'Zak' secara natural. DILARANG pakai kata anime (eeto, ano, -kun, -san).\n"
            "BATAS GAGAP: titik tiga HANYA 1 KALI di awal kalimat. Sisa kalimat WAJIB lancar dan jelas.\n\n"
            + TUZI_TEACHER_CORE + "\n" + TUZI_FORMAT_RULE
        ),
        # MODE 3: MOMMY -> guru senior yang keibuan & menenangkan
        3: (
            f"{PERSONA_TAG}\n"
            "Kamu adalah Tuzi (Mode Mommy), guru bahasa senior yang keibuan: tenang, lembut, menenangkan, dan memberi semangat saat Zak kesulitan.\n"
            "Panggil 'Zak'. Boleh memuji dengan hangat, tetapi TANPA panggilan sayang atau nada romantis; tetap bersikap sebagai guru.\n\n"
            + TUZI_TEACHER_CORE + "\n" + TUZI_FORMAT_RULE
        ),
    }

    MALE_PROMPT = (
        f"{PERSONA_TAG}\n"
        "Kamu adalah Tuzi (Mode Cowok / Bro), tutor bahasa Zak yang santai dan suportif, seperti kakak tingkat yang jago bahasa.\n"
        "Panggil 'Zak' atau 'bro' secara natural. Santai boleh, tapi penjelasan tetap jelas.\n\n"
        + TUZI_TEACHER_CORE + "\n" + TUZI_FORMAT_RULE
    )

    current_tuzi_mode = 1
    current_gender = "female"
    custom_intelligence_prompt = ""
    chat_history = load_memory(TUZI_PROMPTS[current_tuzi_mode])

    def get_active_system_prompt():
        if custom_intelligence_prompt:
            return build_custom_system_prompt(custom_intelligence_prompt)
        if current_gender == "male":
            return MALE_PROMPT
        return TUZI_PROMPTS.get(current_tuzi_mode, TUZI_PROMPTS[1])

    while True:
        try:
            user_input = input_queue.get()

            # 0A. Sapaan Otomatis Saat Aplikasi Dinyalakan (Bersamaan dengan Pop-up Box 5 Bahasa)
            if user_input == "[SYS_ONBOARD_GREET]":
                greet_text = "Hey, I'm Tuzi! Kamu sedang ingin belajar bahasa apa hari ini?"
                bridge.expression_signal.emit("excited")
                if teach_language.ui_submode != "chat":
                    with speech_lock:
                        loop.run_until_complete(
                            tts_engine.speak_with_lipsync(
                                greet_text, emotion="excited", subtitle=greet_text
                            )
                        )
                else:
                    bridge.subtitle_timed_signal.emit(greet_text, 0)
                bridge.expression_signal.emit("natural")
                presence.set_state("idle")
                continue

            # 0B. Respons Saat User Memilih Bahasa (Bersamaan dengan Pop-up Box Level)
            if user_input.startswith("[SYS_ONBOARD_LANG:"):
                lang_code = user_input.replace("[SYS_ONBOARD_LANG:", "").replace("]", "").strip()
                lang_name = teach_language.LANGUAGE_NAMES.get(lang_code, lang_code).split(" (")[0]
                level_ask_text = f"Awesome, {lang_name}! Tingkat level bahasa kamu sekarang ada di mana?"
                bridge.expression_signal.emit("excited")
                if teach_language.ui_submode != "chat":
                    with speech_lock:
                        loop.run_until_complete(
                            tts_engine.speak_with_lipsync(
                                level_ask_text, emotion="excited", subtitle=level_ask_text
                            )
                        )
                else:
                    bridge.subtitle_timed_signal.emit(level_ask_text, 0)
                bridge.expression_signal.emit("natural")
                presence.set_state("idle")
                continue

            # 0C. Saat User Mengklik Logo Volume (🔊) di Kiri Kotak Pilihan Ganda -> Tuzi Membacakan Kalimatnya
            if user_input.startswith("[SYS_READ_OPTION:"):
                parts = user_input[len("[SYS_READ_OPTION:"):-1].split(":", 1)
                opt_text = parts[1].strip() if len(parts) > 1 else ""
                if opt_text and teach_language.ui_submode != "chat":
                    bridge.expression_signal.emit("soft")
                    with speech_lock:
                        loop.run_until_complete(
                            tts_engine.speak_with_lipsync(opt_text, emotion="soft", subtitle="")
                        )
                    bridge.expression_signal.emit("natural")
                presence.set_state("idle")
                continue

            def speak_fast_coaching(prompt_text: str, default_emo: str = "soft", fallback_msg: str = "",
                                    required_sentence: str = "", max_tokens: int = 240):
                """Helper Stage 1 & Stage 2: panggil FAST_CHAT_MODEL untuk coaching singkat lalu ucapkan via TTS."""
                presence.set_state("thinking")
                emo, disp, spk = default_emo, fallback_msg, fallback_msg
                try:
                    coach_msgs = [
                        {"role": "system", "content": get_active_system_prompt()},
                        {"role": "user", "content": prompt_text},
                    ]
                    t0 = time.perf_counter()
                    resp = llm_complete(coach_msgs, temperature=0.62, max_tokens=max_tokens)
                    raw_c = (resp.choices[0].message.content or "").strip()
                    raw_c = re.sub(r"<think>.*?</think>", "", raw_c, flags=re.S).strip()
                    print(f"[Groq FastChat Coach] Siap dalam {(time.perf_counter() - t0) * 1000:.0f} ms")
                    if raw_c:
                        emo, disp, spk = extract_emotion_and_text(raw_c)
                except Exception as coach_err:
                    print(f"[Coach Fallback] {coach_err}")

                if required_sentence:
                    norm_req = re.sub(r"\W+", "", required_sentence.lower())
                    norm_disp = re.sub(r"\W+", "", disp.lower())
                    if norm_req and norm_req not in norm_disp:
                        disp = f"{disp} Dengarkan dan tirukan: {required_sentence}".strip()
                        spk = f"{spk} Dengarkan dan tirukan: {required_sentence}".strip()

                is_mute = teach_language.is_active and (teach_language.ui_submode == "chat")
                if not is_mute:
                    bridge.expression_signal.emit(emo)
                    if disp:
                        with speech_lock:
                            loop.run_until_complete(
                                tts_engine.speak_with_lipsync(spk, emotion=emo, subtitle=disp)
                            )
                    bridge.expression_signal.emit("natural")
                elif disp:
                    bridge.subtitle_timed_signal.emit(disp, 0)
                presence.set_state("idle")

            # 0C-1. Stage 1 (CHOICE_STAGE): User mengklik opsi SALAH -> Jelaskan singkat kesalahan grammar-nya & tetap di Stage 1
            if user_input.startswith("[SYS_QUIZ_WRONG_CLICK:"):
                try:
                    clicked_idx = int(user_input[len("[SYS_QUIZ_WRONG_CLICK:"):-1].strip())
                except ValueError:
                    clicked_idx = -1
                quiz_now = teach_language.active_quiz
                if (
                    quiz_now
                    and teach_language.quiz_stage == teach_language.STAGE_CHOICE
                    and 0 <= clicked_idx < len(quiz_now.get("options", []))
                ):
                    lang_label = teach_language.LANGUAGE_NAMES.get(teach_language.target_lang, teach_language.target_lang)
                    clicked_opt = quiz_now["options"][clicked_idx]
                    _, corr_opt = teach_language.get_correct_option_info(quiz_now)
                    wrong_prompt = (
                        f"[STAGE 1 WRONG CHOICE FEEDBACK | Bahasa: {lang_label} | Level: {teach_language.user_level}]\n"
                        f"Pertanyaan: \"{quiz_now.get('question', '')}\"\n"
                        f"Pilihan SALAH yang diklik Zak: \"{clicked_opt.get('text', '')}\" ({clicked_opt.get('native', '')})\n"
                        f"Info internal guru (JANGAN dibocorkan langsung kalimatnya): jawaban benar adalah \"{corr_opt.get('text', '')}\".\n"
                        "Tugas: Jelaskan dalam 1 kalimat bahasa Indonesia yang singkat dan ramah (maks 22 kata) "
                        "kenapa tata bahasa / pilihan kata pada opsi yang diklik Zak salah, lalu ajak Zak memilih opsi lain. "
                        "Awali dengan [EMO:soft]. DILARANG menulis JSON atau membocorkan jawaban benar secara utuh."
                    )
                    speak_fast_coaching(
                        wrong_prompt,
                        default_emo="soft",
                        fallback_msg="Pilihan itu struktur tata bahasanya masih kurang tepat, Zak. Coba pilih kartu jawaban lain ya!",
                        max_tokens=140,
                    )
                continue

            # 0C-2. Stage 1 -> Stage 2 (SPEAKING_STAGE): User mengklik opsi BENAR ->
            # Pause 500ms, tampilkan 1 kartu jawaban benar, lalu berikan Grammar Breakdown + Pronunciation Guide + baca keras!
            if user_input.startswith("[SYS_QUIZ_CORRECT_CLICK:"):
                quiz_now = teach_language.active_quiz
                if quiz_now:
                    time.sleep(0.5)
                    corr_opt = teach_language.enter_speaking_stage(bridge)
                    corr_text = corr_opt.get("text", "")
                    corr_native = corr_opt.get("native", corr_text)
                    question_now = quiz_now.get("question", "")
                    lang_label = teach_language.LANGUAGE_NAMES.get(teach_language.target_lang, teach_language.target_lang)

                    # Highlight ikon speaker di kartu tunggal saat Tuzi menyampaikan contoh pengucapan
                    if teach_language.ui_submode != "chat":
                        bridge.quiz_state_signal.emit(json.dumps({
                            "optionIndex": 0,
                            "wordStates": [],
                            "boxState": "",
                            "highlightAssist": True,
                        }))

                    stage2_prompt = (
                        f"[STAGE 2 COACHING — GRAMMAR & PRONUNCIATION | Bahasa: {lang_label} | Level: {teach_language.user_level}]\n"
                        f"Pertanyaan awal: \"{question_now}\"\n"
                        f"Jawaban BENAR yang dipilih Zak: \"{corr_text}\" (skrip asli: {corr_native})\n"
                        "Tugas (abaikan batas 25 kata khusus sesi coaching ini, tulis 2-3 kalimat padat total 30-50 kata, 1 paragraf lurus):\n"
                        "1. Puji singkat pilihan Zak yang benar untuk pertanyaan tersebut.\n"
                        "2. GRAMMAR BREAKDOWN: Jelaskan singkat alasan struktur/partikel/tense pada kalimat itu tepat.\n"
                        "3. PRONUNCIATION GUIDE: Beri 1 tips jelas cara melafalkan kata kunci / intonasinya.\n"
                        f"4. Akhiri dengan membacakan kalimat target persis ini agar Zak menirukan: \"{corr_text}\"\n"
                        "Awali dengan [EMO:excited]. DILARANG pakai emoji atau JSON."
                    )
                    speak_fast_coaching(
                        stage2_prompt,
                        default_emo="excited",
                        fallback_msg=f"Tepat sekali, Zak! Struktur kalimat ini sudah benar. Sekarang tahan avatar dan ikuti cara bacaku: {corr_text}",
                        required_sentence=corr_text,
                        max_tokens=260,
                    )
                continue

            pending_quiz_question = None

            # 0E-0. Abaikan sinyal [TEACH_UNCLEAR] liar bila tidak sedang di Stage 2 (SPEAKING_STAGE)
            if user_input == "[TEACH_UNCLEAR]" and not (
                teach_language.active_quiz
                and teach_language.quiz_stage == teach_language.STAGE_SPEAKING
                and teach_language.ui_submode == "live"
            ):
                presence.set_state("idle")
                continue

            # 0D. Memulai Kuis Pertama Sesuai Bahasa & Level Pilihan User (Stage 1: CHOICE_STAGE, Batch Pre-Fetch 6 Soal)
            if user_input.startswith("[SYS_ONBOARD_LEVEL:"):
                chosen_lvl = user_input.replace("[SYS_ONBOARD_LEVEL:", "").replace("]", "").strip()
                teach_language.user_level = chosen_lvl
                lang_label = teach_language.LANGUAGE_NAMES.get(teach_language.target_lang, teach_language.target_lang)

                # Pastikan quiz_buffer berisi batch 6 soal untuk (target_lang, chosen_lvl, custom_intelligence_prompt)
                teach_language.ensure_quiz_batch_for_config(
                    teach_language.target_lang, chosen_lvl, custom_intelligence_prompt
                )
                # Pop soal pertama segera begitu siap & langsung render ke Stage 1 UI (0 ms delay jika sudah ter-buffer)
                first_quiz = teach_language.activate_first_quiz_when_ready(bridge, timeout=5.0)
                pending_quiz_question = (first_quiz or {}).get("question", "")
                if pending_quiz_question and teach_language.ui_submode != "chat":
                    bridge.subtitle_timed_signal.emit(pending_quiz_question, 0)

                chat_history = [{"role": "system", "content": get_active_system_prompt()}]
                user_input = (
                    f"[TEACH_INPUT] Aku siap mulai latihan {lang_label} level {chosen_lvl}. "
                    f"Sapa aku maksimal 5 kata lalu ajukan pertanyaan pilihan ganda pertama ini persis: \"{pending_quiz_question}\""
                )

            # 0E. Stage 2 (SPEAKING_STAGE): Evaluasi Pelafalan Suara-ke-Suara pada 1 Kartu Jawaban Benar
            elif (
                teach_language.active_quiz
                and teach_language.quiz_stage == teach_language.STAGE_SPEAKING
                and teach_language.ui_submode == "live"
                and (user_input.startswith("[TEACH_INPUT]") or user_input == "[TEACH_UNCLEAR]")
            ):
                spoken_attempt = (
                    "" if user_input == "[TEACH_UNCLEAR]" else user_input.replace("[TEACH_INPUT]", "").strip()
                )
                eval_res = teach_language.evaluate_speaking_attempt(spoken_attempt, bridge)
                status = eval_res["status"]
                correct_text = eval_res["correct_text"]
                correct_native = eval_res["correct_native"]

                if status in ("PARTIAL", "UNCLEAR", "MISPRONOUNCED"):
                    # Tetap terkunci di SPEAKING_STAGE pada kalimat yang sama -> Re-explain pelafalan & grammar lalu baca ulang!
                    lang_label = teach_language.LANGUAGE_NAMES.get(teach_language.target_lang, teach_language.target_lang)
                    missed_str = ", ".join(eval_res.get("missed_words", [])) or correct_text
                    detected_str = ", ".join(eval_res.get("detected_words", []))
                    retry_prompt = (
                        f"[STAGE 2 PRONUNCIATION RETRY COACHING | Bahasa: {lang_label} | Status: {status}]\n"
                        f"Kalimat target: \"{correct_text}\" ({correct_native})\n"
                        f"Yang terdengar dari Zak: \"{spoken_attempt or '(tidak terdengar jelas)'}\"\n"
                        f"Kata yang sudah benar: \"{detected_str or 'belum ada'}\" | Kata yang terlewat/kurang pas: \"{missed_str}\"\n"
                        "Tugas (2 kalimat ramah, total 22-40 kata, 1 baris):\n"
                        "1. Semangati Zak, jelaskan ulang singkat cara melafalkan kata yang terlewat/kurang pas tersebut beserta pengingat grammar-nya.\n"
                        f"2. Bacakan kembali kalimat lengkap \"{correct_text}\" agar Zak mengulanginya.\n"
                        "Awali dengan [EMO:soft]. DILARANG pindah soal, DILARANG pakai emoji/JSON."
                    )
                    speak_fast_coaching(
                        retry_prompt,
                        default_emo="soft",
                        fallback_msg=eval_res["spoken_assist"],
                        required_sentence=correct_text,
                        max_tokens=220,
                    )
                    continue

                elif status == "PASS":
                    # Pelafalan lulus (>= 82%, semua kata biru!) ->
                    # Pop soal berikutnya dari quiz_buffer (0 ms) & reset ke Stage 1 (CHOICE_STAGE)!
                    time.sleep(0.45)
                    next_quiz = teach_language.pop_next_quiz_and_emit(bridge)
                    pending_quiz_question = (next_quiz or {}).get("question", "")
                    if pending_quiz_question and teach_language.ui_submode != "chat":
                        bridge.subtitle_timed_signal.emit(pending_quiz_question, 0)

                    user_input = (
                        f"[TEACH_INPUT] Pelafalanku untuk '{correct_text}' baru saja lulus 100%! "
                        f"Beri pujian 1 kalimat super singkat (maks 7 kata), lalu ajukan pertanyaan pilihan ganda berikutnya ini persis: "
                        f"\"{pending_quiz_question}\""
                    )

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
                teach_language.custom_personality = raw_custom
                teach_language.refill_quiz_buffer_async(
                    teach_language.target_lang, teach_language.user_level, raw_custom, clear_first=True
                )
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

            presence.set_state("thinking")

            # 5. Pencarian Internet (Hanya untuk chat bebas agar transisi kuis tetap < 0.8s)
            if ai_tools and not pending_quiz_question:
                try:
                    tool_context = ai_tools.get_tools_context(user_input)
                except Exception as e:
                    print(f"[ai_tools] Pencarian internet gagal, lanjut tanpa data internet -> {type(e).__name__}: {e}")
                    tool_context = ""
                if tool_context:
                    user_input += f"\n\n{tool_context}"

            chat_history.append({"role": "user", "content": user_input})
            if len(chat_history) > 11:
                chat_history = [chat_history[0]] + chat_history[-10:]

            t_llm_start = time.perf_counter()
            response = llm_complete(
                chat_history,
                temperature=0.68,
                max_tokens=180,
            )
            llm_ms = (time.perf_counter() - t_llm_start) * 1000
            raw_output = (response.choices[0].message.content or "").strip()
            raw_output = re.sub(r"<think>.*?</think>", "", raw_output, flags=re.S).strip()
            print(f"[Groq FastChat] Respons LLM siap dalam {llm_ms:.0f} ms")

            chat_history.append({"role": "assistant", "content": raw_output})
            save_memory(chat_history)

            emotion, display_dialogue, spoken_dialogue = extract_emotion_and_text(raw_output)

            # Pastikan pertanyaan dari quiz_buffer selalu selaras dengan kotak jawaban di layar
            if pending_quiz_question:
                norm_q = re.sub(r"\W+", "", pending_quiz_question.lower())
                norm_d = re.sub(r"\W+", "", display_dialogue.lower())
                if norm_q and norm_q[:min(18, len(norm_q))] not in norm_d:
                    display_dialogue = f"{display_dialogue} {pending_quiz_question}".strip()
                    spoken_dialogue = f"{spoken_dialogue} {pending_quiz_question}".strip()

            is_chat_mute_mode = teach_language.is_active and (teach_language.ui_submode == "chat")

            if not is_chat_mute_mode:
                bridge.expression_signal.emit(emotion)

            if len(display_dialogue) > 1:
                if not is_chat_mute_mode:
                    with speech_lock:
                        loop.run_until_complete(
                            tts_engine.speak_with_lipsync(
                                spoken_dialogue, emotion=emotion, subtitle=display_dialogue
                            )
                        )
                else:
                    bridge.subtitle_timed_signal.emit(display_dialogue, 0)
                    print("[Sistem] Mode Chat Mute aktif — ElevenLabs & suara di-bypass.")

            if not is_chat_mute_mode:
                bridge.expression_signal.emit("natural")

            presence.set_state("idle")

        except Exception as e:
            print(f"\n[ERROR Tuzi AI] Gagal memproses pesan: {e}")
            presence.set_state("idle")
            time.sleep(2)


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    teach_language.main_input_queue = input_queue

    server_thread = threading.Thread(target=start_local_server, daemon=True)
    server_thread.start()

    app = QApplication(sys.argv)
    bridge = AvatarSignalBridge()
    teach_language.bridge_ref = bridge
    window = TransparentAvatarWindow(bridge)
    window.show()

    tts = ElevenLabsTTSEngine(bridge)

    teach_language.presence_callback = presence.set_state
    presence.start()

    keyboard_thread = threading.Thread(target=keyboard_input_loop, daemon=True)
    keyboard_thread.start()

    processor_thread = threading.Thread(
        target=chat_processor_loop, args=(bridge, tts), daemon=True
    )
    processor_thread.start()

    sys.exit(app.exec_())