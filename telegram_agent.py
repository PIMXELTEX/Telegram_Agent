import os
import json
import asyncio
import time
import psycopg2
import psycopg2.extras
from telethon import TelegramClient, events
import google.generativeai as genai
from dotenv import load_dotenv

# --- Load Configuration ---
load_dotenv() # Load .env file from root directory

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("Error: config.json not found!")
        exit()
    except json.JSONDecodeError:
        print("Error: config.json is not a valid JSON file.")
        exit()

def load_prompt_config_from_txt():
    config_path = os.path.join(os.path.dirname(__file__), 'prompt_config.txt')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return "", {}, ""

    system_prompt = ""
    personas = {}
    default_persona = ""

    sections = content.split('[')[1:]
    for section in sections:
        header, body = section.split(']', 1)
        body = body.strip()
        if header == 'system_prompt':
            system_prompt = body
        elif header.startswith('persona:'):
            user = header.split(':', 1)[1]
            personas[user] = body
        elif header == 'default_persona':
            default_persona = body

    return system_prompt, personas, default_persona

config = load_config()
system_prompt, personas, default_persona = load_prompt_config_from_txt()

API_ID = config.get('api_id')
API_HASH = config.get('api_hash')
GEMINI_API_KEY = config.get('gemini_api_key')
GEMINI_MODEL = config.get('gemini_model', 'gemini-1.5-flash')

# --- Database Setup ---
DATABASE_URL = os.environ.get('DATABASE_URL')
PROFILE_PICS_PATH = os.path.join(os.path.dirname(__file__), 'web_panel', 'static', 'profile_pics')

def get_db_connection():
    """Creates a PostgreSQL database connection."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    db_dir = os.path.dirname(PROFILE_PICS_PATH) # Ensure profile pics dir exists
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable not set.")
        exit()

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if messages table exists
        cur.execute("SELECT to_regclass('public.messages')")
        if cur.fetchone()[0] is None:
            print("Creating messages table...")
            cur.execute('''
                CREATE TABLE messages (
                    id SERIAL PRIMARY KEY,
                    direction TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    profile_pic_path TEXT
                )
            ''')

        # Check if users table exists
        cur.execute("SELECT to_regclass('public.users')")
        if cur.fetchone()[0] is None:
            print("Creating users table...")
            cur.execute('''
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY,
                    blocked BOOLEAN NOT NULL DEFAULT false
                )
            ''')
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")
        exit()

def log_to_db(direction, user_id, text, profile_pic_path):
    """Logs a message directly to the PostgreSQL database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO messages (direction, user_id, text, timestamp, profile_pic_path) VALUES (%s, %s, %s, %s, %s)',
            (direction, str(user_id), text, time.time(), profile_pic_path)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Database error in log_to_db: {e}")

# --- Initialize Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=system_prompt)
    print("Gemini model initialized successfully.")
except Exception as e:
    print(f"Error initializing Gemini: {e}")
    exit()

# --- Initialize Telegram Client ---
SESSION_NAME = 'telegram_session'
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.is_private and not event.message.out:
        sender = await event.get_sender()
        if sender.username:
            user_identifier = sender.username
        else:
            user_identifier = f"{sender.first_name} {sender.last_name or ''}".strip()

        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

            # Check if user is blocked
            cur.execute("SELECT blocked FROM users WHERE user_id = %s", (user_identifier,))
            result = cur.fetchone()
            if result and result['blocked']:
                print(f"Ignoring message from blocked user: {user_identifier}")
                cur.close()
                conn.close()
                return
            
            # Add user to users table if not exists
            cur.execute("INSERT INTO users (user_id, blocked) VALUES (%s, false) ON CONFLICT (user_id) DO NOTHING", (user_identifier,))
            conn.commit()
            cur.close()
            conn.close()

            # Download profile picture
            profile_pic_filename = f"{user_identifier}.jpg"
            profile_pic_abs_path = os.path.join(PROFILE_PICS_PATH, profile_pic_filename)

            downloaded_path = await client.download_profile_photo(sender, file=profile_pic_abs_path)

            if downloaded_path:
                profile_pic_rel_path = os.path.join('profile_pics', profile_pic_filename).replace('\\', '/')
            else:
                profile_pic_rel_path = None

            message_text = event.text
            print(f"Received message from {user_identifier}: {message_text}")
            log_to_db('in', user_identifier, message_text, profile_pic_rel_path)

            async with client.action(event.sender_id, 'typing'):
                persona = personas.get(user_identifier, default_persona)
                prompt = f"{persona}\n\n{message_text}"
                response = await asyncio.to_thread(model.generate_content, prompt)
                gemini_response = response.text

                await event.respond(gemini_response)
                print(f"Sent response to {user_identifier}: {gemini_response}")
                log_to_db('out', user_identifier, gemini_response, profile_pic_rel_path)

        except Exception as e:
            error_message = f"An error occurred in handle_new_message: {e}"
            print(error_message)
            # Avoid logging errors to DB if DB connection itself is the issue
            if "database" not in str(e).lower():
                log_to_db('out', user_identifier, error_message, profile_pic_rel_path)
            await event.respond("Sorry, something went wrong while processing your request.")

async def main():
    print("Starting the Telegram agent...")
    if not os.path.exists(PROFILE_PICS_PATH):
        os.makedirs(PROFILE_PICS_PATH)
    await client.start(phone=os.environ.get('PHONE'))
    print("Client started. Listening for messages...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    init_db()
    asyncio.run(main())
