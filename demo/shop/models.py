from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)


class Tag(models.Model):
    label = models.CharField(max_length=50)


class Book(models.Model):
    title = models.CharField(max_length=200)
    year = models.IntegerField()
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    tags = models.ManyToManyField(Tag, related_name="books")
