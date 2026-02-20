import os
import logging
import threading
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import asyncpg
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# --- Web Server for Render Health Checks ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server_address = ('', port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    logger.info(f"Starting web server on port {port}")
    httpd.serve_forever()

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
RAZORPAY_LINK = "https://razorpay.me/@gateprep?amount=CVDUr6Uxp2FOGZGwAHntNg%3D%3D"

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Course Data ---
# If you don't have an ID yet, use 0. DO NOT leave it blank like "mat_msg_id": }
COURSES = {
    "c_gsssb": {
        "name": "GSSSB Non-Tech",
        "price": 499,
        "subjects": {
            "s_maths": {"name": "Maths", "vid_msg_id": 10, "mat_msg_id": 11},
            "s_reason": {"name": "Reasoning", "vid_msg_id": 12, "mat_msg_id": 13},
            "s_polity": {"name": "Polity", "vid_msg_id": 14, "mat_msg_id": 15},
            "s_env": {"name": "Environment", "vid_msg_id": 16, "mat_msg_id": 17},
            "s_lang": {"name": "Language", "vid_msg_id": 18, "mat_msg_id": 19}
        }
    },
    "c_gpsc": {
        "name": "GPSC AE Civil",
        "price": 999,
        "subjects": {
            "s_survey": {"name": "Surveying", "vid_msg_id": 20, "mat_msg_id": 21},
            "s_enve": {"name": "Environment Engg", "vid_msg_id": 22, "mat_msg_id": 23},
            "s_bim": {"name": "BIM", "vid_msg_id": 24, "mat_msg_id": 25},
            "s_ecv": {"name": "ECV", "vid_msg_id": 26, "mat_msg_id": 27}
        }
    }
}

