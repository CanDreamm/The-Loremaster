"""Encode a separate JPEG for VK message previews; keep publication originals intact."""
from io import BytesIO
from pathlib import Path
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .providers import ProviderError


def preview_jpeg(path):
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > 10 * 1024 * 1024:
            raise ProviderError('Фото предпросмотра отсутствует или превышает 10 МБ.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(file) as source:
                if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 40_000_000:
                    raise ProviderError('Фото предпросмотра должно быть JPEG, PNG или WebP размером до 40 мегапикселей.')
                source.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                image = ImageOps.exif_transpose(source)
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    flattened = Image.new('RGB', rgba.size, 'white')
                    flattened.paste(rgba, mask=rgba.getchannel('A'))
                    image = flattened
                else:
                    image = image.convert('RGB')
                output = BytesIO()
                image.save(output, format='JPEG', quality=92)
                return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ProviderError('Не удалось прочитать файл изображения для предпросмотра ВК. '
                            'Оригинал сохранён; повторная генерация ИИ не нужна.') from None
