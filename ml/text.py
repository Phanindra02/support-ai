"""Text cleaning shared by training (ml/prepare.py) and the model service (ml/service.py).

One function for both, so a message is cleaned the same way when it's predicted as when the
model learned. Cleaned differently, the model would see words it was never trained on.
"""

import html
import re

URL = re.compile(r"https?://\S+")
# Tweets start with the airline's handle. Left in, a model could learn which airline people
# complain about instead of the words that show a mood.
HANDLE = re.compile(r"@\w+")
SPACES = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """Lowercase text without links, @handles, HTML escapes like &amp;, or runs of spaces."""
    text = html.unescape(text)
    text = URL.sub(" ", text)
    text = HANDLE.sub(" ", text)
    return SPACES.sub(" ", text).strip().lower()
