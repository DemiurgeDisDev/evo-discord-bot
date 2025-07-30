from flask import Flask, jsonify, request, redirect, session
from flask_cors import CORS
import os
import requests
from requests_oauthlib import OAuth2Session
import firebase_admin
from firebase_admin import credentials, firestore
from cryptography.fernet import Fernet

# --- INITIALIZATION ---
app = Flask(__name__)

# --- CONFIGURATION FROM ENVIRONMENT VARIABLES ---
app.secret_key = os.getenv('FLASK_SECRET_KEY')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')

# Ensure encryption key is loaded
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
    return cipher_suite.decrypt(encrypted_key.encode()).decode()

def get_user_guilds(token):
    discord = OAuth2Session(DISCORD_CLIENT_ID, token=token)
    response = discord.get(API_BASE_URL + '/users/@me/guilds')
    if response.status_code == 200:
        return response.json()
    return []

# --- API ROUTES ---

@app.route('/')
def home():
    return jsonify({"status": "online", "message": "Evo Backend is running successfully!"})

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
    discord = OAuth2Session(DISCORD_CLIENT_ID, state=session.get('oauth2_state'), redirect_uri=DISCORD_REDIRECT_URI)
    token = discord.fetch_token(
        TOKEN_URL,
        client_secret=DISCORD_CLIENT_SECRET,
        authorization_response=request.url,
    )
    session['discord_token'] = token
    user_info_response = discord.get(API_BASE_URL + '/users/@me')
    user_info = user_info_response.json()
    session['user'] = user_info
    return redirect(f"{FRONTEND_URL}/?loggedin=true")

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({"status": "success", "message": "Logged out"})

@app.route('/api/me')
def get_current_user():
    user = session.get('user')
    if user:
        return jsonify(user)
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
                user_admin_configs.append({
                    "id": config.id,
                    "name": matching_guild.get('name'),
                    "icon": icon_url
                })
    return jsonify(user_admin_configs)

@app.route('/api/available-servers', methods=['GET'])
def get_available_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    
    user_guilds = get_user_guilds(session.get('discord_token'))
    admin_guilds = []
    for guild in user_guilds:
        if (int(guild['permissions']) & 0x8) == 0x8:
            icon_hash = guild.get('icon')
            icon_url = f"https://cdn.discordapp.com/icons/{guild['id']}/{icon_hash}.png" if icon_hash else f"https://placehold.co/64x64/7f9cf5/ffffff?text={guild.get('name', '?')[0]}"
            admin_guilds.append({
                "id": guild['id'],
                "name": guild['name'],
                "icon": icon_url
            })
    return jsonify(admin_guilds)

@app.route('/api/remove-server/<server_id>', methods=['DELETE'])
def remove_server(server_id):
    """Deletes a server's config from Firebase."""
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500
    
    try:
        db.collection('server_configs').document(server_id).delete()
        # In a real app, you'd also delete the memories for this server
        print(f"Removed server config: {server_id}")
        return jsonify({"status": "success", "message": f"Server {server_id} removed."})
    except Exception as e:
        print(f"Error removing server {server_id}: {e}")
        return jsonify({"error": "Failed to remove server"}), 500

@app.route('/api/server-settings/<server_id>', methods=['POST'])
def save_server_settings(server_id):
    """Creates or updates settings for a server in Firebase."""
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    if not db: return jsonify({"error": "Database not connected"}), 500

    settings = request.json
    
    encrypted_key = encrypt_key(settings.get('api_key', ''))
    encrypted_backup_key = encrypt_key(settings.get('backup_api_key', ''))

    # Check if a document for this server already exists
    server_ref = db.collection('server_configs').document(server_id)
    doc = server_ref.get()

    if not doc.exists:
        # Document does not exist, so we create it with defaults
        print(f"Creating new config for server {server_id}")
        new_config = {
            "server_name": settings.get('server_name'), # Sent from frontend now
            "ai_model": settings.get('ai_model'),
            "encrypted_api_key": encrypted_key,
            "encrypted_backup_api_key": encrypted_backup_key,
            "user_premium": False, # Default value
            "server_premium": False # Default value
        }
        # Add premium fields if they exist
        if 'custom_name' in settings and settings.get('custom_name'):
            new_config['custom_bot_name'] = settings.get('custom_name')
        if 'custom_personality' in settings and settings.get('custom_personality'):
            new_config['custom_personality'] = settings.get('custom_personality')
        
        server_ref.set(new_config)
    else:
        # Document exists, so we update it
        print(f"Updating config for server {server_id}")
        update_data = {
            "ai_model": settings.get('ai_model'),
            "encrypted_api_key": encrypted_key,
            "encrypted_backup_api_key": encrypted_backup_key,
        }
        if 'custom_name' in settings and settings.get('custom_name'):
            update_data['custom_bot_name'] = settings.get('custom_name')
        if 'custom_personality' in settings and settings.get('custom_personality'):
            update_data['custom_personality'] = settings.get('custom_personality')
        
        server_ref.update(update_data)
    
    print(f"Saved settings for server {server_id}")
    return jsonify({"status": "success", "message": f"Settings for server {server_id} saved."})

# This part allows us to run the app.
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
