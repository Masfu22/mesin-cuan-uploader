"""
koyeb_app/main.py - YouTube Upload Scheduler
Polling GDrive setiap 5 menit, upload ke YouTube kalau sudah waktunya.
HTTP server di port 8080 untuk keep-alive ping dari Uptime.com

Notifikasi Telegram — hanya event penting (tidak spam):
  🚀 Bot start
  📥 Mulai download & upload
  ✅ Upload berhasil
  ❌ Upload gagal
  ⚠️  Error channel (token expired, dll)
  📊 Daily summary (tiap tengah malam UTC)

Setup (set env vars di Koyeb):
  TELEGRAM_BOT_TOKEN  → token dari @BotFather
  TELEGRAM_CHAT_ID    → dari api.telegram.org/bot<TOKEN>/getUpdates
"""

import os
import json
import time
import base64
import tempfile
import logging
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("uploader")

POLL_INTERVAL_SEC = max(30, int(os.environ.get("POLL_INTERVAL_SEC", "300")))  # minimum 30 detik
GDRIVE_ROOT       = "mesin_cuan"
QUEUE_FOLDER      = "queue"
DONE_FOLDER       = "done"

CHANNELS = [
    {"id": "ch_id_horror", "env_key": "TOKEN_CH_ID_HORROR", "language": "id"},
    {"id": "ch_id_psych",  "env_key": "TOKEN_CH_ID_PSYCH",  "language": "id"},
    {"id": "ch_en_horror", "env_key": "TOKEN_CH_EN_HORROR", "language": "en"},
    {"id": "ch_en_psych",  "env_key": "TOKEN_CH_EN_PSYCH",  "language": "en"},
]

_status = {
    "last_poll":      None,
    "uploads_today":  0,
    "errors_today":   0,
    "skipped_today":  0,
    "running":        True,
    "last_summary":   None,   # tanggal terakhir summary dikirim (YYYY-MM-DD)
    "per_channel":    {ch["id"]: 0 for ch in CHANNELS},  # upload per channel hari ini
}


# ─── Telegram Notifikasi ──────────────────────────────────────────────────────

