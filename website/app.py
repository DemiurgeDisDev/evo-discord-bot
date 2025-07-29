from flask import Flask, jsonify, request
from flask_cors import CORS # To handle requests between frontend and backend
import os

# Initialize the Flask app
app = Flask(__name__)
# Enable CORS to allow our frontend to talk to our backend
CORS(app)

# --- MOCK DATABASE (This will be replaced by Firebase later) ---
# We're storing the data here in memory for now. It will reset if the server restarts.
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


# --- API ROUTES (The "Engine" for our website) ---

@app.route('/')
def home():
    """A simple route to confirm the backend is running."""
    return jsonify({
        "status": "online",
        "message": "Evo Backend is running successfully!"
    })

@app.route('/api/user-servers', methods=['GET'])
def get_user_servers():
    """Returns the list of servers the user has already configured."""
    return jsonify(mock_db["user_servers"])

@app.route('/api/available-servers', methods=['GET'])
def get_available_servers():
    """Returns the list of servers the user is an admin of, which they can add."""
    # In a real app, this would talk to the Discord API.
    return jsonify(mock_db["available_servers"])

@app.route('/api/add-server', methods=['POST'])
def add_server():
    """Handles adding a new server to the user's dashboard."""
    data = request.json
    server_id = data.get('id')
    server_name = data.get('name')
    server_icon = data.get('icon')

    if not server_id or not server_name or not server_icon:
        return jsonify({"error": "Missing server data"}), 400

    # Check if server is already added
    if not any(s['id'] == server_id for s in mock_db['user_servers']):
        new_server = {"id": server_id, "name": server_name, "icon": server_icon}
        mock_db['user_servers'].append(new_server)
        print(f"Added server: {new_server}") # For debugging
    
    # Find the server to return it (even if it already existed)
    added_server = next((s for s in mock_db['user_servers'] if s['id'] == server_id), None)
    return jsonify({"status": "success", "server": added_server})

# NEW: Route to remove a server
@app.route('/api/remove-server/<server_id>', methods=['DELETE'])
def remove_server(server_id):
    """Handles removing a server from the user's dashboard."""
    initial_len = len(mock_db['user_servers'])
    mock_db['user_servers'] = [s for s in mock_db['user_servers'] if s['id'] != server_id]
    if len(mock_db['user_servers']) < initial_len:
        print(f"Removed server: {server_id}")
        return jsonify({"status": "success", "message": f"Server {server_id} removed."})
    else:
        return jsonify({"error": "Server not found"}), 404


@app.route('/api/server-settings/<server_id>', methods=['POST'])
def save_server_settings(server_id):
    """Handles saving the settings for a specific server."""
    settings = request.json
    print(f"Received settings for server {server_id}:")
    print(f"  AI Model: {settings.get('ai_model')}")
    print(f"  API Key: ...{settings.get('api_key', '')[-4:]}")
    print(f"  Backup Key: ...{settings.get('backup_api_key', '')[-4:]}")
    
    # In a real app, this is where you would encrypt the keys and save to Firebase.
    
    return jsonify({"status": "success", "message": f"Settings for server {server_id} received."})


# This part allows us to run the app.
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
