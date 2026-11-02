# Contributing

Thanks for considering a contribution to **My Computer for Nautilus**.

## Design philosophy

We follow the [GNOME Human Interface Guidelines](https://developer.gnome.org/hig/) and
Adwaita design and interaction patterns as closely as possible, the goal is for **My
Computer** to feel like a native part of Nautilus, not a bolted-on plugin. Prefer existing
libadwaita/GTK widgets and style classes over custom CSS or bespoke widgets, and check
that any new interaction matches how Nautilus itself already behaves.

## Before you open a pull request

**Base your pull request on the `dev` branch, not `main`.** `main` only receives tested,
released changes; `dev` is where ongoing work and contributions land before a release.
If your PR is opened against `main` by mistake, you can retarget it to `dev` from the
pull request's "Edit" button on GitHub.

## Translations

Adding or improving a translation is the easiest way to contribute.

Translations are managed on [Weblate](https://hosted.weblate.org/projects/nautilus-my-computer/),
not through direct pull requests. Sign up for a free Weblate account, join the project, pick
your language, and translate directly in the web editor. Approved changes are synced back to
this repository automatically as a pull request, you don't need to touch `po/*.po` files
yourself.

If your language isn't listed yet, use Weblate's "start new translation" option on the
project page.

## Code contributions

1. For bigger changes, open an issue or a draft pull request early, so we can agree on the
   approach. This extension is community driven: opening early lets others join in, share
   feedback, test, or suggest a better solution before the work is done.
2. Follow the existing layout: `nautilus-my-computer.py` is only a thin entry shim, and the code
   lives in the `nautilus_my_computer/` package, one module per Nautilus surface it reads from or
   injects into. Don't add new top-level `*.py` files next to the entry point, Nautilus loads every
   one of them as a possible extension.
3. Run before committing:
   ```bash
   ruff format
   ruff check
   ```
4. Test your change against a real Nautilus session:
   ```bash
   cp nautilus-my-computer.py ~/.local/share/nautilus-python/extensions/
   cp -r nautilus_my_computer ~/.local/share/nautilus-python/extensions/
   rm -rf ~/.local/share/nautilus-python/extensions/__pycache__
   rm -rf ~/.local/share/nautilus-python/extensions/nautilus_my_computer/__pycache__
   nautilus -q; sleep 1 && nautilus
   ```
5. Commit messages follow `<type>: <short description> (#<PR>)` (e.g. `fix: ...`, `feat: ...`,
   `i18n: ...`, `l10n: ...`), imperative mood, max 72 characters including the suffix. Each
   commit ends with the number of your pull request, e.g. `fix: guard missing content-type (#198)`.
   You only get that number once the pull request is open, so amend or reword your commits
   afterwards and force-push. Keep one logical change per commit, pull requests are not
   squashed. Checks run automatically and tell you which number to use.

## Reporting bugs

Use the [bug report form](https://github.com/yannmasoch/nautilus-my-computer/issues/new?template=bug_report.yml),
it asks for everything needed to reproduce the problem. If Nautilus crashes, attach the debug log:
```bash
nautilus -q; sleep 1 && DEBUG=1 nautilus > /tmp/nautilus.log 2>&1
grep "MyComputer:" /tmp/nautilus.log
```

For feature ideas, use the
[feature request form](https://github.com/yannmasoch/nautilus-my-computer/issues/new?template=feature_request.yml).

## Code of conduct

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree your contribution is licensed under this project's MIT license.

---

**My Computer** started as a simple solo project and quickly grew into something
community-driven. Every translation, bug report, and pull request makes it better.
Thanks for being part of that, we're glad you're here!
