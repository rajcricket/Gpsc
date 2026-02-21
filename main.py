import os
import logging
import threading
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import asyncpg
from telegram.constants import ParseMode

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
COURSES = {
    "c_gsssb": {
        "name": "GSSSB Non-Tech and CCE",
        "price": 199,
        "subjects": {
            "s_maths": {"name": "Maths", "vid_msg_id": 3, "mat_msg_id": 99},
            "s_reason": {"name": "Reasoning", "vid_msg_id": 33, "mat_msg_id": 100},
            "s_polity": {"name": "Polity", "vid_msg_id": 47, "mat_msg_id": 0},
            "s_env": {"name": "Environment", "vid_msg_id": 58, "mat_msg_id": 0},
            "s_lang": {"name": "Language", "vid_msg_id": 64, "mat_msg_id": 0}
        }
    },
    "c_gpsc": {
        "name": "GPSC AE Civil",
        "price": 199,
        "subjects": {
            "s_survey": {"name": "Surveying", "vid_msg_id": 68, "mat_msg_id": 0},
            "s_enve": {"name": "Environment Engg", "vid_msg_id": 81, "mat_msg_id": 0},
            "s_bim": {"name": "BIM", "vid_msg_id": 66, "mat_msg_id": 0},
            "s_ecv": {"name": "ECV", "vid_msg_id": 86, "mat_msg_id": 0}
        }
    }
}

# --- Database Functions ---
async def init_db(pool):
    async with pool.acquire() as conn:
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
    
    welcome_text = (
        f"👋 Welcome, <b>{html.escape(user.first_name)}</b>!\n\n"
        "<b>📖 How to use this platform:</b>\n"
        "1. Select your target exam category below.\n"
        "2. Choose a subject to view free demo lectures and study materials.\n"
        "3. Purchase the full course to unlock complete preparation content.\n\n"
        "🌟 <b>Note:</b> These courses feature premium, high-quality lectures from <b>Web Sankul Academy</b> to ensure top-tier preparation.\n\n"
        "Please select a course category below to begin:"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return SELECTING_ACTION

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Action cancelled. Returning to main menu.")
    return await start(update, context)

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
    
    text = (
        f"📚 <b>{html.escape(course['name'])}</b>\n\n"
        "<b>What you will get:</b> Complete video lectures and high-quality PDF materials for all the subjects listed below.\n\n"
        "Select a subject to explore free demos or proceed to purchase:"
    )
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
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
    
    text = (
        f"📘 <b>{html.escape(course['name'])} &gt; {html.escape(subject['name'])}</b>\n\n"
        "Evaluate the content quality before you commit. Watch the demo video or read the demo PDF provided by Web Sankul Academy.\n\n"
        "Choose an action below:"
    )
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return SELECTING_ACTION

async def send_demo_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    parts = query.data.split('_')
    course_key = f"{parts[2]}_{parts[3]}"
    subj_key = f"{parts[4]}_{parts[5]}"
    subject = COURSES[course_key]["subjects"][subj_key]
    
    msg_id = subject["vid_msg_id"] if parts[1] == "vid" else subject["mat_msg_id"]
    
    if CHANNEL_ID == 0 or msg_id == 0:
        # Check 1: Show alert if no demo is available
        await query.answer("No demo available for this subject yet.", show_alert=True)
        return SELECTING_ACTION
    
    await query.answer()
    
    try:
        await context.bot.copy_message(
            chat_id=update.effective_chat.id, 
            from_chat_id=CHANNEL_ID, 
            message_id=msg_id,
            protect_content=True 
        )
        
        # Check 2: Add 'Purchase full course' button after delivering demo
        keyboard = [
            [InlineKeyboardButton("🛒 Purchase Full Course", callback_data="buy_course")],
            [InlineKeyboardButton("⬅️ Back to Subjects", callback_data=course_key)]
        ]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Liked the demo? Unlock the complete Web Sankul Academy course for {html.escape(COURSES[course_key]['name'])} below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logger.error(f"Copy failed: {e}")
        await query.message.reply_text("Sorry, the file could not be loaded. Please ensure the bot is an admin in the private channel.")
    
    return SELECTING_ACTION

async def handle_buy_or_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "talk_admin":
        await query.edit_message_text(text="Please type your message and send it. I will forward it to the admin.", parse_mode=ParseMode.HTML)
        return FORWARD_TO_ADMIN
    elif query.data == "buy_course":
        course = context.user_data.get('selected_course')
        if not course:
            await query.edit_message_text("Session expired. Please start over using /start.")
            return SELECTING_ACTION
            
        keyboard = [[InlineKeyboardButton(f"💳 Pay ₹{course['price']} Now", url=RAZORPAY_LINK)],
                    [InlineKeyboardButton("✅ Already Paid? Share Screenshot", callback_data="share_screenshot")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=context.user_data['back_to_course_key'])]]
        buy_text = f"✅ <b>Purchase {html.escape(course['name'])}</b>\n\n<b>Price: ₹{course['price']}</b>\n\nPay via Razorpay and share your screenshot here."
        
        # We use send_message here in case this was triggered from the post-demo message (which we shouldn't edit away)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=buy_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return SELECTING_ACTION
    elif query.data == "share_screenshot":
        await query.edit_message_text(text="Please send the screenshot of your payment now.", parse_mode=ParseMode.HTML)
        return FORWARD_SCREENSHOT

