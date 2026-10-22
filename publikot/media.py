"""Ordered photos, including older single-photo drafts."""
import json
from pathlib import Path

from .providers import ProviderError


def paths(value):
    if value is None or value == '':
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    result = list(value)
    if len(result) > 3 or any(not isinstance(p, str) or not p for p in result):
        raise ProviderError('Публикация может содержать не более трёх фотографий.')
    return result


def version_images(version):
    values = dict(version)
    return paths(json.loads(values.get('image_paths') or '[]') or values.get('image_path'))


def publication_images(version):
    images = version_images(version)
    return images if len(images) > 1 else images[0] if images else None
