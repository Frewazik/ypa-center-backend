from __future__ import annotations

import factory
from factory.django import DjangoModelFactory

from apps.content.models import GalleryImage


class GalleryImageFactory(DjangoModelFactory):
    class Meta:
        model = GalleryImage

    image_url = factory.Sequence(lambda n: f"https://cdn.example.com/gallery/{n}.jpg")
    order = factory.Sequence(lambda n: n)
    is_published = True