# --- Secure Input Handlers ---
async def wrong_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Please type a text message, or send /cancel to go back to the menu.")
    return FORWARD_TO_ADMIN

async def wrong_input_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Please upload a photo screenshot, or send /cancel to go back to the menu.")
    return FORWARD_SCREENSHOT

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    course = context.user_data.get('selected_course', {'name': 'General Query'})
    
    text = f"📩 <b>New Message</b>\nFrom: {html.escape(user.first_name)} (ID: <code>{user.id}</code>)\nContext: <b>{html.escape(course['name'])}</b>\n\nMessage:\n{html.escape(update.message.text)}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML)
    
    await update.message.reply_text("✅ Message sent to admin. They will reply to you here shortly.")
    return await start(update, context)

async def forward_screenshot_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    course = context.user_data.get('selected_course', {'name': 'Unknown'})
    
    caption = f"📸 <b>Payment Screenshot</b>\nFrom: {html.escape(user.first_name)} (ID: <code>{user.id}</code>)\nCourse: <b>{html.escape(course['name'])}</b>\n\nReply to this message to send the course link."
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.HTML)
    
    await update.message.reply_text("✅ Screenshot received. The admin will verify it and send you the access link soon.")
    return await start(update, context)

# --- 2-Way Chat & Admin Broadcast ---
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID or not update.message.reply_to_message: return
    orig = update.message.reply_to_message.text or update.message.reply_to_message.caption
    
    if orig and "(ID: " in orig:
        try:
            user_id = int(orig.split("(ID: ")[1].split(")")[0].replace('`', ''))
            text = f"👑 <b>Admin replied:</b>\n\n{html.escape(update.message.text)}\n\n---\n<i>You can reply to this message to chat back.</i>"
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            await update.message.reply_text("✅ Reply sent successfully.")
        except Exception as e:
            logger.error(f"Error extracting user ID: {e}")
            await update.message.reply_text("❌ Failed to parse User ID.")

async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    replied = update.message.reply_to_message
    if replied and replied.from_user.is_bot and "Admin replied:" in (replied.text or ""):
        text = f"↪️ <b>Follow-up</b> from {html.escape(update.effective_user.first_name)} (ID: <code>{update.effective_user.id}</code>):\n\n{html.escape(update.message.text)}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML)
        await update.message.reply_text("✅ Your reply has been sent to the admin.")

# Check 3: Broadcast Command Logic
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /broadcast <your message here>")
        return
        
    message = " ".join(context.args)
    pool = context.bot_data.get('db_pool')
    
    if not pool:
        await update.message.reply_text("❌ Database not connected. Cannot fetch users.")
        return
        
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")
        
    if not users:
        await update.message.reply_text("No users found in the database.")
        return
        
    success_count = 0
    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user['user_id'], 
                text=f"📢 <b>Announcement:</b>\n\n{html.escape(message)}", 
                parse_mode=ParseMode.HTML
            )
            success_count += 1
        except Exception as e:
            logger.warning(f"Failed to send broadcast to {user['user_id']}: {e}")
            
    await update.message.reply_text(f"✅ Broadcast complete. Successfully sent to {success_count}/{len(users)} users.")

# --- System & Setup ---
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    pool = context.bot_data['db_pool']
    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        stats = await conn.fetch("SELECT action, count FROM stats ORDER BY count DESC")
    
    text = f"📊 <b>Database Stats</b>\n\n<b>Total Registered Users:</b> <code>{user_count}</code>\n\n<b>Interactions:</b>\n"
    for row in stats:
        text += f"- {html.escape(row['action'])}: <code>{row['count']}</code>\n"
        
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if ADMIN_ID:
            await context.bot.send_message(
                chat_id=ADMIN_ID, 
                text=f"🚨 <b>Bot Error</b>\n<pre>{html.escape(str(context.error))}</pre>", 
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Failed to send error to admin: {e}")

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
            FORWARD_TO_ADMIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, forward_to_admin),
                MessageHandler(filters.ALL & ~filters.COMMAND, wrong_input_text)
            ],
            FORWARD_SCREENSHOT: [
                MessageHandler(filters.PHOTO, forward_screenshot_to_admin),
                MessageHandler(filters.ALL & ~filters.COMMAND, wrong_input_screenshot)
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel)
        ],
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("broadcast", broadcast)) # Added Broadcast Command
    application.add_handler(MessageHandler(filters.REPLY & filters.User(ADMIN_ID), reply_to_user))
    application.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_user_reply))
    application.add_error_handler(error_handler)
    
    application.run_polling()

if __name__ == "__main__":
    main()
