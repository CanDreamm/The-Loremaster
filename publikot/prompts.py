"""Build separate text and image instructions, keeping image inputs by role."""
from dataclasses import dataclass
import re


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


def text_volume_policy(project, brief, feedback):
    # Do not add a competing numeric default when length/long form is requested.
    sources = (feedback, brief, project.get('text_prompt', ''), project.get('text_template', ''),
               project.get('concept', ''))
    explicit = any(re.search(
        r'\d[\d\s.,\-–—]*(?:(?:тыс(?:яч[аиу]?)?\.?|[кk])\s*)?'
        r'(?:символ|знак|слов|абзац|предложени|строк)|стать[яюи]|разв[её]рнут|подробн|объ[её]м|длиннее',
        value, re.I) for value in sources)
    return (
        'Приоритет требований к тексту: последние замечания пользователя, затем текущее задание, '
        'затем правила и шаблон проекта. Указанный в задании объём относится к готовой публикации. '
        'Если в замечаниях объём не изменён, сохрани объём из исходного задания. '
        'Не подменяй запрошенный объём более коротким анонсом или резюме. '
        'Число символов считай вместе с пробелами, если пользователь не указал иначе. '
        'Если указано конкретное число символов, стремись к этому объёму; '
        'ограничение сверху применяй только при словах «до», «не более» или аналогичных. '
        'Перед ответом проверь соответствие запрошенному объёму. '
        + ('' if explicit else 'Если объём нигде не задан, напиши короткий пост до 500 символов. ')
        + 'Манеру речи, рассказчика и оформление бери из концепции проекта.')


def build_prompts(project, brief, materials=(), reference_path=None, previous='', feedback='', body=''):
    context = '\n\n'.join(f'{label}:\n{project[key]}' for key, label in (
        ('concept', 'Концепция проекта'), ('text_prompt', 'Правила для текста'),
    ) if project.get(key))
    inputs = []
    descriptions = []
    for material in materials:
        if not material or material['kind'] == 'style':
            continue
        role = material['kind']
        label = 'Фото персонажей'
        inputs.append({'role': role, 'path': material['path']})
        descriptions.append(label + ': материал проекта.' +
                            ('\nОписание: ' + material['description'] if material['description'] else ''))
    if reference_path:
        inputs.append({'role': 'reference', 'path': reference_path})
        descriptions.append('Дополнительный референс: фото из текущего задания. '
                            'Что с ним сделать, указано в сообщении пользователя.')
    material_context = '\n\n'.join(descriptions)
    text = '\n\n'.join(filter(None, (
        project['text_template'],
        context, material_context, 'Задание пользователя:\n' + brief,
        'Предыдущий текст:\n' + previous if previous else '',
        'Исправления текста:\n' + feedback if feedback else '',
        text_volume_policy(project, brief, feedback),
    )))
    image = '\n\n'.join(filter(None, (
        project['image_template'], context,
        'Правила изображения:\n' + project['image_prompt'] if project.get('image_prompt') else '',
        material_context, 'Задание пользователя:\n' + brief,
        'Согласуемый текст публикации:\n' + body if body else '',
    )))
    return PublicationPrompts(text, image, inputs)
