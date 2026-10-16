"""Static client identity contract; no GitHub, HA or App Store requests."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag, attributes):
        self.tags.append(tag)
        values = dict(attributes)
        if tag == "link":
            self.links.append(values)


class OAuthClientPageTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[3] / "docs"
        self.raw = (self.directory / "index.html").read_bytes()
        self.page = PageParser()
        self.page.feed(self.raw.decode("utf-8"))

    def test_callback_is_declared_in_home_assistant_scan_window(self):
        scanned = PageParser()
        scanned.feed(self.raw[:10000].decode("utf-8"))
        callbacks = [link["href"] for link in scanned.links if link.get("rel") == "redirect_uri"]
        self.assertEqual(callbacks, ["halthy-ha://auth/callback"])

    def test_canonical_client_id_uses_repository_pages_path(self):
        self.assertIn({"rel": "canonical", "href": "https://mosher23.github.io/Halthy/"}, self.page.links)

    def test_page_does_not_receive_or_process_credentials(self):
        self.assertNotIn("form", self.page.tags)
        self.assertNotIn("input", self.page.tags)
        self.assertNotIn("script", self.page.tags)
        self.assertIn("form-action 'none'", self.raw.decode("utf-8"))

    def test_pages_source_is_static_and_identifies_development_status(self):
        self.assertTrue((self.directory / ".nojekyll").exists())
        self.assertIn("currently released app uses manual-token setup", self.raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
