import unittest

from publikot.prompts import build_prompts, TEXT_TEMPLATE, IMAGE_TEMPLATE


class TextVolumeTests(unittest.TestCase):
    def setUp(self):
        self.project = {'text_template': TEXT_TEMPLATE, 'image_template': IMAGE_TEMPLATE}

    def test_explicit_task_length_has_no_competing_default(self):
        for length in ('1500 символов', '1 500 знаков', '1,5 тыс. символов',
                       '1500–2000 символов', '300 слов', '5 абзацев'):
            with self.subTest(length=length):
                prompt = build_prompts(self.project, 'Напиши историю на ' + length).text
                self.assertIn(length, prompt)
                self.assertNotIn('500 символов.', prompt)
                self.assertIn('последние замечания пользователя, затем текущее задание', prompt)

    def test_project_length_and_long_form_suppress_the_numeric_default(self):
        for field in ('text_prompt', 'text_template', 'concept'):
            project = {**self.project, field: 'Пиши истории объёмом 1500 знаков.'}
            self.assertNotIn('до 500 символов', build_prompts(project, 'История про кота').text)
        self.assertNotIn('до 500 символов', build_prompts(self.project, 'Напиши подробную статью').text)

    def test_feedback_overrides_project_length_and_keeps_original_task_visible(self):
        project = {**self.project, 'text_prompt': 'Пиши до 500 символов.'}
        prompt = build_prompts(project, 'Напиши 1500 символов', previous='Старый текст',
                               feedback='Увеличь до 2000 символов').text
        self.assertIn('Увеличь до 2000 символов', prompt)
        self.assertIn('Напиши 1500 символов', prompt)
        self.assertTrue(prompt.endswith('Манеру речи, рассказчика и оформление бери из концепции проекта.'))
        self.assertEqual(prompt.count('до 500 символов'), 1)  # Only the user's saved project rule.
        self.assertIn('Если в замечаниях объём не изменён', prompt)

    def test_default_remains_for_a_short_task_without_length(self):
        self.assertIn('до 500 символов', build_prompts(self.project, 'Кот открыл кофейню').text)

    def test_concept_is_used_and_removed_fields_and_style_reference_are_ignored(self):
        project = {**self.project, 'concept': 'Разговорная речь. Акварель.',
                   'tone': 'REMOVED-TONE', 'visual_style': 'REMOVED-STYLE'}
        prompts = build_prompts(project, 'История', materials=[
            {'kind': 'style', 'path': '/old-style.png', 'description': 'REMOVED-CAPTION'},
            {'kind': 'character', 'path': '/cat.png', 'description': 'Рыжий кот'}])
        for prompt in (prompts.text, prompts.image):
            self.assertIn('Разговорная речь. Акварель.', prompt)
            self.assertIn('Рыжий кот', prompt)
            self.assertNotIn('REMOVED-', prompt)
        self.assertEqual(prompts.materials, [{'role': 'character', 'path': '/cat.png'}])

    def test_image_prompt_does_not_depend_on_generated_publication_or_text_corrections(self):
        project = {**self.project, 'concept': 'Концепция персонажа', 'text_prompt': 'TEXT_ONLY_RULE'}
        prompts = build_prompts(project, 'Исходное задание', reference_path='/task-photo.png',
            previous='PREVIOUS_GENERATED_TEXT', feedback='TEXT_CORRECTION', body='NEW_GENERATED_TEXT')
        self.assertNotIn('Исходное задание', prompts.image)
        self.assertIn('Концепция персонажа', prompts.image)
        for excluded in ('TEXT_ONLY_RULE', 'PREVIOUS_GENERATED_TEXT', 'TEXT_CORRECTION',
                         'NEW_GENERATED_TEXT', 'Дополнительный референс'):
            self.assertNotIn(excluded, prompts.image)
        self.assertEqual(prompts.image, build_prompts(project, 'Исходное задание', body='DIFFERENT_TEXT').image)

