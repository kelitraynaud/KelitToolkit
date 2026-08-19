# Kelit Toolkit

Blender add-on to prepare and send scenes to Unreal Engine: USD scene sync
with native LevelSequence animation, naming conventions, collisions, LODs,
high-to-low-poly baking, materials, and batch file export.

Developed by [Kélit Raynaud](https://github.com/kelitraynaud). Vibecoded.
Bug reports, feature requests and pull requests are welcome, see
[CONTRIBUTING.md](CONTRIBUTING.md).

> **Status: pre-release (0.9.x).** Shared for testing; the public 1.0.0 will
> follow once it has settled.

## Requirements

- Blender 5.0+ (uses the bundled `pxr` USD bindings)
- Unreal Engine 5.5+ (developed and tested against 5.8) with:
  - **Python Editor Script Plugin** (remote execution enabled)
  - **USD Importer** plugin (for USD Scene Sync)

The add-on can configure the Unreal side for you: if a send fails, the
**Connection Doctor** dialog opens, reads your project files, shows what is
missing and fixes it (plugins enabled, remote execution turned on, with
backups). You can also run it any time from the panel's Advanced section.

## Install

1. Download the zip from the latest
   [release](https://github.com/kelitraynaud/KelitToolkit/releases).
2. In Blender: *Edit > Preferences > Add-ons > Install…* and pick the zip.
3. Enable **Kelit Toolkit**. The panel lives in the 3D View sidebar (N key).
4. Open your Unreal project, select your objects in Blender, and click
   **Send Scene via USD**. If anything is not set up yet, the Connection
   Doctor tells you what and offers to fix it.

## How it works

One click on **Send Scene via USD** creates three things in Unreal:

1. **Assets**: your meshes, imported into the Content Browser under
   `Content Folder/<blend name>/`. Reusable bricks.
2. **Actors**: instances of those assets placed in the currently open level,
   one per Blender object, at the exact world transform, with the parenting
   rebuilt. Each actor carries an invisible tag ("this .blend, this object").
3. **A LevelSequence** (with *Include Animation*): the native sequence that
   animates those actors and carries the camera, created at
   `Content Folder/<blend name>/LS_<blend name>`.

**Re-syncing** uses the tags: the objects you send again replace their own
old actors, everything else in the level is untouched. The **sequence is
rebuilt from scratch** on every sync, so keep manual sequencer work in a
separate sequence.

The **Content Folder** is just the storage address. Keep one fixed folder
per Unreal project: changing it between two syncs of the same .blend leaves
the plugin looking in the new place while the old assets stay in the old one.

## USD Scene Sync

Objects disabled for **render** (camera toggle) are skipped. Objects merely
disabled in the viewport (monitor toggle) ARE exported: the sync temporarily
re-enables them so a lightened viewport never loses geometry in Unreal.

### "Include Animation"

Turns the scene into a native Unreal LevelSequence, built by script rather
than by UE's USD actor import (which nests everything under one actor):

- every object's transform is sampled per frame in Blender and written as a
  real transform track, one binding per moving actor, plain StaticMeshActors
  set to Movable, no nested scene components;
- **Preserve Hierarchy** (default): animated nulls, their children and the
  camera keep their Blender parenting. Keys are baked in parent space, so
  attached actors reproduce the same world motion;
- **editable keyframes by default**: the sequence keeps the keyframes
  authored in Blender and only inserts extra keys where Unreal's AUTO
  tangents would drift beyond 0.1% of the motion's amplitude; densely-baked
  imports (a key per frame) collapse to minimal keys. `Every Frame (exact)`
  gives a dense bake instead;
- the scene camera becomes a **CineCameraActor** with focal length, filmback
  and depth of field from Blender, wired to a **Camera Cuts track**;
  **animated focus** (keyed focus distance or a moving focus target) becomes
  a real focus track on the camera component; by default the camera (and the
  null chain that only drives it) is carried inside the sequence as
  spawnables;
- mirrored (negative-scale) objects keep their mirroring;
- cache-driven duplicates (a keyframed hierarchy shipped together with an
  Alembic-cache copy of itself) are skipped automatically;
- the dialog options are remembered per scene.

Camera orientation is derived from forward/up vectors through
`MathLibrary.make_rot_from_xz`, which is unambiguous between Blender's
convention (-Z forward, +Y up) and Unreal's (+X forward, +Z up).

### Make Sequence Self-Contained

Converts every binding to spawnables so the whole sequence carries its own
actors: no level dependency, no broken bindings. The level's attach
hierarchy is replayed as native **Attach Tracks**, and static objects join
the sequence with their level transform.

## Other tools

- **Quick tool search** at the top of the panel: type to filter every tool.
- **File export**: unified export with automatic skeletal/static/scene
  routing (USD primary, FBX fallback), plus a batch FBX export with embedded
  UCX/UBX/USP collisions and LODs.
- **High to Low Poly (Bake)**: low-poly copy decimated to a triangle budget,
  Smart UV unwrap, Normal/AO/BaseColor baked from the high-poly (the pair is
  isolated during the bake), textures packed, original kept hidden.
- **Scene cleanup**: *Delete Unused Empties* (protects camera rigs,
  constraint/DOF/driver targets), *Bake Camera Animation* (bakes the final
  world motion and focus onto the camera so its rig becomes deletable),
  *Remove Empty Parents*, hidden-object and orphan-data cleanup.
- **Normalize Scene Scale**: rescales objects AND cameras around the world
  origin so mis-scaled imports reach scale 1.0 at real size with the framing
  preserved (keyframes, camera clip/focus and light power follow).
- **Naming**: UE conventions (SM_/M_/T_ prefixes, PascalCase), find and
  replace, batch normalization.
- **Collisions and LODs**: convex UCX / box / sphere collisions, LOD chains.
- **Materials**: Simple-PBR conversion, procedural baking, auto PBR setup
  from texture folders, and **Build Material Instances**: rebuilds the
  Blender materials in UE as instances of a single master material, with
  textures exported and imported by the add-on itself.
- **Instances**: duplicate detection and conversion to instances,
  instance-safe apply of scale and rotation.

## FAQ

**"No running Unreal Editor found"**
Three causes, in order of likelihood: Unreal is not running with your
project open; the Python Editor Script Plugin is not enabled; remote
execution is not ticked in Project Settings > Plugins > Python. The
Connection Doctor (opens automatically on failure) can fix the last two:
point it at your project folder, click OK, restart Unreal. Also check you
do not have two editors open, the wrong one may answer.

**Actors spawn but they are empty (no mesh)**
The USD Importer plugin is disabled in your project. The Connection Doctor
enables it; restart Unreal afterwards.

**Second sync says "sequence not found", or assets appear in two places**
The Content Folder changed between two syncs of the same .blend. The plugin
stores everything under `Content Folder/<blend name>/`; keep one fixed
folder per project.

**Some objects did not arrive**
Objects disabled for render (camera toggle) are skipped on purpose.
Viewport-hidden objects (monitor toggle) ARE exported. Cache-driven
duplicates (a keyframed hierarchy plus an Alembic-cache copy of itself,
common in C4D exports) are skipped automatically: the keyframed copy is the
one that arrives.

**My re-synced sequence lost the tracks I added by hand**
Re-syncing rebuilds the LevelSequence from scratch. Keep manual work
(lights, audio, extra cameras) in a separate sequence.

**Everything is tiny (or huge) in Unreal**
Your import is at 0.01 scale (common with C4D/FBX round-trips). Run
**Normalize Scene Scale** first: objects and cameras reach scale 1.0 at
real size with the framing preserved.

**Flat surfaces are invisible from one side, like inverted normals**
Unreal's USD importer creates two-sided material instances but leaves the
override off. Enable **Force Double-Sided Materials** in the sync options,
only if you need it: double-sided shading costs performance.

**The camera arrives blurred**
Unreal's CineCamera defaults to a 1 km manual focus. The sync mirrors
Blender's depth of field instead: check your camera's *Depth of Field*
checkbox in Blender. Animated focus is carried as a track since 0.9.1.

**An FBX I imported by hand looks wrong in UE 5.5+**
Unreal's Interchange importer handles some FBX differently. **Fix
Interchange FBX (Permanent)** (Advanced section) writes the legacy-importer
flag into your project config; restart Unreal.

**How do I remove everything the plugin created?**
**Clear All Synced Actors** removes this .blend's actors from the level;
the imported assets live under `Content Folder/<blend name>/` and can be
deleted there.

**How do I update the add-on?**
Grab the latest zip from the
[releases](https://github.com/kelitraynaud/KelitToolkit/releases) page, or
add the extension repository once and update natively from Blender (see
Updates below).

## Updates

Blender can update the add-on natively. One click: in the add-on
preferences (or the panel's Advanced section), press **Enable Native
Updates**. It registers the toolkit's update repository and Blender then
shows new versions in *Preferences > Get Extensions*.

Manual equivalent: add
`https://kelitraynaud.github.io/KelitToolkit/index.json` as a remote
repository in *Get Extensions > Repositories*. If you install the toolkit
from the repository, remove the zip-installed copy so only one version runs.

## Technical notes (UE 5.8)

- Mesh prims (and their `displayName` metadata) are renamed after the
  Blender object; a `_Mesh` suffix protects names ending in digits from UE's
  trailing-number stripping.
- Import uses `kinds_to_collapse=0` and `use_prim_kinds_for_collapsing=False`
  so nested meshes are never merged.
- Asset matching: mesh-data hint first, then prim-name hint, then name
  variants, then a geometry fingerprint for UE-deduplicated identical
  meshes.
- Unreal's AUTO key tangents were measured against the editor (flat at end
  keys and at interior extrema, otherwise central difference clamped to 1.5x
  the smaller adjacent secant), so the key reduction predicts what Unreal
  will interpolate.
- Level actors are never renamed through the API (a name clash is a fatal
  engine error); only sequence-namespace binding names are set.
- Large remote-execution responses are reassembled across TCP segments (the
  vendored client originally read a single segment).
- The remote-execution endpoints can be changed in the add-on preferences if
  the UE project uses non-default Python settings.

## Versioning

**0.9.1**: current pre-release:

- Connection Doctor: on a failed send, a dialog reads the Unreal project
  files, shows what is missing (Python plugin, USD Importer, remote
  execution) and fixes it with backups.
- Progress feedback during the sync (per-phase cursor and status text).
- Sync dialog options remembered per scene.
- Animated depth of field carried as a focus track on the camera component.
- Mirrored (negative-scale) objects keep their mirroring everywhere
  (placement, animation, skeletal).
- Skeletal export neutralizes the full root transform (no more double
  rotation/scale on spawn).
- Correct FOV for vertical and auto sensor fit (portrait renders).
- Asset matching by mesh-data name (object names that sanitize identically
  no longer collide).
- Native update channel through a self-hosted extension repository.
- "How it works" and FAQ sections in this README.

<details>
<summary>Development history</summary>

- 0.9.0: first shared pre-release: full code audit and hardening pass
  (instance-safe applies with shape-key support, convex UCX, scene-scoped
  cleanups, linked-data guards, filename dedup, texture tool fixes,
  large-scene performance), headless regression suite, Send2UE integration
  removed (the USD workflow replaced it)
- 0.8: animated hierarchies preserved through the sync (parent-space keys),
  editable keyframes with a calibrated tangent model, camera package as
  spawnables, Attach Tracks in self-contained sequences, transport fixes
- 0.7: unified file export, quick tool search, Normalize Scene Scale,
  camera-safe scene cleaning, panel reorganized into workflow sections
- 0.6: standalone remote transport (Epic's client vendored)
- 0.5: spawnable sequences, camera depth of field, safe binding naming
- 0.4: material bridge (master material + instances, texture export)
- 0.3: native LevelSequence animation, CineCameraActor + Camera Cuts
- 0.2: USD Scene Sync, per-object re-sync, batch FBX rewrite
- 0.1: modular reorganization of the original single-file toolkit

</details>

## License

Copyright (C) 2026 Kélit Raynaud.

This add-on is free software, licensed under the **GNU General Public
License v3.0 or later**, see [LICENSE](LICENSE).

`dependencies/remote_execution.py` is Copyright Epic Games, Inc., taken from
the MIT-licensed [BlenderTools](https://github.com/poly-hammer/BlenderTools)
project and redistributed under its own terms, see
[dependencies/LICENSE-BlenderTools.txt](dependencies/LICENSE-BlenderTools.txt).
The coordinate conversions follow the same convention as BlenderTools.

Artwork, scenes and assets produced with this add-on are yours and are not
covered by this licence.
