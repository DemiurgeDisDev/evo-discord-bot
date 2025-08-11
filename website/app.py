from flask import Flask, jsonify, request, redirect, session
from flask_cors import CORS
import os
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from cryptography.fernet import Fernet
import traceback
from urllib.parse import urlencode

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
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify guilds'
    }
    authorization_url = f"{AUTHORIZATION_BASE_URL}?{urlencode(params)}"
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    try:
        if 'error' in request.args:
            return f"An error occurred: {request.args['error_description']}", 400

        code = request.args.get('code')
        if not code:
            return "Missing authorization code from Discord.", 400

        data = {
            'client_id': DISCORD_CLIENT_ID,
            'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': DISCORD_REDIRECT_URI
        }
        headers = { 
            'Content-Type': 'application/x-www-form-urlencoded',
            # FIX: Add a standard browser User-Agent to avoid being rate-limited
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
        }
        
        token_response = requests.post(TOKEN_URL, data=data, headers=headers)
        token_response.raise_for_status()
        token_data = token_response.json()

        session['discord_token'] = token_data

        user_headers = { 'Authorization': f"Bearer {token_data['access_token']}" }
        user_info_response = requests.get(f"{API_BASE_URL}/users/@me", headers=user_headers)
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

@app.route('/api/user-servers', methods=['GET'])
def get_user_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500
    
    token = session.get('discord_token')
    headers = { 'Authorization': f"Bearer {token['access_token']}" }
    response = requests.get(f"{API_BASE_URL}/users/@me/guilds", headers=headers)
    user_guilds = response.json() if response.status_code == 200 else []
    
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
    
    token = session.get('discord_token')
    headers = { 'Authorization': f"Bearer {token['access_token']}" }
    response = requests.get(f"{API_BASE_URL}/users/@me/guilds", headers=headers)
    user_guilds = response.json() if response.status_code == 200 else []
    
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

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
