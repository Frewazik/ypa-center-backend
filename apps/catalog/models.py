from __future__ import annotations

from django.db import models


class Activity(models.Model):
    name = models.CharField("Название", max_length=255)
    slug = models.SlugField("Слуг", unique=True)
    category = models.CharField("Категория", max_length=50, default="CLUB")
    price = models.IntegerField("Цена", default=0)
    is_active = models.BooleanField("Активен", default=True)

    # ПОЧЕМУ: медиа хранится в S3, витрина получает готовый CDN-URL.
    # ImageField потребовал бы Pillow — новую зависимость
    cover_image = models.URLField("Обложка (URL)", max_length=500, blank=True)
    short_description = models.CharField("Краткое описание", max_length=255, blank=True)
    description = models.TextField("Описание", blank=True)
    features = models.JSONField("Особенности", default=list, blank=True)
    tags = models.JSONField("Теги", default=list, blank=True)

    class Meta:
        db_table = "activity"

    def __str__(self) -> str:
        return self.name
