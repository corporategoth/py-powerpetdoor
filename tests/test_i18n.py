"""Tests for `powerpetdoor.i18n`.

The module sits underneath error reporting - `t()` is called *while*
building the text that describes a failure - so "never raises" is the
property most of these pin. A locale file that is missing, corrupt, not an
object, or carrying placeholders that do not match the call site must all
degrade to the English default rather than raise from inside whatever the
caller was trying to report.
"""

from __future__ import annotations

import json

import pytest

from powerpetdoor import i18n


@pytest.fixture(autouse=True)
def _clean_i18n():
    """Every test starts from English with nothing cached."""
    i18n.reset_for_testing()
    yield
    i18n.reset_for_testing()


@pytest.fixture
def locales(tmp_path, monkeypatch):
    """Redirect the module at a writable locale directory."""
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reset_for_testing()
    return tmp_path


def write_locale(directory, name: str, mapping) -> None:
    (directory / f"{name}.json").write_text(json.dumps(mapping), encoding="utf-8")


class TestNormalizeLocale:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("en_US.UTF-8", "en_us"),
            ("en-US", "en_us"),
            ("  EN_us  ", "en_us"),
            ("de_DE@euro", "de_de"),
            ("fr", "fr"),
        ],
    )
    def test_the_spellings_the_environment_actually_hands_over(self, raw, expected):
        assert i18n.normalize_locale(raw) == expected


