"""Explicit headline and article body, with no inferred or truncated title."""
from .providers import ProviderError

TITLE_POLICY = (
    'Обязательный формат готовой публикации: первая строка — отдельный заголовок, '
    'затем пустая строка и основной текст. Заголовок до 140 символов, без Markdown, '
    'кавычек и подписи «Заголовок». Он должен ясно называть тему и естественно '
    'содержать подходящие поисковые слова, привлекать внимание умеренной интригой. '
    'Без обмана, неподтверждённых обещаний, сенсаций, преувеличений и набивки ключевыми словами. '
    'Заголовок должен соответствовать содержанию и манере проекта. '
    'При изменении текста также верни заголовок и основной текст в этом формате. '
    'Запрошенный объём относится ко всей публикации вместе с заголовком.'
)


def split_publication(text, title=None):
    first, separator, content = text.strip().replace('\r\n', '\n').partition('\n\n')
    if (not separator or not content.strip() or not first.strip() or '\n' in first
            or len(first.strip()) > 140):
        raise ProviderError('В тексте нет отдельного заголовка: нужна первая строка до 140 символов, '
                            'пустая строка и основной текст. Исправьте текст перед публикацией.')
    first = first.strip()
    if title is not None and (not title or first != title):
        raise ProviderError('Для Дзена не сохранён отдельный заголовок. '
                            'Нажмите «Изменить текст» и попросите добавить заголовок.')
    return first, content.strip()
