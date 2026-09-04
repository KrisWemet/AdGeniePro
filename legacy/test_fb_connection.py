# test_fb_connection.py

import os
import requests
from dotenv import load_dotenv

# 1) Load environment variables from your .env file
load_dotenv()

# 2) Read the token from .env
access_token = os.getenv("FB_ACCESS_TOKEN")
if not access_token:
    raise ValueError("FB_ACCESS_TOKEN is not set in .env")

# 3) Make a simple GET request to list your ad accounts
url = "https://graph.facebook.com/v16.0/me/adaccounts"
params = {
    "access_token": access_token,
    "fields": "id,name"  # Optional: Specify fields to reduce response size
}

try:
    response = requests.get(url, params=params)
    response.raise_for_status()  # Raises an HTTPError for bad responses

    # 4) Print out the results
    print("Status Code:", response.status_code)
    print("Response Text:", response.text)  # The raw string
    print()

    # Optionally parse JSON
    data = response.json()
    print("Response JSON parsed:", data)

except requests.exceptions.HTTPError as err:
    print(f"HTTP error occurred: {err}")
    print(f"Response content: {response.content}")
except Exception as e:
    print("An error occurred:", e)