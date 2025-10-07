import logging
import asyncio
import sys
import os
import tempfile
import shutil
import re
import aiohttp
import subprocess
from urllib.parse import urlparse, parse_qs, urlunparse

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties

import yt_dlp
from yt_dlp.utils import DownloadError


TOKEN = "8094506328:AAEMCScDztRsiKbI6aJF6-KsbjRCzBGI0gE"  # 🔒 Заміни токен перед деплоєм
ds = Dispatcher()


COOKIES_PATH = os.environ.get("COOKIES_PATH")

if not COOKIES_PATH or not os.path.exists(COOKIES_PATH):
    print("❌ cookies.txt не знайдено. Перевір змінну COOKIES_PATH")
else:
    print("✅ cookies.txt знайдено за шляхом:", COOKIES_PATH)

class FilenameCollectorPP(yt_dlp.postprocessor.common.PostProcessor):
    def __init__(self):
        super().__init__(None)
        self.filenames = []

    def run(self, information):
        self.filenames.append(information["filepath"])
        return [], information


@ds.message(Command("start"))
async def command_start_handler(message: types.Message):
    await message.answer(f"Привіт, {message.from_user.first_name}! Надішли /search 'назва пісні', щоб завантажити музику 🎵")
    print("єєєєєєєє")

@ds.message(Command("search"))
async def search_cmd(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Будь ласка, введи назву пісні після команди. Наприклад: /search Imagine Dragons - Believer")
        return

    query = parts[1]

    # Skip playlists: if user sends a YouTube playlist URL without a specific video, reject;
    # if a video URL includes a playlist param, strip it.
    try:
        parsed = urlparse(query)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            host = parsed.netloc.lower()
            if any(h in host for h in ["youtube.com", "youtu.be"]):
                params = parse_qs(parsed.query)
                path = parsed.path or ""
                has_video = 'v' in params or 'youtu.be' in host and path.strip('/')
                has_playlist = 'list' in params or 'playlist' in path
                if has_playlist and not has_video:
                    await message.answer("❌ Плейлисти не підтримуються. Надішліть посилання на одне відео або назву пісні.")
                    return
                if has_playlist and has_video:
                    # strip playlist params
                    safe_query_items = [(k, v) for k, vals in params.items() for v in vals if k != 'list']
                    new_query = '&'.join([f"{k}={v}" for k, v in safe_query_items])
                    query = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', new_query, ''))
    except Exception:
        pass

    # Configure yt-dlp to NOT download; we will send audio by URL (no ffmpeg)
    YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'extract_flat': 'discard_in_playlist',
        'extractor_retries': 3,
        'fragment_retries': 3,
        'concurrent_fragment_downloads': 1,
        'ignore_no_formats_error': True,
        'cookiefile': COOKIES_PATH,  # 🔹 додаємо підтримку cookies
        'http_headers': {
             'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36',
             'Accept-Language': 'en-US,en;q=0.9'
        },
    }

    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            search_result = ydl.extract_info(f"ytsearch:{query}", download=False)

            if not search_result or 'entries' not in search_result or len(search_result['entries']) == 0:
                await message.answer("❌ Не знайдено відео за вашим запитом. Спробуйте ще раз.")
                return

            video = search_result['entries'][0]
            title = video.get("title") or "Аудіо"

            # Choose an audio-only format URL, prefer direct file URLs over streaming manifests
            audio_url = None
            stream_protocols = {'m3u8', 'm3u8_native', 'http_dash_segments', 'dash'}
            formats = video.get('formats') or []
            preferred = [
                f for f in formats
                if f.get('vcodec') == 'none' and f.get('acodec') and f.get('url')
            ]
            direct_candidates = [f for f in preferred if (f.get('protocol') or '').lower() not in stream_protocols]
            stream_candidates = [f for f in preferred if (f.get('protocol') or '').lower() in stream_protocols]

            def sort_key(f):
                return (
                    0 if (str(f.get('acodec','')).lower().startswith('opus') or f.get('ext') == 'webm') else 1,
                    f.get('filesize') or f.get('filesize_approx') or 10**12,
                    - (f.get('abr') or 0)
                )

            direct_candidates.sort(key=sort_key)
            stream_candidates.sort(key=sort_key)

            use_stream_via_ffmpeg = False
            if direct_candidates:
                audio_url = direct_candidates[0]['url']
            elif stream_candidates:
                audio_url = stream_candidates[0]['url']
                use_stream_via_ffmpeg = True
            elif video.get('url'):
                audio_url = video.get('url')
                use_stream_via_ffmpeg = True

            if not audio_url:
                await message.answer("❌ Не вдалося отримати аудіо-посилання. Спробуйте інший запит.")
                return

            # Download the audio to a temp file (no conversion) to avoid Telegram URL fetch timeouts
            tmp_dir = tempfile.mkdtemp(prefix="tgdl_")
            safe_title = re.sub(r"[^\w\-\. ]+", "_", title).strip() or "audio"
            ext = None
            if preferred:
                ext = preferred[0].get('ext')
            if not ext:
                ext = 'webm'
            file_name = f"{safe_title}.{ext}"
            file_path = os.path.join(tmp_dir, file_name)

            # If direct file URL available, download it; otherwise we'll pass URL directly to ffmpeg with headers
            if not use_stream_via_ffmpeg:
                max_bytes = 48 * 1024 * 1024
                total = 0
                timeout = aiohttp.ClientTimeout(total=180)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(audio_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36',
                        'Accept': '*/*',
                        'Referer': 'https://www.youtube.com/'
                    }) as resp:
                        resp.raise_for_status()
                        with open(file_path, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(128 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise DownloadError('File too large to send via Telegram')
                                f.write(chunk)
                if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024 * 50:
                    raise DownloadError('Downloaded file is too small; likely blocked')

            # Convert to MP3 using ffmpeg
            mp3_path = os.path.join(tmp_dir, f"{safe_title}.mp3")
            ffmpeg_location = os.getenv("FFMPEG_LOCATION")
            ffmpeg_exec = None
            if ffmpeg_location:
                if os.path.isfile(ffmpeg_location):
                    ffmpeg_exec = ffmpeg_location
                else:
                    candidate = os.path.join(ffmpeg_location, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
                    if os.path.isfile(candidate):
                        ffmpeg_exec = candidate
            if not ffmpeg_exec:
                # Try to resolve from PATH
                from shutil import which
                ffmpeg_exec = which('ffmpeg') or which('ffmpeg.exe')
            if not ffmpeg_exec:
                await message.answer("❌ FFmpeg не знайдено. Додайте до PATH або задайте FFMPEG_LOCATION (шлях до bin або ffmpeg.exe).")
                return

            try:
                ffmpeg_cmd = [ffmpeg_exec, '-y']
                if use_stream_via_ffmpeg:
                    header_str = 'User-Agent: Mozilla/5.0\r\nAccept: */*\r\nReferer: https://www.youtube.com/\r\n'
                    ffmpeg_cmd += ['-headers', header_str, '-i', audio_url]
                else:
                    ffmpeg_cmd += ['-i', file_path]
                ffmpeg_cmd += ['-vn', '-acodec', 'libmp3lame', '-b:a', '192k', mp3_path]
                result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            except FileNotFoundError:
                await message.answer("❌ FFmpeg не знайдено у вказаному шляху. Перевірте FFMPEG_LOCATION або PATH.")
                return
            except subprocess.CalledProcessError as e:
                # Fallback: let yt-dlp handle conversion via postprocessor (more robust for HLS/DASH)
                try:
                    outtmpl = os.path.join(tmp_dir, "%(title).200B-%(id)s.%(ext)s")
                    ydl_download_opts = {
                        'format': 'bestaudio/best',
                        'noplaylist': True,
                        'outtmpl': outtmpl,
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                    }
                    ffmpeg_location = os.getenv("FFMPEG_LOCATION")
                    if ffmpeg_location and os.path.exists(ffmpeg_location):
                        # Accept either bin folder or ffmpeg.exe path
                        ydl_download_opts['ffmpeg_location'] = ffmpeg_location if os.path.isdir(ffmpeg_location) else os.path.dirname(ffmpeg_location)

                    filename_collector = FilenameCollectorPP()
                    with yt_dlp.YoutubeDL(ydl_download_opts) as ydl2:
                        ydl2.add_post_processor(filename_collector)
                        video_url = video.get('webpage_url') or video.get('url') or f"https://www.youtube.com/watch?v={video.get('id')}"
                        ydl2.extract_info(video_url, download=True)

                    if filename_collector.filenames:
                        produced = filename_collector.filenames[0]
                    else:
                        produced = yt_dlp.YoutubeDL({'outtmpl': outtmpl}).prepare_filename(video)
                    root, _ = os.path.splitext(produced)
                    mp3_path = root + ".mp3"
                except Exception as e2:
                    err_snippet = (getattr(e, 'stderr', '') or getattr(e, 'stdout', '') or str(e2)).splitlines()[-1:] or [""]
                    await message.answer("❌ Помилка конвертації FFmpeg: " + " ".join(err_snippet))
                    return

            performer = video.get('uploader') or None
            await message.reply_audio(
                audio=types.FSInputFile(mp3_path),
                caption=f"🎵 {title}",
                title=title,
                performer=performer
            )
    except DownloadError as e:
        err_text = str(e)
        if "ffmpeg" in err_text.lower() or "ffprobe" in err_text.lower():
            await message.answer("❌ Помилка обробки медіа. Спробуйте ще раз або інший трек.")
        elif "403" in err_text or "forbidden" in err_text.lower() or "signature" in err_text.lower():
            await message.answer(
                "❌ YouTube відповів 403/Signature error. Спробуйте:\n"
            )
        elif "copyright" in err_text.lower() or "unavailable" in err_text.lower():
            await message.answer("❌ Відео недоступне для завантаження. Спробуйте інший трек.")
        else:
            await message.answer(f"❌ Помилка завантаження: {err_text}")
    except Exception as e:
        await message.answer(f"❌ Несподівана помилка: {str(e)}")
    finally:
        try:
            if 'file_path' in locals() and file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        try:
            if 'mp3_path' in locals() and mp3_path and os.path.exists(mp3_path):
                os.remove(mp3_path)
        except Exception:
            pass
        try:
            if 'tmp_dir' in locals() and tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env variable is not set")
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await ds.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())



