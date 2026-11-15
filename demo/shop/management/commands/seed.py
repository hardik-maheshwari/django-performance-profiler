from django.conf import settings
from django.core.management.base import BaseCommand
from elasticsearch import Elasticsearch

from shop.models import Author, Book, Tag


class Command(BaseCommand):
    help = "Create demo rows and the Elasticsearch index (idempotent)."

    def handle(self, *args, **opts):
        if not Book.objects.exists():
            tags = [Tag.objects.create(label=l) for l in ("db", "search", "python", "perf")]
            for a in range(1, 6):
                author = Author.objects.create(name=f"Author {a}")
                for b in range(1, 5):
                    book = Book.objects.create(title=f"Book {a}.{b}", year=2000 + a + b, author=author)
                    book.tags.set(tags[: (a + b) % 4 + 1])
        es = Elasticsearch(settings.ES_URL)
        if not es.indices.exists(index="books"):
            es.indices.create(index="books", mappings={"properties": {
                "title": {"type": "text"}, "author": {"type": "text"}, "year": {"type": "integer"}}})
            for b in Book.objects.select_related("author"):
                es.index(index="books", id=b.id, document={"title": b.title, "year": b.year, "author": b.author.name})
            es.indices.refresh(index="books")
        if not es.indices.exists(index="books_kw"):
            # Same `year` field as `books`, mapped as keyword: lets /search/partials/
            # produce a real shard failure (the query is valid here, not there).
            es.indices.create(index="books_kw", mappings={"properties": {"year": {"type": "keyword"}}})
            es.index(index="books_kw", id=1, document={"year": "not-a-year"}, refresh=True)
        self.stdout.write(f"seeded: {Book.objects.count()} books")
