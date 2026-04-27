import os
import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
SECRET_KEY = os.getenv("FYERS_SECRET_KEY")
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI")

PORT = 8000
auth_code = None


class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = parse_qs(urlparse(self.path).query)
        if "auth_code" in query:
            auth_code = query["auth_code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;">
                <div style="text-align:center;"><h2>Authorization Successful!</h2>
                <p>You can close this window now.</p></div></body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing auth_code parameter")

    def log_message(self, format, *args):
        pass


def generate_token():
    global auth_code
    print("\n=== Fyers Access Token Generator ===\n")

    if not all([CLIENT_ID, SECRET_KEY, REDIRECT_URI]):
        print("ERROR: Missing Fyers credentials in .env file")
        print("Ensure FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI are set")
        return

    base_url = "https://api-t1.fyers.in/api/v3/generate-authcode"
    params = f"client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&state=token_gen"
    auth_url = f"{base_url}?{params}"

    server = HTTPServer(("0.0.0.0", PORT), AuthHandler)
    print(f"1. Starting local server on port {PORT}...")
    print(f"2. Opening browser for Fyers authentication...")
    print(f"3. Auth URL: {auth_url}")

    webbrowser.open(auth_url)
    print("4. Waiting for authorization callback...")

    server.timeout = 120
    server.handle_request()
    server.server_close()

    if not auth_code:
        print("\nERROR: No auth code received. Timed out or cancelled.")
        return

    print(f"\n5. Auth code received. Exchanging for access token...")

    import requests
    import hashlib

    token_url = "https://api-t1.fyers.in/api/v3/validate-authcode"
    app_id_hash = hashlib.sha256(f"{CLIENT_ID}:{SECRET_KEY}".encode()).hexdigest()
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code,
    }

    try:
        resp = requests.post(token_url, json=payload)
        data = resp.json()

        if data.get("s") == "ok":
            access_token = data.get("access_token")
            if access_token:
                from trade_manager import save_token

                save_token(access_token)
                print(f"\n✓ Token generated and saved successfully!")
                print(f"  Access Token: {access_token[:50]}...")
                print(f"  Token expires in 24 hours")
            else:
                print(f"\nERROR: No access_token in response: {data}")
        else:
            print(f"\nERROR: API returned error: {data}")
    except Exception as e:
        print(f"\nERROR: Failed to exchange auth code: {e}")


if __name__ == "__main__":
    generate_token()
