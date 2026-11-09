import io
import json
import os
import re
import threading
import wave
import audioop
import pygame
import speech_recognition as sr
from PyQt5.QtCore import Qt, QPoint, QFile, QIODevice
from PyQt5.QtWidgets import QApplication, QMainWindow

import config

# Opsional: Modul Noise Suppression (noisereduce & numpy)
try:
    import numpy as np
    import noisereduce as nr
    HAS_NOISEREDUCE = True
except ImportError:
    HAS_NOISEREDUCE = False

# Opsional: Model STT Whisper Large V3 Turbo via Groq
try:
    from groq import Groq
    _groq_key = getattr(config, "GROQ_API_KEY", "")
    groq_stt_client = (
        Groq(api_key=_groq_key)
        if (_groq_key and not _groq_key.startswith("MASUKKAN"))
        else None
    )
except ImportError:
    groq_stt_client = None

# Langsung aktif sejak aplikasi dijalankan
is_active = True
is_fullscreen = False        # False = Mode Kecil/Minimize (1600x900), True = Full Screen (1920x1080)
ui_submode = "live"          # "live" (Voice Mic-to-Mic) atau "chat" (Text Chatbot - Total Mute)
current_gender = "male"      # Default: "male" (Biru) atau "female" (Pink)
current_female_mode = 1      # 1 = Dandere, 2 = Shy, 3 = Mommy
custom_personality = ""      # Menyimpan instruksi Personal Intelligence dari user
target_lang = "en-US"        # en-US, ja-JP, id-ID, ko-KR, zh-CN
user_level = "Beginner"      # Beginner, Elementary, Intermediate, Advanced, Expert
selected_mic_index = getattr(config, "MIC_DEVICE_INDEX", None)
main_input_queue = None
main_window_ref = None

LANGUAGE_NAMES = {
    "id-ID": "Indonesian (Bahasa Indonesia)",
    "ja-JP": "Japanese (Bahasa Jepang)",
    "en-US": "English (Bahasa Inggris)",
    "ko-KR": "Korean (Bahasa Korea)",
    "zh-CN": "Chinese Mandarin (Bahasa Mandarin)",
}

LEVEL_PEDAGOGY = {
    "Beginner": (
        "LEVEL: BEGINNER (A1). Teach from zero using guided repetition. "
        "Explain briefly in Indonesian/English, give Zak 1 simple word or short daily phrase in the target language "
        "(include romaji/pinyin if Japanese/Korean/Chinese), and ask him to repeat it."
    ),
    "Elementary": (
        "LEVEL: ELEMENTARY (A2). Use simple daily Q&A (hobbies, food, daily routine). "
        "Mix simple target language sentences with brief explanations, and gently correct basic grammar."
    ),
    "Intermediate": (
        "LEVEL: INTERMEDIATE (B1-B2). Use 85-90% target language in a fun real-life roleplay scenario "
        "(e.g. ordering at a cafe, traveling, casual hangout). Correct unnatural phrasing briefly first."
    ),
    "Advanced": (
        "LEVEL: ADVANCED (C1). Speak 100% in the target language. Discuss interesting topics (movies, tech, daily life), "
        "introduce natural idioms/slang, and refine subtle grammar or word choices."
    ),
    "Expert": (
        "LEVEL: EXPERT (C2). Speak 100% in the target language at natural native pace with authentic expressions/slang. "
        "Challenge Zak with engaging follow-up questions and polish any minor nuance errors."
    ),
}

# Diisi oleh run_tuzi.py -> dipakai untuk Discord Rich Presence
presence_callback = None


def _set_presence(state: str):
    if presence_callback:
        try:
            presence_callback(state)
        except Exception:
            pass


# Ukuran Mode Kecil / Minimize (1600x900) & Mode Full Screen (1920x1080)
TEACH_WIN_WIDTH = 1600
TEACH_WIN_HEIGHT = 900
FULLSCREEN_WIN_WIDTH = 1920
FULLSCREEN_WIN_HEIGHT = 1080

# Ukuran Asli Mode Overlay Desktop
DEFAULT_WIN_WIDTH = 600
DEFAULT_WIN_HEIGHT = 1300


def get_available_microphones() -> list:
    """Mengambil daftar mikrofon yang terdeteksi di perangkat Windows."""
    mic_list = []
    try:
        names = sr.Microphone.list_microphone_names()
        for idx, name in enumerate(names):
            lower_n = name.lower()
            if any(k in lower_n for k in ["output", "speaker", "headphone", "mapper - output"]):
                continue
            mic_list.append({"index": idx, "name": name})
    except Exception as e:
        print(f"[MIC WARNING] Gagal memuat daftar mikrofon: {e}")
    return mic_list


def clean_voice_audio(raw_pcm: bytes, sample_rate: int = 16000) -> bytes:
    """
    Membersihkan rekaman suara dengan Noise Suppression (Spectral Gating)
    dan Auto-Gain Normalization agar suara manusia terdengar jernih.
    """
    if not raw_pcm:
        return raw_pcm

    try:
        if HAS_NOISEREDUCE:
            audio_np = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32)
            audio_np = audio_np - np.mean(audio_np)

            if len(audio_np) > sample_rate * 0.25:
                reduced_np = nr.reduce_noise(
                    y=audio_np,
                    sr=sample_rate,
                    prop_decrease=0.82,
                    stationary=True
                )
            else:
                reduced_np = audio_np

            peak = np.max(np.abs(reduced_np))
            if peak > 150:
                target_peak = 24000.0
                gain = min(6.0, target_peak / peak)
                reduced_np = np.clip(reduced_np * gain, -32767, 32767)

            return reduced_np.astype(np.int16).tobytes()

        else:
            rms = audioop.rms(raw_pcm, 2)
            if 100 < rms < 4000:
                factor = min(4.5, 4500.0 / rms)
                raw_pcm = audioop.mul(raw_pcm, 2, factor)
            return raw_pcm
    except Exception as e:
        print(f"[Noise Suppression Warning] {e}")
        return raw_pcm


def stop_all_voice_playback():
    """Menghentikan suara Tuzi secara instan."""
    try:
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
    except Exception:
        pass


def get_qwebchannel_js() -> str:
    """Membaca qwebchannel.js langsung dari resource internal PyQt5."""
    qfile = QFile(":/qtwebchannel/qwebchannel.js")
    if qfile.open(QIODevice.ReadOnly):
        content = bytes(qfile.readAll()).decode("utf-8", errors="ignore")
        qfile.close()
        return content
    return ""


def get_main_window():
    """Mengambil referensi window utama PyQt5."""
    global main_window_ref
    if main_window_ref is not None:
        return main_window_ref
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QMainWindow):
            main_window_ref = widget
            return widget
    return None


def set_fullscreen_state(fullscreen: bool):
    """Mengubah ukuran aplikasi antara Mode Minimize/Kecil (1600x900) dan Full Screen (1920x1080)."""
    global is_fullscreen
    win = get_main_window()
    if not win:
        return

    is_fullscreen = fullscreen
    screen_geo = QApplication.primaryScreen().geometry()

    if is_fullscreen:
        win.resize(FULLSCREEN_WIN_WIDTH, FULLSCREEN_WIN_HEIGHT)
        center_x = (screen_geo.width() - FULLSCREEN_WIN_WIDTH) // 2
        center_y = (screen_geo.height() - FULLSCREEN_WIN_HEIGHT) // 2
        win.move(QPoint(max(0, center_x), max(0, center_y)))
        win.webview.page().runJavaScript(
            "document.body.classList.add('fullscreen-mode');"
        )
        print("\n[TUZI UI] Masuk ke Mode Full Screen (1920x1080). Tekan F11 untuk keluar.")
    else:
        win.resize(TEACH_WIN_WIDTH, TEACH_WIN_HEIGHT)
        center_x = (screen_geo.width() - TEACH_WIN_WIDTH) // 2
        center_y = (screen_geo.height() - TEACH_WIN_HEIGHT) // 2
        win.move(QPoint(center_x, center_y))
        win.webview.page().runJavaScript(
            "document.body.classList.remove('fullscreen-mode');"
        )
        print("\n[TUZI UI] Kembali ke Mode Minimize / Windowed (1600x900).")


