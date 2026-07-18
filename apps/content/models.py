from __future__ import annotations

from django.db import models


class GalleryImage(models.Model):
    # ПОЧЕМУ: медиа хранится в S3, витрина получает готовый CDN-URL
    image_url = models.URLField("Изображение (URL)", max_length=500)
    order = models.IntegerField("Порядок", default=0, db_index=True)
    is_published = models.BooleanField("Опубликовано", default=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        db_table = "gallery_image"
        verbose_name = "Фото галереи"
        verbose_name_plural = "Фото галереи"
        ordering = ("order", "id")

    def __str__(self) -> str:
        return f"Фото #{self.pk} (порядок {self.order})"
