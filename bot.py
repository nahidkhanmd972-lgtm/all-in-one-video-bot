import os
import re
import uuid
import shutil
import logging
import asyncio
from pathlib import Path
from typing import Optional, Dict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
import yt_dlp

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_URL = "https://t.me/NahidDevOfficial"

# Standard Telegram Bot API upload size limit: 50 MB
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Platform registry with display names and URL matching patterns
PLATFORMS: Dict[str, Dict[str, str]] = {
    "tiktok": {
        "name": "TikTok",
        "regex": r"(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/.*",
    },
    "youtube": {
        "name": "YouTube",
        "regex": r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.*",
    },
    "instagram": {
        "name": "Instagram",
        "regex": r"(https?://)?(www\.)?instagram\.com/(p|reel|tv)/.*",
    },
    "facebook": {
        "name": "Facebook",
        "regex": r"(https?://)?(www\.|web\.|m\.)?(facebook\.com|fb\.watch)/.*",
    },
    "twitter": {
        "name": "Twitter / X",
        "regex": r"(https?://)?(www\.)?(twitter\.com|x\.com)/.+/status/.*",
    },
}

# ---------------------------------------------------------------------------
# UI Helpers
# ---------------------------------------------------------------------------
def build_main_menu() -> InlineKeyboardMarkup:
    """Builds the main inline keyboard with supported platforms and admin button."""
    keyboard = [
        [
            InlineKeyboardButton("🎵 TikTok", callback_data="platform_tiktok"),
            InlineKeyboardButton("▶️ YouTube", callback_data="platform_youtube"),
        ],
        [
            InlineKeyboardButton("📸 Instagram", callback_data="platform_instagram"),
            InlineKeyboardButton("📘 Facebook", callback_data="platform_facebook"),
        ],
        [
            InlineKeyboardButton("🐦 Twitter / X", callback_data="platform_twitter"),
        ],
        [
            InlineKeyboardButton("👨‍💻 অ্যাডমিনের সাথে যোগাযোগ", url=ADMIN_URL),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------
# Bot Handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command, clears previous state, and displays the platform menu."""
    # Reset selected platform state
    context.user_data.pop("selected_platform", None)

    welcome_text = (
        "👋 **Welcome to the Universal Video Downloader Bot!**\n\n"
        "Please select the platform you want to download video from using the buttons below:"
    )
    if update.message:
        await update.message.reply_text(
            welcome_text,
            reply_markup=build_main_menu(),
            parse_mode=ParseMode.MARKDOWN,
        )


async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stores the platform selection in user context and prompts for the URL."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("platform_"):
        return

    platform_key = data.replace("platform_", "")
    platform_info = PLATFORMS.get(platform_key)

    if not platform_info:
        await query.edit_message_text("❌ Unknown platform selected. Please try /start again.")
        return

    # Store user's selected platform
    context.user_data["selected_platform"] = platform_key

    await query.edit_message_text(
        text=(
            f"✅ **You selected {platform_info['name']}.**\n\n"
            f"Now please send the correct {platform_info['name']} video link.\n\n"
            "_Send /start anytime if you want to switch platforms._"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


def run_ytdlp_download(url: str, output_template: str) -> Optional[dict]:
    """Blocking worker function executed in an executor thread to run yt-dlp."""
    ydl_opts = {
        # Select best mp4 video + best audio, or fallback to single best stream under standard sizes
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "socket_timeout": 30,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info_dict)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Validates URL according to current selection, downloads, and sends video."""
    message = update.message
    if not message or not message.text:
        return

    user_text = message.text.strip()
    selected_platform_key = context.user_data.get("selected_platform")

    # 1. State Check: User must select a platform first
    if not selected_platform_key or selected_platform_key not in PLATFORMS:
        await message.reply_text(
            "⚠️ Please select a platform first by clicking one of the buttons below:",
            reply_markup=build_main_menu(),
        )
        return

    platform_info = PLATFORMS[selected_platform_key]
    platform_name = platform_info["name"]
    pattern = platform_info["regex"]

    # 2. Domain & Platform Validation
    if not re.search(pattern, user_text, re.IGNORECASE):
        await message.reply_text(
            f"⚠️ **Invalid link for {platform_name}!**\n\n"
            f"You previously selected **{platform_name}**, but the link you provided does not match.\n"
            f"Please send a valid **{platform_name}** link, or click /start to pick another platform.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 3. Preparation for Processing & Server Hygiene
    status_msg = await message.reply_text("⏳ Processing your link, please wait...")
    job_id = uuid.uuid4().hex
    temp_dir = Path(f"/tmp/ytdl_{job_id}")
    output_template = str(temp_dir / "%(title).50s [%(id)s].%(ext)s")

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await message.chat.send_action(action=ChatAction.UPLOAD_VIDEO)

        # Execute yt-dlp safely off the event loop
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, run_ytdlp_download, user_text, output_template)

        # Locate downloaded output file
        downloaded_files = list(temp_dir.glob("*.mp4")) or list(temp_dir.glob("*.*"))
        if not downloaded_files:
            raise FileNotFoundError("Video file could not be generated or found on the server.")

        video_path = downloaded_files[0]
        file_size = video_path.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            await status_msg.edit_text(
                "❌ **File too large:** The requested video exceeds Telegram's 50MB bot upload limit."
            )
            return

        title = info.get("title", "Downloaded Video") if info else "Downloaded Video"
        await status_msg.edit_text("📤 Uploading video to Telegram...")

        with open(video_path, "rb") as video_file:
            await message.reply_video(
                video=video_file,
                caption=f"🎬 **{title}**\n\nDownloaded via @{context.bot.username}",
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
            )

        # Clean up the initial status update
        await status_msg.delete()

    except yt_dlp.utils.DownloadError as err:
        logger.warning("yt-dlp download error: %s", str(err))
        await status_msg.edit_text(
            "❌ **Download failed:** The video may be private, age-restricted, removed, or geo-blocked."
        )
    except Exception as exc:
        logger.error("Unexpected error handling video: %s", str(exc), exc_info=True)
        await status_msg.edit_text(
            "❌ An unexpected error occurred while processing your request. Please try again later."
        )
    finally:
        # Strict server hygiene: wipe the temporary folder and its contents
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Application Initialization
# ---------------------------------------------------------------------------
def main() -> None:
    """Bootstraps and starts the polling service."""
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing. Exiting.")
        raise ValueError("BOT_TOKEN environment variable not set.")

    # Extended timeout configuration to avoid dropped polling sessions on cloud providers
    custom_request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(custom_request)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(platform_callback, pattern=r"^platform_"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url))

    logger.info("Bot successfully initialized. Starting long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
