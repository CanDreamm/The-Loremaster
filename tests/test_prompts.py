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
        self.assertTrue(prompt.endswith('Манеру речи и рассказчика бери из настроек проекта.'))
        self.assertEqual(prompt.count('до 500 символов'), 1)  # Only the user's saved project rule.
        self.assertIn('Если в замечаниях объём не изменён', prompt)

    def test_default_remains_for_a_short_task_without_length(self):
        self.assertIn('до 500 символов', build_prompts(self.project, 'Кот открыл кофейню').text)

