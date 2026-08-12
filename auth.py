from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


PROJECT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TOKEN_FILE = PROJECT_DIR / "token.json"

SCOPES = [
    "https://mail.google.com/",
]


def main() -> None:
    if not CREDENTIALS_FILE.is_file():
        raise FileNotFoundError(
            f"Google OAuth credentials were not found at {CREDENTIALS_FILE}."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        SCOPES,
    )
    creds = flow.run_local_server(port=8080)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    print(f"Gmail authorization completed. Token saved to: {TOKEN_FILE}")


if __name__ == "__main__":
    main()