# --- Database Functions ---
async def init_db(pool):
    async with pool.acquire() as conn:
        # 1. Create tables if they don't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stats (
                action TEXT PRIMARY KEY,
                count INT DEFAULT 0
            );
        ''')
        # 2. Patch the missing column issue dynamically
        try:
            await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;')
        except Exception as e:
            logger.warning(f"DB Alter check: {e}")

async def track_user(pool, user_id, first_name):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, first_name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name",
            user_id, first_name
        )

async def increment_stat(pool, action_name):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO stats (action, count) VALUES ($1, 1) ON CONFLICT (action) DO UPDATE SET count = stats.count + 1",
            action_name
        )

# --- Conversation States ---
SELECTING_ACTION, FORWARD_TO_ADMIN, FORWARD_SCREENSHOT = range(3)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    pool = context.bot_data.get('db_pool')
    if pool:
        await track_user(pool, user.id, user.first_name)
        await increment_stat(pool, "bot_starts")
    
    keyboard = [
        [InlineKeyboardButton(COURSES["c_gsssb"]["name"], callback_data="c_gsssb")],
        [InlineKeyboardButton(COURSES["c_gpsc"]["name"], callback_data="c_gpsc")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = fr"👋 Welcome, {escape_markdown(user.first_name, 2)}\!\n\nPlease select a course category below to begin:"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return SELECTING_ACTION

async def course_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    course_key = query.data
    course = COURSES.get(course_key)
    if not course: return SELECTING_ACTION
    
    pool = context.bot_data.get('db_pool')
    if pool: await increment_stat(pool, f"view_{course_key}")

    keyboard = [[InlineKeyboardButton(subj["name"], callback_data=f"subj_{course_key}_{sk}")] for sk, subj in course["subjects"].items()]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")])
    
    text = fr"📚 *{escape_markdown(course['name'], 2)}*\n\nSelect a subject to view demos or materials:"
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    return SELECTING_ACTION

async def subject_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    course_key, subj_key = f"{parts[1]}_{parts[2]}", f"{parts[3]}_{parts[4]}"
    
    course = COURSES[course_key]
    subject = course["subjects"][subj_key]
    context.user_data.update({'selected_course': course, 'selected_subject': subject, 'back_to_course_key': course_key})

    keyboard = [
        [InlineKeyboardButton("🎥 Watch Demo Video", callback_data=f"demo_vid_{course_key}_{subj_key}")],
        [InlineKeyboardButton("📄 View Demo Material", callback_data=f"demo_mat_{course_key}_{subj_key}")],
        [InlineKeyboardButton("🛒 Buy Full Course", callback_data="buy_course")],
        [InlineKeyboardButton("💬 Talk to Admin", callback_data="talk_admin")],
        [InlineKeyboardButton("⬅️ Back to Subjects", callback_data=course_key)]
    ]
    text = fr"📘 *{escape_markdown(course['name'], 2)} > {escape_markdown(subject['name'], 2)}*\n\nChoose an action below:"
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    return SELECTING_ACTION

async def send_demo_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    subject = COURSES[f"{parts[2]}_{parts[3]}"]["subjects"][f"{parts[4]}_{parts[5]}"]
    msg_id = subject["vid_msg_id"] if parts[1] == "vid" else subject["mat_msg_id"]
    
    if CHANNEL_ID == 0 or msg_id == 0:
        await query.message.reply_text("Admin hasn't configured the demo links yet.")
    else:
        try:
            await context.bot.copy_message(chat_id=update.effective_chat.id, from_chat_id=CHANNEL_ID, message_id=msg_id)
        except Exception as e:
            logger.error(f"Copy failed: {e}")
            await query.message.reply_text("Sorry, the file could not be loaded. Please ensure the bot is an admin in the private channel.")
    return SELECTING_ACTION

async def handle_buy_or_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "talk_admin":
        await query.edit_message_text(text=r"Please type your message and send it\. I will forward it to the admin\.", parse_mode=ParseMode.MARKDOWN_V2)
        return FORWARD_TO_ADMIN
    elif query.data == "buy_course":
        course = context.user_data['selected_course']
        keyboard = [[InlineKeyboardButton(f"💳 Pay ₹{course['price']} Now", url=RAZORPAY_LINK)],
                    [InlineKeyboardButton("✅ Already Paid? Share Screenshot", callback_data="share_screenshot")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=context.user_data['back_to_course_key'])]]
        buy_text = fr"✅ *Purchase {escape_markdown(course['name'], 2)}*\n\n*Price: ₹{course['price']}*\n\nPay via Razorpay and share your screenshot here\."
        await query.edit_message_text(text=buy_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        return SELECTING_ACTION
    elif query.data == "share_screenshot":
        await query.edit_message_text(text=r"Please send the screenshot of your payment now\.", parse_mode=ParseMode.MARKDOWN_V2)
        return FORWARD_SCREENSHOT

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user, course = update.effective_user, context.user_data.get('selected_course', {'name': 'General Query'})
    text = fr"📩 *New Message*\nFrom: {escape_markdown(user.first_name, 2)} \(ID: `{user.id}`\)\nContext: *{escape_markdown(course['name'], 2)}*\n\n{escape_markdown(update.message.text, 2)}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text(r"✅ Message sent to admin\.", parse_mode=ParseMode.MARKDOWN_V2)
    return await start(update, context)

async def forward_screenshot_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user, course = update.effective_user, context.user_data.get('selected_course', {'name': 'Unknown'})
    caption = fr"📸 *Payment Screenshot*\nFrom: {escape_markdown(user.first_name, 2)} \(ID: `{user.id}`\)\nCourse: *{escape_markdown(course['name'], 2)}*\n\nReply to this with the private link\."
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text(r"✅ Screenshot received\. Admin will review it shortly\.", parse_mode=ParseMode.MARKDOWN_V2)
    return await start(update, context)

async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID or not update.message.reply_to_message: return
    orig = update.message.reply_to_message.text or update.message.reply_to_message.caption
    if orig and "(ID: " in orig:
        user_id = int(orig.split("(ID: ")[1].split(")")[0].replace('`', ''))
        text = fr"👑 *Admin replied:*\n\n{escape_markdown(update.message.text, 2)}\n\n\-\-\-\n_Reply to this message to chat back\._"
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_text("✅ Reply sent.")

async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    replied = update.message.reply_to_message
    if replied and replied.from_user.is_bot and "Admin replied:" in (replied.text or ""):
        text = fr"↪️ *Follow\-up* from {escape_markdown(update.effective_user.first_name, 2)} \(ID: `{update.effective_user.id}`\):\n\n{escape_markdown(update.message.text, 2)}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_text("✅ Reply sent.")

async def post_init(application: Application):
    if DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL)
        application.bot_data['db_pool'] = pool
        await init_db(pool)

def main() -> None:
    threading.Thread(target=run_web_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(start, pattern="^main_menu$"),
                CallbackQueryHandler(course_menu, pattern="^c_"),
                CallbackQueryHandler(subject_menu, pattern="^subj_"),
                CallbackQueryHandler(send_demo_content, pattern="^demo_"),
                CallbackQueryHandler(handle_buy_or_admin, pattern="^talk_admin$|^buy_course$|^share_screenshot$"),
            ],
            FORWARD_TO_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, forward_to_admin)],
            FORWARD_SCREENSHOT: [MessageHandler(filters.PHOTO, forward_screenshot_to_admin)],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    application.add_handler(conv)
    application.add_handler(MessageHandler(filters.REPLY & filters.User(ADMIN_ID), reply_to_user))
    application.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_user_reply))
    application.run_polling()

if __name__ == "__main__":
    main()
