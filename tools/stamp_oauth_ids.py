"""Used by the release build: fills multistream/oauth_ids.py from the
repository's Actions secrets (TWITCH_CLIENT_ID, GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET, FACEBOOK_APP_ID, FACEBOOK_CLIENT_TOKEN), so they're in the app without being in the code.
A missing secret leaves that platform's "Log in" button hidden."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "multistream", "oauth_ids.py")

with open(PATH, encoding="utf-8") as f:
    text = f.read()
for name in ("TWITCH_CLIENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "FACEBOOK_APP_ID", "FACEBOOK_CLIENT_TOKEN"):
    value = os.environ.get(name, "").strip()
    print(f"{name}: {'set' if value else 'not set'}")
    if value:
        text = re.sub(rf'^{name} = ".*"$', f"{name} = {value!r}".replace("'", '"'), text, flags=re.M)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(text)
