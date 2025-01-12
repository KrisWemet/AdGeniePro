import os
import requests
from dotenv import load_dotenv

load_dotenv()

class FbAgent:
    def __init__(self):
        self.access_token = os.getenv("FB_ACCESS_TOKEN")
        self.ad_account_id = os.getenv("FB_AD_ACCOUNT_ID")  # e.g. 1234567890 (no "act_" prefix)
        self.api_version = "v16.0"

    def get_campaigns(self):
        url = f"https://graph.facebook.com/{self.api_version}/act_{self.ad_account_id}/campaigns"
        params = {"access_token": self.access_token}
        r = requests.get(url, params=params)

        # --- ADD THIS DEBUG PRINT RIGHT BEFORE raise_for_status() ---
        print("DEBUG: Status Code =", r.status_code)
        print("DEBUG: Response text =", r.text)

        # This line throws a Python error if the HTTP status is 4xx or 5xx
        r.raise_for_status()

        return r.json()

if __name__ == "__main__":
    fb = FbAgent()
    print("DEBUG - Token:", fb.access_token)
    print("DEBUG - Ad account:", fb.ad_account_id)

    response = fb.get_campaigns()
    print("Your campaigns:", response)