def _send_telegram(message: str):
    """
    Kirim pesan ke Telegram via Bot API.
    Gagal kirim hanya di-log, tidak mengganggu proses upload.

    Env vars:
      TELEGRAM_BOT_TOKEN  → dari @BotFather
      TELEGRAM_CHAT_ID    → chat id tujuan
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return  # Env belum di-set, skip tanpa error

    try:
        url     = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML"
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(f"Telegram response: {resp.status}")
    except Exception as e:
        logger.warning(f"Gagal kirim Telegram notif: {e}")


# ─── Daily Summary ────────────────────────────────────────────────────────────

def _check_and_send_daily_summary(now_utc: datetime):
    """
    Kirim daily summary tiap tengah malam UTC.
    Hanya dikirim sekali per hari, dicek setiap polling.
    Setelah kirim, reset semua counter harian.
    """
    today_str = now_utc.strftime("%Y-%m-%d")

    # Kirim summary di jam 00:00–00:05 UTC, dan belum pernah kirim hari ini
    is_midnight_window = now_utc.hour == 0 and now_utc.minute < 5
    already_sent_today = _status["last_summary"] == today_str

    if not is_midnight_window or already_sent_today:
        return

    # Susun ringkasan per channel
    per_ch_lines = ""
    for ch_id, count in _status["per_channel"].items():
        per_ch_lines += f"  • {ch_id}: <b>{count} video</b>\n"

    summary_msg = (
        f"📊 <b>Daily Summary — {today_str}</b>\n\n"
        f"✅ Upload berhasil : <b>{_status['uploads_today']} video</b>\n"
        f"❌ Error           : <b>{_status['errors_today']}x</b>\n"
        f"⏭️ Skip (jadwal)   : <b>{_status['skipped_today']}x</b>\n\n"
        f"<b>Per Channel:</b>\n{per_ch_lines}\n"
        f"Polling interval: {POLL_INTERVAL_SEC // 60} menit"
    )

    _send_telegram(summary_msg)
    logger.info("Daily summary terkirim ke Telegram")

    # Reset semua counter harian
    _status["uploads_today"] = 0
    _status["errors_today"]  = 0
    _status["skipped_today"] = 0
    _status["last_summary"]  = today_str
    for ch_id in _status["per_channel"]:
        _status["per_channel"][ch_id] = 0


# ─── HTTP Keep-alive server ───────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = json.dumps({
            "status":        "running",
            "last_poll":     _status["last_poll"],
            "uploads_today": _status["uploads_today"],
            "errors_today":  _status["errors_today"],
            "skipped_today": _status["skipped_today"],
            "per_channel":   _status["per_channel"],
        })
        self.wfile.write(resp.encode())

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def _start_health_server():
    port   = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server jalan di port {port}")
    server.serve_forever()


# ─── Token helpers ────────────────────────────────────────────────────────────

def _load_creds_from_env(env_key: str):
    """
    Muat OAuth token Google dari environment variable.
    Format utama: JSON authorized-user dari Google OAuth.
    Kompatibilitas: Base64 yang isinya JSON (bukan pickle).
    """
    value = os.environ.get(env_key)
    if not value:
        raise EnvironmentError(f"Env variable '{env_key}' tidak ditemukan.")

    try:
        token_info = json.loads(value)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(value.encode("utf-8"), validate=True)
            token_info = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ValueError(
                f"'{env_key}' harus berisi OAuth token JSON mentah atau Base64 dari JSON."
            ) from exc

    if not isinstance(token_info, dict) or not token_info.get("refresh_token"):
        raise ValueError(
            f"'{env_key}' bukan OAuth authorized-user JSON yang valid: refresh_token tidak ada."
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_authorized_user_info(token_info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise ValueError(f"Kredensial pada '{env_key}' tidak valid atau gagal diperbarui.")
    return creds


def _get_upload_privacy(metadata: dict) -> str:
    """Privasi upload aman secara default; ubah lewat YOUTUBE_PRIVACY_STATUS."""
    privacy = os.environ.get("YOUTUBE_PRIVACY_STATUS", "private").strip().lower()
    allowed = {"private", "unlisted", "public"}
    if privacy not in allowed:
        raise ValueError(
            "YOUTUBE_PRIVACY_STATUS harus private, unlisted, atau public."
        )
    return privacy


def _get_drive_service(creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds)


def _get_youtube_service(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds)


# ─── GDrive helpers ───────────────────────────────────────────────────────────

def _find_folder(service, name: str, parent_id) -> str | None:
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id)").execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None


def _ensure_folder(service, name: str, parent_id) -> str:
    folder_id = _find_folder(service, name, parent_id)
    if folder_id:
        return folder_id
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return service.files().create(body=meta, fields="id").execute()["id"]


def _list_subfolders(service, parent_id: str) -> list:
    query   = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)", orderBy="name").execute()
    return results.get("files", [])


def _find_file_in_folder(service, folder_id: str, filename: str) -> str | None:
    query   = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files   = results.get("files", [])
    return files[0]["id"] if files else None


def _download_file(service, file_id: str, dest_path: str):
    from googleapiclient.http import MediaIoBaseDownload
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def _move_to_done(service, folder_id: str, done_root_id: str, ch_id: str):
    ch_done    = _ensure_folder(service, ch_id, done_root_id)
    file_info  = service.files().get(fileId=folder_id, fields="parents").execute()
    old_parent = file_info.get("parents", [""])[0]
    service.files().update(
        fileId=folder_id,
        addParents=ch_done,
        removeParents=old_parent,
        fields="id, parents"
    ).execute()


def _update_metadata_file(service, file_id: str, metadata: dict):
    """Simpan status upload ke metadata Drive agar retry tidak mengunggah duplikat."""
    from googleapiclient.http import MediaFileUpload

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as tmp:
            temp_path = tmp.name
            json.dump(metadata, tmp, ensure_ascii=False, indent=2)
        media = MediaFileUpload(temp_path, mimetype="application/json", resumable=False)
        service.files().update(fileId=file_id, media_body=media).execute()
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# ─── YouTube upload ───────────────────────────────────────────────────────────

def _upload_to_youtube(yt_service, video_path: str, thumbnail_path: str, metadata: dict) -> str:
    from googleapiclient.http import MediaFileUpload

    language = metadata.get("language", "id")
    body = {
        "snippet": {
            "title":                metadata["title"],
            "description":          metadata["description"],
            "tags":                 metadata.get("tags", []),
            "categoryId":           metadata.get("category_id", "27"),
            "defaultLanguage":      language,
            "defaultAudioLanguage": language,
        },
        "status": {
            "privacyStatus":           _get_upload_privacy(metadata),
            "selfDeclaredMadeForKids": metadata.get("made_for_kids", False),
            "embeddable":              True,
            "publicStatsViewable":     True,
            "license":                 "youtube",
            "containsSyntheticMedia":  metadata.get("contains_ai_content", True),
        },
        "recordingDetails": {
            "recordingDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
        }
    }

    media   = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
    request = yt_service.videos().insert(part="snippet,status,recordingDetails", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"Upload progress: {int(status.progress()*100)}%")

    video_id = response["id"]
    url      = f"https://www.youtube.com/watch?v={video_id}"

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            media_thumb = MediaFileUpload(thumbnail_path, mimetype="image/png")
            yt_service.thumbnails().set(videoId=video_id, media_body=media_thumb).execute()
        except Exception as e:
            logger.warning(f"Thumbnail gagal: {e}")

    return url


# ─── Main polling ─────────────────────────────────────────────────────────────

def process_queue():
    now_utc = datetime.now(timezone.utc)
    logger.info(f"Polling — {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _status["last_poll"] = now_utc.isoformat()

    # Cek & kirim daily summary kalau sudah tengah malam
    _check_and_send_daily_summary(now_utc)

    for ch in CHANNELS:
        ch_id   = ch["id"]
        env_key = ch["env_key"]

        # Channel tanpa token memang belum diaktifkan; jangan dianggap error
        # dan jangan mengirim notifikasi error berulang setiap polling.
        if not os.environ.get(env_key, "").strip():
            logger.info(f"[{ch_id}] Token belum dikonfigurasi; channel dilewati.")
            continue

        try:
            creds   = _load_creds_from_env(env_key)
            drive   = _get_drive_service(creds)
            youtube = _get_youtube_service(creds)

            root_id  = _find_folder(drive, GDRIVE_ROOT, None)
            if not root_id: continue
            queue_id = _find_folder(drive, QUEUE_FOLDER, root_id)
            if not queue_id: continue
            ch_queue = _find_folder(drive, ch_id, queue_id)
            if not ch_queue: continue

            subfolders = _list_subfolders(drive, ch_queue)
            if not subfolders:
                continue

            done_id = _ensure_folder(drive, DONE_FOLDER, root_id)

            for folder in subfolders:
                folder_id   = folder["id"]
                folder_name = folder["name"]

                # Download metadata
                meta_id = _find_file_in_folder(drive, folder_id, "metadata.json")
                if not meta_id:
                    continue

                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    meta_path = tmp.name
                _download_file(drive, meta_id, meta_path)
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                finally:
                    if os.path.exists(meta_path):
                        os.remove(meta_path)

                # Jangan pernah mengirim antrean channel A ke akun channel B.
                if metadata.get("channel_id") != ch_id:
                    logger.error(
                        f"[{ch_id}] Queue {folder_name} dilewati: "
                        f"channel_id metadata tidak cocok."
                    )
                    continue

                if metadata.get("status") == "uploaded":
                    # Upload sebelumnya sudah sukses; hanya ulangi pemindahan arsip.
                    _move_to_done(drive, folder_id, done_id, ch_id)
                    logger.info(f"[{ch_id}] {folder_name} sudah terunggah; dipindahkan ke done.")
                    continue

                if metadata.get("status") != "ready":
                    continue

                # Cek jadwal publish — skip tanpa notif (tidak spam)
                publish_at = metadata.get("publish_at", "")
                if publish_at:
                    try:
                        pub_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        if pub_dt > now_utc:
                            logger.info(f"[{ch_id}] {folder_name} belum waktunya, skip.")
                            _status["skipped_today"] += 1
                            continue
                    except Exception as exc:
                        logger.error(
                            f"[{ch_id}] {folder_name} dilewati: publish_at tidak valid ({exc})."
                        )
                        continue

                # Download video — mulai proses nyata, kirim notif
                video_id_gdrive = _find_file_in_folder(drive, folder_id, "video.mp4")
                if not video_id_gdrive:
                    continue

                logger.info(f"[{ch_id}] Downloading: {folder_name}...")
                _send_telegram(
                    f"📥 <b>Mulai proses upload</b>\n"
                    f"Channel : <code>{ch_id}</code>\n"
                    f"Video   : <code>{folder_name}</code>"
                )

                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    video_path = tmp.name
                _download_file(drive, video_id_gdrive, video_path)

                # Download thumbnail
                thumb_path = None
                thumb_id   = _find_file_in_folder(drive, folder_id, "thumbnail.png")
                if thumb_id:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        thumb_path = tmp.name
                    _download_file(drive, thumb_id, thumb_path)

                # Upload ke YouTube
                logger.info(f"[{ch_id}] Uploading: {metadata['title']}")
                try:
                    url = _upload_to_youtube(youtube, video_path, thumb_path, metadata)
                    logger.info(f"[{ch_id}] ✅ {url}")

                    # Simpan penanda sukses sebelum memindahkan folder. Jika proses
                    # berhenti setelah upload, polling berikutnya tidak upload ulang.
                    metadata["status"] = "uploaded"
                    metadata["uploaded_url"] = url
                    metadata["uploaded_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        _update_metadata_file(drive, meta_id, metadata)
                    except Exception as marker_error:
                        logger.error(
                            f"[{ch_id}] Upload sukses tetapi marker metadata gagal: {marker_error}. "
                            "Mencoba arsipkan folder agar tidak di-upload ulang."
                        )
                        _move_to_done(drive, folder_id, done_id, ch_id)
                    else:
                        _move_to_done(drive, folder_id, done_id, ch_id)

                    _status["uploads_today"]          += 1
                    _status["per_channel"][ch_id]     += 1

                    # ✅ Notif sukses
                    _send_telegram(
                        f"✅ <b>Upload berhasil!</b>\n"
                        f"Channel : <code>{ch_id}</code>\n"
                        f"Judul   : {metadata['title']}\n"
                        f"URL     : {url}\n"
                        f"Total hari ini: <b>{_status['uploads_today']} video</b>"
                    )

                except Exception as e:
                    logger.error(f"[{ch_id}] Upload gagal: {e}")
                    _status["errors_today"] += 1

                    # ❌ Notif gagal
                    _send_telegram(
                        f"❌ <b>Upload GAGAL!</b>\n"
                        f"Channel : <code>{ch_id}</code>\n"
                        f"Video   : <code>{folder_name}</code>\n"
                        f"Error   : <code>{str(e)[:300]}</code>"
                    )

                finally:
                    if os.path.exists(video_path): os.remove(video_path)
                    if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

                time.sleep(60)  # Jeda 60 detik antar upload

        except Exception as e:
            logger.error(f"[{ch_id}] Error: {e}")
            _status["errors_today"] += 1

            # ⚠️ Notif error channel (token expired, dll)
            _send_telegram(
                f"⚠️ <b>Error channel!</b>\n"
                f"Channel : <code>{ch_id}</code>\n"
                f"Error   : <code>{str(e)[:300]}</code>"
            )


def main():
    logger.info("🚀 YouTube Uploader dimulai")
    logger.info(f"   Polling setiap {POLL_INTERVAL_SEC // 60} menit")

    # Notif start — hanya sekali saat pertama jalan
    _send_telegram(
        f"🚀 <b>YouTube Uploader started!</b>\n"
        f"Polling setiap {POLL_INTERVAL_SEC // 60} menit\n"
        f"Channels aktif: <b>{len(CHANNELS)}</b>\n"
        f"Daily summary: tiap 00:00 UTC (07:00 WIB)"
    )

    # Start HTTP server di thread terpisah (untuk Uptime.com ping)
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    while True:
        try:
            process_queue()
        except Exception as e:
            logger.error(f"Polling error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()