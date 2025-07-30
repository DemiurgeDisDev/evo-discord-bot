from flask import Flask, jsonify, request, redirect, session
from flask_cors import CORS
import os
import requests
from requests_oauthlib import OAuth2Session

# Initialize the Flask app
app = Flask(__name__)

# --- CONFIGURATION FROM ENVIRONMENT VARIABLES ---
app.secret_key = os.getenv('FLASK_SECRET_KEY')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL')

# --- NEW: SESSION COOKIE CONFIGURATION FOR PRODUCTION ---
# This ensures cookies are sent correctly between the backend and frontend on different domains.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',
)

# --- CORS CONFIGURATION ---
CORS(app, supports_credentials=True, origins=[FRONTEND_URL])

# Discord API endpoints
API_BASE_URL = 'https://discord.com/api'
AUTHORIZATION_BASE_URL = 'https://discord.com/api/oauth2/authorize'
TOKEN_URL = 'https://discord.com/api/oauth2/token'

# --- MOCK DATABASE (This will be replaced by Firebase later) ---
mock_db = {
    "user_servers": [
        {"id": '1', "name": 'Gaming Zone', "icon": 'https://placehold.co/64x64/7f9cf5/ffffff?text=GZ'},
        {"id": '2', "name": 'Art Club', "icon": 'https://placehold.co/64x64/f56565/ffffff?text=AC'}
    ],
    "available_servers": [
        {"id": '1', "name": 'Gaming Zone', "icon": 'https://placehold.co/64x64/7f9cf5/ffffff?text=GZ'},
        {"id": '2', "name": 'Art Club', "icon": 'https://placehold.co/64x64/f56565/ffffff?text=AC'},
        {"id": '3', "name": 'Study Group', "icon": 'https://placehold.co/64x64/48bb78/ffffff?text=SG'},
        {"id": '4', "name": 'Anime Fans', "icon": 'https://placehold.co/64x64/f6e05e/ffffff?text=AF'}
    ]
}
# --- END OF MOCK DATABASE ---


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
    try:
        discord = OAuth2Session(DISCORD_CLIENT_ID, state=session.get('oauth2_state'), redirect_uri=DISCORD_REDIRECT_URI)
        token = discord.fetch_token(
            TOKEN_URL,
            client_secret=DISCORD_CLIENT_SECRET,
            authorization_response=request.url
        )
        
        session['discord_token'] = token
        user_info_response = discord.get(API_BASE_URL + '/users/@me')
        user_info = user_info_response.json()
        session['user'] = user_info
        print(f"User logged in: {user_info.get('username')}#{user_info.get('discriminator')}")

        # --- DEBUGGING LOGS ---
        frontend_url = os.getenv('FRONTEND_URL')
        print(f"DEBUG: Read FRONTEND_URL from environment: {frontend_url}")

        if not frontend_url:
            print("ERROR: FRONTEND_URL environment variable is not set!")
            return jsonify({"error": "Backend configuration issue: FRONTEND_URL is missing."}), 500

        final_redirect_url = f"{frontend_url}/?loggedin=true"
        print(f"DEBUG: Redirecting user to: {final_redirect_url}")
        # --- END DEBUGGING LOGS ---

        return redirect(final_redirect_url)

    except Exception as e:
        print(f"An error occurred in the callback function: {e}")
        return jsonify({"error": "An internal error occurred during login."}), 500


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


# --- EXISTING API ROUTES ---

@app.route('/api/user-servers', methods=['GET'])
def get_user_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    return jsonify(mock_db["user_servers"])

@app.route('/api/available-servers', methods=['GET'])
def get_available_servers():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    return jsonify(mock_db["available_servers"])

@app.route('/api/add-server', methods=['POST'])
def add_server():
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    data = request.json
    server_id = data.get('id')
    server_name = data.get('name')
    server_icon = data.get('icon')
    if not server_id or not server_name or not server_icon:
        return jsonify({"error": "Missing server data"}), 400
    if not any(s['id'] == server_id for s in mock_db['user_servers']):
        new_server = {"id": server_id, "name": server_name, "icon": server_icon}
        mock_db['user_servers'].append(new_server)
    added_server = next((s for s in mock_db['user_servers'] if s['id'] == server_id), None)
    return jsonify({"status": "success", "server": added_server})

@app.route('/api/remove-server/<server_id>', methods=['DELETE'])
def remove_server(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    initial_len = len(mock_db['user_servers'])
    mock_db['user_servers'] = [s for s in mock_db['user_servers'] if s['id'] != server_id]
    if len(mock_db['user_servers']) < initial_len:
        return jsonify({"status": "success", "message": f"Server {server_id} removed."})
    else:
        return jsonify({"error": "Server not found"}), 404

@app.route('/api/server-settings/<server_id>', methods=['POST'])
def save_server_settings(server_id):
    if not session.get('user'): return jsonify({"error": "Not logged in"}), 401
    settings = request.json
    print(f"Received settings for server {server_id}: {settings}")
    return jsonify({"status": "success", "message": f"Settings for server {server_id} received."})

# This part allows us to run the app.
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
