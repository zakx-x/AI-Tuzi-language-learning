import os
import re
import threading
import pygame
import speech_recognition as sr
from PyQt5.QtCore import Qt, QPoint, QFile, QIODevice
from PyQt5.QtWidgets import QApplication

# Langsung aktif sejak aplikasi dijalankan
is_active = True
ui_submode = "live"      # "live" (Voice Mic-to-Mic) atau "chat" (Text Chatbot - Total Mute)
current_gender = "male"  # Default: "male" (Biru) atau "female" (Pink)
current_female_mode = 1  # 1 = Dandere, 2 = Shy, 3 = Mommy
target_lang = "en-US"
main_input_queue = None

# Ukuran Aplikasi Desktop (1600x900)
TEACH_WIN_WIDTH = 1600
TEACH_WIN_HEIGHT = 900

# Ukuran Asli Mode Overlay Desktop
DEFAULT_WIN_WIDTH = 600
DEFAULT_WIN_HEIGHT = 1300

def stop_all_voice_playback():
    """Menghentikan suara Tuzi secara instan saat pindah ke mode Chat (Total Mute)."""
    try:
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
    except Exception:
        pass

def get_qwebchannel_js() -> str:
    """Membaca qwebchannel.js langsung dari resource internal PyQt5 agar tidak diblokir protokol HTTP."""
    qfile = QFile(":/qtwebchannel/qwebchannel.js")
    if qfile.open(QIODevice.ReadOnly):
        content = bytes(qfile.readAll()).decode("utf-8", errors="ignore")
        qfile.close()
        return content
    return ""

class HoldToTalkRecorder:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.is_recording = False
        self.frames = []
        self.lang = "en-US"

    def start_recording(self, payload="en-US"):
        global target_lang, ui_submode, current_gender, current_female_mode

        # 1. Perintah Keluar Instan dari Tombol Hang-up Merah
        if payload == "CMD:EXIT":
            print("\n[Sistem] Tombol Hang-up ditekan. Menutup Aplikasi Tuzi 1.0...")
            os._exit(0)

        # 2. Perintah Pindah Tab (LIVE / CHAT)
        if payload == "MODE:LIVE":
            ui_submode = "live"
            print("\n[TUZI UI] Beralih ke tab LIVE (Voice Mode Aktif).")
            return
        elif payload == "MODE:CHAT":
            ui_submode = "chat"
            stop_all_voice_playback()
            print("\n[TUZI UI] Beralih ke tab CHAT (Total Mute Aktif).")
            return

        # 3. Perintah Swap Gender dari Settings (MALE / FEMALE)
        if payload.startswith("GENDER:"):
            new_gender = payload.split(":")[1].lower()
            current_gender = new_gender
            print(f"\n[TUZI SETTINGS] Gender diubah ke: {current_gender.upper()}")
            if main_input_queue:
                main_input_queue.put(f"[SYS_GENDER:{current_gender}]")
            return

        # 4. Perintah Ganti Tipe Suara Female (1=Dandere, 2=Shy, 3=Mommy)
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

        # 5. Pesan Teks dari Mode Chatbot (Total Mute)
        if payload.startswith("CHAT:"):
            chat_text = payload[5:].strip()
            if chat_text and main_input_queue:
                ui_submode = "chat"
                stop_all_voice_playback()
                print(f"\n[TEACH CHAT - MUTE] Zak: {chat_text}")
                main_input_queue.put(f"[TEACH_INPUT] {chat_text}")
            return

        # 6. Rekaman Suara Push-to-Talk pada Avatar Blob (Hanya di Mode Live)
        if ui_submode == "chat" or self.is_recording:
            return
        self.lang = payload
        target_lang = payload
        self.is_recording = True
        self.frames = []
        threading.Thread(target=self._record_loop, daemon=True).start()

    def _record_loop(self):
        try:
            with sr.Microphone(sample_rate=16000) as source:
                print(f"\n[TEACH MIC - {self.lang}] Mendengarkan... (Tahan Avatar)")
                while self.is_recording:
                    buffer = source.stream.read(source.CHUNK)
                    self.frames.append(buffer)

            if len(self.frames) > 3 and ui_submode == "live":
                print("[TEACH MIC] Memproses ucapan Zak...")
                raw_data = b"".join(self.frames)
                audio_data = sr.AudioData(raw_data, 16000, 2)
                text = self.recognizer.recognize_google(audio_data, language=self.lang)
                if text and text.strip() and main_input_queue:
                    print(f"[TEACH MIC] Zak: {text}")
                    main_input_queue.put(f"[TEACH_INPUT] {text.strip()}")
        except sr.UnknownValueError:
            print("[TEACH MIC] Suara tidak terdengar jelas.")
        except Exception as e:
            print(f"[TEACH MIC Error] {e}")

    def stop_recording(self):
        self.is_recording = False

