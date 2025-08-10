import discord
from discord import app_commands
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
from dotenv import load_dotenv
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')
FIREBASE_CREDENTIALS_JSON = os.getenv('FIREBASE_CREDENTIALS_JSON')
WEBSITE_URL = "https://evo-discord-bot.onrender.com/" # Your website URL

if not all([DISCORD_BOT_TOKEN, ENCRYPTION_KEY, FIREBASE_CREDENTIALS_JSON]):
    raise ValueError("One or more critical environment variables are missing.")

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# --- Initialize Services ---
try:
    cred_json = json.loads(FIREBASE_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Evo has successfully connected to Firebase.")
except Exception as e:
    print(f"FATAL: Could not connect to Firebase: {e}")
    exit()

try:
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
intents.members = True

class EvoClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = EvoClient(intents=intents)

# ==================================================================================
# 2. HELPER FUNCTIONS
# ==================================================================================

def decrypt_key(encrypted_key):
    if not encrypted_key: return ""
    try: return cipher_suite.decrypt(encrypted_key.encode()).decode()
    except Exception: return ""

async def get_or_create_webhook(channel):
    webhooks = await channel.webhooks()
    for webhook in webhooks:
        if webhook.user == bot.user:
            return webhook
    return await channel.create_webhook(name=f"{bot.user.name}'s Webhook")

async def update_summaries(model, memory_ref, user_name, conversation_exchange, old_personal_summary):
    print(f"Starting personal summary reflection for user: {user_name}")
    try:
        personal_summary_prompt = f"""
        You are a memory assistant. Your job is to update a user summary based on a new conversation.
        The user's name is {user_name}.
        Here is the old summary of the user: --- {old_personal_summary} ---
        Here is the latest conversation exchange: --- {conversation_exchange} ---
        Based on this new information, provide an updated summary of the user. The summary should be a concise paragraph, written in the third person.
        Keep the summary under 200 words. If no new important personal information was learned, just return the original summary.
        """
        personal_response = await model.generate_content_async(personal_summary_prompt)
        new_personal_summary = personal_response.text
        await asyncio.to_thread(memory_ref.set, {'personal_summary': new_personal_summary}, merge=True)
        print(f"Successfully updated personal summary for {user_name}.")
    except Exception as e:
        print(f"Could not update personal summary for {user_name}. Error: {e}")

async def update_gossip_summary(model, server_id, mentioned_user, author_name, message_content):
    print(f"Starting gossip reflection for mentioned user: {mentioned_user.display_name}")
    try:
        gossip_memory_ref = db.collection('memories').document(server_id).collection('users').document(str(mentioned_user.id))
        gossip_memory_doc = gossip_memory_ref.get()
        gossip_memory = gossip_memory_doc.to_dict() if gossip_memory_doc.exists else {}
        old_gossip_summary = gossip_memory.get('gossip_summary', 'No gossip available.')

        gossip_prompt = f"""
        You are a memory assistant. You are listening to a conversation.
        The user '{author_name}' just said the following about '{mentioned_user.display_name}':
        ---
        {message_content}
        ---
        Here is the old gossip summary you have about '{mentioned_user.display_name}':
        ---
        {old_gossip_summary}
        ---
        Based on what '{author_name}' said, provide an updated gossip summary for '{mentioned_user.display_name}'.
        Keep the summary under 200 words. If no new important information was learned, just return the original summary.
        """
        gossip_response = await model.generate_content_async(gossip_prompt)
        new_gossip_summary = gossip_response.text
        await asyncio.to_thread(gossip_memory_ref.set, {'gossip_summary': new_gossip_summary}, merge=True)
        print(f"Successfully updated gossip summary for {mentioned_user.display_name}.")
    except Exception as e:
        print(f"Could not update gossip summary for {mentioned_user.display_name}. Error: {e}")

# ==================================================================================
# 3. SLASH COMMANDS
# ==================================================================================

@bot.tree.command(name="evo", description="Check Evo's setup status for this server.")
@app_commands.default_permissions(administrator=True)
async def evo_setup_check(interaction: discord.Interaction):
    server_id = str(interaction.guild.id)
    server_ref = db.collection('server_configs').document(server_id)
    server_doc = server_ref.get()

    if server_doc.exists:
        embed = discord.Embed(
            title="Evo is Ready!",
            description=f"Your bot is all set-up and ready to chat.\n\nYou can make changes to your bot's settings at any time on the [Evo Dashboard]({WEBSITE_URL}).",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="Evo is Not Yet Setup",
            description=f"Evo needs to be configured before she can start chatting on this server.\n\nAn administrator can set her up in a few clicks on the [Evo Dashboard]({WEBSITE_URL}).",
            color=discord.Color.red()
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==================================================================================
# 4. DISCORD EVENTS
# ==================================================================================

@bot.event
async def on_ready():
    print(f'Evo is online! Logged in as {bot.user}')
    for guild in bot.guilds:
        server_ref = db.collection('server_configs').document(str(guild.id))
        server_doc = server_ref.get()
        if server_doc.exists:
            server_config = server_doc.to_dict()
            bot_name = server_config.get('custom_bot_name')
            if bot_name and guild.me.nick != bot_name:
                try: await guild.me.edit(nick=bot_name)
                except discord.Forbidden: pass
        else:
            # Point 1: If name is not changed (no config), set to Evo
            if guild.me.nick != "Evo":
                try: await guild.me.edit(nick="Evo")
                except discord.Forbidden: pass

@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild: return

    server_id = str(message.guild.id)
    server_ref = db.collection('server_configs').document(server_id)
    server_doc = server_ref.get()
    
    if not server_doc.exists: return

    server_config = server_doc.to_dict()
    bot_name = server_config.get('custom_bot_name', DEFAULT_PERSONALITY.get('name', 'Evo')).lower()

    is_reply = message.reference and message.reference.resolved and message.reference.resolved.author == bot.user
    is_mentioned = bot.user.mentioned_in(message)
    is_name_called = bot_name in message.content.lower()

    if not (is_reply or is_mentioned or is_name_called): return

    designated_channel = server_config.get('designated_channel')
    if designated_channel and designated_channel != 'all' and str(message.channel.id) != designated_channel: return

    async with message.channel.typing():
        try:
            user_id = str(message.author.id)
            memory_ref = db.collection('memories').document(server_id).collection('users').document(user_id)
            memory_doc = memory_ref.get()
            user_memory = memory_doc.to_dict() if memory_doc.exists else {}
            
            conversation_history = user_memory.get('conversation_history', [])
            personal_summary = user_memory.get('personal_summary', 'No summary available.')
            
            final_personality = server_config.get('custom_personality') or DEFAULT_PERSONALITY.get('system_prompt_components', {}).get('personality')
            rules = "\n".join(DEFAULT_PERSONALITY.get('system_prompt_components', {}).get('rules', []))
            
            # Point 3: Inject the name separately
            name_instruction = f"You are {server_config.get('custom_bot_name', 'Evo')}."
            system_instruction = f"{name_instruction}\n{final_personality}\n\n{rules}"
            
            prompt = f"""
            Here is a summary of what you know about the user '{message.author.display_name}':
            {personal_summary}
            Recent conversation history (user messages are prefixed with 'User:', your responses with 'AI:'):
            {''.join(conversation_history)}
            Now, respond to this new message from the user:
            User: {message.clean_content}
            """

            api_key = decrypt_key(server_config.get('encrypted_api_key', ''))
            backup_api_key = decrypt_key(server_config.get('encrypted_backup_api_key', ''))
            ai_response_text = None
            model = None

            for key in [api_key, backup_api_key]:
                if not key: continue
                try:
                    genai.configure(api_key=key)
                    model = genai.GenerativeModel(
                        model_name=server_config.get('ai_model', 'gemini-pro'),
                        system_instruction=system_instruction
                    )
                    response = await model.generate_content_async(prompt)
                    ai_response_text = response.text
                    break
                except Exception as e:
                    print(f"AI API call failed with a key. Trying next one. Error: {e}")
            
            if not ai_response_text:
                await message.reply("I'm having trouble connecting to my brain right now. Please check my API key configuration on the website.")
                return

            reply_to_user = ai_response_text
            if reply_to_user.lower().strip().startswith('ai:'):
                reply_to_user = reply_to_user.strip()[3:].lstrip()

            custom_avatar_url = server_config.get('custom_avatar_url')
            if custom_avatar_url:
                webhook = await get_or_create_webhook(message.channel)
                await webhook.send(
                    content=reply_to_user,
                    username=server_config.get('custom_bot_name', bot.user.name),
                    avatar_url=custom_avatar_url
                )
            else:
                await message.reply(reply_to_user)

            latest_exchange = f"User: {message.clean_content}\nAI: {ai_response_text}\n"
            new_history = conversation_history + [latest_exchange]
            
            await asyncio.to_thread(memory_ref.set, {'conversation_history': new_history[-10:]}, merge=True)
            
            if model:
                # Update personal summary for the author
                await update_summaries(model, memory_ref, message.author.display_name, latest_exchange, personal_summary)
                
                # Point 2: Update gossip summary for mentioned users
                if message.mentions:
                    for mentioned_user in message.mentions:
                        if mentioned_user != bot.user:
                            await update_gossip_summary(model, server_id, mentioned_user, message.author.display_name, message.clean_content)
            
            new_name = server_config.get('custom_bot_name')
            if new_name and message.guild.me.nick != new_name:
                try: await message.guild.me.edit(nick=new_name)
                except discord.Forbidden: print(f"Could not change nickname on server {server_id}. Missing permissions.")

        except Exception as e:
            print(f"An unexpected error occurred in on_message: {e}")
            await message.reply("Something went very wrong while I was thinking. My apologies!")

# ==================================================================================
# 5. RUN THE BOT
# ==================================================================================
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
