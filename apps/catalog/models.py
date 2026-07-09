from django.db import models


class Activity(models.Model):
    name = models.CharField("Название", max_length=255)
    slug = models.SlugField("Слуг", unique=True)
    category = models.CharField("Категория", max_length=50, default="CLUB")
    price = models.IntegerField("Цена", default=0)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        db_table = "activity"
