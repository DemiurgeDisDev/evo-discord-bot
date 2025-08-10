from flask import Flask, jsonify, request, redirect, session
from flask_cors import CORS
import os
import requests
from requests_oauthlib import OAuth2Session
import firebase_admin
from firebase_admin import credentials, firestore
from cryptography.fernet import Fernet
import traceback

# --- INITIALIZATION ---
app = Flask(__name__)

# --- CONFIGURATION FROM ENVIRONMENT VARIABLES ---
app.secret_key = os.getenv('FLASK_SECRET_KEY')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')
BOT_TOKEN = os.getenv('BOT_TOKEN')
IMGBB_API_KEY = os.getenv('IMGBB_API_KEY')

if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY not found in environment variables.")
cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# --- FIREBASE INITIALIZATION ---
try:
    if 'FIREBASE_CREDENTIALS_JSON' in os.environ:
        import json
        cred_json = json.loads(os.environ.get('FIREBASE_CREDENTIALS_JSON'))
        cred = credentials.Certificate(cred_json)
    else:
        # This path is for local development, Render uses the environment variable.
        cred = credentials.ApplicationDefault()
        
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Successfully connected to Firebase.")
except Exception as e:
    print(f"Could not connect to Firebase: {e}")
    db = None

# --- CORS & SESSION CONFIGURATION ---
CORS(app, supports_credentials=True, origins=[FRONTEND_URL])
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',
)

# --- DISCORD API DETAILS ---
API_BASE_URL = 'https://discord.com/api'
AUTHORIZATION_BASE_URL = 'https://discord.com/api/oauth2/authorize'
TOKEN_URL = 'https://discord.com/api/oauth2/token'

# --- HELPER FUNCTIONS ---
def encrypt_key(key):
    if not key: return ""
    return cipher_suite.encrypt(key.encode()).decode()

def decrypt_key(encrypted_key):
    if not encrypted_key: return ""
    try:
        return cipher_suite.decrypt(encrypted_key.encode()).decode()
    except Exception:
        return ""

def get_user_guilds(token):
    discord = OAuth2Session(DISCORD_CLIENT_ID, token=token)
    response = discord.get(API_BASE_URL + '/users/@me/guilds')
    return response.json() if response.status_code == 200 else []

# --- API ROUTES ---

@app.route('/')
def home():
    return jsonify({"status": "online", "message": "Evo Backend is running successfully!"})

@app.route('/api/bot-info')
def get_bot_info():
    if not BOT_TOKEN: return jsonify({"error": "Bot token not configured"}), 500
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    
    app_response = requests.get(f"{API_BASE_URL}/oauth2/applications/@me", headers=headers)
    if app_response.status_code == 200:
        app_info = app_response.json()
        icon_hash = app_info.get('icon')
        app_id = app_info.get('id')
        if icon_hash:
            avatar_url = f"https://cdn.discordapp.com/app-icons/{app_id}/{icon_hash}.png?size=64"
            return jsonify({"avatar": avatar_url})

    user_response = requests.get(f"{API_BASE_URL}/users/@me", headers=headers)
    if user_response.status_code == 200:
        bot_user = user_response.json()
        avatar_hash = bot_user.get('avatar')
        bot_id = bot_user.get('id')
        if avatar_hash:
            avatar_url = f"https://cdn.discordapp.com/avatars/{bot_id}/{avatar_hash}.png?size=64"
        else:
            avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"
        return jsonify({"avatar": avatar_url})

    return jsonify({"error": "Failed to fetch bot info"}), 500

