import discord
import os
import json
import asyncio
from cryptography.fernet import Fernet
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

# ==================================================================================
# 1. STARTUP SEQUENCE
# ==================================================================================

# --- Load Secrets ---
# In production on Render, these are loaded from Environment Variables.
# For local testing, you can use a .env file (make sure it's in your .gitignore).
from dotenv import load_dotenv
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')
FIREBASE_CREDENTIALS_JSON = os.getenv('FIREBASE_CREDENTIALS_JSON')

if not all([DISCORD_BOT_TOKEN, ENCRYPTION_KEY, FIREBASE_CREDENTIALS_JSON]):
    raise ValueError("One or more critical environment variables are missing.")

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# --- Initialize Services ---
# Initialize Firebase
try:
    cred_json = json.loads(FIREBASE_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Evo has successfully connected to Firebase.")
except Exception as e:
    print(f"FATAL: Could not connect to Firebase: {e}")
    exit()

# Load Default Personality
try:
    # The path is now relative to the /bot directory where the script runs
    with open('personality.json', 'r') as f:
        DEFAULT_PERSONALITY = json.load(f)
    print("Default personality.json loaded.")
except FileNotFoundError:
    print("FATAL: personality.json not found. The bot needs its base personality to function.")
    exit()

# --- Connect to Discord ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True # Required to get member info for webhooks

bot = discord.Client(intents=intents)

# ==================================================================================
# 2. HELPER FUNCTIONS
# ==================================================================================

def decrypt_key(encrypted_key):
    if not encrypted_key:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_key.encode()).decode()
    except Exception:
        return "" # Return empty if decryption fails

async def get_or_create_webhook(channel):
    """Gets an existing webhook or creates a new one for the bot."""
    webhooks = await channel.webhooks()
    for webhook in webhooks:
        if webhook.user == bot.user:
            return webhook
    return await channel.create_webhook(name=f"{bot.user.name}'s Webhook")

# ==================================================================================
# 3. DISCORD EVENTS
# ==================================================================================

@bot.event
async def on_ready():
    """Announce Readiness and Sync Commands."""
    print(f'Evo is online! Logged in as {bot.user}')
    # In a real application, you would sync slash commands here.
    # For now, we'll just print a message.
    print("Slash command syncing would happen here.")

@bot.event
async def on_message(message):
    """The Main Loop: Listening for and processing messages."""
    
    # --- The Filtering Funnel ---
    # 1. Is the message from me?
    if message.author == bot.user:
        return

    # Fetch server configuration first, as it's needed for filtering.
    server_id = str(message.guild.id)
    server_ref = db.collection('server_configs').document(server_id)
    server_doc = server_ref.get()
    
    if not server_doc.exists:
        return # Server is not configured at all.

    server_config = server_doc.to_dict()
    bot_name = server_config.get('custom_bot_name', DEFAULT_PERSONALITY.get('name', 'Evo')).lower()

    # 2. Am I being spoken to?
    is_reply = message.reference and message.reference.resolved and message.reference.resolved.author == bot.user
    is_mentioned = bot.user.mentioned_in(message)
    is_name_called = bot_name in message.content.lower()

    if not (is_reply or is_mentioned or is_name_called):
        return

    # 3. Is this server configured for this channel?
    designated_channel = server_config.get('designated_channel')
    if designated_channel and designated_channel != 'all' and designated_channel != str(message.channel.id):
        return

    # --- The Thinking Process ---
    async with message.channel.typing():
        try:
            # 1. Fetch User Memories
            user_id = str(message.author.id)
            memory_ref = db.collection('memories').document(server_id).collection('users').document(user_id)
            memory_doc = memory_ref.get()
            user_memory = memory_doc.to_dict() if memory_doc.exists else {}
            
            conversation_history = user_memory.get('conversation_history', [])
            personal_summary = user_memory.get('personal_summary', 'No summary available.')
            gossip_summary = user_memory.get('gossip_summary', 'No gossip available.')

            # 2. Determine Personality
            final_personality = server_config.get('custom_personality') or DEFAULT_PERSONALITY.get('system_prompt_components', {}).get('personality')
            rules = "\n".join(DEFAULT_PERSONALITY.get('system_prompt_components', {}).get('rules', []))
            system_instruction = f"{final_personality}\n\n{rules}"
            
            # 3. Build the Final Prompt
            # For simplicity, we'll build a basic prompt for now. A more complex one would structure the memories.
            prompt = f"""
            Here is a summary of what you know about the user '{message.author.display_name}':
            {personal_summary}

            Here is some gossip you've heard about them:
            {gossip_summary}

            Recent conversation history (user messages are prefixed with 'User:', your responses with 'AI:'):
            {''.join(conversation_history)}

            Now, respond to this new message from the user:
            User: {message.clean_content}
            """

            # 4. Talk to the AI
            api_key = decrypt_key(server_config.get('encrypted_api_key', ''))
            backup_api_key = decrypt_key(server_config.get('encrypted_backup_api_key', ''))
            ai_response_text = None

            for key in [api_key, backup_api_key]:
                if not key:
                    continue
                try:
                    genai.configure(api_key=key)
                    model = genai.GenerativeModel(
                        model_name=server_config.get('ai_model', 'gemini-pro'),
                        system_instruction=system_instruction
                    )
                    response = await model.generate_content_async(prompt)
                    ai_response_text = response.text
                    break # Success, so break the loop
                except Exception as e:
                    print(f"AI API call failed with a key. Trying next one. Error: {e}")
            
            if not ai_response_text:
                await message.reply("I'm having trouble connecting to my brain right now. Please check my API key configuration on the website.")
                return

            # --- Action and Learning Phase ---
            
            # Clean the response for the user-facing message, removing the "AI:" prefix if it exists.
            reply_to_user = ai_response_text
            if reply_to_user.lower().strip().startswith('ai:'):
                reply_to_user = reply_to_user.strip()[3:].lstrip()

            # 1. Send the Reply
            custom_avatar_url = server_config.get('custom_avatar_url')
            if custom_avatar_url:
                webhook = await get_or_create_webhook(message.channel)
                await webhook.send(
                    content=reply_to_user, # Use the cleaned version
                    username=server_config.get('custom_bot_name', bot.user.name),
                    avatar_url=custom_avatar_url
                )
            else:
                await message.reply(reply_to_user) # Use the cleaned version

            # 2. Update Memories (Learning)
            # Add the new exchange to history, using the ORIGINAL uncleaned version for the AI's context.
            new_history = conversation_history + [f"User: {message.clean_content}\n", f"AI: {ai_response_text}\n"]
            # Keep only the last 10 items (5 exchanges)
            memory_ref.set({
                'conversation_history': new_history[-10:]
            }, merge=True)

            # Perform reflection calls (simplified for this version)
            # In a full implementation, you would make two more API calls here to update summaries.
            print("Reflection and summary update would happen here.")
            
            # 3. Update Server Nickname if needed
            new_name = server_config.get('custom_bot_name')
            if new_name and message.guild.me.nick != new_name:
                try:
                    await message.guild.me.edit(nick=new_name)
                except discord.Forbidden:
                    print(f"Could not change nickname on server {server_id}. Missing permissions.")


        except Exception as e:
            print(f"An unexpected error occurred in on_message: {e}")
            await message.reply("Something went very wrong while I was thinking. My apologies!")


# ==================================================================================
# 4. RUN THE BOT
# ==================================================================================
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
