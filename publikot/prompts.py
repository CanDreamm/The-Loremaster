"""Build separate text and image instructions, keeping image inputs by role."""
from dataclasses import dataclass


TEXT_TEMPLATE = """Создай публикацию на основе моего сообщения и приложенных материалов.

Учитывай весь контекст проекта: концепцию, тон общения, правила текста, визуальный стиль и персонажей, а также дополнительные референсы.

Представь, какой человек мог бы стоять за этим проектом: его характер, мышление, вкус, чувство юмора и манеру общения. Пиши так, будто именно этот человек решил поделиться моей идеей.

Не пересказывай задание буквально. Сам найди интересную подачу, соблюдай правила проекта и не придумывай неподтверждённых фактов. Текст должен быть живым, естественным, без шаблонов и ощущения, что его написал ИИ.

Выдай только готовую публикацию."""

IMAGE_TEMPLATE = """Создай изображение на основе моего сообщения и приложенных материалов.

Учитывай весь контекст проекта: концепцию, тон общения, правила текста, визуальный стиль и персонажей, а также дополнительные референсы.

Представь, как человек с характером, вкусом и мировоззрением этого проекта придумал бы и создал такую картинку.

Сам выбирай сцену, композицию, атмосферу и уместных персонажей. Если приложено фото предмета, сохрани его узнаваемые особенности. Если приложены референсы, учитывай их содержание, детали и настроение, адаптируя к стилю проекта.

Все элементы должны выглядеть естественно и принадлежать одному миру. Не перегружай изображение деталями и не добавляй лишнего. При противоречиях ориентируйся на правила проекта и моё текущее задание.

Сразу создавай готовое изображение."""


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
        descriptions.append(label + ': материал проекта.' +
                            ('\nОписание: ' + material['description'] if material['description'] else ''))
    if reference_path:
        inputs.append({'role': 'reference', 'path': reference_path})
        descriptions.append('Дополнительный референс: фото из текущего задания. '
                            'Что с ним сделать, указано в сообщении пользователя.')
    material_context = '\n\n'.join(descriptions)
    text = '\n\n'.join(filter(None, (
        project['text_template'], context, material_context, 'Задание пользователя:\n' + brief,
        'Предыдущий текст:\n' + previous if previous else '',
        'Исправления текста:\n' + feedback if feedback else '',
    )))
    image = '\n\n'.join(filter(None, (
        project['image_template'], context,
        'Правила изображения:\n' + project['image_prompt'] if project.get('image_prompt') else '',
        material_context, 'Задание пользователя:\n' + brief,
        'Согласуемый текст публикации:\n' + body if body else '',
    )))
    return PublicationPrompts(text, image, inputs)
