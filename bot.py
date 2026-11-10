   import os
import glob
import uuid
import shutil
import logging
import asyncio
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
import yt_dlp
import static_ffmpeg

# ---------------------------------------------------------------------------
# ১. রুট ছাড়া সার্ভারে FFmpeg লোড করা (মার্জিং ফেইল হওয়া চিরতরে বন্ধ)
# ---------------------------------------------------------------------------
try:
    static_ffmpeg.add_paths()
except Exception as e:
    pass

# ---------------------------------------------------------------------------
# ২. লগিং কনফিগারেশন
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ৩. কনস্ট্যান্ট ও এনভায়রনমেন্ট
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = "NahidDevOfficial"  # আপনার আপডেট করা এডমিন ইউজারনেম
MAX_FILE_SIZE_BYTES = 48 * 1024 * 1024  # টেলিগ্রাম ফ্রি বটের জন্য ৪৮ এমবি সেফ লিমিট

# ---------------------------------------------------------------------------
# ৪. রেন্ডার ফ্রি টিয়ার পোর্ট কিপ-অ্যালাইভ সার্ভার (সার্ভার কিল হওয়া আটকাবে)
# ---------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - 24/7 Bot Engine Active")

    def log_message(self, format, *args):
        return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ---------------------------------------------------------------------------
# ৫. স্টার্ট কমান্ড ও মেনু ডিজাইন
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👋 Hello **{user.first_name}**!\n\n"
        "🚀 **Welcome to the Ultimate All-in-One Video Downloader Bot.**\n"
        "You can download videos effortlessly from **YouTube, TikTok, Facebook, Instagram, Twitter/X**, and more!\n\n"
        "👇 *Please choose an option below to get started:*"
    )
    
    keyboard = [
        [InlineKeyboardButton("📥 How to Download", callback_data="menu_download")],
        [InlineKeyboardButton("🌐 Supported Platforms", callback_data="menu_platforms"),
         InlineKeyboardButton("ℹ️ Help & Guide", callback_data="menu_help")],
        [InlineKeyboardButton("👨‍💻 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# ৬. বাটন হ্যান্ডলার
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "menu_download":
        text = (
            "📥 **How to Download:**\n\n"
            "Simply send me a direct video link from any supported platform, and I will instantly process and send the video file to you!"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Home", callback_data="menu_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        
    elif data == "menu_platforms":
        text = (
            "🌐 **Fully Supported Platforms:**\n\n"
            "• **YouTube** (Shorts & Standard Videos)\n"
            "• **TikTok** (Without Watermark)\n"
            "• **Facebook** (Reels & Public Videos)\n"
            "• **Instagram** (Reels, Posts & IGTV)\n"
            "• **Twitter / X** (Clips & Videos)\n\n"
            "Just paste any link from these platforms in the chat!"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Home", callback_data="menu_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        
    elif data == "menu_help":
        text = (
            "ℹ️ **Help & Information:**\n\n"
            "1. Send a valid video URL.\n"
            "2. Wait a few seconds while the engine extracts the media.\n"
            "3. Receive your video file directly in chat.\n\n"
            "If you encounter any issues or need custom support, click below to contact the admin."
        )
        keyboard = [
            [InlineKeyboardButton("👨‍💻 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
            [InlineKeyboardButton("🔙 Back to Home", callback_data="menu_home")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        
    elif data == "menu_home":
        await start(update, context)

# ---------------------------------------------------------------------------
# ৭. কোর yt-dlp ডাউনলোড ইঞ্জিন (Worker Thread)
# ---------------------------------------------------------------------------
def run_ytdlp(url: str, output_path: str):
    ydl_opts = {
        'format': 'bestvideo[filesize<42M][ext=mp4]+bestaudio[ext=m4a]/best[filesize<48M][ext=mp4]/best[filesize<48M]/best',
        'outtmpl': output_path,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web']
            }
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info)

# ---------------------------------------------------------------------------
# ৮. নন-ব্লকিং মেসেজ হ্যান্ডলার
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    url = message.text.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        await message.reply_text("⚠️ Please send a valid video URL starting with `http://` or `https://`", parse_mode=ParseMode.MARKDOWN)
        return

    status_message = await message.reply_text("⏳ *Processing your link... Fetching video from platform.*", parse_mode=ParseMode.MARKDOWN)

    job_id = uuid.uuid4().hex
    temp_dir = Path(f"/tmp/ytdl_{job_id}")
    output_template = str(temp_dir / "%(id)s.%(ext)s")

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await message.chat.send_action(action=ChatAction.UPLOAD_VIDEO)

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, run_ytdlp, url, output_template)

        downloaded_files = glob.glob(f"{temp_dir}/*")
        if not downloaded_files:
            raise FileNotFoundError("Video download failed on the server.")

        video_path = downloaded_files[0]
        file_size = os.path.getsize(video_path)

        if file_size > MAX_FILE_SIZE_BYTES:
            await status_message.edit_text("❌ *Error:* The video file size exceeds Telegram's 50MB free bot limit.", parse_mode=ParseMode.MARKDOWN)
            return

        title = info.get("title", "Video") if info else "Video"
        clean_title = "".join(ch for ch in title if ch.isalnum() or ch in " -_()[]")[:50]

        await status_message.edit_text("📤 *Uploading video to Telegram...*", parse_mode=ParseMode.MARKDOWN)

        with open(video_path, "rb") as video_file:
            await message.reply_video(
                video=video_file,
                caption=f"🎬 *{clean_title}*\n\n✅ *Downloaded successfully via All-in-One Bot!*",
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300
            )
            
        await status_message.delete()

    except yt_dlp.utils.DownloadError as dl_err:
        err_text = str(dl_err).lower()
        logger.error(f"yt-dlp Download Error: {dl_err}")
        if "confirm you're not a bot" in err_text or "sign in" in err_text:
            await status_message.edit_text("❌ Cloud IP restricted by platform. Try a different platform or video.")
        elif "private" in err_text:
            await status_message.edit_text("❌ This video is private or unavailable.")
        else:
            await status_message.edit_text("❌ Failed to process this video link. Please verify the URL.")
            
    except Exception as e:
        logger.error(f"Error handling video: {e}", exc_info=True)
        await status_message.edit_text("❌ An unexpected error occurred while downloading.")
        
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# ৯. মেইন ফাংশন
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set!")
        return

    threading.Thread(target=run_http_server, daemon=True).start()

    custom_request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=60.0
    )

    application = Application.builder().token(BOT_TOKEN).request(custom_request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot engine is fully initialized and listening...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()