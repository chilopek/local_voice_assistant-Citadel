"""Speech preview contract; no audio, model, requests or optional dependencies."""
import subprocess
import sys
import unittest

from assistant.speech import concise_speech


NOTICE = "Подробности — в чате."


class ConciseSpeechTests(unittest.TestCase):
    def test_empty(self):
        for text in ("", " ", "\n\t"):
            self.assertEqual(concise_speech(text), "")

    def test_short_success_and_russian_negation(self):
        for text in ("Готово. Файл сохранён.", "Не удалось открыть файл. Изменения не сохранены.",
                     "Saved 12 files. No files were deleted."):
            self.assertEqual(concise_speech(text), text)

    def test_long_research_keeps_first_three_complete_sentences(self):
        text = ("Исследование охватило 1200 участников. Средний результат вырос на 3,5 процента. "
                "Данные относятся к периоду с 2020 по 2024 год. "
                + "Авторы приводят отдельные результаты по возрастным группам. " * 30)
        self.assertEqual(concise_speech(text),
                         "Исследование охватило 1200 участников. Средний результат вырос на 3,5 процента. "
                         "Данные относятся к периоду с 2020 по 2024 год. " + NOTICE)

    def test_success_followed_by_failure_is_not_cherry_picked(self):
        text = "Готово. Архив создан. Скопировано 12 файлов. Но загрузка не удалась: ошибка 403."
        self.assertEqual(concise_speech(text),
                         "Архив создан. Скопировано 12 файлов. Но загрузка не удалась: ошибка 403. " + NOTICE)
        text = "Готово. Архив создан, но загрузка не удалась: ошибка 403. Повторите попытку позже."
        self.assertEqual(concise_speech(text), text)

    def test_later_conditions_do_not_veto_a_useful_prefix(self):
        prefix = "Этап завершён. Файл создан. Путь записан. "
        for qualifier in ("Только при температуре выше 20 градусов.",
                          "Без резервной копии продолжать нельзя.", "Если сервер доступен."):
            self.assertEqual(concise_speech(prefix + qualifier), prefix + NOTICE)

    def test_english_contractions_and_partial_errors_are_preserved(self):
        for failure in ("Upload didn't complete.", "Permission denied.", "Completed only 2 of 12 steps.",
                        "Пока выполнено лишь 2 из 12 этапов.", "Процесс завершился аварийно.",
                        "However, the result is not final.", "Но браузер не открылся.",
                        "Connection refused."):
            result = concise_speech("Done. Archive created. Log saved. " + failure)
            self.assertIn(failure, result)
            self.assertTrue(result.endswith(NOTICE))
            self.assertLessEqual(len(result), 420)

    def test_long_ssh_howto_speaks_intro_despite_later_negative_advice(self):
        intro = ("SSH — протокол для защищённого удалённого доступа к компьютеру. "
                 "Он шифрует соединение и позволяет выполнять команды на сервере. "
                 "Обычно сервер принимает подключения на порту 22. ")
        text = (intro + "\n## Подключение\n"
                "1. Узнай адрес сервера и имя пользователя.\n"
                "2. Открой терминал и введи команду подключения.\n"
                "```sh\nssh user@example.org\n```\n"
                "## Если соединение не устанавливается\n"
                "Если сервер недоступен, проверь сеть и настройки порта. "
                "Не отключай проверку ключа сервера без необходимости. "
                "Никому не передавай пароль и закрытый ключ. "
                "Для регулярной работы используй ключи вместо пароля. " * 2)
        self.assertEqual(concise_speech(text), intro + NOTICE)

    def test_research_explanation_is_not_an_action_report(self):
        intro = "SSH защищает соединение. Протокол использует шифрование. Стандартный порт — 22. "
        self.assertEqual(concise_speech(intro + "Ошибка подключения означает проблему с сетью."), intro + NOTICE)

    def test_failure_replaces_success_sentences_to_fit_budget(self):
        success = "Файлы успешно сохранены в архив " + "проекта " * 35 + "."
        failure = "Загрузка не удалась: ошибка 403, передано только 2 из 12 файлов."
        text = "Готово. " + success + " Журнал сохранён. " + failure
        result = concise_speech(text)
        self.assertIn(failure, result)
        self.assertTrue(result.endswith(NOTICE))
        self.assertLessEqual(len(result), 420)

    def test_overlong_success_can_be_dropped_for_short_failure(self):
        failure = "Но проверка не прошла: ошибка 5."
        text = "Создан архив " + "проекта " * 70 + ". " + failure
        self.assertEqual(concise_speech(text), failure + " " + NOTICE)

    def test_qualifiers_in_selected_sentence_or_bullet_are_preserved(self):
        for prefix in ("", "- "):
            item = "Установлено только 2 пакета из 12, остальные не установлены."
            self.assertEqual(concise_speech(prefix + item + "\n\n" + "Дальнейшее описание. " * 20),
                             item + " Дальнейшее описание. Дальнейшее описание. " + NOTICE)

    def test_hypothetical_errors_and_negated_failures_are_not_promoted(self):
        intro = "Готово. Архив создан. Журнал сохранён. "
        for advice in ("Если загрузка не удалась, проверьте пароль.",
                       "For example, upload failed means a network problem.",
                       "Ошибок нет. Проверка прошла без ошибок.",
                       "No errors occurred. Upload didn't fail."):
            self.assertEqual(concise_speech(intro + advice), intro + NOTICE)

    def test_headings_and_three_bullets(self):
        text = "# Результат\n\n- **Создано 12 файлов**\n- Проверка пройдена\n- Сервис перезапущен\n- Журнал сохранён"
        self.assertEqual(concise_speech(text),
                         "Создано 12 файлов. Проверка пройдена. Сервис перезапущен. " + NOTICE)

    def test_numbered_lists_and_indented_continuation(self):
        text = "1. Сохранено 3.14 МБ\n   из 12 МБ, остальные не изменены.\n2) Проверено 2 файла."
        self.assertEqual(concise_speech(text),
                         "Сохранено 3.14 МБ из 12 МБ, остальные не изменены. Проверено 2 файла.")

    def test_bullet_is_atomic(self):
        text = "- Готово. " + "Скопированы файлы в папку архива. " * 15 + "Но проверка не прошла."
        self.assertEqual(concise_speech(text), NOTICE)

    def test_code_only_and_unclosed_fences(self):
        for text in ("```python\nprint('hello')\n```", "~~~sh\nrm example\n~~~",
                     "```python\nprint('hello')", "    print('hello')"):
            self.assertEqual(concise_speech(text), "Код — в чате.")

    def test_mainly_code(self):
        text = "Вот пример:\n```python\n" + "print('example')\n" * 30 + "```"
        self.assertEqual(concise_speech(text), "Код — в чате.")

    def test_code_must_not_hide_error(self):
        text = "Команда не выполнена: ошибка 5.\n```\n" + "traceback\n" * 60 + "```"
        self.assertEqual(concise_speech(text), "Команда не выполнена: ошибка 5. " + NOTICE)

    def test_urls_links_and_citations(self):
        text = ("См. [отчёт](https://example.org/report). Рост составил 3.14 процента [1]. "
                "Данные подтверждены【2†source】. https://example.org/more")
        self.assertEqual(concise_speech(text), "См. отчёт. Рост составил 3.14 процента. Данные подтверждены.")

    def test_linked_numeric_citations_and_other_domain_suffixes(self):
        text = "Данные подтверждены [2](https://example.ai/paper). Отчёт опубликован (example.co.uk)."
        self.assertEqual(concise_speech(text), "Данные подтверждены. Отчёт опубликован.")

    def test_domain_and_version_do_not_split_sentences(self):
        text = "Версия 1.2.3 опубликована (example.org). Размер 3.14 МБ. Проверено 12 файлов."
        self.assertEqual(concise_speech(text), "Версия 1.2.3 опубликована. Размер 3.14 МБ. Проверено 12 файлов.")

    def test_link_removal_does_not_leave_dangling_phrase(self):
        self.assertEqual(concise_speech("Версия доступна на https://example.org."), NOTICE)
        self.assertEqual(concise_speech("Available at https://example.org."), NOTICE)

    def test_abbreviations_and_initials(self):
        text = "А. С. Пушкин жил в г. Москве, т. е. в России. См. стр. 12. Цена 3.5 тыс. руб."
        self.assertEqual(concise_speech(text), text)

    def test_tables_and_headings_are_not_read(self):
        text = "## Итоги\nПроверено 12 записей.\n\n| Имя | Число |\n|---|---|\n| A | 12 |"
        self.assertEqual(concise_speech(text), "Проверено 12 записей. " + NOTICE)

    def test_setext_and_bold_headings(self):
        for heading in ("Результаты\n==========", "**Результаты:**"):
            self.assertEqual(concise_speech(heading + "\n- Файлы сохранены\n- Проверка пройдена"),
                             "Файлы сохранены. Проверка пройдена. " + NOTICE)

    def test_action_report_promotes_failure_from_heading_or_table(self):
        for tail in ("# Ошибка проверки", "Ошибка проверки\n---",
                     "| Загрузка | Ошибка 403 |"):
            result = concise_speech("Готово.\n\n" + tail)
            self.assertIn("Ошибка", result)
            self.assertTrue(result.endswith(NOTICE))
            self.assertNotEqual(result, NOTICE)

    def test_negative_headings_do_not_veto_explanations(self):
        for heading in ("# Если сервер недоступен", "**Не передавай пароль:**",
                        "Ошибка подключения\n---"):
            self.assertEqual(concise_speech("SSH защищает соединение.\n\n" + heading),
                             "SSH защищает соединение. " + NOTICE)

    def test_list_item_cannot_be_split_at_semicolon(self):
        text = "- Данные сохранены в отдельный архив; " + "описание структуры " * 40 + "."
        self.assertEqual(concise_speech(text), NOTICE)

    def test_long_sentence_can_end_at_semicolon(self):
        text = "Данные успешно сохранены в отдельный архив; " + "подробная информация о структуре архива " * 20 + "."
        self.assertEqual(concise_speech(text), "Данные успешно сохранены в отдельный архив. " + NOTICE)

    def test_long_sentence_without_safe_clause_returns_notice(self):
        self.assertEqual(concise_speech("Исследование " + "содержит подробные данные " * 60 + "."), NOTICE)

    def test_long_sentence_clause_does_not_hide_negation(self):
        text = "Данные успешно сохранены в отдельный архив; но " + "проверка " * 90 + "не завершена."
        self.assertEqual(concise_speech(text), NOTICE)

    def test_short_unpunctuated_answer_and_punctuation(self):
        self.assertEqual(concise_speech("Файл сохранён"), "Файл сохранён.")
        text = "«Файл сохранён!» Проверено? Да…"
        self.assertEqual(concise_speech(text), text)

    def test_reserved_notice_budget_does_not_shorten_full_answer(self):
        text = "Данные " + "а" * 395 + "."
        self.assertEqual(concise_speech(text), text)

    def test_length_and_determinism(self):
        for text in ("Файл сохранён. " * 100, "x" * 2000, "- " + "данные " * 100,
                     "# Только заголовок", "[1]: https://example.org"):
            with self.subTest(text=text[:25]):
                result = concise_speech(text)
                self.assertEqual(result, concise_speech(text))
                self.assertLessEqual(len(result), 420)
                self.assertTrue(result.endswith((".", "?", "!", "…")))

    def test_import_and_call_do_not_load_backends_or_make_requests(self):
        # A fresh isolated process catches accidental import-time side effects too.
        script = '''
import sys
def audit(event, args):
    if event.startswith(("socket.", "http.client.")):
        raise AssertionError("network request: " + event)
sys.addaudithook(audit)
from assistant.speech import concise_speech
assert concise_speech("Готово.") == "Готово."
assert not {"assistant.voice", "torch", "numpy", "requests", "urllib.request", "ollama"}.intersection(sys.modules)
'''
        subprocess.run([sys.executable, "-B", "-c", script], check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
