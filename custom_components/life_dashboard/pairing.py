"""The pairing URL a QR code carries.

The QR is an ordinary https URL, because that is the one thing a phone camera knows
what to do with. Everything the phone needs is in the fragment, after the #, which
a browser never sends to any server (RFC 3986, section 3.5). On a phone with the app
installed, an Android App Link opens the app straight from the camera; without the
app, the page at PAIR_PAGE explains where to get it. An iPhone's camera always opens
that page; the iOS app takes the fragment from the page's lifedashboard:// link, not
from its Android intent:// link.

The format is shared with the app, which parses it, and with any receiver that wants
to be pairable the same way. Keep the two sides' test vectors identical.

This module imports nothing from Home Assistant.
"""

from __future__ import annotations

from html import escape
from typing import Final
from urllib.parse import quote, urlencode

PAIR_PAGE: Final = "https://owen282000.github.io/life-dashboard-companion-app/pair"
PAIR_VERSION: Final = "1"
DEFAULT_NAME: Final = "Home Assistant"

# This integration accepts both sections, which is also what the app assumes when a code
# does not say. The field is therefore left out of the code: it is the default spelled
# out, and every character costs modules a phone has to resolve through camera blur.


def pairing_url(webhook_url: str, secret: str, *, name: str = DEFAULT_NAME) -> str:
    """Build the URL the QR carries.

    Every value is fully percent-encoded (nothing left "safe"), so the app can split
    the fragment on & and = before decoding and never has to guess whether a / or a
    , inside a value was meant literally.
    """
    fragment = urlencode(
        {
            "v": PAIR_VERSION,
            "url": webhook_url,
            "secret": secret,
            "name": name,
        },
        quote_via=quote,
        safe="",
    )
    return f"{PAIR_PAGE}#{fragment}"


def qr_markup(pair_url: str, *, scale: int = 5) -> str:
    """The QR as the frontend renders it inside a dialog description.

    hassfest refuses HTML inside strings.json, so the element travels as a
    description placeholder instead, the way core's TOTP setup ships its <svg>. The
    frontend's markdown sanitizer whitelists ha-qr-code with exactly these
    attributes and draws it on a canvas in the theme's colours, so it stays
    scannable in dark mode. A frontend that does not know the element simply shows
    nothing there, and the URL and secret below it still work.
    """
    return (
        f'<ha-qr-code data="{pair_url}" scale="{scale}" error-correction-level="medium">'
        "</ha-qr-code>"
    )


def by_hand_markup(webhook_url: str, secret: str) -> str:
    """The URL and the secret behind a fold, for whoever cannot scan.

    HTML rather than markdown because it goes in through a placeholder, like the QR:
    hassfest allows no markup in strings.json, and Home Assistant's markdown renderer
    only takes details and summary as raw HTML.
    """
    return (
        "<details><summary><b>Or paste by hand</b></summary>"
        "<p>In the app, paste both into the webhook settings on the Health tab, and on "
        "Android on the Screen Time tab as well.</p>"
        f"<p>URL</p><pre><code>{escape(webhook_url)}</code></pre>"
        f"<p>Signing secret</p><pre><code>{escape(secret)}</code></pre>"
        "</details>"
    )


def layout_markup(pair_url: str, *, note: str = "") -> str:
    """The code on the left in its own frame, the three steps flowing beside it.

    A one-cell table floated left: the only way Home Assistant's markdown allows text
    next to an element, and its own table styling turns the cell into a frame around
    the code. A two-cell table would draw a grid with an empty column under the short
    steps. HTML rather than markdown because it goes in through a placeholder, and the
    step texts live here for that reason.
    """
    return (
        '<table align="left"><tr><td>' + qr_markup(pair_url, scale=4) + "</td></tr></table>"
        "<p><b>Scan with your phone</b></p>"
        "<p>1. Tap <b>Scan a pairing code</b> in the app, or point the phone's camera at "
        "the code.</p>"
        "<p>2. Check what it fills in and tap <b>Pair</b>.</p>"
        "<p>3. Tap <b>Sync Now</b>.</p>" + (f"<p>{note}</p>" if note else "")
    )
