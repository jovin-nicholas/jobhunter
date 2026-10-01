"""Example plugin: a board that reads a JSON feed. Add `example_feed: {url: ...}` under `boards:` to use it.

A board is one class with a name, an optional Options dataclass (checked at startup), and `search(ctx)` yielding
Job records. `ctx.http` is a rate-limited HTTP client; `ctx.queries`, `ctx.locations` and `ctx.max_age_hours` come
from the settings file.
"""
from dataclasses import dataclass

from jobhunter import Job, board


@board("example_feed")
class ExampleFeed:
    @dataclass
    class Options:
        url: str

    def __init__(self, options):
        self.options = self.Options(**options)

    def search(self, ctx):
        for item in ctx.http.get_json(self.options.url):
            yield Job(id=f"example_feed_{item['id']}", title=item["title"], company=item["company"],
                      location=item.get("location", ""), url=item["url"],
                      description=item.get("description", ""), posted_at=item.get("posted_at", ""))
