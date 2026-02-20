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
DATABASE_URL = os.environ.get("DATABASE_URL") # Your NeonDB Connection String
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0")) # e.g., -100123456789
RAZORPAY_LINK = "https://razorpay.me/@gateprep?amount=CVDUr6Uxp2FOGZGwAHntNg%3D%3D"

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Course & Subject Data Structure ---
# UPDATE THE 'vid_msg_id' and 'mat_msg_id' WITH THE ACTUAL MESSAGE IDs FROM YOUR CHANNEL
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

# --- Database Functions (NeonDB) ---
async def init_db(pool):
    """Creates necessary tables if they don't exist."""
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stats (
                action TEXT PRIMARY KEY,
                count INT DEFAULT 0
            );
        ''')

async def track_user(pool, user_id, first_name):
    """Adds a new user to the DB."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, first_name) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
            user_id, first_name
        )

async def increment_stat(pool, action_name):
    """Tracks views and clicks."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO stats (action, count) VALUES ($1, 1) ON CONFLICT (action) DO UPDATE SET count = stats.count + 1",
            action_name
        )

# --- Conversation States ---
SELECTING_ACTION, FORWARD_TO_ADMIN, FORWARD_SCREENSHOT = range(3)

# --- Main Menu & Navigation ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    pool = context.bot_data['db_pool']
    await track_user(pool, user.id, user.first_name)
    await increment_stat(pool, "bot_starts")
    
    keyboard = [
        [InlineKeyboardButton(COURSES["c_gsssb"]["name"], callback_data="c_gsssb")],
        [InlineKeyboardButton(COURSES["c_gpsc"]["name"], callback_data="c_gpsc")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = f"👋 Welcome, {escape_markdown(user.first_name, 2)}\!\n\nPlease select a course category below to begin:"
    
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
    
    pool = context.bot_data['db_pool']
    await increment_stat(pool, f"view_{course_key}")

    keyboard = []
    # Build Subject Buttons dynamically
    for subj_key, subj_data in course["subjects"].items():
        keyboard.append([InlineKeyboardButton(subj_data["name"], callback_data=f"subj_{course_key}_{subj_key}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"📚 *{escape_markdown(course['name'], 2)}*\n\nSelect a subject to view demos or materials:"
    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return SELECTING_ACTION

async def subject_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Data format: subj_c_gsssb_s_maths
    parts = query.data.split('_')
    course_key = f"{parts[1]}_{parts[2]}" # c_gsssb
    subj_key = f"{parts[3]}_{parts[4]}"   # s_maths
    
    course = COURSES.get(course_key)
    subject = course["subjects"].get(subj_key)
    
    # Save selection to context for buying/screenshots later
    context.user_data['selected_course'] = course
    context.user_data['selected_subject'] = subject
    context.user_data['back_to_course_key'] = course_key

    keyboard = [
        [InlineKeyboardButton("🎥 Watch Demo Video", callback_data=f"demo_vid_{course_key}_{subj_key}")],
        [InlineKeyboardButton("📄 View Demo Material", callback_data=f"demo_mat_{course_key}_{subj_key}")],
        [InlineKeyboardButton("🛒 Buy Full Course", callback_data="buy_course")],
        [InlineKeyboardButton("💬 Talk to Admin", callback_data="talk_admin")],
        [InlineKeyboardButton("⬅️ Back to Subjects", callback_data=course_key)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"📘 *{escape_markdown(course['name'], 2)} > {escape_markdown(subject['name'], 2)}*\n\nChoose an action below:"
    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return SELECTING_ACTION

# --- Action Handlers (Demos & Buying) ---
async def send_demo_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    data = query.data
    # Format: demo_vid_c_gsssb_s_maths OR demo_mat_c_gsssb_s_maths
    parts = data.split('_')
    action_type = parts[1] # 'vid' or 'mat'
    course_key = f"{parts[2]}_{parts[3]}"
    subj_key = f"{parts[4]}_{parts[5]}"
    
    subject = COURSES[course_key]["subjects"][subj_key]
    msg_id = subject["vid_msg_id"] if action_type == "vid" else subject["mat_msg_id"]
    
    if CHANNEL_ID == 0 or msg_id == 0:
        await query.message.reply_text("Admin hasn't configured the demo links yet. Please check back later.")
        return SELECTING_ACTION

    try:
        # THE MAGIC TRICK: copy_message sends it purely as the bot, without "forwarded from"
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id
        )
    except Exception as e:
        logger.error(f"Failed to copy message {msg_id} from {CHANNEL_ID}: {e}")
        await query.message.reply_text("Sorry, the demo file could not be loaded right now.")
        
    return SELECTING_ACTION

async def handle_buy_or_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "talk_admin":
        await query.edit_message_text(text="Please type your message and send it\. I will forward it to the admin\.", parse_mode=ParseMode.MARKDOWN_V2)
        return FORWARD_TO_ADMIN
    
    elif action == "buy_course":
        course = context.user_data.get('selected_course')
        back_key = context.user_data.get('back_to_course_key', 'main_menu')
        
        keyboard = [
            [InlineKeyboardButton(f"💳 Pay ₹{course['price']} Now", url=RAZORPAY_LINK)],
            [InlineKeyboardButton("✅ Already Paid? Share Screenshot", callback_data="share_screenshot")],
            [InlineKeyboardButton("⬅️ Back", callback_data=back_key)] 
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        escaped_name = escape_markdown(course['name'], 2)
        buy_text = f"✅ *Purchase {escaped_name}*\n\n*Price: ₹{course['price']}*\n\nPay via Razorpay and share your screenshot here\."
        await query.edit_message_text(text=buy_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return SELECTING_ACTION
        
    elif action == "share_screenshot":
        await query.edit_message_text(text="Please send the screenshot of your payment now\.", parse_mode=ParseMode.MARKDOWN_V2)
        return FORWARD_SCREENSHOT

# --- Admin & 2-Way Chat (Preserved from previous logic) ---
async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    course = context.user_data.get('selected_course', {'name': 'General Query'})
    escaped_name = escape_markdown(course['name'], 2)
    escaped_msg = escape_markdown(update.message.text, 2)
    
    text = f"📩 *New Message*\nFrom: {escape_markdown(user.first_name, 2)} \(ID: `{user.id}`\)\nContext: *{escaped_name}*\n\n{escaped_msg}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("✅ Message sent to admin\.")
    return await start(update, context) # Return to start

async def forward_screenshot_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    course = context.user_data.get('selected_course', {'name': 'Unknown'})
    escaped_name = escape_markdown(course['name'], 2)
    
    caption = f"📸 *Payment Screenshot*\nFrom: {escape_markdown(user.first_name, 2)} \(ID: `{user.id}`\)\nCourse: *{escaped_name}*\n\nReply to this with the private link\."
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("✅ Screenshot received\. Admin will review it shortly\.")
    return await start(update, context)

async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    msg = update.effective_message
    if not msg.reply_to_message: return
    
    orig_text = msg.reply_to_message.text or msg.reply_to_message.caption
    if orig_text and "(ID: " in orig_text:
        try:
            user_id = int(orig_text.split("(ID: ")[1].split(")")[0].replace('`', ''))
            escaped_reply = escape_markdown(msg.text, 2)
            final_text = f"👑 *Admin replied:*\n\n{escaped_reply}\n\n\\-\\-\\-\n_Reply to this message to chat back\._"
            await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode=ParseMode.MARKDOWN_V2)
            await msg.reply_text("✅ Reply sent.")
        except Exception as e:
            await msg.reply_text(f"❌ Error: {e}")

async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    replied = update.message.reply_to_message
    if replied and replied.from_user.is_bot and "Admin replied:" in replied.text:
        escaped_msg = escape_markdown(update.message.text, 2)
        text = f"↪️ *Follow\-up* from {escape_markdown(user.first_name, 2)} \(ID: `{user.id}`\):\n\n{escaped_msg}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_text("✅ Reply sent to admin.")

# --- Admin Database Commands ---
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    pool = context.bot_data['db_pool']
    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        stats = await conn.fetch("SELECT action, count FROM stats ORDER BY count DESC")
    
    text = f"📊 *Database Stats*\n\n*Total Registered Users:* `{user_count}`\n\n*Interactions:*\n"
    for row in stats:
        text += f"\\- {escape_markdown(row['action'], 2)}: `{row['count']}`\n"
        
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: `/broadcast your message`", parse_mode=ParseMode.MARKDOWN_V2)
        return
        
    pool = context.bot_data['db_pool']
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
        
    sent, failed = 0, 0
    escaped_msg = escape_markdown(msg, 2)
    for u in users:
        try:
            await context.bot.send_message(chat_id=u['user_id'], text=escaped_msg, parse_mode=ParseMode.MARKDOWN_V2)
            sent += 1
        except:
            failed += 1
    await update.message.reply_text(f"📢 Broadcast done\. Sent: {sent}, Failed: {failed}")

# --- Startup & Main ---
async def post_init(application: Application):
    """Initializes the database connection pool on startup."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set! Bot cannot start.")
        return
    logger.info("Connecting to NeonDB...")
    pool = await asyncpg.create_pool(DATABASE_URL)
    application.bot_data['db_pool'] = pool
    await init_db(pool)
    logger.info("Database initialized successfully.")

def main() -> None:
    if not BOT_TOKEN or not ADMIN_ID or not DATABASE_URL:
        logger.error("FATAL: Missing Environment Variables (BOT_TOKEN, ADMIN_ID, or DATABASE_URL).")
        return

    # Web server thread
    threading.Thread(target=run_web_server, daemon=True).start()

    # Pass the post_init function to the builder
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
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

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(MessageHandler(filters.REPLY & filters.User(user_id=ADMIN_ID), reply_to_user))
    application.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_user_reply))

    logger.info("Polling started...")
    application.run_polling()

if __name__ == "__main__":
    main()
