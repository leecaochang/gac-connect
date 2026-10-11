import ast
from html.parser import HTMLParser
from pathlib import Path


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_captcha_controls_have_names_help_and_live_status():
    source = Path(__file__).resolve().parents[1] / "src/gac_connect/cli.py"
    tree = ast.parse(source.read_text())
    html = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_PAGE" for target in node.targets))
    page = Page(html)
    tags = {attrs["id"]: attrs for _, attrs in page.tags if "id" in attrs}
    assert any(tag == "html" and attrs.get("lang") == "en" for tag, attrs in page.tags)
    assert any(tag == "label" and attrs.get("for") == "s" for tag, attrs in page.tags)
    slider = tags["s"]
    assert slider["type"] == "range" and slider["step"] == "1"
    assert slider["aria-describedby"] in tags
    assert tags["m"]["role"] == "status"
    assert tags["m"]["aria-live"] == "polite" and tags["m"]["aria-atomic"] == "true"
    assert all("alt" in attrs for tag, attrs in page.tags if tag == "img")
    assert "document.onkeydown" not in html  # native range arrows must not fire twice
