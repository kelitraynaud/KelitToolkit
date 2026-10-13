# Contributing to Kelit Toolkit

Thanks for wanting to make this better. The project is vibecoded and open to
contributions.

## Reporting bugs

Open an [issue](https://github.com/kelitraynaud/KelitToolkit/issues) with:

- Blender version, Unreal version, OS
- What you did, what you expected, what happened (the exact error text helps)
- If possible, a minimal .blend that reproduces it
- For sync issues: the UE Output Log lines tagged `[B2UE]`

## Suggesting features

Open an issue describing the workflow problem first, not just the solution.

## Pull requests

- Keep one PR = one topic.
- Match the surrounding code style (plain `bpy` API, explicit names, comments
  only where the code can't say it).
- Blender 5.x rules to respect: no `action.fcurves` direct access (slotted
  actions, see `iter_action_fcurves` in `utils.py`), no removed 4.x APIs
  (`use_auto_smooth`, ...).
- Unreal-side code (the script templates in `operators/usd_sync.py`,
  `operators/ue_materials.py` and the commands sent by
  `operators/ue_remote.py`) must never call `actor.rename()` on a level
  actor: it is a fatal engine error. Read the "Technical notes" section of the
  README before touching the send.
- Any tool that deletes materials or images goes through
  `viewports_to_solid()` (`operators/materials.py`): deleting them while EEVEE
  draws crashes Blender.
- Never use the em dash or en dash characters in labels, tooltips, messages
  or docs.
- Run the test suite before opening a PR: `python tests/run_tests.py` (add
  `--quick` to skip the slow bake test, `--pure` for the Blender-free part).
  - Blender is found through the `BLENDER_EXE` environment variable or the
    usual install paths.
  - The headless tests load the add-on under the module name `KelitToolkit`
    (a `scripts/addons/KelitToolkit` folder). To test a checkout without
    installing it, copy it to `<folder>/addons/KelitToolkit` and set
    `BLENDER_USER_SCRIPTS=<folder>` and
    `KELIT_TEST_ADDON_ROOT=<folder>/addons/KelitToolkit`.
  - The runner gives every Blender a temporary configuration folder and
    backs up your preferences to `%TEMP%/kelit_userpref_backups` first. A
    headless test started any other way refuses to run, and saving
    preferences is blocked inside the tests: a test must never be able to
    touch your real Blender settings.
  - Headless tests live in `tests/headless/` and share `_harness.py`; the
    same pattern works for new tests.

## License

By contributing you agree that your contributions are licensed under the
GPL-3.0-or-later, like the rest of the project.
