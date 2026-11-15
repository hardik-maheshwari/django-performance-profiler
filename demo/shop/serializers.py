from rest_framework import serializers

from .models import Book


class BookSerializer(serializers.ModelSerializer):
    # Both fields walk a relation per row: the N+1s the profiler should catch.
    author_name = serializers.SerializerMethodField()
    tag_labels = serializers.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "title", "year", "author_name", "tag_labels"]

    def get_author_name(self, book):
        return book.author.name  # forward FK -> missing select_related

    def get_tag_labels(self, book):
        return [t.label for t in book.tags.all()]  # m2m -> never prefetched


class BustedPrefetchSerializer(BookSerializer):
    def get_tag_labels(self, book):
        # .filter() builds a new queryset and bypasses the prefetch cache.
        return [t.label for t in book.tags.filter(label__isnull=False)]
