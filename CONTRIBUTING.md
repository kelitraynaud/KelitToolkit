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
- Unreal-side code (the embedded script templates in `operators/usd_sync.py`)
  must never call `actor.rename()` on a level actor: it is a fatal engine
  error. Read the "Technical notes" section of the README before touching the
  sync.
- Test headless when possible: `blender -b --python tests/smoke_test.py -- report.json`
  must stay green, and the same pattern works for new tests.

## License

By contributing you agree that your contributions are licensed under the
GPL-3.0-or-later, like the rest of the project.
