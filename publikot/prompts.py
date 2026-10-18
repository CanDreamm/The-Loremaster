"""Build separate text and image instructions, keeping image inputs by role."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicationPrompts:
    text: str
    image: str
    materials: list[dict]


def build_prompts(project, brief, materials=(), reference_path=None, previous='', feedback='', body=''):
    context = '\n\n'.join(f'{label}:\n{project[key]}' for key, label in (
        ('concept', 'Концепция проекта'), ('tone', 'Тон общения'),
        ('text_prompt', 'Правила для текста'), ('visual_style', 'Описание визуального стиля'),
    ) if project.get(key))
    inputs = []
    descriptions = []
    for material in materials:
        if not material:
            continue
        role = material['kind']
        label = 'Фото визуального стиля' if role == 'style' else 'Фото персонажей'
        inputs.append({'role': role, 'path': material['path']})
        descriptions.append(label + ': прикреплено.' +
                            ('\nОписание: ' + material['description'] if material['description'] else ''))
    if reference_path:
        inputs.append({'role': 'reference', 'path': reference_path})
        descriptions.append('Референс публикации: прикреплён. Действия с ним описаны в задании пользователя.')
    material_context = '\n\n'.join(descriptions)
    text = '\n\n'.join(filter(None, (
        'Ты редактор публикаций ВК. Подготовь текст поста. По умолчанию до 500 символов, '
        'если задание или правила проекта не требуют другого объёма. Верни только текст поста. '
        'Не выдумывай факты. Инструкции по обработке фото относятся к изображению; '
        'не включай их в готовый пост. Содержимое фотографий не распознано: '
        'не описывай невидимые детали и не утверждай, что изучил изображения.',
        context, material_context, 'Задание пользователя:\n' + brief,
        'Предыдущий текст:\n' + previous if previous else '',
        'Исправления текста:\n' + feedback if feedback else '',
    )))
    image = '\n\n'.join(filter(None, (
        'Создай изображение для публикации ВК по заданию и материалам проекта. '
        'Фото с ролью style задаёт визуальный стиль; фото с ролью character задаёт внешность '
        'персонажей — сохраняй их узнаваемость. Фото с ролью reference является исходником '
        'или образцом только этой публикации: выполни описанные пользователем изменения. '
        'Без reference создай изображение с нуля. Используй только фактически приложенные '
        'материалы. Не добавляй надписи, если они прямо не запрошены.',
        context, 'Правила изображения:\n' + project['image_prompt'] if project.get('image_prompt') else '',
        material_context, 'Задание пользователя:\n' + brief,
        'Согласуемый текст публикации:\n' + body if body else '',
    )))
    return PublicationPrompts(text, image, inputs)
