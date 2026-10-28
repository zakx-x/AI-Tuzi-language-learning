"""
discord_presence.py — Discord Rich Presence untuk Tuzi (via pypresence).

Menampilkan status Tuzi di profil Discord:
    Learning English with Tuzi
    Tuzi is talking · Voice call

Catatan desain:
  * Berjalan di thread sendiri -> Discord mati / lambat TIDAK akan membekukan Tuzi.
  * Auto-reconnect kalau Discord dibuka belakangan atau tertutup di tengah jalan.
  * Update digabung (coalesce) & dibatasi ~1x / 4 detik (batas Discord: 5 update / 20 detik).
  * Privasi: hanya mengirim status, nama persona, bahasa target, dan mode (voice/chat).
    Isi percakapan, nama, dan prompt kustom TIDAK PERNAH dikirim.
  * Kalau pypresence belum terpasang atau DISCORD_CLIENT_ID kosong, modul ini diam saja.
"""

import asyncio
import threading
import time

try:
    from pypresence import Presence
except ImportError:  # pypresence belum di-install
    Presence = None


LANG_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "id": "Indonesian",
    "ko": "Korean",
    "zh": "Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ar": "Arabic",
    "ru": "Russian",
}

STATUS_TEXT = {
    "idle": "Idle",
    "listening": "Speaking to Tuzi",
    "thinking": "Tuzi is thinking...",
    "speaking": "Tuzi is talking",
}


class DiscordPresence:
    def __init__(
        self,
        client_id,
        enabled=True,
        context_provider=None,
        large_image="tuzi",
        small_icons=False,
        min_interval=4.0,
        reconnect_delay=15.0,
    ):
        """
        client_id         : Application ID dari Discord Developer Portal.
        context_provider  : fungsi tanpa argumen -> dict {"persona", "language", "mode"}.
        large_image       : nama art asset besar yang di-upload di Developer Portal.
        small_icons       : True jika kamu upload asset kecil bernama idle/listening/thinking/speaking.
        """
        self.client_id = str(client_id or "").strip()
        self.enabled = bool(enabled) and bool(self.client_id)
        self._ctx = context_provider
        self.large_image = large_image
        self.small_icons = small_icons
        self.min_interval = min_interval
        self.reconnect_delay = reconnect_delay

        self._state = "idle"
        self._start_ts = int(time.time())
        self._rpc = None
        self._last_payload = None
        self._warned = False

        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- public
    def start(self):
        if not self.enabled:
            print("[Discord RPC] Nonaktif (DISCORD_CLIENT_ID kosong di config.py).")
            return
        if Presence is None:
            print("[Discord RPC] pypresence belum terpasang -> jalankan: pip install pypresence")
            self.enabled = False
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="DiscordRPC", daemon=True)
        self._thread.start()

    def set_state(self, state: str):
        """state: 'idle' | 'listening' | 'thinking' | 'speaking'. Aman dipanggil dari thread mana pun."""
        if state not in STATUS_TEXT:
            return
        self._state = state
        self._dirty.set()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # --------------------------------------------------------------- internal
    def _build_payload(self) -> dict:
        ctx = {}
        if self._ctx:
            try:
                ctx = self._ctx() or {}
            except Exception:
                ctx = {}

        lang_code = str(ctx.get("language") or "en").split("-")[0].lower()
        language = LANG_NAMES.get(lang_code, lang_code.upper() or "a language")
        persona = ctx.get("persona") or "Tuzi"
        mode = "Text chat" if ctx.get("mode") == "chat" else "Voice call"

        payload = {
            "details": f"Learning {language} with Tuzi",
            "state": f"{STATUS_TEXT[self._state]} \u00b7 {mode}",
            "start": self._start_ts,
            "large_text": f"Tuzi \u2014 {persona} mode",
        }
        if self.large_image:
            payload["large_image"] = self.large_image
        if self.small_icons:
            payload["small_image"] = self._state
            payload["small_text"] = STATUS_TEXT[self._state]
        return payload

    def _connect(self) -> bool:
        try:
            rpc = Presence(self.client_id)
            rpc.connect()
        except Exception as e:
            if type(e).__name__ == "InvalidID":
                print("[Discord RPC] DISCORD_CLIENT_ID tidak valid -> fitur dimatikan.")
                self.enabled = False
            elif not self._warned:
                print("[Discord RPC] Discord desktop belum terdeteksi, akan dicoba lagi otomatis.")
                self._warned = True
            return False
        self._rpc = rpc
        self._last_payload = None
        self._warned = False
        print("[Discord RPC] Terhubung ke Discord.")
        return True

    def _drop(self, err):
        print(f"[Discord RPC] Koneksi terputus ({type(err).__name__}), mencoba menyambung ulang...")
        try:
            self._rpc.close()
        except Exception:
            pass
        self._rpc = None

    def _shutdown(self):
        if self._rpc is not None:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None

    def _run(self):
        # pypresence memakai asyncio di balik layar -> thread ini butuh event loop sendiri
        asyncio.set_event_loop(asyncio.new_event_loop())

        last_sent = 0.0
        last_poll = 0.0

        while not self._stop.is_set():
            if self._rpc is None:
                if not self._connect():
                    if not self.enabled:
                        return
                    if self._stop.wait(self.reconnect_delay):
                        break
                    continue
                self._dirty.set()

            triggered = self._dirty.wait(timeout=1.0)
            if not triggered and time.time() - last_poll < 10.0:
                continue
            last_poll = time.time()  # tiap 10 detik cek ulang persona/bahasa walau state sama

            payload = self._build_payload()
            if payload == self._last_payload:
                self._dirty.clear()
                continue

            # Batasi laju update; state terbaru yang dikirim (update berurutan digabung)
            remaining = self.min_interval - (time.time() - last_sent)
            if remaining > 0 and self._stop.wait(remaining):
                break
            self._dirty.clear()
            payload = self._build_payload()

            try:
                self._rpc.update(**payload)
                self._last_payload = payload
                last_sent = time.time()
            except Exception as e:
                self._drop(e)

        self._shutdown()