class HoldToTalkRecorder:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.energy_threshold = 250
        self.is_recording = False
        self.frames = []
        self.lang = "en-US"

    def start_recording(self, payload="en-US"):
        global target_lang, user_level, ui_submode, current_gender, current_female_mode, custom_personality, selected_mic_index

        # 1. Perintah Keluar Instan dari Tombol Hang-up Merah
        if payload == "CMD:EXIT":
            print("\n[Sistem] Tombol Hang-up ditekan. Menutup Aplikasi Tuzi 1.0...")
            os._exit(0)

        # 2. Perintah Full Screen (1920x1080) & Minimize/Windowed (1600x900) via Tombol UI / F11
        if payload == "WIN:FULLSCREEN":
            set_fullscreen_state(True)
            return
        elif payload == "WIN:MINIMIZE":
            set_fullscreen_state(False)
            return
        elif payload == "WIN:TOGGLE_F11":
            set_fullscreen_state(not is_fullscreen)
            return

        # 3. Perintah Onboarding: Sapaan Awal -> Pilih Bahasa -> Pilih Level
        if payload == "ONBOARD:START":
            if main_input_queue:
                main_input_queue.put("[SYS_ONBOARD_GREET]")
            return
        elif payload.startswith("ONBOARD:LANG:"):
            chosen_lang = payload.split(":", 2)[2].strip()
            target_lang = chosen_lang
            self.lang = chosen_lang
            stop_all_voice_playback()
            print(f"\n[TUZI ONBOARDING] Bahasa dipilih: {LANGUAGE_NAMES.get(target_lang, target_lang)}")
            if main_input_queue:
                main_input_queue.put(f"[SYS_ONBOARD_LANG:{target_lang}]")
            return
        elif payload.startswith("ONBOARD:LEVEL:"):
            chosen_level = payload.split(":", 2)[2].strip()
            user_level = chosen_level
            stop_all_voice_playback()
            print(f"\n[TUZI ONBOARDING] Level dipilih: {user_level} ({target_lang})")
            if main_input_queue:
                main_input_queue.put(f"[SYS_ONBOARD_LEVEL:{user_level}]")
            return

        # 4. Perintah Pindah Tab (LIVE / CHAT)
        if payload == "MODE:LIVE":
            ui_submode = "live"
            print("\n[TUZI UI] Beralih ke tab LIVE (Voice Mode Aktif).")
            return
        elif payload == "MODE:CHAT":
            ui_submode = "chat"
            stop_all_voice_playback()
            print("\n[TUZI UI] Beralih ke tab CHAT (Total Mute Aktif).")
            return

        # 5. Perintah Pilih Perangkat Mikrofon dari Menu Settings
        if payload.startswith("MIC_DEV:"):
            val = payload.split(":")[1].strip()
            if val == "DEFAULT" or val == "":
                selected_mic_index = None
                print("\n[TUZI MIC] Menggunakan Mikrofon Default Sistem.")
            else:
                try:
                    selected_mic_index = int(val)
                    print(f"\n[TUZI MIC] Beralih ke Mikrofon Device Index #{selected_mic_index}")
                except ValueError:
                    selected_mic_index = None
            return

        # 6. Perintah Swap Gender dari Settings (MALE / FEMALE)
        if payload.startswith("GENDER:"):
            new_gender = payload.split(":")[1].lower()
            current_gender = new_gender
            print(f"\n[TUZI SETTINGS] Gender diubah ke: {current_gender.upper()}")
            if main_input_queue:
                main_input_queue.put(f"[SYS_GENDER:{current_gender}]")
            return

        # 7. Perintah Ganti Tipe Suara Female (1=Dandere, 2=Shy, 3=Mommy)
        if payload.startswith("FMODE:"):
            try:
                fmode = int(payload.split(":")[1])
                current_female_mode = fmode
                print(f"\n[TUZI SETTINGS] Voice Female diatur ke Mode {fmode}")
                if main_input_queue:
                    main_input_queue.put(f"[SYS_FMODE:{fmode}]")
            except ValueError:
                pass
            return

        # 8. Perintah Personal Intelligence (Custom Personality dari User)
        if payload.startswith("CUSTOM_PROMPT:"):
            custom_text = payload[len("CUSTOM_PROMPT:"):].strip()
            custom_personality = custom_text
            if custom_personality:
                print(f"\n[Personal Intelligence] Kepribadian kustom aktif: {custom_personality}")
            else:
                print("\n[Personal Intelligence] Kepribadian dikembalikan ke Default.")
            if main_input_queue:
                main_input_queue.put(f"[SYS_CUSTOM_PROMPT:{custom_text}]")
            return

        # 9. Pesan Teks dari Mode Chatbot (Total Mute)
        if payload.startswith("CHAT:"):
            chat_text = payload[5:].strip()
            if chat_text and main_input_queue:
                ui_submode = "chat"
                stop_all_voice_playback()
                print(f"\n[TEACH CHAT - MUTE] Zak: {chat_text}")
                main_input_queue.put(f"[TEACH_INPUT] {chat_text}")
            return

        # 10. Rekaman Suara Push-to-Talk pada Avatar Blob (Hanya di Mode Live)
        if ui_submode == "chat" or self.is_recording:
            return
        self.lang = payload
        target_lang = payload
        self.is_recording = True
        self.frames = []
        _set_presence("listening")
        threading.Thread(target=self._record_loop, daemon=True).start()

    def _transcribe_audio(self, cleaned_pcm: bytes, sample_rate: int, sample_width: int) -> str:
        """
        Menerjemahkan rekaman suara menggunakan Whisper Large V3 Turbo (jika GROQ_API_KEY aktif)
        atau Google STT dengan audio yang sudah dibersihkan oleh Noise Suppression.
        """
        if groq_stt_client is not None:
            try:
                wav_buf = io.BytesIO()
                with wave.open(wav_buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(sample_width)
                    wf.setframerate(sample_rate)
                    wf.writeframes(cleaned_pcm)
                wav_buf.seek(0)

                iso_lang = self.lang.split("-")[0].lower()  # "en", "ja", "id", "ko", "zh"
                transcription = groq_stt_client.audio.transcriptions.create(
                    file=("speech.wav", wav_buf.read()),
                    model="whisper-large-v3-turbo",
                    language=iso_lang,
                    temperature=0.0,
                )
                if transcription and transcription.text:
                    return transcription.text.strip()
            except Exception as e:
                print(f"[Whisper STT Fallback ke Google] {e}")

        audio_data = sr.AudioData(cleaned_pcm, sample_rate, sample_width)
        return self.recognizer.recognize_google(audio_data, language=self.lang)

    def _record_loop(self):
        queued = False
        try:
            stop_all_voice_playback()

            mic_kwargs = {"sample_rate": 16000, "chunk_size": 1024}
            if selected_mic_index is not None:
                mic_kwargs["device_index"] = selected_mic_index

            with sr.Microphone(**mic_kwargs) as source:
                sample_rate = source.SAMPLE_RATE
                sample_width = source.SAMPLE_WIDTH
                print(f"\n[TEACH MIC - {self.lang}] Mendengarkan... (Silakan bicara)")

                min_chunks = 6
                while self.is_recording or len(self.frames) < min_chunks:
                    buffer = source.stream.read(source.CHUNK)
                    self.frames.append(buffer)
                    if not self.is_recording and len(self.frames) >= min_chunks:
                        break

            raw_data = b"".join(self.frames)
            if len(raw_data) < 2048 or ui_submode != "live":
                return

            rms_level = audioop.rms(raw_data, sample_width)
            print(f"[TEACH MIC] Memproses suara Zak (Level Input Mic: {rms_level})...")
            if rms_level < 25:
                print("[TEACH MIC] Peringatan: Sinyal mikrofon sangat kecil/kosong. Cek pilihan Mic di menu Settings!")

            cleaned_pcm = clean_voice_audio(raw_data, sample_rate=sample_rate)

            text = self._transcribe_audio(cleaned_pcm, sample_rate, sample_width)
            if text and text.strip() and main_input_queue:
                print(f"[TEACH MIC] Zak: {text}")
                main_input_queue.put(f"[TEACH_INPUT] {text.strip()}")
                queued = True
                _set_presence("thinking")

        except sr.UnknownValueError:
            print("[TEACH MIC] Suara tidak terdengar jelas. Coba bicara lebih dekat atau ganti Mic di Settings.")
        except Exception as e:
            print(f"[TEACH MIC Error] Gagal mengakses mikrofon: {e}")
        finally:
            if not queued:
                _set_presence("idle")

    def stop_recording(self):
        self.is_recording = False


ptt_recorder = HoldToTalkRecorder()


def handle_teach_mode(user_input: str, bridge):
    """Menangani input selama aplikasi Tuzi 1.0 berjalan & menyuntikkan konteks Bahasa + Level."""
    global is_active, custom_personality

    if user_input.startswith("[SYS_CUSTOM_PROMPT:"):
        custom_personality = user_input[len("[SYS_CUSTOM_PROMPT:"):-1].strip()
        return None

    is_teach_input = False
    if user_input.startswith("[TEACH_INPUT]"):
        is_teach_input = True
        user_input = user_input.replace("[TEACH_INPUT]", "").strip()

    exit_trigger = re.search(
        r"\b(exit\s+teach|keluar\s+mode\s+belajar|stop\s+belajar|tutup\s+mode\s+belajar)\b",
        user_input.lower()
    )

    if exit_trigger:
        print("\n[Sistem] Menutup Aplikasi Tuzi 1.0...")
        os._exit(0)

    if is_active and is_teach_input:
        lang_label = LANGUAGE_NAMES.get(target_lang, target_lang)
        level_guide = LEVEL_PEDAGOGY.get(user_level, LEVEL_PEDAGOGY["Beginner"])
        persona_rule = (
            f"IMPORTANT PERSONAL INTELLIGENCE OVERRIDE: You MUST adopt this exact attitude and speaking style: '{custom_personality}'. "
            if custom_personality
            else "Keep your current persona mode. "
        )
        user_input += (
            f"\n\n[SYSTEM INFO - LANGUAGE TUTOR ACTIVE | Target Language: {lang_label} | Proficiency: {user_level}: "
            f"1. {persona_rule}"
            f"2. {level_guide} "
            "3. If Zak's grammar or phrasing is unnatural, briefly correct it first. "
            "4. Keep your reply concise, natural, and under 22 words!]"
        )

    return user_input


def apply_window_state(window, active: bool):
    """Mengatur ukuran jendela ke 1600x900 di tengah layar."""
    global main_window_ref
    main_window_ref = window
    screen_geo = QApplication.primaryScreen().geometry()
    if hasattr(window, "anim"):
        window.anim.stop()

    if active:
        window.webview.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        window.resize(TEACH_WIN_WIDTH, TEACH_WIN_HEIGHT)
        center_x = (screen_geo.width() - TEACH_WIN_WIDTH) // 2
        center_y = (screen_geo.height() - TEACH_WIN_HEIGHT) // 2
        window.move(QPoint(center_x, center_y))
        window.webview.page().runJavaScript("if(window.setTeachModeUI) setTeachModeUI(true);")
    else:
        window.webview.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        window.resize(DEFAULT_WIN_WIDTH, DEFAULT_WIN_HEIGHT)
        right_x = screen_geo.width() - DEFAULT_WIN_WIDTH
        right_y = screen_geo.height() - DEFAULT_WIN_HEIGHT - 45
        window.move(QPoint(right_x, right_y))
        window.webview.page().runJavaScript("if(window.setTeachModeUI) setTeachModeUI(false);")


def get_ui_injection_js() -> str:
    """Menyuntikkan UI dengan Pop-Up Bubble Choice Box 2 Baris (Dengan Bendera) & Anti-Null Guard."""
    qwebchannel_code = get_qwebchannel_js()
    mics_json = json.dumps(get_available_microphones())

    ui_code = """
    (function() {
        function initTuziTeachUI() {
            if (!document.body || !document.head) {
                setTimeout(initTuziTeachUI, 50);
                return;
            }

            // Hapus overlay lama jika sempat tersisa agar tidak konflik
            let oldOverlay = document.getElementById('tuzi-teach-overlay');
            if (oldOverlay) oldOverlay.remove();

            const AVAILABLE_MICS = __MICS_JSON__;

            let style = document.getElementById('tuzi-teach-style');
            if (!style) {
                style = document.createElement('style');
                style.id = 'tuzi-teach-style';
                document.head.appendChild(style);
            }

            style.innerHTML = `
                /* === TEMA DASAR: LIGHT MODE (DEFAULT) & PITCH BLACK DARK MODE === */
                body.teach-mode-active {
                    background: #F4F5F8 !important;
                    border: 3px solid #0D0D0D !important;
                    border-radius: 28px !important;
                    overflow: hidden !important;
                    box-sizing: border-box !important;
                    transition: background 0.25s ease, border-color 0.25s ease;
                }
                body.teach-mode-active.dark-mode {
                    background: #000000 !important;
                    border-color: #222222 !important;
                }
                body.teach-mode-active.fullscreen-mode {
                    border-radius: 0px !important;
                    border: none !important;
                }

                /* Pastikan kanvas VTuber selalu disembunyikan saat Teach Mode aktif */
                body.teach-mode-active #canvas,
                body.teach-mode-active canvas {
                    display: none !important;
                }
                body.teach-mode-active.chat-tab-open #tuzi-subtitle-box,
                body.teach-mode-active.chat-tab-open #ai-blob-stage,
                body.teach-mode-active.chat-tab-open #tuzi-choice-container {
                    display: none !important;
                }

                /* === SUBTITLE TANPA BACKGROUND DI BAWAH AVATAR (DINAIKKAN KE ATAS) === */
                body.teach-mode-active #tuzi-subtitle-box {
                    position: fixed !important;
                    top: calc(50% + 88px) !important;
                    left: 50% !important;
                    transform: translateX(-50%) !important;
                    background: transparent !important;
                    backdrop-filter: none !important;
                    -webkit-backdrop-filter: none !important;
                    border: none !important;
                    box-shadow: none !important;
                    color: #0D0D0D !important;
                    text-shadow: none !important;
                    font-size: 19.5px !important;
                    font-weight: 600 !important;
                    line-height: 1.5 !important;
                    padding: 0 24px !important;
                    max-width: 780px !important;
                    z-index: 999992 !important;
                    transition: top 0.45s cubic-bezier(0.34, 1.56, 0.64, 1), color 0.25s ease !important;
                }
                body.teach-mode-active.dark-mode #tuzi-subtitle-box { color: #FFFFFF !important; }

                /* Saat Box Pilihan Muncul: Subtitle naik ke tengah tepat di bawah Avatar */
                body.teach-mode-active.choices-active #tuzi-subtitle-box {
                    top: calc(50% - 18px) !important;
                }

                #tuzi-teach-overlay {
                    display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
                    pointer-events: none; z-index: 999990;
                    font-family: 'Segoe UI', -apple-system, sans-serif; box-sizing: border-box;
                }
                body.teach-mode-active #tuzi-teach-overlay {
                    display: flex; flex-direction: column; justify-content: space-between; align-items: center;
                }

                /* === PANGGUNG AVATAR AI BLOB (DINAIKKAN KE ATAS & MENGECIL SAAT BOX MUNCUL) === */
                #ai-blob-stage {
                    pointer-events: auto; position: fixed; top: 39%; left: 50%;
                    transform: translate(-50%, -50%) scale(1);
                    display: flex; flex-direction: column;
                    align-items: center; justify-content: center; z-index: 999991; user-select: none;
                    transition: transform 0.48s cubic-bezier(0.34, 1.56, 0.64, 1), top 0.48s cubic-bezier(0.34, 1.56, 0.64, 1);
                }
                body.teach-mode-active.choices-active #ai-blob-stage {
                    top: 27%;
                    transform: translate(-50%, -50%) scale(0.80);
                }

                /* === CONTAINER & BUBBLE BOX PILIHAN BAHASA / LEVEL (2 BARIS: 3 ATAS, 2 BAWAH) === */
                #tuzi-choice-container {
                    pointer-events: auto;
                    position: fixed;
                    top: calc(50% + 42px);
                    left: 50%;
                    transform: translateX(-50%);
                    display: none;
                    flex-wrap: wrap;
                    align-items: center;
                    justify-content: center;
                    width: 92%;
                    max-width: 680px;
                    z-index: 999994;
                    box-sizing: border-box;
                }
                #tuzi-choice-container.show {
                    display: flex;
                }

                .tuzi-choice-box {
                    min-width: 184px;
                    height: 62px;
                    padding: 0 28px;
                    margin: 11px 14px !important;
                    background: #FFFFFF;
                    border: 1.5px solid rgba(156, 163, 175, 0.42);
                    border-radius: 20px;
                    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
                    color: #6B7280;
                    font-size: 16px;
                    font-weight: 600;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    text-align: center;
                    cursor: pointer;
                    user-select: none;
                    outline: none;
                    opacity: 0;
                    transform: translateY(18px) scale(0.75);
                    animation: bubblePopUp 0.46s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
                    transition:
                        transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1),
                        box-shadow 0.2s ease,
                        border-color 0.2s ease,
                        color 0.2s ease;
                }
                .tuzi-choice-box:hover {
                    transform: translateY(-4px) scale(1.05);
                    border-color: rgba(107, 114, 128, 0.68);
                    box-shadow: 0 14px 30px rgba(0, 0, 0, 0.11);
                    color: #374151;
                }
                .tuzi-choice-box:active {
                    transform: scale(0.95);
                }

                .choice-flag-icon {
                    width: 24px;
                    height: 17px;
                    border-radius: 4px;
                    margin-right: 11px;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
                    flex-shrink: 0;
                    overflow: hidden;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                }

                @keyframes bubblePopUp {
                    0% {
                        opacity: 0;
                        transform: translateY(20px) scale(0.72);
                    }
                    70% {
                        opacity: 1;
                        transform: translateY(-3px) scale(1.03);
                    }
                    100% {
                        opacity: 1;
                        transform: translateY(0) scale(1);
                    }
                }

                /* === TOMBOL BURGER (KIRI ATAS) === */
                #tuzi-burger-btn {
                    pointer-events: auto; position: fixed; top: 24px; left: 24px;
                    width: 46px; height: 46px; border-radius: 14px;
                    background: #FFFFFF; color: #0D0D0D; border: 2px solid #0D0D0D;
                    cursor: pointer; display: flex; align-items: center; justify-content: center;
                    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.10); transition: all 0.2s ease;
                    z-index: 999995; outline: none;
                }
                #tuzi-burger-btn:hover { transform: scale(1.05); }
                body.teach-mode-active.dark-mode #tuzi-burger-btn {
                    background: #0D0D0D; color: #FFFFFF; border-color: #262626;
                }

                /* === TOMBOL FULLSCREEN / MINIMIZE & LIGHT/DARK MODE (KANAN ATAS) === */
                #tuzi-window-size-btn {
                    pointer-events: auto; position: fixed; top: 24px; right: 80px;
                    width: 46px; height: 46px; border-radius: 14px;
                    background: #FFFFFF; color: #0D0D0D; border: 2px solid #0D0D0D;
                    cursor: pointer; display: flex; align-items: center; justify-content: center;
                    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.10); transition: all 0.2s ease;
                    z-index: 999995; outline: none;
                }
                #tuzi-window-size-btn:hover { transform: scale(1.06); }
                body.teach-mode-active.dark-mode #tuzi-window-size-btn {
                    background: #0D0D0D; color: #FFFFFF; border-color: #262626;
                }
                body.teach-mode-active.fullscreen-mode #tuzi-window-size-btn {
                    display: none !important;
                }

                #tuzi-theme-btn {
                    pointer-events: auto; position: fixed; top: 24px; right: 24px;
                    width: 46px; height: 46px; border-radius: 14px;
                    background: #0D0D0D; color: #FFFFFF; border: 2px solid #0D0D0D;
                    cursor: pointer; display: flex; align-items: center; justify-content: center;
                    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14); transition: all 0.2s ease;
                    z-index: 999995; outline: none;
                }
                #tuzi-theme-btn:hover { transform: scale(1.06); }
                body.teach-mode-active.dark-mode #tuzi-theme-btn {
                    background: #0D0D0D; border-color: #262626;
                }
                #tuzi-theme-btn .sun-icon { display: none; color: #FFFFFF; }
                #tuzi-theme-btn .moon-icon { display: block; color: #FFFFFF; }
                body.teach-mode-active.dark-mode #tuzi-theme-btn .sun-icon { display: block; }
                body.teach-mode-active.dark-mode #tuzi-theme-btn .moon-icon { display: none; }

                #blob-ptt-hint {
                    margin-bottom: 12px; background: transparent; border: none;
                    color: #6B7280; opacity: 0.55; font-size: 14px; font-weight: 600;
                    letter-spacing: 0.8px; transition: all 0.25s ease; pointer-events: none;
                }
                body.teach-mode-active.dark-mode #blob-ptt-hint { color: #A3A3A3; }
                #ai-blob-stage.listening #blob-ptt-hint { color: #3F49CC; opacity: 0.9; }
                body.female-avatar #ai-blob-stage.listening #blob-ptt-hint { color: #EC4899; }

                #ai-blob-wrapper {
                    position: relative; width: 300px; height: 300px;
                    display: flex; align-items: center; justify-content: center;
                    cursor: pointer; animation: blobFloat 3.6s ease-in-out infinite;
                }
                @keyframes blobFloat {
                    0%, 100% { transform: translateY(0px); }
                    50% { transform: translateY(-12px); }
                }

                #ai-blob-body {
                    position: relative; width: 230px; height: 215px;
                    border-radius: 50% 50% 48% 52% / 50% 50% 50% 50%;
                    background: radial-gradient(circle at 38% 30%, #6875F5 0%, #3F49CC 58%, #2E36A8 100%);
                    box-shadow:
                        0 22px 45px rgba(55, 65, 195, 0.32),
                        inset -10px -14px 28px rgba(18, 22, 90, 0.45),
                        inset 12px 12px 24px rgba(155, 168, 255, 0.35);
                    transition:
                        border-radius 0.38s cubic-bezier(0.34, 1.56, 0.64, 1),
                        transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1),
                        background 0.35s ease, box-shadow 0.3s ease;
                    display: flex; align-items: center; justify-content: center; z-index: 3;
                }

                .blob-lobe {
                    position: absolute;
                    background: radial-gradient(circle at 40% 35%, #6A77F7 0%, #3E48C9 75%, #2E36A8 100%);
                    border-radius: 50%; opacity: 0; transform: scale(0.2);
                    transition: all 0.38s cubic-bezier(0.34, 1.56, 0.64, 1);
                    z-index: 2; pointer-events: none;
                }
                .blob-lobe.left { width: 96px; height: 82px; bottom: 24px; left: -28px; border-radius: 55% 45% 50% 50%; }
                .blob-lobe.right { width: 78px; height: 70px; bottom: 36px; right: -18px; border-radius: 45% 55% 50% 50%; }

                body.female-avatar #ai-blob-body {
                    background: radial-gradient(circle at 38% 30%, #FF94C2 0%, #EC4899 58%, #BE185D 100%);
                    box-shadow:
                        0 22px 45px rgba(236, 72, 153, 0.32),
                        inset -10px -14px 28px rgba(131, 24, 67, 0.45),
                        inset 12px 12px 24px rgba(255, 205, 228, 0.45);
                }
                body.female-avatar .blob-lobe {
                    background: radial-gradient(circle at 40% 35%, #FF94C2 0%, #EC4899 75%, #BE185D 100%);
                }

                #ai-blob-wrapper.listening #ai-blob-body {
                    width: 242px; height: 222px;
                    border-radius: 54% 46% 45% 55% / 58% 52% 48% 42%;
                    transform: rotate(-10deg) scale(1.06);
                    box-shadow:
                        0 0 0 12px rgba(92, 107, 245, 0.16),
                        0 28px 60px rgba(55, 65, 195, 0.48),
                        inset -10px -14px 28px rgba(18, 22, 90, 0.45),
                        inset 12px 12px 24px rgba(175, 188, 255, 0.45);
                }
                body.female-avatar #ai-blob-wrapper.listening #ai-blob-body {
                    box-shadow:
                        0 0 0 12px rgba(244, 114, 182, 0.20),
                        0 28px 60px rgba(236, 72, 153, 0.48),
                        inset -10px -14px 28px rgba(131, 24, 67, 0.45),
                        inset 12px 12px 24px rgba(255, 210, 232, 0.50);
                }
                #ai-blob-wrapper.listening .blob-lobe.left { opacity: 1; transform: scale(1) translate(-6px, 4px) rotate(-14deg); }
                #ai-blob-wrapper.listening .blob-lobe.right { opacity: 1; transform: scale(1) translate(6px, 2px) rotate(12deg); }

                .blob-eyes-container {
                    display: flex; align-items: center; justify-content: center;
                    transform: translate(12px, -16px);
                    transition: transform 0.28s cubic-bezier(0.25, 1, 0.5, 1); pointer-events: none;
                }
                #ai-blob-wrapper.listening .blob-eyes-container { transform: translate(24px, -24px) rotate(-4deg); }
                .blob-eye {
                    width: 24px; height: 54px; background: #F4F6FF; border-radius: 999px; margin: 0 11px;
                    box-shadow: 0 2px 10px rgba(15, 20, 80, 0.25);
                    transition: height 0.18s ease, width 0.18s ease, border-radius 0.2s ease, transform 0.2s ease;
                }
                .blob-eyes-container.blink .blob-eye { height: 6px !important; }
                .blob-eyes-container.emo-excited .blob-eye { height: 44px; width: 26px; border-radius: 50% 50% 25% 25%; transform: scaleY(1.08); }
                .blob-eyes-container.emo-soft .blob-eye { height: 34px; border-radius: 14px 14px 999px 999px; opacity: 0.92; }
                .blob-eyes-container.emo-angry .blob-eye.left-eye { height: 40px; transform: rotate(16deg); border-radius: 6px 20px 16px 16px; }
                .blob-eyes-container.emo-angry .blob-eye.right-eye { height: 40px; transform: rotate(-16deg); border-radius: 20px 6px 16px 16px; }

                .blob-ripple {
                    position: absolute; width: 230px; height: 230px; border-radius: 50%;
                    border: 3px solid rgba(88, 101, 242, 0.45); opacity: 0; pointer-events: none; z-index: 1;
                }
                body.female-avatar .blob-ripple { border-color: rgba(244, 114, 182, 0.50); }
                #ai-blob-wrapper.listening .blob-ripple.r1 { animation: rippleOut 1.6s infinite ease-out; }
                #ai-blob-wrapper.listening .blob-ripple.r2 { animation: rippleOut 1.6s infinite ease-out 0.8s; }
                @keyframes rippleOut {
                    0% { transform: scale(0.95); opacity: 0.85; }
                    100% { transform: scale(1.65); opacity: 0; }
                }

                .bubble-particle {
                    position: absolute; border-radius: 50%;
                    background: radial-gradient(circle at 30% 30%, #9BA6FF, #4D59E0);
                    opacity: 0; pointer-events: none; z-index: 4;
                }
                body.female-avatar .bubble-particle { background: radial-gradient(circle at 30% 30%, #FBCFE8, #EC4899); }
                @keyframes bubbleRise {
                    0% { transform: translate(0, 0) scale(0.4); opacity: 0.9; }
                    100% { transform: translate(var(--bx), var(--by)) scale(1.15); opacity: 0; }
                }

                /* === SIDEBAR MENU KIRI === */
                #tuzi-sidebar {
                    pointer-events: auto; position: fixed; top: 0; left: 0;
                    width: 300px; height: 100vh; background: #FFFFFF; color: #0D0D0D;
                    border-right: 2px solid #E5E5E5; box-shadow: 10px 0 30px rgba(0, 0, 0, 0.08);
                    display: flex; flex-direction: column; padding: 22px 16px;
                    box-sizing: border-box; overflow-y: auto;
                    transform: translateX(-105%);
                    transition: transform 0.25s cubic-bezier(0.25, 1, 0.5, 1), background 0.25s ease;
                    z-index: 999998;
                }
                body.teach-mode-active.dark-mode #tuzi-sidebar {
                    background: #050505; color: #F5F5F5; border-right-color: #1F1F1F;
                }
                #tuzi-sidebar.open { transform: translateX(0); }

                .sidebar-header {
                    display: flex; align-items: center; justify-content: space-between;
                    margin-bottom: 18px; padding: 0 4px;
                }
                .sidebar-brand {
                    display: flex; align-items: center; font-size: 20px; font-weight: 900;
                    color: inherit; letter-spacing: 0.8px;
                }
                .sidebar-brand svg { margin-right: 10px; }
                .sidebar-collapse-btn {
                    width: 34px; height: 34px; border-radius: 8px; background: transparent;
                    color: inherit; border: none; cursor: pointer; display: flex;
                    align-items: center; justify-content: center;
                }
                .sidebar-collapse-btn:hover { background: rgba(128,128,128,0.15); }

                .mode-switch-pill {
                    display: flex; background: #F2F2F2; border: 1.5px solid #E0E0E0;
                    border-radius: 999px; padding: 4px; margin-bottom: 20px;
                }
                body.teach-mode-active.dark-mode .mode-switch-pill {
                    background: #111111; border-color: #242424;
                }
                .mode-tab-btn {
                    flex: 1; height: 36px; border: none; border-radius: 999px;
                    background: transparent; color: #737373; font-size: 13.5px; font-weight: 700;
                    cursor: pointer; display: flex; align-items: center; justify-content: center;
                    transition: all 0.2s ease; outline: none;
                }
                .mode-tab-btn.active { background: #0D0D0D; color: #FFFFFF; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18); }
                body.teach-mode-active.dark-mode .mode-tab-btn.active { background: #FFFFFF; color: #000000; }
                .live-badge {
                    font-size: 9px; font-weight: 800; padding: 2px 5px;
                    border-radius: 6px; background: rgba(128, 128, 128, 0.25); margin-left: 6px;
                }

                .sidebar-menu-list { display: flex; flex-direction: column; }
                .sidebar-item {
                    display: flex; align-items: center; justify-content: space-between;
                    height: 44px; padding: 0 12px; margin-bottom: 6px; border-radius: 12px;
                    background: transparent; color: inherit; border: none; font-size: 14px;
                    font-weight: 600; cursor: pointer; text-align: left;
                    transition: background 0.15s ease; outline: none; width: 100%; box-sizing: border-box;
                }
                .sidebar-item-left { display: flex; align-items: center; }
                .sidebar-item-left svg { margin-right: 14px; }
                .sidebar-item:hover { background: rgba(128, 128, 128, 0.14); }
                .sidebar-section-label {
                    font-size: 12px; font-weight: 700; color: #737373;
                    margin: 16px 0 8px 12px; text-transform: uppercase; letter-spacing: 0.6px;
                }

                .chevron-down { transition: transform 0.22s ease; opacity: 0.75; }
                #menu-open-settings.expanded .chevron-down { transform: rotate(180deg); }

                /* === MENU LIST SETTINGS KE BAWAH === */
                #tuzi-settings-dropdown {
                    display: none;
                    flex-direction: column;
                    background: #0A0A0A;
                    color: #F5F5F5;
                    border: 1.5px solid #242424;
                    border-radius: 20px;
                    padding: 12px 10px;
                    margin: 2px 0 10px 0;
                    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45);
                    box-sizing: border-box;
                    animation: slideDownList 0.2s ease-out;
                }
                body.teach-mode-active:not(.dark-mode) #tuzi-settings-dropdown {
                    background: #F8F9FA;
                    color: #171717;
                    border: 1.5px solid #D4D4D4;
                    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
                }
                #tuzi-settings-dropdown.open { display: flex; }
                @keyframes slideDownList {
                    from { opacity: 0; transform: translateY(-6px); }
                    to { opacity: 1; transform: translateY(0); }
                }

                .dropdown-section-title {
                    font-size: 11.5px; font-weight: 700; color: #8A8A8A;
                    padding: 4px 10px 6px 10px; letter-spacing: 0.3px;
                }
                .dropdown-divider {
                    height: 1px; background: rgba(128, 128, 128, 0.2); margin: 8px 4px;
                }

                .dropdown-gender-box { padding: 4px 6px 6px 6px; }
                .gender-switch-container {
                    display: flex; background: #161616;
                    padding: 4px; border-radius: 999px; border: 1px solid #262626;
                }
                body.teach-mode-active:not(.dark-mode) .gender-switch-container {
                    background: #EAEAEA; border-color: #D4D4D4;
                }
                .gender-switch-btn {
                    flex: 1; height: 36px; border: none; border-radius: 999px;
                    font-size: 13px; font-weight: 800; cursor: pointer;
                    background: transparent; color: inherit; opacity: 0.7;
                    transition: all 0.2s ease; outline: none;
                }
                .gender-switch-btn.male-btn.active {
                    opacity: 1; background: #3B82F6; color: #FFFFFF;
                    box-shadow: 0 3px 10px rgba(59, 130, 246, 0.35);
                }
                .gender-switch-btn.female-btn.active {
                    opacity: 1; background: #F9A8D4; color: #831843;
                    box-shadow: 0 3px 10px rgba(244, 114, 182, 0.40);
                }

                #female-voice-options {
                    display: none; flex-direction: column; margin-top: 8px;
                }
                #female-voice-options.show { display: flex; }
                .fmode-row { display: flex; justify-content: space-between; }
                .fmode-btn {
                    flex: 1; height: 32px; margin: 0 3px; border-radius: 10px;
                    border: 1px solid #2E2E2E; background: transparent;
                    color: inherit; font-size: 11.5px; font-weight: 700; cursor: pointer;
                }
                body.teach-mode-active:not(.dark-mode) .fmode-btn { border-color: #CCCCCC; }
                .fmode-btn.active { background: #F472B6; color: #FFFFFF; border-color: #F472B6; }

                .mic-select-box { padding: 2px 6px 6px 6px; }
                #tuzi-mic-select {
                    width: 100%; height: 36px; border-radius: 10px; padding: 0 10px;
                    font-size: 12.5px; font-weight: 600; background: #161616;
                    color: #F5F5F5; border: 1px solid #2E2E2E; outline: none; cursor: pointer;
                }
                body.teach-mode-active:not(.dark-mode) #tuzi-mic-select {
                    background: #FFFFFF; color: #171717; border-color: #CCCCCC;
                }

                .dropdown-list-item {
                    display: flex; align-items: center; justify-content: space-between;
                    width: 100%; height: 42px; padding: 0 12px; margin-top: 2px;
                    border-radius: 999px; background: transparent; color: inherit;
                    border: 1.5px solid transparent; font-size: 13.5px; font-weight: 600;
                    cursor: pointer; text-align: left; transition: all 0.15s ease;
                    box-sizing: border-box; outline: none;
                }
                .dropdown-list-item:hover {
                    background: rgba(255, 255, 255, 0.08);
                    border-color: rgba(255, 255, 255, 0.28);
                }
                body.teach-mode-active:not(.dark-mode) .dropdown-list-item:hover {
                    background: rgba(0, 0, 0, 0.06);
                    border-color: rgba(0, 0, 0, 0.25);
                }
                .dropdown-item-left { display: flex; align-items: center; }
                .dropdown-item-left svg { margin-right: 12px; opacity: 0.9; }
                .pi-status-dot {
                    font-size: 10px; font-weight: 800; padding: 2px 7px;
                    border-radius: 999px; background: rgba(59, 130, 246, 0.2);
                    color: #3B82F6; display: none;
                }
                .pi-status-dot.active { display: inline-block; }

                /* === MODAL KOLOM CHAT PERSONAL INTELLIGENCE === */
                #tuzi-pi-modal {
                    pointer-events: auto; display: none; position: fixed;
                    top: 0; left: 0; width: 100vw; height: 100vh;
                    background: rgba(0, 0, 0, 0.65); align-items: center;
                    justify-content: center; z-index: 10000000;
                }
                #tuzi-pi-modal.open { display: flex; }

                .pi-card {
                    width: 540px; height: 430px; background: #FFFFFF; color: #0D0D0D;
                    border: 2.5px solid #0D0D0D; border-radius: 24px; padding: 22px 24px;
                    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5); box-sizing: border-box;
                    display: flex; flex-direction: column; justify-content: space-between;
                }
                body.teach-mode-active.dark-mode .pi-card {
                    background: #080808; color: #F5F5F5; border-color: #262626;
                }
                .pi-header {
                    display: flex; align-items: center; justify-content: space-between;
                    padding-bottom: 12px; border-bottom: 1.5px solid rgba(128, 128, 128, 0.2);
                }
                .pi-title-wrap { display: flex; align-items: center; font-size: 17px; font-weight: 800; }
                .pi-title-wrap svg { margin-right: 10px; }
                .pi-header-actions { display: flex; align-items: center; }
                .pi-btn-reset {
                    height: 30px; padding: 0 12px; margin-right: 10px; border-radius: 999px;
                    border: 1.5px solid rgba(128, 128, 128, 0.35); background: transparent;
                    color: inherit; font-size: 11.5px; font-weight: 700; cursor: pointer;
                }
                .pi-btn-reset:hover { background: rgba(239, 68, 68, 0.15); border-color: #EF4444; color: #EF4444; }
                .pi-close-btn {
                    background: transparent; color: inherit; border: none;
                    font-size: 19px; font-weight: bold; cursor: pointer;
                }

                #pi-chat-history {
                    flex: 1; overflow-y: auto; padding: 14px 4px;
                    display: flex; flex-direction: column; box-sizing: border-box;
                }
                .pi-msg-row { display: flex; width: 100%; margin-bottom: 12px; }
                .pi-msg-row.user { justify-content: flex-end; }
                .pi-msg-row.sys { justify-content: flex-start; }
                .pi-msg-bubble {
                    max-width: 82%; padding: 11px 15px; border-radius: 16px;
                    font-size: 13.5px; font-weight: 500; line-height: 1.45;
                }
                .pi-msg-row.user .pi-msg-bubble {
                    background: #FFFFFF; color: #000000; border: 1.5px solid #000000; border-bottom-right-radius: 4px;
                }
                body.teach-mode-active:not(.dark-mode) .pi-msg-row.user .pi-msg-bubble {
                    background: #0D0D0D; color: #FFFFFF;
                }
                .pi-msg-row.sys .pi-msg-bubble {
                    background: #141414; color: #E5E5E5;
                    border: 1px solid #262626; border-bottom-left-radius: 4px;
                }
                body.teach-mode-active:not(.dark-mode) .pi-msg-row.sys .pi-msg-bubble {
                    background: #F3F4F6; color: #111111; border-color: #D1D5DB;
                }

                .pi-chat-input-bar {
                    display: flex; align-items: center; background: #111111;
                    border: 2px solid #262626; border-radius: 16px; padding: 6px 8px 6px 14px;
                }
                body.teach-mode-active:not(.dark-mode) .pi-chat-input-bar {
                    background: #F9FAFB; border-color: #0D0D0D;
                }
                #pi-prompt-input {
                    flex: 1; border: none; outline: none; background: transparent;
                    color: inherit; font-size: 14px; font-weight: 500; font-family: inherit;
                }
                #pi-save-btn {
                    width: 38px; height: 38px; border-radius: 12px; border: none;
                    background: #0D0D0D; color: #FFFFFF; cursor: pointer;
                    display: flex; align-items: center; justify-content: center; margin-left: 8px;
                }
                body.teach-mode-active.dark-mode #pi-save-btn { background: #FFFFFF; color: #000000; }

                /* === TAMPILAN CHATBOT (TOTAL MUTE MODE) === */
                #tuzi-chat-view {
                    pointer-events: auto; display: none; position: fixed;
                    top: 0; left: 0; width: 100vw; height: 100vh; background: #FFFFFF;
                    flex-direction: column; justify-content: space-between; align-items: center;
                    padding: 28px 40px 30px 40px; box-sizing: border-box; z-index: 999993;
                }
                body.teach-mode-active.dark-mode #tuzi-chat-view { background: #000000; }
                body.teach-mode-active.chat-tab-open #tuzi-chat-view { display: flex; }
                body.teach-mode-active.chat-tab-open .discord-call-wrapper { display: none !important; }

                .chat-top-bar {
                    width: 100%; max-width: 820px; display: flex; align-items: center;
                    justify-content: space-between; padding-bottom: 14px; border-bottom: 1.5px solid #EBEBEB;
                }
                body.teach-mode-active.dark-mode .chat-top-bar { border-bottom-color: #1F1F1F; }
                .chat-top-title { font-size: 16px; font-weight: 800; color: #0D0D0D; letter-spacing: 0.5px; }
                body.teach-mode-active.dark-mode .chat-top-title { color: #FFFFFF; }

                #tuzi-chat-messages {
                    width: 100%; max-width: 820px; flex: 1; overflow-y: auto;
                    padding: 24px 8px; display: flex; flex-direction: column; box-sizing: border-box;
                }
                .chat-bubble-row { display: flex; width: 100%; margin-bottom: 16px; }
                .chat-bubble-row.user { justify-content: flex-end; }
                .chat-bubble-row.bot { justify-content: flex-start; }
                .chat-bubble {
                    max-width: 72%; padding: 13px 18px; border-radius: 18px;
                    font-size: 15px; font-weight: 500; line-height: 1.5; word-wrap: break-word;
                }
                .chat-bubble-row.user .chat-bubble { background: #0D0D0D; color: #FFFFFF; border-bottom-right-radius: 4px; }
                body.teach-mode-active.dark-mode .chat-bubble-row.user .chat-bubble {
                    background: #FFFFFF; color: #000000;
                }
                .chat-bubble-row.bot .chat-bubble {
                    background: #F7F7F7; color: #0D0D0D; border: 1.5px solid #0D0D0D; border-bottom-left-radius: 4px;
                }
                body.teach-mode-active.dark-mode .chat-bubble-row.bot .chat-bubble {
                    background: #0D0D0D; color: #F5F5F5; border-color: #262626;
                }

                .chat-input-bar {
                    width: 100%; max-width: 820px; display: flex; align-items: center;
                    background: #FFFFFF; border: 2px solid #0D0D0D; border-radius: 20px;
                    padding: 8px 12px 8px 20px; box-sizing: border-box;
                }
                body.teach-mode-active.dark-mode .chat-input-bar {
                    background: #0A0A0A; border-color: #262626;
                }
                #tuzi-chat-input {
                    flex: 1; border: none; outline: none; background: transparent;
                    color: inherit; font-size: 15px; font-weight: 500; font-family: inherit;
                }
                body.teach-mode-active.dark-mode #tuzi-chat-input { color: #FFFFFF; }
                #tuzi-chat-send {
                    width: 42px; height: 42px; border-radius: 14px; background: #0D0D0D;
                    color: #FFFFFF; border: none; cursor: pointer; display: flex;
                    align-items: center; justify-content: center; margin-left: 10px;
                }
                body.teach-mode-active.dark-mode #tuzi-chat-send { background: #FFFFFF; color: #000000; }

                /* === BOTTOM DOCK (HANYA TOMBOL HANGUP MERAH) === */
                .discord-call-wrapper {
                    pointer-events: auto; display: flex; align-items: center;
                    justify-content: center; margin-bottom: 34px; margin-top: auto; z-index: 10;
                }
                .hangup-btn {
                    width: 78px; height: 68px; background: #DA373C; color: #FFFFFF;
                    border: none; border-radius: 20px; cursor: pointer; display: flex;
                    align-items: center; justify-content: center;
                    box-shadow: 0 12px 30px rgba(218, 55, 60, 0.35);
                    transition: all 0.15s ease; outline: none;
                }
                .hangup-btn:hover { background: #A12828; transform: scale(1.04); }
                .hangup-btn:active { transform: scale(0.96); }
            `;

            let overlay = document.createElement('div');
            overlay.id = 'tuzi-teach-overlay';
            overlay.innerHTML = `
                <button id="tuzi-burger-btn" title="Buka Menu">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
                        <line x1="3" y1="6" x2="21" y2="6"></line>
                        <line x1="3" y1="12" x2="21" y2="12"></line>
                        <line x1="3" y1="18" x2="21" y2="18"></line>
                    </svg>
                </button>

                <button id="tuzi-window-size-btn" title="Full Screen 1920x1080 (Tekan F11 untuk kembali ke ukuran Minimize)">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path>
                    </svg>
                </button>

                <button id="tuzi-theme-btn" title="Ganti Light / Dark Mode">
                    <svg class="sun-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="5" fill="#FFFFFF"></circle>
                        <line x1="12" y1="1" x2="12" y2="3"></line>
                        <line x1="12" y1="21" x2="12" y2="23"></line>
                        <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
                        <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
                        <line x1="1" y1="12" x2="3" y2="12"></line>
                        <line x1="21" y1="12" x2="23" y2="12"></line>
                        <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
                        <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
                    </svg>
                    <svg class="moon-icon" width="21" height="21" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                    </svg>
                </button>

                <div id="ai-blob-stage">
                    <div id="blob-ptt-hint">hold to speak</div>
                    <div id="ai-blob-wrapper" title="Tekan dan tahan Avatar untuk berbicara">
                        <div class="blob-ripple r1"></div>
                        <div class="blob-ripple r2"></div>
                        <div id="blob-bubble-layer"></div>
                        <div id="ai-blob-body">
                            <div class="blob-lobe left"></div>
                            <div class="blob-lobe right"></div>
                            <div class="blob-eyes-container" id="blob-eyes">
                                <div class="blob-eye left-eye"></div>
                                <div class="blob-eye right-eye"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- CONTAINER BOX BUBBLE PILIHAN (DI BAWAH SUBTITLE) -->
                <div id="tuzi-choice-container"></div>

                <!-- SIDEBAR KIRI -->
                <div id="tuzi-sidebar">
                    <div class="sidebar-header">
                        <div class="sidebar-brand">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M12 2L14.8 9.2L22 12L14.8 14.8L12 22L9.2 14.8L2 12L9.2 9.2L12 2Z"/>
                            </svg>
                            <span>TUZI</span>
                        </div>
                        <button class="sidebar-collapse-btn" id="close-sidebar-btn" title="Tutup Menu">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="3" y="3" width="18" height="18" rx="4"></rect>
                                <line x1="9" y1="3" x2="9" y2="21"></line>
                            </svg>
                        </button>
                    </div>

                    <div class="mode-switch-pill">
                        <button class="mode-tab-btn" id="tab-btn-chat">Chat</button>
                        <button class="mode-tab-btn active" id="tab-btn-live">
                            <span>Live</span>
                            <span class="live-badge">VOICE</span>
                        </button>
                    </div>

                    <div class="sidebar-menu-list">
                        <button class="sidebar-item" id="menu-new-chat">
                            <div class="sidebar-item-left">
                                <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                                </svg>
                                <span>New chat</span>
                            </div>
                        </button>

                        <button class="sidebar-item" id="menu-search-chat">
                            <div class="sidebar-item-left">
                                <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <circle cx="11" cy="11" r="8"></circle>
                                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                                </svg>
                                <span>Search chats</span>
                            </div>
                        </button>

                        <button class="sidebar-item" id="menu-open-settings">
                            <div class="sidebar-item-left">
                                <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <circle cx="12" cy="12" r="3"></circle>
                                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
                                </svg>
                                <span>Settings</span>
                            </div>
                            <svg class="chevron-down" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                                <polyline points="6 9 12 15 18 9"></polyline>
                            </svg>
                        </button>

                        <!-- LIST MENU SETTINGS KE BAWAH -->
                        <div id="tuzi-settings-dropdown">
                            <div class="dropdown-section-title">Swap Gender & Voice</div>
                            <div class="dropdown-gender-box">
                                <div class="gender-switch-container">
                                    <button class="gender-switch-btn male-btn active" id="btn-gender-male">♂ Male</button>
                                    <button class="gender-switch-btn female-btn" id="btn-gender-female">♀ Female</button>
                                </div>
                                <div id="female-voice-options">
                                    <div class="fmode-row">
                                        <button class="fmode-btn active" data-fmode="1">Dandere</button>
                                        <button class="fmode-btn" data-fmode="2">Shy</button>
                                        <button class="fmode-btn" data-fmode="3">Mommy</button>
                                    </div>
                                </div>
                            </div>

                            <div class="dropdown-divider"></div>
                            <div class="dropdown-section-title">Microphone Input (Noise Suppressed)</div>
                            <div class="mic-select-box">
                                <select id="tuzi-mic-select" title="Pilih perangkat mikrofon">
                                    <option value="DEFAULT">🎙️ Default System Microphone</option>
                                </select>
                            </div>

                            <div class="dropdown-divider"></div>
                            <div class="dropdown-section-title">Preferences</div>

                            <button class="dropdown-list-item" id="btn-open-pi">
                                <div class="dropdown-item-left">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path>
                                        <circle cx="9" cy="7" r="4"></circle>
                                        <path d="M19 8l1.2 2.8L23 12l-2.8 1.2L19 16l-1.2-2.8L15 12l2.8-1.2L19 8z"></path>
                                    </svg>
                                    <span>Personal Intelligence</span>
                                </div>
                                <span class="pi-status-dot" id="pi-badge">ON</span>
                            </button>

                            <button class="dropdown-list-item" id="btn-dropdown-theme">
                                <div class="dropdown-item-left">
                                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <circle cx="12" cy="12" r="5"></circle>
                                        <line x1="12" y1="1" x2="12" y2="3"></line>
                                        <line x1="12" y1="21" x2="12" y2="23"></line>
                                    </svg>
                                    <span>Theme</span>
                                </div>
                            </button>
                        </div>

                        <div class="sidebar-section-label">Language Target</div>
                        <button class="sidebar-item sidebar-lang-shortcut" data-lang="id-ID">
                            <div class="sidebar-item-left"><span>🇮🇩 &nbsp;Indonesian Practice</span></div>
                        </button>
                        <button class="sidebar-item sidebar-lang-shortcut" data-lang="ja-JP">
                            <div class="sidebar-item-left"><span>🇯🇵 &nbsp;Japanese Practice</span></div>
                        </button>
                        <button class="sidebar-item sidebar-lang-shortcut" data-lang="en-US">
                            <div class="sidebar-item-left"><span>🇺🇸 &nbsp;English Practice</span></div>
                        </button>
                        <button class="sidebar-item sidebar-lang-shortcut" data-lang="ko-KR">
                            <div class="sidebar-item-left"><span>🇰🇷 &nbsp;Korean Practice</span></div>
                        </button>
                        <button class="sidebar-item sidebar-lang-shortcut" data-lang="zh-CN">
                            <div class="sidebar-item-left"><span>🇨🇳 &nbsp;Chinese Practice</span></div>
                        </button>
                    </div>
                </div>

                <!-- MODAL KOLOM CHAT PERSONAL INTELLIGENCE -->
                <div id="tuzi-pi-modal">
                    <div class="pi-card">
                        <div class="pi-header">
                            <div class="pi-title-wrap">
                                <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path>
                                    <circle cx="9" cy="7" r="4"></circle>
                                    <path d="M19 8l1.2 2.8L23 12l-2.8 1.2L19 16l-1.2-2.8L15 12l2.8-1.2L19 8z"></path>
                                </svg>
                                <span>Personal Intelligence</span>
                            </div>
                            <div class="pi-header-actions">
                                <button class="pi-btn-reset" id="pi-reset-btn" title="Kembalikan ke Default">Reset Default</button>
                                <button class="pi-close-btn" id="close-pi-btn">✕</button>
                            </div>
                        </div>

                        <div id="pi-chat-history">
                            <div class="pi-msg-row sys">
                                <div class="pi-msg-bubble">
                                    Ketik di kolom bawah untuk mengatur bebas sifat, kepribadian, atau gaya bicara Tuzi di luar mode bawaan.
                                </div>
                            </div>
                        </div>

                        <div class="pi-chat-input-bar">
                            <input type="text" id="pi-prompt-input" placeholder="Contoh: Bicara seperti guru yang santai dan suka kasih kuis..." autocomplete="off" />
                            <button id="pi-save-btn" title="Terapkan Kepribadian">
                                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                                    <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                </div>

                <!-- TAMPILAN CHATBOT UTAMA (MUTE MODE) -->
                <div id="tuzi-chat-view">
                    <div class="chat-top-bar">
                        <div style="width: 44px;"></div>
                        <div class="chat-top-title">● TUZI CHATBOT (MUTE MODE)</div>
                        <button class="hangup-btn" id="exit-teach-btn-chat" style="width: 52px; height: 42px; border-radius: 12px;" title="Tutup Aplikasi">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/>
                            </svg>
                        </button>
                    </div>

                    <div id="tuzi-chat-messages">
                        <div class="chat-bubble-row bot">
                            <div class="chat-bubble">Halo Zak! Di mode Chat ini suaraku di-mute sepenuhnya. Ketik kalimat yang ingin kamu latih di bawah ini ya.</div>
                        </div>
                    </div>

                    <div class="chat-input-bar">
                        <input type="text" id="tuzi-chat-input" placeholder="Ketik pesan ke Tuzi..." autocomplete="off" />
                        <button id="tuzi-chat-send" title="Kirim Pesan">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                            </svg>
                        </button>
                    </div>
                </div>

                <!-- BOTTOM DOCK (HANYA TOMBOL HANGUP) -->
                <div class="discord-call-wrapper">
                    <button class="hangup-btn" id="exit-teach-btn" title="Tutup Aplikasi">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/>
                        </svg>
                    </button>
                </div>
            `;
            document.body.appendChild(overlay);

            // 1. LANGSUNG AKTIFKAN MODE BLOB SEKARANG JUGA (SEMBUNYIKAN VTUBER #canvas)
            document.body.classList.add('teach-mode-active');

            // 2. DEKLARASIKAN SEMUA ELEMEN DOM TERLEBIH DAHULU DENGAN PENGAMAN ANTI-NULL
            let sidebar = document.getElementById('tuzi-sidebar');
            let burgerBtn = document.getElementById('tuzi-burger-btn');
            let closeSidebarBtn = document.getElementById('close-sidebar-btn');
            let settingsDropdown = document.getElementById('tuzi-settings-dropdown');
            let openSettingsBtn = document.getElementById('menu-open-settings');
            let themeBtn = document.getElementById('tuzi-theme-btn');
            let dropdownThemeBtn = document.getElementById('btn-dropdown-theme');
            let winSizeBtn = document.getElementById('tuzi-window-size-btn');

            let blobStage = document.getElementById('ai-blob-stage');
            let blobWrapper = document.getElementById('ai-blob-wrapper');
            let blobBody = document.getElementById('ai-blob-body');
            let blobEyes = document.getElementById('blob-eyes');
            let bubbleLayer = document.getElementById('blob-bubble-layer');
            let pttHint = document.getElementById('blob-ptt-hint');

            let tabChat = document.getElementById('tab-btn-chat');
            let tabLive = document.getElementById('tab-btn-live');
            let chatMessages = document.getElementById('tuzi-chat-messages');
            let chatInput = document.getElementById('tuzi-chat-input');
            let chatSend = document.getElementById('tuzi-chat-send');

            let choiceContainer = document.getElementById('tuzi-choice-container');
            if (!choiceContainer) {
                choiceContainer = document.createElement('div');
                choiceContainer.id = 'tuzi-choice-container';
                overlay.appendChild(choiceContainer);
            }

            let micSelect = document.getElementById('tuzi-mic-select');
            if (micSelect && AVAILABLE_MICS && AVAILABLE_MICS.length > 0) {
                AVAILABLE_MICS.forEach(m => {
                    let opt = document.createElement('option');
                    opt.value = String(m.index);
                    opt.innerText = '🎙️ [' + m.index + '] ' + m.name;
                    micSelect.appendChild(opt);
                });
                micSelect.onchange = () => {
                    if (window.pyBridge) {
                        window.pyBridge.start_ptt('MIC_DEV:' + micSelect.value);
                    }
                };
            }

            window.currentTeachLang = 'en-US';
            function setLanguage(lang) {
                window.currentTeachLang = lang;
            }

            window.setTeachModeUI = function(active) {
                if (active) {
                    document.body.classList.add('teach-mode-active');
                } else {
                    document.body.classList.remove('teach-mode-active');
                    document.body.classList.remove('chat-tab-open');
                    if (sidebar) sidebar.classList.remove('open');
                }
            };

            // 3. SISTEM BUBBLE CHOICE BOX (PILIH BAHASA DENGAN BENDERA -> PILIH LEVEL TANPA BENDERA)
            const FLAG_SVGS = {
                'id-ID': `<svg viewBox="0 0 30 20" width="24" height="17"><rect width="30" height="10" fill="#E70011"/><rect y="10" width="30" height="10" fill="#FFFFFF"/></svg>`,
                'ja-JP': `<svg viewBox="0 0 30 20" width="24" height="17"><rect width="30" height="20" fill="#FFFFFF"/><circle cx="15" cy="10" r="6" fill="#BC002D"/></svg>`,
                'en-US': `<svg viewBox="0 0 30 20" width="24" height="17"><rect width="30" height="20" fill="#B22234"/><rect y="3" width="30" height="2.5" fill="#FFFFFF"/><rect y="8" width="30" height="2.5" fill="#FFFFFF"/><rect y="13" width="30" height="2.5" fill="#FFFFFF"/><rect y="18" width="30" height="2" fill="#FFFFFF"/><rect width="14" height="11" fill="#3C3B6E"/></svg>`,
                'ko-KR': `<svg viewBox="0 0 30 20" width="24" height="17"><rect width="30" height="20" fill="#FFFFFF"/><circle cx="15" cy="10" r="5" fill="#CD2E3A"/><path d="M10,10 A5,5 0 0,0 20,10 A2.5,2.5 0 0,1 15,10 A2.5,2.5 0 0,0 10,10 Z" fill="#0047A0"/></svg>`,
                'zh-CN': `<svg viewBox="0 0 30 20" width="24" height="17"><rect width="30" height="20" fill="#DE2910"/><polygon points="6,3 7.2,6.8 11,6.8 7.9,9 9.1,12.8 6,10.5 2.9,12.8 4.1,9 1,6.8 4.8,6.8" fill="#FFDE00"/></svg>`
            };

            const LANGUAGE_CHOICES = [
                { label: 'Indonesia', code: 'id-ID', flag: FLAG_SVGS['id-ID'] },
                { label: 'Jepang', code: 'ja-JP', flag: FLAG_SVGS['ja-JP'] },
                { label: 'Inggris', code: 'en-US', flag: FLAG_SVGS['en-US'] },
                { label: 'Korea', code: 'ko-KR', flag: FLAG_SVGS['ko-KR'] },
                { label: 'China', code: 'zh-CN', flag: FLAG_SVGS['zh-CN'] }
            ];

            const LEVEL_CHOICES = [
                { label: 'Beginner', level: 'Beginner' },
                { label: 'Elementary', level: 'Elementary' },
                { label: 'Intermediate', level: 'Intermediate' },
                { label: 'Advanced', level: 'Advanced' },
                { label: 'Expert', level: 'Expert' }
            ];

            function renderChoiceBoxes(items, onSelect) {
                if (!choiceContainer) return;
                choiceContainer.innerHTML = '';
                document.body.classList.add('choices-active');
                choiceContainer.classList.add('show');

                items.forEach((item, idx) => {
                    let btn = document.createElement('button');
                    btn.className = 'tuzi-choice-box';
                    btn.style.animationDelay = (idx * 0.055) + 's';
                    if (item.flag) {
                        btn.innerHTML = `<span class="choice-flag-icon">${item.flag}</span><span>${item.label}</span>`;
                    } else {
                        btn.innerText = item.label;
                    }
                    btn.onclick = () => onSelect(item);
                    choiceContainer.appendChild(btn);
                });
            }

            function hideChoiceBoxes() {
                if (choiceContainer) {
                    choiceContainer.classList.remove('show');
                    choiceContainer.innerHTML = '';
                }
                document.body.classList.remove('choices-active');
            }

            window.showLanguageOnboarding = function() {
                renderChoiceBoxes(LANGUAGE_CHOICES, (selected) => {
                    setLanguage(selected.code);
                    if (window.pyBridge) {
                        window.pyBridge.start_ptt('ONBOARD:LANG:' + selected.code);
                    }
                    window.showLevelOnboarding();
                });
            };

            window.showLevelOnboarding = function() {
                renderChoiceBoxes(LEVEL_CHOICES, (selected) => {
                    hideChoiceBoxes();
                    if (window.pyBridge) {
                        window.pyBridge.start_ptt('ONBOARD:LEVEL:' + selected.level);
                    }
                });
            };

            // 4. EVENT LISTENER TOMBOL TEMA, WINDOW, & SIDEBAR
            function toggleTheme() {
                document.body.classList.toggle('dark-mode');
            }
            if (themeBtn) themeBtn.onclick = toggleTheme;
            if (dropdownThemeBtn) dropdownThemeBtn.onclick = toggleTheme;

            if (winSizeBtn) {
                winSizeBtn.onclick = () => {
                    document.body.classList.add('fullscreen-mode');
                    if (window.pyBridge) window.pyBridge.start_ptt('WIN:FULLSCREEN');
                };
            }

            window.addEventListener('keydown', (e) => {
                if (e.key === 'F11' || e.keyCode === 122) {
                    e.preventDefault();
                    if (document.body.classList.contains('fullscreen-mode')) {
                        document.body.classList.remove('fullscreen-mode');
                        if (window.pyBridge) window.pyBridge.start_ptt('WIN:MINIMIZE');
                    } else {
                        document.body.classList.add('fullscreen-mode');
                        if (window.pyBridge) window.pyBridge.start_ptt('WIN:FULLSCREEN');
                    }
                }
            });

            if (burgerBtn && sidebar) burgerBtn.onclick = () => sidebar.classList.add('open');
            if (closeSidebarBtn && sidebar) closeSidebarBtn.onclick = () => sidebar.classList.remove('open');
            if (openSettingsBtn && settingsDropdown) {
                openSettingsBtn.onclick = (e) => {
                    e.stopPropagation();
                    settingsDropdown.classList.toggle('open');
                    openSettingsBtn.classList.toggle('expanded');
                };
            }

            let btnMale = document.getElementById('btn-gender-male');
            let btnFemale = document.getElementById('btn-gender-female');
            let femaleOptions = document.getElementById('female-voice-options');

            if (btnMale && btnFemale && femaleOptions) {
                btnMale.onclick = () => {
                    btnMale.classList.add('active');
                    btnFemale.classList.remove('active');
                    femaleOptions.classList.remove('show');
                    document.body.classList.remove('female-avatar');
                    if (window.pyBridge) window.pyBridge.start_ptt('GENDER:MALE');
                };

                btnFemale.onclick = () => {
                    btnFemale.classList.add('active');
                    btnMale.classList.remove('active');
                    femaleOptions.classList.add('show');
                    document.body.classList.add('female-avatar');
                    if (window.pyBridge) window.pyBridge.start_ptt('GENDER:FEMALE');
                };
            }

            document.querySelectorAll('.fmode-btn').forEach(btn => {
                btn.onclick = () => {
                    document.querySelectorAll('.fmode-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    let modeNum = btn.getAttribute('data-fmode');
                    if (window.pyBridge) window.pyBridge.start_ptt('FMODE:' + modeNum);
                };
            });

            let piModal = document.getElementById('tuzi-pi-modal');
            let openPiBtn = document.getElementById('btn-open-pi');
            let closePiBtn = document.getElementById('close-pi-btn');
            let piHistory = document.getElementById('pi-chat-history');
            let piInput = document.getElementById('pi-prompt-input');
            let piSaveBtn = document.getElementById('pi-save-btn');
            let piResetBtn = document.getElementById('pi-reset-btn');
            let piBadge = document.getElementById('pi-badge');

            if (openPiBtn && piModal) {
                openPiBtn.onclick = () => {
                    if (sidebar) sidebar.classList.remove('open');
                    piModal.classList.add('open');
                    if (piInput) setTimeout(() => piInput.focus(), 80);
                };
            }
            if (closePiBtn && piModal) closePiBtn.onclick = () => piModal.classList.remove('open');
            if (piModal) {
                piModal.onclick = (e) => {
                    if (e.target === piModal) piModal.classList.remove('open');
                };
            }

            function appendPiMessage(role, text) {
                if (!piHistory) return;
                let row = document.createElement('div');
                row.className = 'pi-msg-row ' + role;
                let bubble = document.createElement('div');
                bubble.className = 'pi-msg-bubble';
                bubble.innerText = text;
                row.appendChild(bubble);
                piHistory.appendChild(row);
                piHistory.scrollTop = piHistory.scrollHeight;
            }

            function submitPersonalIntelligence() {
                if (!piInput) return;
                let customVal = piInput.value.trim();
                if (!customVal) return;
                appendPiMessage('user', customVal);
                piInput.value = '';
                if (window.pyBridge) {
                    window.pyBridge.start_ptt('CUSTOM_PROMPT:' + customVal);
                }
                if (piBadge) piBadge.classList.add('active');
                appendPiMessage('sys', 'Kepribadian berhasil diperbarui! Tuzi sekarang mengikuti instruksimu.');
            }

            if (piSaveBtn) piSaveBtn.onclick = submitPersonalIntelligence;
            if (piInput) {
                piInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') submitPersonalIntelligence();
                });
            }
            if (piResetBtn) {
                piResetBtn.onclick = () => {
                    if (piInput) piInput.value = '';
                    if (window.pyBridge) window.pyBridge.start_ptt('CUSTOM_PROMPT:');
                    if (piBadge) piBadge.classList.remove('active');
                    appendPiMessage('sys', 'Kepribadian dikembalikan ke mode bawaan (Default).');
                };
            }

            // 5. ANIMASI AVATAR BLOB & PUSH-TO-TALK
            let isHolding = false;
            let bubbleTimer = null;

            if (blobEyes) {
                setInterval(() => {
                    blobEyes.classList.add('blink');
                    setTimeout(() => blobEyes.classList.remove('blink'), 150);
                }, 3800);
            }

            window.addEventListener('mousemove', (e) => {
                if (isHolding || !blobEyes) return;
                let dx = ((e.clientX - window.innerWidth / 2) / (window.innerWidth / 2)) * 18;
                let dy = ((e.clientY - window.innerHeight / 2) / (window.innerHeight / 2)) * 14;
                blobEyes.style.transform = `translate(${10 + dx}px, ${-14 + dy}px)`;
            });

            function spawnBubble() {
                if (!isHolding || !bubbleLayer) return;
                let b = document.createElement('div');
                b.className = 'bubble-particle';
                let size = Math.floor(Math.random() * 18) + 10;
                b.style.width = size + 'px';
                b.style.height = size + 'px';
                let angle = Math.random() * Math.PI * 2;
                let dist = 90 + Math.random() * 55;
                b.style.setProperty('--bx', Math.cos(angle) * dist + 'px');
                b.style.setProperty('--by', (Math.sin(angle) * dist - 40) + 'px');
                b.style.animation = 'bubbleRise 0.95s ease-out forwards';
                bubbleLayer.appendChild(b);
                setTimeout(() => b.remove(), 950);
            }

            const origSetMouthOpen = window.setMouthOpen;
            window.setMouthOpen = function(val) {
                if (origSetMouthOpen) origSetMouthOpen(val);
                if (!isHolding && blobBody && !document.body.classList.contains('chat-tab-open')) {
                    let sX = 1 + (val * 0.14);
                    let sY = 1 - (val * 0.08);
                    blobBody.style.transform = `scale(${sX.toFixed(3)}, ${sY.toFixed(3)})`;
                }
            };

            const origSetExpression = window.setExpression;
            window.setExpression = function(emo) {
                if (origSetExpression) origSetExpression(emo);
                if (!blobEyes) return;
                blobEyes.classList.remove('emo-excited', 'emo-soft', 'emo-angry');
                if (emo === 'excited') blobEyes.classList.add('emo-excited');
                else if (emo === 'soft' || emo === 'flirty') blobEyes.classList.add('emo-soft');
                else if (emo === 'angry' || emo === 'sad') blobEyes.classList.add('emo-angry');
            };

            function startHold(e) {
                e.preventDefault();
                if (isHolding || document.body.classList.contains('chat-tab-open')) return;
                hideChoiceBoxes();
                isHolding = true;
                if (blobWrapper) blobWrapper.classList.add('listening');
                if (blobStage) blobStage.classList.add('listening');
                if (blobEyes) blobEyes.style.transform = '';
                if (pttHint) pttHint.innerText = 'listening...';
                spawnBubble();
                bubbleTimer = setInterval(spawnBubble, 220);
                if (window.pyBridge) window.pyBridge.start_ptt(window.currentTeachLang);
            }

            function stopHold(e) {
                if (!isHolding) return;
                isHolding = false;
                clearInterval(bubbleTimer);
                if (blobWrapper) blobWrapper.classList.remove('listening');
                if (blobStage) blobStage.classList.remove('listening');
                if (pttHint) pttHint.innerText = 'hold to speak';
                if (window.pyBridge) window.pyBridge.stop_ptt();
            }

            if (blobWrapper) blobWrapper.addEventListener('mousedown', startHold);
            window.addEventListener('mouseup', stopHold);

            // 6. MODE CHAT & SUBTITLE HANDLER
            function switchSubMode(mode) {
                if (mode === 'chat') {
                    if (tabChat) tabChat.classList.add('active');
                    if (tabLive) tabLive.classList.remove('active');
                    document.body.classList.add('chat-tab-open');
                    let subElem = document.getElementById('tuzi-subtitle-box');
                    if (subElem) {
                        subElem.innerHTML = '';
                        subElem.classList.remove('show');
                    }
                    if (window.pyBridge) window.pyBridge.start_ptt('MODE:CHAT');
                    if (chatInput) setTimeout(() => chatInput.focus(), 100);
                } else {
                    if (tabLive) tabLive.classList.add('active');
                    if (tabChat) tabChat.classList.remove('active');
                    document.body.classList.remove('chat-tab-open');
                    if (window.pyBridge) window.pyBridge.start_ptt('MODE:LIVE');
                }
                if (sidebar) sidebar.classList.remove('open');
            }

            if (tabChat) tabChat.onclick = () => switchSubMode('chat');
            if (tabLive) tabLive.onclick = () => switchSubMode('live');

            let newChatBtn = document.getElementById('menu-new-chat');
            if (newChatBtn) {
                newChatBtn.onclick = () => {
                    if (sidebar) sidebar.classList.remove('open');
                    if (document.body.classList.contains('chat-tab-open') && chatMessages) {
                        chatMessages.innerHTML = `
                            <div class="chat-bubble-row bot">
                                <div class="chat-bubble">Sesi chat baru dimulai (Mode Mute). Silakan ketik kalimat yang ingin kamu latih.</div>
                            </div>
                        `;
                    } else {
                        window.showLanguageOnboarding();
                        if (window.pyBridge) window.pyBridge.start_ptt('ONBOARD:START');
                    }
                };
            }

            let searchChatBtn = document.getElementById('menu-search-chat');
            if (searchChatBtn) {
                searchChatBtn.onclick = () => {
                    switchSubMode('chat');
                    if (chatInput) chatInput.focus();
                };
            }

            window.appendTeachChatBubble = function(sender, text) {
                if (!text || !text.trim() || !chatMessages) return;
                let row = document.createElement('div');
                row.className = 'chat-bubble-row ' + sender;
                let bubble = document.createElement('div');
                bubble.className = 'chat-bubble';
                bubble.innerText = text;
                row.appendChild(bubble);
                chatMessages.appendChild(row);
                chatMessages.scrollTop = chatMessages.scrollHeight;
            };

            function sendChatMessage() {
                if (!chatInput) return;
                let text = chatInput.value.trim();
                if (!text) return;
                window.appendTeachChatBubble('user', text);
                chatInput.value = '';
                if (window.pyBridge) {
                    window.pyBridge.start_ptt('CHAT:' + text);
                }
            }

            if (chatSend) chatSend.onclick = sendChatMessage;
            if (chatInput) {
                chatInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') sendChatMessage();
                });
            }

            const origSetSubtitle = window.setSubtitle;
            window.setSubtitle = function(htmlContent, durationMs) {
                const isChatTab = document.body.classList.contains('chat-tab-open');
                if (isChatTab) {
                    if (htmlContent && htmlContent.trim() !== '') {
                        window.appendTeachChatBubble('bot', htmlContent);
                    }
                } else {
                    if (origSetSubtitle) origSetSubtitle(htmlContent, durationMs);
                    if (htmlContent && htmlContent.trim() !== '') {
                        window.appendTeachChatBubble('bot', htmlContent);
                    }
                }
            };

            document.querySelectorAll('.sidebar-lang-shortcut').forEach(btn => {
                btn.addEventListener('click', () => {
                    let code = btn.getAttribute('data-lang');
                    setLanguage(code);
                    if (sidebar) sidebar.classList.remove('open');
                    if (window.pyBridge) window.pyBridge.start_ptt('ONBOARD:LANG:' + code);
                    window.showLevelOnboarding();
                });
            });

            function triggerHangup() {
                if (window.pyBridge) {
                    window.pyBridge.start_ptt('CMD:EXIT');
                    window.pyBridge.exit_teach_ui();
                }
            }
            let exitBtn1 = document.getElementById('exit-teach-btn');
            let exitBtn2 = document.getElementById('exit-teach-btn-chat');
            if (exitBtn1) exitBtn1.addEventListener('click', triggerHangup);
            if (exitBtn2) exitBtn2.addEventListener('click', triggerHangup);

            // 7. TAMPILKAN BOX BAHASA & HUBUNGKAN QWEBCHANNEL DI AKHIR SETELAH SEMUA DOM SIAP
            window.showLanguageOnboarding();

            function connectPyBridge() {
                if (typeof QWebChannel !== 'undefined' && typeof qt !== 'undefined' && qt.webChannelTransport) {
                    new QWebChannel(qt.webChannelTransport, function(channel) {
                        window.pyBridge = channel.objects.bridge;
                        console.log('[TUZI] QWebChannel berhasil terhubung!');
                        window.pyBridge.start_ptt('ONBOARD:START');
                    });
                } else {
                    setTimeout(connectPyBridge, 100);
                }
            }
            connectPyBridge();
        }

        initTuziTeachUI();
    })();
    """.replace("__MICS_JSON__", mics_json)

    return qwebchannel_code + "\n" + ui_code