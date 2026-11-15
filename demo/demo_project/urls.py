from django.urls import path

from shop import views

urlpatterns = [
    path("books/", views.books_n_plus_one),
    path("books/busted-prefetch/", views.books_busted_prefetch),
    path("books/fixed/", views.books_fixed),
    path("books/traced/", views.books_traced),
    path("search/", views.search_loop),
    path("search/traced/", views.search_traced),
    path("search/parallel/", views.search_parallel),
    path("search/partials/", views.search_partials),
    path("reindex/", views.reindex_with_bad_doc),
    path("reindex/parallel/", views.reindex_parallel),
]
