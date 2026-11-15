"""Each endpoint triggers one thing the profiler detects. Reports go to the
`web` container's logs."""
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from elasticsearch import Elasticsearch, helpers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django_perf_profiler.es_spy.es_spy import es_spy
from django_perf_profiler.sql_spy.sql_spy import sql_spy
from django_perf_profiler.trace_capture import BasicStrategy, capture

from .models import Book
from .serializers import BookSerializer, BustedPrefetchSerializer

INDEX = "books"
KW_INDEX = "books_kw"  # same `year` field, mapped as keyword (see seed)
AUTHORS = ["Author 1", "Author 2", "Author 3", "Author 4"]


def _es():
    return Elasticsearch(settings.ES_URL)


@api_view(["GET"])
def books_n_plus_one(request):
    with sql_spy("books: N+1"):
        data = BookSerializer(Book.objects.all()[:20], many=True).data
    return Response(data)


@api_view(["GET"])
def books_busted_prefetch(request):
    qs = Book.objects.select_related("author").prefetch_related("tags")[:20]
    with sql_spy("books: busted prefetch"):
        data = BustedPrefetchSerializer(qs, many=True).data
    return Response(data)


@api_view(["GET"])
def books_fixed(request):
    qs = Book.objects.select_related("author").prefetch_related("tags")[:20]
    with sql_spy("books: fixed"):
        data = BookSerializer(qs, many=True).data
    return Response(data)


@api_view(["GET"])
def books_traced(request):
    # Call tracer + SQL spy together: per-function SQL vs. Python time.
    with capture("books: traced", [BasicStrategy("/tmp/books_traced.txt")]):
        data = BookSerializer(Book.objects.all()[:20], many=True).data
    return Response(data)


def _recent_titles():
    return [b.title for b in Book.objects.all()[:5]]


def _author_hits(es, name):
    r = es.search(index=INDEX, query={"match": {"author": name}}, size=5)
    return r["hits"]["total"]["value"]


@api_view(["GET"])
def search_loop(request):
    es = _es()
    with es_spy("search: one query per author"):
        hits = {name: _author_hits(es, name) for name in AUTHORS}
    return Response(hits)


@api_view(["GET"])
def search_traced(request):
    # Call tracer + ES spy (+ SQL spy): per-function ES vs. SQL vs. Python time.
    es = _es()
    with capture("search: traced", [BasicStrategy("/tmp/search_traced.txt")]):
        hits = {name: _author_hits(es, name) for name in AUTHORS}
        titles = _recent_titles()
    return Response({"hits": hits, "titles": titles})


@api_view(["GET"])
def search_parallel(request):
    # The spy follows each search into the pool's worker threads.
    es = _es()
    with es_spy("search: thread pool"):
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(lambda n: _author_hits(es, n), AUTHORS))
    return Response(dict(zip(AUTHORS, counts)))


@api_view(["GET"])
def search_partials(request):
    # Three ways a search comes back HTTP 200 with something missing.
    es = _es()
    with es_spy("search: 200s that hide failures"):
        # `year` is an integer in books, a keyword in books_kw: the term query
        # fails on books' shard only -> 200 with _shards.failed = 1.
        es.search(index=f"{INDEX},{KW_INDEX}", query={"term": {"year": "not-a-year"}})
        # msearch: the second sub-search targets a missing index.
        es.msearch(searches=[{"index": INDEX}, {"query": {"match_all": {}}},
                             {"index": "no-such-index"}, {"query": {"match_all": {}}}])
        # mget: the second doc lives in a missing index.
        es.mget(docs=[{"_index": INDEX, "_id": "1"}, {"_index": "no-such-index", "_id": "1"}])
    return Response({"ok": True})


@api_view(["POST", "GET"])
def reindex_with_bad_doc(request):
    es = _es()
    ops = []
    for b in Book.objects.select_related("author")[:10]:
        ops += [{"index": {"_index": INDEX, "_id": b.id}},
                {"title": b.title, "year": b.year, "author": b.author.name}]
    # `year` is mapped as an integer: this doc fails, yet the bulk returns HTTP 200.
    ops += [{"index": {"_index": INDEX, "_id": "bad"}},
            {"title": "Broken", "year": "not-a-year", "author": "Nobody"}]
    with es_spy("reindex: bulk with one bad doc"):
        r = es.bulk(operations=ops, refresh=True)
    return Response({"errors": r["errors"], "items": len(r["items"])})


@api_view(["POST", "GET"])
def reindex_parallel(request):
    # helpers.parallel_bulk runs chunks on multiprocessing's ThreadPool (not
    # concurrent.futures): 10 docs / chunk_size 5 -> 2 bulk ops, both recorded.
    es = _es()
    actions = [
        {"_index": INDEX, "_id": b.id, "title": b.title, "year": b.year, "author": b.author.name}
        for b in Book.objects.select_related("author")[:10]
    ]
    with es_spy("reindex: parallel_bulk"):
        results = list(helpers.parallel_bulk(es, actions, thread_count=2, chunk_size=5))
    return Response({"indexed": sum(ok for ok, _ in results)})