class TestLoadLocale:
    def test_a_present_file_is_loaded(self, locales):
        write_locale(locales, "de_de", {"a.b": "Guten Tag"})
        assert i18n.load_locale("de_de") == {"a.b": "Guten Tag"}

    def test_the_second_load_is_served_from_cache(self, locales):
        write_locale(locales, "de_de", {"a.b": "first"})
        assert i18n.load_locale("de_de")["a.b"] == "first"

        write_locale(locales, "de_de", {"a.b": "second"})
        assert i18n.load_locale("de_de")["a.b"] == "first"

    def test_a_missing_non_default_locale_warns(self, locales, caplog):
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("nl_nl") == {}
        assert "No translation file for locale 'nl_nl'" in caplog.text

    def test_a_missing_default_locale_is_silent(self, locales, caplog):
        """English lives in the source as `t()` defaults; a file is optional."""
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale(i18n.DEFAULT_LOCALE) == {}
        assert caplog.text == ""

    def test_corrupt_json_is_ignored_rather_than_raised(self, locales, caplog):
        (locales / "de_de.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("de_de") == {}
        assert "Ignoring unusable translation file" in caplog.text

    def test_a_json_array_is_not_a_translation_table(self, locales, caplog):
        write_locale(locales, "de_de", ["nope"])
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("de_de") == {}
        assert "is not a JSON object" in caplog.text

    def test_non_string_values_are_dropped_and_counted(self, locales, caplog):
        write_locale(locales, "de_de", {"good": "ja", "bad": 5, "worse": None})
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("de_de") == {"good": "ja"}
        assert "Ignored 2 non-string" in caplog.text

    def test_an_all_string_file_logs_nothing(self, locales, caplog):
        """The `dropped` count gates the warning; zero must stay quiet."""
        write_locale(locales, "de_de", {"good": "ja"})
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("de_de") == {"good": "ja"}
        assert caplog.text == ""


class TestSelectingALocale:
    def test_set_locale_normalizes_and_returns_what_took_effect(self, locales):
        write_locale(locales, "de_de", {"a": "b"})
        assert i18n.set_locale("de-DE.UTF-8") == "de_de"
        assert i18n.get_locale() == "de_de"

    def test_an_unknown_locale_is_accepted_and_renders_english(self, locales):
        assert i18n.set_locale("xx_yy") == "xx_yy"
        assert i18n.t("some.key", "English") == "English"

    def test_available_locales_always_include_english(self, locales):
        write_locale(locales, "de_de", {})
        assert i18n.get_available_locales() == ["de_de", "en_us"]

    def test_an_unreadable_locale_directory_still_answers_english(
        self, locales, monkeypatch, caplog
    ):
        """Listing locales must not raise from underneath a caller."""

        def boom(_pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(type(locales), "glob", lambda self, pattern: boom(pattern))
        with caplog.at_level("DEBUG", logger="powerpetdoor.i18n"):
            assert i18n.get_available_locales() == [i18n.DEFAULT_LOCALE]
        assert "Could not list locales" in caplog.text

    def test_the_extracted_catalogue_is_not_offered_as_a_language(self, locales):
        """messages.json sits beside the locales but is the English source."""
        write_locale(locales, i18n.CATALOG_STEM, {"a.b": "English"})
        write_locale(locales, "de_de", {})
        assert i18n.get_available_locales() == ["de_de", "en_us"]

    def test_the_display_name_comes_from_the_file(self, locales):
        write_locale(locales, "de_de", {i18n.LANGUAGE_NAME_KEY: "Deutsch"})
        assert i18n.get_locale_name("de_de") == "Deutsch"

    def test_without_that_key_the_code_is_used(self, locales):
        write_locale(locales, "de_de", {})
        assert i18n.get_locale_name("de-de") == "DE_DE"


class TestTranslate:
    def test_a_hit_replaces_the_default(self, locales):
        write_locale(locales, "de_de", {"greet": "Guten Tag"})
        i18n.set_locale("de_de")
        assert i18n.t("greet", "Good day") == "Guten Tag"

    def test_a_miss_renders_the_english_default(self, locales):
        write_locale(locales, "de_de", {})
        i18n.set_locale("de_de")
        assert i18n.t("greet", "Good day") == "Good day"

    def test_a_miss_under_a_non_default_locale_is_recorded(self, locales):
        write_locale(locales, "de_de", {})
        i18n.set_locale("de_de")
        i18n.t("greet", "Good day")
        assert ("de_de", "greet") in i18n.missing_keys()

    def test_a_miss_under_english_is_not_recorded(self, locales):
        """English *is* the defaults; every key would be 'missing'."""
        i18n.t("greet", "Good day")
        assert i18n.missing_keys() == set()

    def test_without_kwargs_the_string_is_returned_untouched(self, locales):
        """Lazy %-style logging placeholders must survive for logging."""
        assert i18n.t("k", "Failed to decode frame (%s): %s") == "Failed to decode frame (%s): %s"

    def test_braces_survive_when_no_kwargs_are_given(self, locales):
        """A frame of JSON in the text is not a format placeholder."""
        assert i18n.t("k", 'saw {"CMD": "X"}') == 'saw {"CMD": "X"}'

    def test_kwargs_are_applied(self, locales):
        assert i18n.t("k", "hold for {seconds}s", seconds=5) == "hold for 5s"

    def test_a_translation_that_does_not_fit_falls_back_to_english(self, locales, caplog):
        write_locale(locales, "de_de", {"k": "halte {sekunden}s"})
        i18n.set_locale("de_de")
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.t("k", "hold for {seconds}s", seconds=5) == "hold for 5s"
        assert "does not fit its arguments" in caplog.text

    def test_when_the_english_does_not_fit_either_it_is_returned_raw(self, locales, caplog):
        """Both format attempts failing must still not raise."""
        write_locale(locales, "de_de", {"k": "{nope}"})
        i18n.set_locale("de_de")
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.t("k", "{alsonope}", seconds=5) == "{alsonope}"

    def test_reset_clears_locale_cache_and_missing_keys(self, locales):
        write_locale(locales, "de_de", {})
        i18n.set_locale("de_de")
        i18n.t("greet", "Good day")
        assert i18n.missing_keys()

        i18n.reset_for_testing()

        assert i18n.get_locale() == i18n.DEFAULT_LOCALE
        assert i18n.missing_keys() == set()


class TestLocaleFromEnvironment:
    def test_the_project_variable_wins(self, monkeypatch):
        monkeypatch.setenv(i18n.LOCALE_ENV_VAR, "de_DE.UTF-8")
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        assert i18n._locale_from_environment() == "de_de"

    def test_the_standard_variables_are_consulted_in_order(self, monkeypatch):
        monkeypatch.delenv(i18n.LOCALE_ENV_VAR, raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LC_MESSAGES", "es_ES")
        monkeypatch.setenv("LANG", "fr_FR")
        assert i18n._locale_from_environment() == "es_es"

    @pytest.mark.parametrize("value", ["C", "POSIX", "C.UTF-8"])
    def test_the_c_locale_means_english_not_a_locale_named_c(self, monkeypatch, value):
        for var in (i18n.LOCALE_ENV_VAR, "LC_ALL", "LC_MESSAGES"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LANG", value)
        assert i18n._locale_from_environment() == i18n.DEFAULT_LOCALE

    def test_an_empty_variable_is_skipped(self, monkeypatch):
        """`LANG=` set-but-empty is common in containers."""
        for var in (i18n.LOCALE_ENV_VAR, "LC_ALL", "LC_MESSAGES"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LANG", "")
        assert i18n._locale_from_environment() == i18n.DEFAULT_LOCALE

    def test_nothing_set_is_english(self, monkeypatch):
        for var in (i18n.LOCALE_ENV_VAR, "LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(var, raising=False)
        assert i18n._locale_from_environment() == i18n.DEFAULT_LOCALE


class TestLocaleFileHeader:
    """The `_`-prefixed header: language name, attribution, revision date.

    This is the format's equivalent of a gettext `.po` header, and it is why
    a header value may be a list where a translation may not.
    """

    def test_header_keys_are_not_offered_as_translations(self, locales):
        write_locale(locales, "de_de", {"_language": "Deutsch", "k": "Hallo"})
        assert i18n.load_locale("de_de") == {"k": "Hallo"}

    def test_a_list_valued_header_is_kept_not_dropped(self, locales, caplog):
        """A translators list would be discarded by the string-only filter."""
        write_locale(locales, "de_de", {"_translators": ["A <a@example.de>"], "k": "Hallo"})
        with caplog.at_level("WARNING", logger="powerpetdoor.i18n"):
            assert i18n.load_locale("de_de") == {"k": "Hallo"}
        assert caplog.text == ""
        assert i18n.get_translators("de_de") == ["A <a@example.de>"]

    def test_the_whole_header_is_readable(self, locales):
        write_locale(
            locales,
            "de_de",
            {"_language": "Deutsch", "_updated": "2026-08-23", "k": "Hallo"},
        )
        assert i18n.get_locale_metadata("de_de") == {
            "_language": "Deutsch",
            "_updated": "2026-08-23",
        }

    def test_a_single_translator_may_be_a_bare_string(self, locales):
        write_locale(locales, "de_de", {"_translators": "Solo <solo@example.de>"})
        assert i18n.get_translators("de_de") == ["Solo <solo@example.de>"]

    def test_non_string_entries_in_the_translator_list_are_skipped(self, locales):
        write_locale(locales, "de_de", {"_translators": ["A <a@example.de>", 42, None]})
        assert i18n.get_translators("de_de") == ["A <a@example.de>"]

    def test_no_attribution_is_an_empty_list_not_an_error(self, locales):
        write_locale(locales, "de_de", {"k": "Hallo"})
        assert i18n.get_translators("de_de") == []

    def test_a_translators_key_of_the_wrong_type_is_ignored(self, locales):
        write_locale(locales, "de_de", {"_translators": {"who": "me"}})
        assert i18n.get_translators("de_de") == []

    def test_a_non_string_language_name_falls_back_to_the_code(self, locales):
        write_locale(locales, "de_de", {"_language": 7})
        assert i18n.get_locale_name("de_de") == "DE_DE"

    def test_the_header_is_cleared_by_reset(self, locales):
        write_locale(locales, "de_de", {"_language": "Deutsch"})
        assert i18n.get_locale_name("de_de") == "Deutsch"

        i18n.reset_for_testing()
        (locales / "de_de.json").unlink()

        assert i18n.get_locale_name("de_de") == "DE_DE"
