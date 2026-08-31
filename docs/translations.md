# Translations

All user-facing text in this project — the simulator CLI, exception
messages and log messages — goes through `powerpetdoor.i18n.t()`:

```python
from .i18n import t

return CommandResult(
    True, t("simulator.commands.control.debug_logging_enabled", "Debug logging enabled")
)
```

The English text is the second argument, so **English lives in the source**.
There is no `en_us.json`, and therefore no second place for English to drift
out of step with the code. A key with no translation renders its default,
which is why adding a language can never break anything.

Wire protocol strings are deliberately *not* translatable. Everything in
`const.py` goes to a device whose firmware cannot be changed — it is not
user-facing text at all. See "Never change the device protocol" in
`.claude/CLAUDE.md`.

## Where translations live

```
src/powerpetdoor/locales/
├── messages.json    generated catalogue — key to English, never loaded at runtime
├── de_de.json       a translation
└── fr_fr.json       another
```

A locale file is a flat JSON object of translation keys to translated text,
preceded by a header. Header keys begin with an underscore and are never
translations — they are this format's equivalent of a gettext `.po`
header, and they are the reason a header value is allowed to be a list
where a translation must be a string.

```json
{
  "_language": "Deutsch",
  "_translators": ["Alex Müller <alex@example.de>", "Sam Weber <sam@example.de>"],
  "_updated": "2026-08-23",

  "simulator.commands.control.debug_logging_enabled": "Debug-Protokoll aktiviert",
  "simulator.commands.control.debug_logging": "Debug-Protokoll: {arg0}"
}
```

| Header key | Meaning |
|---|---|
| `_language` | The language's name **as its own speakers write it** — `Deutsch`, not `German` |
| `_translators` | Attribution: a string, or a list of them, conventionally `Name <email>`. Reachable is the point: whoever picks the file up next needs to be able to ask a question |
| `_updated` | ISO 8601 date the translation was last revised |

From code, the header is available without loading a translation:

```python
i18n.get_locale_name("de_de")  # 'Deutsch'
i18n.get_translators("de_de")  # ['Alex Müller <alex@example.de>', ...]
i18n.get_locale_metadata("de_de")  # the whole header
```

## Adding a language

1. **Create the file**, prefilled with the English and a header stub. The
   code is the lower-cased IETF-ish name with an underscore — `de_de`,
   `fr_fr`, `pt_br`, or just `fr`.

   ```bash
   python scripts/check_translations.py --init-locale de_de
   ```

   This writes `src/powerpetdoor/locales/de_de.json`. It refuses to
   overwrite an existing file.

2. **Fill in the header**: `_language`, `_translators`, `_updated`.

3. **Translate the values**, leaving the keys alone. To see how a string is
   actually used before you word it — "Closed" is a different word for a
   door, a connection and a schedule window — ask where it comes from:

   ```bash
   # one key, or any substring/glob of one
   python scripts/check_translations.py --locate debug_logging

   # every key, with its source locations
   python scripts/check_translations.py --locations
   ```

   ```
   simulator.commands.control.debug_logging_enabled
       text: 'Debug logging enabled'
       at:   src/powerpetdoor/simulator/commands/control.py:76
   ```

   Add `--json` to get the same thing as data you can pipe:

   ```bash
   python scripts/check_translations.py --locations --json > /tmp/where.json
   ```

   Locations are deliberately **not** stored in the repository. A line
   number moves whenever anything above it does, so a committed copy would
   be stale by the next unrelated edit — and it costs milliseconds to
   extract from the source, which is never stale.

4. **Keep the placeholders.** `{name}` and `{arg0}` are substituted at
   runtime, and `%s` is substituted by the logging module. A translation
   whose placeholders do not match its call site is caught and falls back to
   English rather than raising, but it will not have translated anything.
   `{arg0}`-style names come from expressions that had no obvious name; the
   catalogue's English shows what lands there.

5. **Check your work:**

   ```bash
   python scripts/check_translations.py --locale de_de --strict
   ```

   * *orphaned* — the key no longer exists in the source. Delete the entry.
   * *missing* — no translation yet. Renders English; not an error.
   * *collision artifact* — two keys with identical English, one a `_N`
     suffix of the other. A bug in the source, not in your translation.
   * *UNATTRIBUTED* — the file has no `_translators`. Not fatal, but it
     means the next person to touch it has nobody to ask.

5. **Bump `_updated`** when you revise it.

## Keeping a translation current

When English changes, `--write-catalog` moves and the audit starts
reporting *missing* (a new or reworded key) or *orphaned* (a deleted one)
against every locale. Neither breaks anything at runtime: missing renders
English, and orphaned is simply ignored. Re-run the audit after pulling:

```bash
python scripts/check_translations.py --locale de_de --strict
```

## Selecting a language

By environment, which is how the simulator CLI picks one up:

```bash
POWERPETDOOR_LOCALE=de_de ppd-simulator
```

`LC_ALL`, `LC_MESSAGES` and `LANG` are consulted in that order if
`POWERPETDOOR_LOCALE` is unset. `C` and `POSIX` mean English.

Or explicitly, from code:

```python
from powerpetdoor import i18n

i18n.set_locale("de_de")
print(i18n.get_available_locales())  # ['de_de', 'en_us']
print(i18n.get_locale_name("de_de"))  # 'Deutsch'
```

An unknown locale is accepted and renders English, so a typo in a
deployment degrades rather than fails.

## Why `messages.json` is committed but locations are not

`messages.json` changes only when translatable *text* does, so its diff is
meaningful: a reworded string shows up in review as something translators
will need to revisit. A pre-commit hook regenerates it, and the audit fails
if it has drifted, so it cannot go stale.

Locations fail both of those tests — they churn on every unrelated edit and
their diff says nothing — so they are computed on demand instead.

## What CI enforces

The lint job runs:

```bash
python scripts/check_translations.py --untranslated --strict
```

which fails on **orphaned** entries, on **collision artifacts**, on a stale
`messages.json`, and on any user-facing string that is not wrapped in `t()`
at all. Missing translations never fail a build: a locale legitimately lags
the source between a string landing and a translator reaching it.

The pre-commit hooks run the same checks, plus one that regenerates
`messages.json` in place so it is never committed out of date.
