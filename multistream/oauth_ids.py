"""The apps registered with each platform for logging in (see accounts.py).
Empty: that platform's "Log in" button is hidden and the key is pasted.

The release build fills these in from the repository's Actions secrets
(Settings > Secrets and variables > Actions: TWITCH_CLIENT_ID,
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET; see tools/stamp_oauth_ids.py), so
they stay out of the code. Running from source, set them here locally.

    Twitch   dev.twitch.tv > Your Console > Applications > Register: category
             "Broadcaster Suite", OAuth redirect http://localhost, client type
             Public. Its Client ID goes here.
    YouTube  console.cloud.google.com: a project with the "YouTube Data API v3"
             enabled, an OAuth consent screen (External, with your Google
             account as a test user) and an OAuth client of type "Desktop
             app". Its Client ID and secret go here (Google treats a desktop
             app's secret as public).
"""

TWITCH_CLIENT_ID = ""
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