ptt_recorder = HoldToTalkRecorder()

def handle_teach_mode(user_input: str, bridge):
    """Menangani input selama aplikasi Tuzi 1.0 berjalan."""
    global is_active

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
        user_input += (
            f"\n\n[SYSTEM INFO - LANGUAGE TUTOR ACTIVE (Target Language: {target_lang}): "
            "1. If Zak's grammar or phrasing is unnatural, briefly correct it first. "
            "2. Reply naturally to keep the practice conversation going OR give him a short phrase to repeat. "
            "3. Stay in character and keep your reply under 20 words!]"
        )

    return user_input

def apply_window_state(window, active: bool):
    """Mengatur ukuran jendela ke 1600x900 di tengah layar."""
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
    """Menyuntikkan qwebchannel.js secara inline beserta UI Light/Dark Mode & Gender Swap."""
    qwebchannel_code = get_qwebchannel_js()
    ui_code = """
    (function() {
        if (document.getElementById('tuzi-teach-overlay')) return;

        // Koneksi langsung QWebChannel tanpa terblokir protokol HTTP -> QRC
        function connectPyBridge() {
            if (typeof QWebChannel !== 'undefined' && typeof qt !== 'undefined' && qt.webChannelTransport) {
                new QWebChannel(qt.webChannelTransport, function(channel) {
                    window.pyBridge = channel.objects.bridge;
                    console.log('[TUZI] QWebChannel berhasil terhubung!');
                });
            } else {
                setTimeout(connectPyBridge, 100);
            }
        }
        connectPyBridge();

        let style = document.createElement('style');
        style.innerHTML = `
            /* === TEMA DASAR: LIGHT MODE (DEFAULT) & DARK MODE === */
            body.teach-mode-active {
                background: #F4F5F8 !important;
                border: 3px solid #0D0D0D !important;
                border-radius: 28px !important;
                overflow: hidden !important;
                box-sizing: border-box !important;
                transition: background 0.3s ease, border-color 0.3s ease;
            }
            body.teach-mode-active.dark-mode {
                background: #0B0D13 !important;
                border-color: #2A2E3D !important;
            }

            body.teach-mode-active #canvas {
                display: none !important;
            }

            body.teach-mode-active.chat-tab-open #tuzi-subtitle-box,
            body.teach-mode-active.chat-tab-open #ai-blob-stage {
                display: none !important;
            }

            /* === SUBTITLE TANPA BACKGROUND DI BAWAH AVATAR === */
            body.teach-mode-active #tuzi-subtitle-box {
                position: fixed !important;
                top: calc(50% + 145px) !important;
                left: 50% !important;
                transform: translateX(-50%) !important;
                background: transparent !important;
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                border: none !important;
                box-shadow: none !important;
                color: #0D0D0D !important;
                text-shadow: none !important;
                font-size: 19px !important;
                font-weight: 600 !important;
                line-height: 1.5 !important;
                padding: 0 24px !important;
                max-width: 760px !important;
                z-index: 999992 !important;
                transition: color 0.3s ease;
            }
            body.teach-mode-active.dark-mode #tuzi-subtitle-box {
                color: #F3F4F6 !important;
            }

            #tuzi-teach-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                pointer-events: none;
                z-index: 999990;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                box-sizing: border-box;
            }
            body.teach-mode-active #tuzi-teach-overlay {
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                align-items: center;
            }

            /* === TOMBOL BURGER (KIRI ATAS) & TOMBOL LIGHT/DARK MODE (KANAN ATAS) === */
            #tuzi-burger-btn {
                pointer-events: auto;
                position: fixed;
                top: 24px;
                left: 24px;
                width: 46px;
                height: 46px;
                border-radius: 14px;
                background: #FFFFFF;
                color: #0D0D0D;
                border: 2px solid #0D0D0D;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.10);
                transition: all 0.2s ease;
                z-index: 999995;
                outline: none;
            }
            #tuzi-burger-btn:hover {
                transform: scale(1.05);
            }
            body.teach-mode-active.dark-mode #tuzi-burger-btn {
                background: #181B24;
                color: #FFFFFF;
                border-color: #2E3446;
            }

            #tuzi-theme-btn {
                pointer-events: auto;
                position: fixed;
                top: 24px;
                right: 24px;
                width: 46px;
                height: 46px;
                border-radius: 14px;
                background: #181B24;
                color: #FFFFFF;
                border: 2px solid #0D0D0D;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
                transition: all 0.2s ease;
                z-index: 999995;
                outline: none;
            }
            #tuzi-theme-btn:hover {
                transform: scale(1.06);
            }
            body.teach-mode-active.dark-mode #tuzi-theme-btn {
                background: #232836;
                border-color: #3B4259;
            }
            #tuzi-theme-btn .sun-icon {
                display: none;
                color: #FFFFFF;
            }
            #tuzi-theme-btn .moon-icon {
                display: block;
                color: #FFFFFF;
            }
            body.teach-mode-active.dark-mode #tuzi-theme-btn .sun-icon {
                display: block;
            }
            body.teach-mode-active.dark-mode #tuzi-theme-btn .moon-icon {
                display: none;
            }

            /* === PANGGUNG AVATAR AI BLOB (MALE = BIRU, FEMALE = PINK) === */
            #ai-blob-stage {
                pointer-events: auto;
                position: fixed;
                top: 46%;
                left: 50%;
                transform: translate(-50%, -50%);
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                z-index: 999991;
                user-select: none;
            }

            #blob-ptt-hint {
                margin-bottom: 14px;
                background: transparent;
                border: none;
                color: #6B7280;
                opacity: 0.55;
                font-size: 14px;
                font-weight: 600;
                letter-spacing: 0.8px;
                transition: all 0.25s ease;
                pointer-events: none;
            }
            body.teach-mode-active.dark-mode #blob-ptt-hint {
                color: #9CA3AF;
            }
            #ai-blob-stage.listening #blob-ptt-hint {
                color: #3F49CC;
                opacity: 0.9;
            }
            body.female-avatar #ai-blob-stage.listening #blob-ptt-hint {
                color: #EC4899;
            }

            #ai-blob-wrapper {
                position: relative;
                width: 300px;
                height: 300px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                animation: blobFloat 3.6s ease-in-out infinite;
            }

            @keyframes blobFloat {
                0%, 100% { transform: translateY(0px); }
                50% { transform: translateY(-12px); }
            }

            #ai-blob-body {
                position: relative;
                width: 230px;
                height: 215px;
                border-radius: 50% 50% 48% 52% / 50% 50% 50% 50%;
                background: radial-gradient(circle at 38% 30%, #6875F5 0%, #3F49CC 58%, #2E36A8 100%);
                box-shadow:
                    0 22px 45px rgba(55, 65, 195, 0.32),
                    inset -10px -14px 28px rgba(18, 22, 90, 0.45),
                    inset 12px 12px 24px rgba(155, 168, 255, 0.35);
                transition:
                    border-radius 0.38s cubic-bezier(0.34, 1.56, 0.64, 1),
                    transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1),
                    background 0.35s ease,
                    box-shadow 0.3s ease;
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 3;
            }

            .blob-lobe {
                position: absolute;
                background: radial-gradient(circle at 40% 35%, #6A77F7 0%, #3E48C9 75%, #2E36A8 100%);
                border-radius: 50%;
                opacity: 0;
                transform: scale(0.2);
                transition: all 0.38s cubic-bezier(0.34, 1.56, 0.64, 1);
                z-index: 2;
                pointer-events: none;
            }
            .blob-lobe.left {
                width: 96px;
                height: 82px;
                bottom: 24px;
                left: -28px;
                border-radius: 55% 45% 50% 50%;
            }
            .blob-lobe.right {
                width: 78px;
                height: 70px;
                bottom: 36px;
                right: -18px;
                border-radius: 45% 55% 50% 50%;
            }

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
                width: 242px;
                height: 222px;
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

            #ai-blob-wrapper.listening .blob-lobe.left {
                opacity: 1;
                transform: scale(1) translate(-6px, 4px) rotate(-14deg);
            }
            #ai-blob-wrapper.listening .blob-lobe.right {
                opacity: 1;
                transform: scale(1) translate(6px, 2px) rotate(12deg);
            }

            .blob-eyes-container {
                display: flex;
                align-items: center;
                justify-content: center;
                transform: translate(12px, -16px);
                transition: transform 0.28s cubic-bezier(0.25, 1, 0.5, 1);
                pointer-events: none;
            }
            #ai-blob-wrapper.listening .blob-eyes-container {
                transform: translate(24px, -24px) rotate(-4deg);
            }

            .blob-eye {
                width: 24px;
                height: 54px;
                background: #F4F6FF;
                border-radius: 999px;
                margin: 0 11px;
                box-shadow: 0 2px 10px rgba(15, 20, 80, 0.25);
                transition:
                    height 0.18s ease,
                    width 0.18s ease,
                    border-radius 0.2s ease,
                    transform 0.2s ease;
            }

            .blob-eyes-container.blink .blob-eye {
                height: 6px !important;
            }

            .blob-eyes-container.emo-excited .blob-eye {
                height: 44px;
                width: 26px;
                border-radius: 50% 50% 25% 25%;
                transform: scaleY(1.08);
            }
            .blob-eyes-container.emo-soft .blob-eye {
                height: 34px;
                border-radius: 14px 14px 999px 999px;
                opacity: 0.92;
            }
            .blob-eyes-container.emo-angry .blob-eye.left-eye {
                height: 40px;
                transform: rotate(16deg);
                border-radius: 6px 20px 16px 16px;
            }
            .blob-eyes-container.emo-angry .blob-eye.right-eye {
                height: 40px;
                transform: rotate(-16deg);
                border-radius: 20px 6px 16px 16px;
            }

            .blob-ripple {
                position: absolute;
                width: 230px;
                height: 230px;
                border-radius: 50%;
                border: 3px solid rgba(88, 101, 242, 0.45);
                opacity: 0;
                pointer-events: none;
                z-index: 1;
            }
            body.female-avatar .blob-ripple {
                border-color: rgba(244, 114, 182, 0.50);
            }
            #ai-blob-wrapper.listening .blob-ripple.r1 {
                animation: rippleOut 1.6s infinite ease-out;
            }
            #ai-blob-wrapper.listening .blob-ripple.r2 {
                animation: rippleOut 1.6s infinite ease-out 0.8s;
            }
            @keyframes rippleOut {
                0% { transform: scale(0.95); opacity: 0.85; }
                100% { transform: scale(1.65); opacity: 0; }
            }

            .bubble-particle {
                position: absolute;
                border-radius: 50%;
                background: radial-gradient(circle at 30% 30%, #9BA6FF, #4D59E0);
                opacity: 0;
                pointer-events: none;
                z-index: 4;
            }
            body.female-avatar .bubble-particle {
                background: radial-gradient(circle at 30% 30%, #FBCFE8, #EC4899);
            }
            @keyframes bubbleRise {
                0% { transform: translate(0, 0) scale(0.4); opacity: 0.9; }
                100% { transform: translate(var(--bx), var(--by)) scale(1.15); opacity: 0; }
            }

            /* === SIDEBAR MENU KIRI === */
            #tuzi-sidebar {
                pointer-events: auto;
                position: fixed;
                top: 0;
                left: 0;
                width: 280px;
                height: 100vh;
                background: #FFFFFF;
                color: #0D0D0D;
                border-right: 2px solid #E5E5E5;
                box-shadow: 10px 0 30px rgba(0, 0, 0, 0.08);
                display: flex;
                flex-direction: column;
                padding: 22px 18px;
                box-sizing: border-box;
                transform: translateX(-105%);
                transition: transform 0.25s cubic-bezier(0.25, 1, 0.5, 1), background 0.3s ease;
                z-index: 999999;
            }
            body.teach-mode-active.dark-mode #tuzi-sidebar {
                background: #12151E;
                color: #F3F4F6;
                border-right-color: #262B3C;
            }
            #tuzi-sidebar.open {
                transform: translateX(0);
            }

            .sidebar-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
                padding: 0 4px;
            }
            .sidebar-brand {
                display: flex;
                align-items: center;
                font-size: 20px;
                font-weight: 900;
                color: inherit;
                letter-spacing: 0.8px;
            }
            .sidebar-brand svg { margin-right: 10px; }
            .sidebar-collapse-btn {
                width: 34px;
                height: 34px;
                border-radius: 8px;
                background: transparent;
                color: inherit;
                border: none;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .sidebar-collapse-btn:hover { background: rgba(128,128,128,0.12); }

            .mode-switch-pill {
                display: flex;
                background: #F2F2F2;
                border: 1.5px solid #E0E0E0;
                border-radius: 999px;
                padding: 4px;
                margin-bottom: 24px;
            }
            body.teach-mode-active.dark-mode .mode-switch-pill {
                background: #1C202E;
                border-color: #2E3448;
            }
            .mode-tab-btn {
                flex: 1;
                height: 36px;
                border: none;
                border-radius: 999px;
                background: transparent;
                color: #6B7280;
                font-size: 13.5px;
                font-weight: 700;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: all 0.2s ease;
                outline: none;
            }
            .mode-tab-btn.active {
                background: #0D0D0D;
                color: #FFFFFF;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
            }
            body.teach-mode-active.dark-mode .mode-tab-btn.active {
                background: #FFFFFF;
                color: #0D0D0D;
            }
            .live-badge {
                font-size: 9px;
                font-weight: 800;
                padding: 2px 5px;
                border-radius: 6px;
                background: rgba(128, 128, 128, 0.25);
                margin-left: 6px;
            }

            .sidebar-menu-list {
                display: flex;
                flex-direction: column;
            }
            .sidebar-item {
                display: flex;
                align-items: center;
                height: 44px;
                padding: 0 12px;
                margin-bottom: 6px;
                border-radius: 12px;
                background: transparent;
                color: inherit;
                border: none;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                text-align: left;
                transition: background 0.15s ease;
                outline: none;
            }
            .sidebar-item svg { margin-right: 14px; }
            .sidebar-item:hover { background: rgba(128, 128, 128, 0.12); }
            .sidebar-section-label {
                font-size: 12px;
                font-weight: 700;
                color: #777777;
                margin: 18px 0 8px 12px;
                text-transform: uppercase;
                letter-spacing: 0.6px;
            }

            /* === MODAL SETTINGS (GENDER SWAP MALE BIRU / FEMALE PINK MUDA) === */
            #tuzi-settings-modal {
                pointer-events: auto;
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background: rgba(0, 0, 0, 0.45);
                align-items: center;
                justify-content: center;
                z-index: 9999999;
            }
            #tuzi-settings-modal.open {
                display: flex;
            }
            .settings-card {
                width: 400px;
                background: #FFFFFF;
                color: #0D0D0D;
                border: 2.5px solid #0D0D0D;
                border-radius: 24px;
                padding: 24px 26px;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
                box-sizing: border-box;
            }
            body.teach-mode-active.dark-mode .settings-card {
                background: #161925;
                color: #FFFFFF;
                border-color: #32394E;
            }
            .settings-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
            }
            .settings-title {
                font-size: 18px;
                font-weight: 800;
            }
            .settings-close {
                background: transparent;
                color: inherit;
                border: none;
                font-size: 20px;
                font-weight: bold;
                cursor: pointer;
            }
            .settings-label {
                font-size: 12.5px;
                font-weight: 700;
                color: #6B7280;
                margin-bottom: 10px;
                text-transform: uppercase;
                letter-spacing: 0.6px;
            }

            .gender-switch-container {
                display: flex;
                background: #F1F5F9;
                padding: 5px;
                border-radius: 999px;
                border: 1.5px solid #E2E8F0;
                margin-bottom: 20px;
            }
            body.teach-mode-active.dark-mode .gender-switch-container {
                background: #0F121A;
                border-color: #262C3D;
            }
            .gender-switch-btn {
                flex: 1;
                height: 44px;
                border: none;
                border-radius: 999px;
                font-size: 14px;
                font-weight: 800;
                cursor: pointer;
                background: transparent;
                color: #64748B;
                transition: all 0.25s ease;
                outline: none;
            }
            .gender-switch-btn.male-btn.active {
                background: #3B82F6;
                color: #FFFFFF;
                box-shadow: 0 4px 14px rgba(59, 130, 246, 0.40);
            }
            .gender-switch-btn.female-btn.active {
                background: #F9A8D4;
                color: #831843;
                box-shadow: 0 4px 14px rgba(244, 114, 182, 0.45);
            }

            #female-voice-options {
                display: none;
                flex-direction: column;
                margin-top: 6px;
            }
            #female-voice-options.show {
                display: flex;
            }
            .fmode-row {
                display: flex;
                justify-content: space-between;
            }
            .fmode-btn {
                flex: 1;
                height: 38px;
                margin: 0 4px;
                border-radius: 12px;
                border: 1.5px solid #E2E8F0;
                background: transparent;
                color: inherit;
                font-size: 12.5px;
                font-weight: 700;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            .fmode-btn.active {
                background: #F472B6;
                color: #FFFFFF;
                border-color: #F472B6;
            }

            /* === TAMPILAN CHATBOT (TOTAL MUTE MODE) === */
            #tuzi-chat-view {
                pointer-events: auto;
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background: #FFFFFF;
                flex-direction: column;
                justify-content: space-between;
                align-items: center;
                padding: 28px 40px 30px 40px;
                box-sizing: border-box;
                z-index: 999993;
            }
            body.teach-mode-active.dark-mode #tuzi-chat-view {
                background: #0B0D13;
            }
            body.teach-mode-active.chat-tab-open #tuzi-chat-view {
                display: flex;
            }
            body.teach-mode-active.chat-tab-open .discord-call-wrapper {
                display: none !important;
            }

            .chat-top-bar {
                width: 100%;
                max-width: 820px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding-bottom: 14px;
                border-bottom: 1.5px solid #EBEBEB;
            }
            body.teach-mode-active.dark-mode .chat-top-bar {
                border-bottom-color: #222634;
            }
            .chat-top-title {
                font-size: 16px;
                font-weight: 800;
                color: #0D0D0D;
                letter-spacing: 0.5px;
            }
            body.teach-mode-active.dark-mode .chat-top-title {
                color: #FFFFFF;
            }

            #tuzi-chat-messages {
                width: 100%;
                max-width: 820px;
                flex: 1;
                overflow-y: auto;
                padding: 24px 8px;
                display: flex;
                flex-direction: column;
                box-sizing: border-box;
            }
            .chat-bubble-row {
                display: flex;
                width: 100%;
                margin-bottom: 16px;
            }
            .chat-bubble-row.user { justify-content: flex-end; }
            .chat-bubble-row.bot { justify-content: flex-start; }
            .chat-bubble {
                max-width: 72%;
                padding: 13px 18px;
                border-radius: 18px;
                font-size: 15px;
                font-weight: 500;
                line-height: 1.5;
                word-wrap: break-word;
            }
            .chat-bubble-row.user .chat-bubble {
                background: #0D0D0D;
                color: #FFFFFF;
                border-bottom-right-radius: 4px;
            }
            body.teach-mode-active.dark-mode .chat-bubble-row.user .chat-bubble {
                background: #3B82F6;
            }
            .chat-bubble-row.bot .chat-bubble {
                background: #F7F7F7;
                color: #0D0D0D;
                border: 1.5px solid #0D0D0D;
                border-bottom-left-radius: 4px;
            }
            body.teach-mode-active.dark-mode .chat-bubble-row.bot .chat-bubble {
                background: #181C28;
                color: #F3F4F6;
                border-color: #2E3446;
            }

            .chat-input-bar {
                width: 100%;
                max-width: 820px;
                display: flex;
                align-items: center;
                background: #FFFFFF;
                border: 2px solid #0D0D0D;
                border-radius: 20px;
                padding: 8px 12px 8px 20px;
                box-sizing: border-box;
            }
            body.teach-mode-active.dark-mode .chat-input-bar {
                background: #151822;
                border-color: #2E3446;
            }
            #tuzi-chat-input {
                flex: 1;
                border: none;
                outline: none;
                background: transparent;
                color: inherit;
                font-size: 15px;
                font-weight: 500;
                font-family: inherit;
            }
            body.teach-mode-active.dark-mode #tuzi-chat-input {
                color: #FFFFFF;
            }
            #tuzi-chat-send {
                width: 42px;
                height: 42px;
                border-radius: 14px;
                background: #0D0D0D;
                color: #FFFFFF;
                border: none;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-left: 10px;
            }
            body.teach-mode-active.dark-mode #tuzi-chat-send {
                background: #3B82F6;
            }

            /* === BOTTOM DOCK === */
            .discord-call-wrapper {
                pointer-events: auto;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-bottom: 34px;
                margin-top: auto;
                z-index: 10;
            }
            .discord-call-wrapper > * {
                margin: 0 10px !important;
            }

            .discord-dock {
                display: flex;
                align-items: center;
                background: #111214;
                padding: 8px 10px;
                border-radius: 20px;
                box-shadow: 0 12px 32px rgba(0, 0, 0, 0.25);
                border: 1px solid rgba(255, 255, 255, 0.08);
            }

            .lang-btn {
                height: 52px;
                min-width: 58px;
                padding: 0 18px;
                margin: 0 4px;
                background: transparent;
                color: #B5BAC1;
                border: none;
                border-radius: 14px;
                font-size: 14px;
                font-weight: 800;
                cursor: pointer;
                transition: all 0.15s ease;
            }
            .lang-btn:hover {
                background: #2B2D31;
                color: #DBDEE1;
            }
            .lang-btn.active {
                background: #FFFFFF;
                color: #0D0D0D;
                box-shadow: 0 3px 10px rgba(0, 0, 0, 0.25);
            }

            .hangup-btn {
                width: 78px;
                height: 68px;
                background: #DA373C;
                color: #FFFFFF;
                border: none;
                border-radius: 20px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 12px 30px rgba(218, 55, 60, 0.35);
                transition: all 0.15s ease;
                outline: none;
            }
            .hangup-btn:hover {
                background: #A12828;
                transform: scale(1.04);
            }
            .hangup-btn:active {
                transform: scale(0.96);
            }
        `;
        document.head.appendChild(style);

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
                        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                        </svg>
                        <span>New chat</span>
                    </button>

                    <button class="sidebar-item" id="menu-search-chat">
                        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="11" cy="11" r="8"></circle>
                            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                        <span>Search chats</span>
                    </button>

                    <button class="sidebar-item" id="menu-open-settings">
                        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="12" cy="12" r="3"></circle>
                            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
                        </svg>
                        <span>Settings</span>
                    </button>

                    <div class="sidebar-section-label">Language Target</div>
                    <button class="sidebar-item sidebar-lang-shortcut" data-lang="en-US">
                        <span>🇺🇸 &nbsp;English Practice</span>
                    </button>
                    <button class="sidebar-item sidebar-lang-shortcut" data-lang="ja-JP">
                        <span>🇯🇵 &nbsp;Japanese Practice</span>
                    </button>
                    <button class="sidebar-item sidebar-lang-shortcut" data-lang="id-ID">
                        <span>🇮🇩 &nbsp;Indonesian Practice</span>
                    </button>
                </div>
            </div>

            <div id="tuzi-settings-modal">
                <div class="settings-card">
                    <div class="settings-header">
                        <div class="settings-title">Settings</div>
                        <button class="settings-close" id="close-settings-btn">✕</button>
                    </div>

                    <div class="settings-label">AI Companion Gender & Voice</div>
                    <div class="gender-switch-container">
                        <button class="gender-switch-btn male-btn active" id="btn-gender-male">♂ Male</button>
                        <button class="gender-switch-btn female-btn" id="btn-gender-female">♀ Female</button>
                    </div>

                    <div id="female-voice-options">
                        <div class="settings-label">Female Personality Type</div>
                        <div class="fmode-row">
                            <button class="fmode-btn active" data-fmode="1">Dandere</button>
                            <button class="fmode-btn" data-fmode="2">Shy</button>
                            <button class="fmode-btn" data-fmode="3">Mommy</button>
                        </div>
                    </div>
                </div>
            </div>

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

            <div class="discord-call-wrapper">
                <div class="discord-dock">
                    <button class="lang-btn active" data-lang="en-US">EN</button>
                    <button class="lang-btn" data-lang="ja-JP">JA</button>
                    <button class="lang-btn" data-lang="id-ID">ID</button>
                </div>

                <button class="hangup-btn" id="exit-teach-btn" title="Tutup Aplikasi">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/>
                    </svg>
                </button>
            </div>
        `;
        document.body.appendChild(overlay);

        let themeBtn = document.getElementById('tuzi-theme-btn');
        themeBtn.onclick = () => {
            document.body.classList.toggle('dark-mode');
        };

        let settingsModal = document.getElementById('tuzi-settings-modal');
        let openSettingsBtn = document.getElementById('menu-open-settings');
        let closeSettingsBtn = document.getElementById('close-settings-btn');
        let btnMale = document.getElementById('btn-gender-male');
        let btnFemale = document.getElementById('btn-gender-female');
        let femaleOptions = document.getElementById('female-voice-options');

        openSettingsBtn.onclick = () => {
            sidebar.classList.remove('open');
            settingsModal.classList.add('open');
        };
        closeSettingsBtn.onclick = () => settingsModal.classList.remove('open');
        settingsModal.onclick = (e) => {
            if (e.target === settingsModal) settingsModal.classList.remove('open');
        };

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

        document.querySelectorAll('.fmode-btn').forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll('.fmode-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                let modeNum = btn.getAttribute('data-fmode');
                if (window.pyBridge) window.pyBridge.start_ptt('FMODE:' + modeNum);
            };
        });

        let blobStage = document.getElementById('ai-blob-stage');
        let blobWrapper = document.getElementById('ai-blob-wrapper');
        let blobBody = document.getElementById('ai-blob-body');
        let blobEyes = document.getElementById('blob-eyes');
        let bubbleLayer = document.getElementById('blob-bubble-layer');
        let pttHint = document.getElementById('blob-ptt-hint');
        let isHolding = false;
        let bubbleTimer = null;

        setInterval(() => {
            blobEyes.classList.add('blink');
            setTimeout(() => blobEyes.classList.remove('blink'), 150);
        }, 3800);

        window.addEventListener('mousemove', (e) => {
            if (isHolding) return;
            let dx = ((e.clientX - window.innerWidth / 2) / (window.innerWidth / 2)) * 18;
            let dy = ((e.clientY - window.innerHeight / 2) / (window.innerHeight / 2)) * 14;
            blobEyes.style.transform = `translate(${10 + dx}px, ${-14 + dy}px)`;
        });

        function spawnBubble() {
            if (!isHolding) return;
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
            blobEyes.classList.remove('emo-excited', 'emo-soft', 'emo-angry');
            if (emo === 'excited') blobEyes.classList.add('emo-excited');
            else if (emo === 'soft' || emo === 'flirty') blobEyes.classList.add('emo-soft');
            else if (emo === 'angry' || emo === 'sad') blobEyes.classList.add('emo-angry');
        };

        function startHold(e) {
            e.preventDefault();
            if (isHolding || document.body.classList.contains('chat-tab-open')) return;
            isHolding = true;
            blobWrapper.classList.add('listening');
            blobStage.classList.add('listening');
            blobEyes.style.transform = '';
            pttHint.innerText = 'listening...';
            spawnBubble();
            bubbleTimer = setInterval(spawnBubble, 220);
            if (window.pyBridge) window.pyBridge.start_ptt(window.currentTeachLang);
        }

        function stopHold(e) {
            if (!isHolding) return;
            isHolding = false;
            clearInterval(bubbleTimer);
            blobWrapper.classList.remove('listening');
            blobStage.classList.remove('listening');
            pttHint.innerText = 'hold to speak';
            if (window.pyBridge) window.pyBridge.stop_ptt();
        }

        blobWrapper.addEventListener('mousedown', startHold);
        window.addEventListener('mouseup', stopHold);

        let sidebar = document.getElementById('tuzi-sidebar');
        let burgerBtn = document.getElementById('tuzi-burger-btn');
        let closeSidebarBtn = document.getElementById('close-sidebar-btn');
        let tabChat = document.getElementById('tab-btn-chat');
        let tabLive = document.getElementById('tab-btn-live');
        let chatMessages = document.getElementById('tuzi-chat-messages');
        let chatInput = document.getElementById('tuzi-chat-input');
        let chatSend = document.getElementById('tuzi-chat-send');

        burgerBtn.onclick = () => sidebar.classList.add('open');
        closeSidebarBtn.onclick = () => sidebar.classList.remove('open');

        function switchSubMode(mode) {
            if (mode === 'chat') {
                tabChat.classList.add('active');
                tabLive.classList.remove('active');
                document.body.classList.add('chat-tab-open');
                // Kosongkan subtitle Live saat masuk ke Chat Mode
                let subElem = document.getElementById('tuzi-subtitle-box');
                if (subElem) {
                    subElem.innerHTML = '';
                    subElem.classList.remove('show');
                }
                if (window.pyBridge) window.pyBridge.start_ptt('MODE:CHAT');
                setTimeout(() => chatInput.focus(), 100);
            } else {
                tabLive.classList.add('active');
                tabChat.classList.remove('active');
                document.body.classList.remove('chat-tab-open');
                if (window.pyBridge) window.pyBridge.start_ptt('MODE:LIVE');
            }
            sidebar.classList.remove('open');
        }

        tabChat.onclick = () => switchSubMode('chat');
        tabLive.onclick = () => switchSubMode('live');

        document.getElementById('menu-new-chat').onclick = () => {
            chatMessages.innerHTML = `
                <div class="chat-bubble-row bot">
                    <div class="chat-bubble">Sesi chat baru dimulai (Mode Mute). Silakan ketik kalimat yang ingin kamu latih.</div>
                </div>
            `;
            switchSubMode('chat');
        };

        document.getElementById('menu-search-chat').onclick = () => {
            switchSubMode('chat');
            chatInput.focus();
        };

        window.appendTeachChatBubble = function(sender, text) {
            if (!text || !text.trim()) return;
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
            let text = chatInput.value.trim();
            if (!text) return;
            window.appendTeachChatBubble('user', text);
            chatInput.value = '';
            if (window.pyBridge) {
                window.pyBridge.start_ptt('CHAT:' + text);
            }
        }

        chatSend.onclick = sendChatMessage;
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') sendChatMessage();
        });

        // Pisahkan output Chat Mode dan Live Mode
        const origSetSubtitle = window.setSubtitle;
        window.setSubtitle = function(htmlContent) {
            const isChatTab = document.body.classList.contains('chat-tab-open');
            if (isChatTab) {
                // Jika di Chat Mode: HANYA masukkan ke bubble chat, JANGAN tampilkan di Live subtitle
                if (htmlContent && htmlContent.trim() !== '') {
                    window.appendTeachChatBubble('bot', htmlContent);
                }
            } else {
                // Jika di Live Mode: tampilkan di subtitle bawah avatar
                if (origSetSubtitle) origSetSubtitle(htmlContent);
                if (htmlContent && htmlContent.trim() !== '') {
                    window.appendTeachChatBubble('bot', htmlContent);
                }
            }
        };

        window.currentTeachLang = 'en-US';
        function setLanguage(lang) {
            window.currentTeachLang = lang;
            document.querySelectorAll('.lang-btn').forEach(b => {
                b.classList.toggle('active', b.getAttribute('data-lang') === lang);
            });
        }

        document.querySelectorAll('.lang-btn').forEach(btn => {
            btn.addEventListener('click', () => setLanguage(btn.getAttribute('data-lang')));
        });
        document.querySelectorAll('.sidebar-lang-shortcut').forEach(btn => {
            btn.addEventListener('click', () => {
                setLanguage(btn.getAttribute('data-lang'));
                sidebar.classList.remove('open');
            });
        });

        // Tombol Hang-up memanggil Python os._exit(0) tanpa menghancurkan QObject terlebih dahulu
        function triggerHangup() {
            if (window.pyBridge) {
                window.pyBridge.start_ptt('CMD:EXIT');
                window.pyBridge.exit_teach_ui();
            }
        }
        document.getElementById('exit-teach-btn').addEventListener('click', triggerHangup);
        document.getElementById('exit-teach-btn-chat').addEventListener('click', triggerHangup);

        window.setTeachModeUI = function(active) {
            if (active) {
                document.body.classList.add('teach-mode-active');
            } else {
                document.body.classList.remove('teach-mode-active');
                document.body.classList.remove('chat-tab-open');
                sidebar.classList.remove('open');
            }
        };

        window.setTeachModeUI(true);
    })();
    """
    return qwebchannel_code + "\n" + ui_code