@app.route('/api/upload-avatar/<server_id>', methods=['POST'])
def upload_avatar(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if 'avatar' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['avatar']
    if file.filename == '': return jsonify({"error": "No selected file"}), 400
    if not IMGBB_API_KEY: return jsonify({"error": "imgbb API key not configured on the server"}), 500

    imgbb_api_url = "https://api.imgbb.com/1/upload"
    payload = {"key": IMGBB_API_KEY}
    
    try:
        response = requests.post(imgbb_api_url, params=payload, files={"image": file})
        response.raise_for_status()
        imgbb_data = response.json()
        if imgbb_data.get('success'):
            avatar_url = imgbb_data['data']['url']
            db.collection('server_configs').document(server_id).update({"custom_avatar_url": avatar_url})
            return jsonify({"success": True, "avatar_url": avatar_url})
        else:
            return jsonify({"error": imgbb_data.get('error', {}).get('message', 'imgbb API returned an error')}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to upload image: {e}"}), 500

# --- DISCORD OAUTH2 ROUTES ---
@app.route('/login')
def login():
    scope = ['identify', 'guilds']
    discord = OAuth2Session(DISCORD_CLIENT_ID, redirect_uri=DISCORD_REDIRECT_URI, scope=scope)
    authorization_url, state = discord.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth2_state'] = state
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    try:
        if 'error' in request.args:
            error_description = request.args.get('error_description', 'No description provided.')
            print(f"Discord returned an error on callback: {request.args['error']} - {error_description}")
            return f"An error occurred during authentication: {error_description}", 400

        session_state = session.get('oauth2_state')
        request_state = request.args.get('state')

        if not session_state or session_state != request_state:
            print(f"State mismatch error. Session state: {session_state}, Request state: {request_state}")
            return "State mismatch. Please try logging in again.", 400

        discord = OAuth2Session(DISCORD_CLIENT_ID, state=session_state, redirect_uri=DISCORD_REDIRECT_URI)
        
        # FIX: Ensure the callback URL uses https when running behind a proxy
        authorization_response = request.url
        if authorization_response.startswith('http://'):
            authorization_response = 'https://' + authorization_response[7:]

        print("Attempting to fetch token from Discord...")
        token = discord.fetch_token(
            TOKEN_URL,
            client_secret=DISCORD_CLIENT_SECRET,
            authorization_response=authorization_response
        )
        
        print("Token fetched successfully.")
        session['discord_token'] = token
        
        user_info_response = discord.get(API_BASE_URL + '/users/@me')
        user_info_response.raise_for_status()
        user_info = user_info_response.json()
        session['user'] = user_info
        
        return redirect(f"{FRONTEND_URL}/?loggedin=true")

    except Exception as e:
        print("--- AN ERROR OCCURRED IN /callback ---")
        traceback.print_exc()
        print("--------------------------------------")
        return "An internal server error occurred during authentication.", 500

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({"status": "success", "message": "Logged out"})

@app.route('/api/me')
def get_current_user():
    user = session.get('user')
    if user: return jsonify(user)
    return jsonify({"error": "Not logged in"}), 401

# --- LIVE DATA API ROUTES ---
@app.route('/api/user-servers', methods=['GET'])
def get_user_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500
    
    user_guilds = get_user_guilds(session.get('discord_token'))
    admin_guild_ids = {g['id'] for g in user_guilds if (int(g['permissions']) & 0x8) == 0x8}

    configs_ref = db.collection('server_configs').stream()
    user_admin_configs = []
    for config in configs_ref:
        if config.id in admin_guild_ids:
            matching_guild = next((g for g in user_guilds if g['id'] == config.id), None)
            if matching_guild:
                icon_hash = matching_guild.get('icon')
                icon_url = f"https://cdn.discordapp.com/icons/{config.id}/{icon_hash}.png" if icon_hash else f"https://placehold.co/64x64/7f9cf5/ffffff?text={matching_guild.get('name', '?')[0]}"
                user_admin_configs.append({"id": config.id, "name": matching_guild.get('name'), "icon": icon_url})
    return jsonify(user_admin_configs)

@app.route('/api/available-servers', methods=['GET'])
def get_available_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500
    
    user_guilds = get_user_guilds(session.get('discord_token'))
    configs_ref = db.collection('server_configs').stream()
    configured_guild_ids = {config.id for config in configs_ref}
    
    available_guilds = []
    for guild in user_guilds:
        if (int(guild['permissions']) & 0x8) == 0x8 and guild['id'] not in configured_guild_ids:
            icon_hash = guild.get('icon')
            icon_url = f"https://cdn.discordapp.com/icons/{guild['id']}/{icon_hash}.png" if icon_hash else f"https://placehold.co/64x64/7f9cf5/ffffff?text={guild.get('name', '?')[0]}"
            available_guilds.append({"id": guild['id'], "name": guild['name'], "icon": icon_url})
            
    return jsonify(available_guilds)

@app.route('/api/remove-server/<server_id>', methods=['DELETE'])
def remove_server(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500
    try:
        db.collection('server_configs').document(server_id).delete()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": "Failed to remove server"}), 500

@app.route('/api/server-channels/<server_id>', methods=['GET'])
def get_server_channels(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not BOT_TOKEN: return jsonify({"error": "Bot token not configured"}), 500

    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    response = requests.get(f"{API_BASE_URL}/guilds/{server_id}/channels", headers=headers)
    
    if response.status_code == 200:
        all_channels = response.json()
        text_channels = [{"id": ch["id"], "name": ch["name"]} for ch in all_channels if ch["type"] == 0]
        return jsonify(text_channels)
    else:
        return jsonify({"error": "Failed to fetch channels. Is the bot on this server?"}), response.status_code

@app.route('/api/server-settings/<server_id>', methods=['GET'])
def get_server_settings(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500

    doc_ref = db.collection('server_configs').document(server_id)
    doc = doc_ref.get()

    if doc.exists:
        data = doc.to_dict()
        api_key = decrypt_key(data.get("encrypted_api_key", ""))
        backup_key = decrypt_key(data.get("encrypted_backup_api_key", ""))
        data["api_key_last4"] = api_key[-4:] if api_key else ""
        data["backup_api_key_last4"] = backup_key[-4:] if backup_key else ""
        if "encrypted_api_key" in data: del data["encrypted_api_key"]
        if "encrypted_backup_api_key" in data: del data["encrypted_backup_api_key"]
        return jsonify(data)
    else:
        return jsonify({"error": "No settings found for this server."}), 404

@app.route('/api/server-settings/<server_id>', methods=['POST'])
def save_server_settings(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500

    settings = request.json
    server_ref = db.collection('server_configs').document(server_id)
    
    update_data = {
        "ai_model": settings.get('ai_model'),
        "designated_channel": settings.get('designated_channel'),
        'custom_bot_name': settings.get('custom_name'),
        'custom_personality': settings.get('custom_personality')
    }

    if settings.get('api_key'):
        update_data["encrypted_api_key"] = encrypt_key(settings.get('api_key'))
    if settings.get('backup_api_key'):
        update_data["encrypted_backup_api_key"] = encrypt_key(settings.get('backup_api_key'))

    server_ref.set(update_data, merge=True)
    
    return jsonify({"status": "success", "message": f"Settings for server {server_id} saved."})

# This part allows us to run the app.
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
