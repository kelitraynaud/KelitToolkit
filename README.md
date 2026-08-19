# Kelit Toolkit

Blender add-on to prepare and send scenes to Unreal Engine: USD scene sync
with native LevelSequence animation, naming conventions, collisions, LODs,
high-to-low-poly baking, materials, and batch file export.

Developed by [Kélit Raynaud](https://github.com/kelitraynaud). Vibecoded.
Bug reports, feature requests and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

> **Status: pre-release (0.9.x).** Shared for testing; the public 1.0.0 will
> follow once it has settled.

## Requirements

- Blender 5.0+ (uses the bundled `pxr` USD bindings)
- Unreal Engine 5.5+ (developed and tested against 5.8) with:
  - **Python Editor Script Plugin** (remote execution enabled)
  - **USD Importer** plugin (for USD Scene Sync)

## Install

1. Download the zip from the latest
   [release](https://github.com/kelitraynaud/KelitToolkit/releases).
2. In Blender: *Edit > Preferences > Add-ons > Install…* and pick the zip.
3. Enable **Kelit Toolkit**. The panel lives in the 3D View sidebar (N key).
4. In your Unreal project: enable the Python Editor Script Plugin and tick
   *Remote Execution* in Project Settings > Plugins > Python.

## USD Scene Sync

One click: exports the selection (with its full parent/child hierarchy) to
binary USD, imports it in Unreal as **separate static meshes with materials**,
spawns **one actor per Blender object at its exact world transform**, and
rebuilds the parenting with actor attachments. Re-syncing replaces only the
objects being sent — other synced objects are kept. "Clear All Synced Actors"
removes everything synced from the current .blend.

Objects disabled for **render** (camera toggle) are skipped. Objects merely
disabled in the viewport (monitor toggle) ARE exported: the sync temporarily
re-enables them so a lightened viewport never loses geometry in Unreal.

### "Include Animation"

Turns the scene into a **native Unreal LevelSequence**, built by script rather
than by UE's USD actor import (which nests everything under one actor):

- every object's transform is sampled per frame in Blender and written as a
  real transform track — one binding per moving actor, plain StaticMeshActors
  set to Movable, no nested scene components;
- **Preserve Hierarchy** (default): animated nulls, their children and the
  camera keep their Blender parenting — keys are baked in *parent space*, so
  attached actors reproduce the same world motion;
- **editable keyframes by default**: the sequence keeps the keyframes authored
  in Blender and only inserts extra keys where Unreal's AUTO tangents would
  drift beyond 0.1% of the motion's amplitude; densely-baked imports (a key
  per frame) collapse to minimal keys. `Every Frame (exact)` gives a dense
  bake instead;
- the scene camera becomes a **CineCameraActor** with focal length, filmback
  and depth of field from Blender, wired to a **Camera Cuts track**; by
  default the camera (and the null chain that only drives it) is carried
  **inside the sequence as spawnables**;
- cache-driven duplicates (a keyframed hierarchy shipped together with an
  Alembic-cache copy of itself) are skipped automatically.

Camera orientation is derived from forward/up vectors through
`MathLibrary.make_rot_from_xz`, which is unambiguous between Blender's
convention (-Z forward, +Y up) and Unreal's (+X forward, +Z up).

### Make Sequence Self-Contained

Converts every binding to spawnables so the whole sequence carries its own
actors: no level dependency, no broken bindings. The level's attach hierarchy
is replayed as native **Attach Tracks**, and static objects join the sequence
with their level transform.

## Other tools

- **Quick tool search** at the top of the panel — type to filter every tool.
- **File export**: unified export with automatic skeletal/static/scene
  routing (USD primary, FBX fallback), plus a batch FBX export with embedded
  UCX/UBX/USP collisions and LODs.
- **High to Low Poly (Bake)**: low-poly copy decimated to a triangle budget,
  Smart UV unwrap, Normal/AO/BaseColor baked from the high-poly (the pair is
  isolated during the bake), textures packed, original kept hidden.
- **Scene cleanup**: *Delete Unused Empties* (protects camera rigs,
  constraint/DOF/driver targets), *Bake Camera Animation* (bakes the final
  world motion + focus onto the camera so its rig becomes deletable),
  *Remove Empty Parents*, hidden-object and orphan-data cleanup.
- **Normalize Scene Scale**: rescales objects AND cameras around the world
  origin so mis-scaled imports reach scale 1.0 at real size with the framing
  preserved (keyframes, camera clip/focus and light power follow).
- **Naming**: UE conventions (SM_/M_/T_ prefixes, PascalCase), find & replace,
  batch normalization.
- **Collisions & LODs**: convex UCX / box / sphere collisions, LOD chains.
- **Materials**: Simple-PBR conversion, procedural baking, auto PBR setup
  from texture folders, and **Build Material Instances** — rebuilds the
  Blender materials in UE as instances of a single master material, with
  textures exported and imported by the add-on itself.
- **Instances**: duplicate detection and conversion to instances,
  instance-safe apply of scale and rotation.

## Technical notes (UE 5.8)

- Mesh prims (and their `displayName` metadata) are renamed after the Blender
  object; a `_Mesh` suffix protects names ending in digits from UE's
  trailing-number stripping.
- Import uses `kinds_to_collapse=0` + `use_prim_kinds_for_collapsing=False` so
  nested meshes are never merged.
- Asset matching: exact prim-name hint, then name variants (±`SM_`), then a
  geometry fingerprint for UE-deduplicated identical meshes.
- Unreal's AUTO key tangents were measured against the editor (flat at end
  keys and at interior extrema, otherwise central difference clamped to 1.5×
  the smaller adjacent secant), so the key reduction predicts what Unreal
  will interpolate.
- Level actors are never renamed through the API (a name clash is a fatal
  engine error); only sequence-namespace binding names are set.
- Large remote-execution responses are reassembled across TCP segments (the
  vendored client originally read a single segment).
- The remote-execution endpoints can be changed in the add-on preferences if
  the UE project uses non-default Python settings.

## Versioning

**0.9.0** — current pre-release: everything above. Includes a full code
audit and hardening pass (instance-safe applies with shape-key support,
convex UCX, scene-scoped cleanups, linked-data guards, filename dedup,
texture tool fixes, large-scene performance work), a headless regression
suite (`tests/smoke_test.py`), and the removal of the old Send2UE
integration — the USD workflow replaced it entirely.

<details>
<summary>Development history</summary>

- 0.8 — animated hierarchies preserved through the sync (parent-space keys),
  editable keyframes with a calibrated tangent model, camera package as
  spawnables, Attach Tracks in self-contained sequences, transport fixes
- 0.7 — unified file export, quick tool search, Normalize Scene Scale,
  camera-safe scene cleaning, panel reorganized into workflow sections
- 0.6 — standalone remote transport (Epic's client vendored)
- 0.5 — spawnable sequences, camera depth of field, safe binding naming
- 0.4 — material bridge (master material + instances, texture export)
- 0.3 — native LevelSequence animation, CineCameraActor + Camera Cuts
- 0.2 — USD Scene Sync, per-object re-sync, batch FBX rewrite
- 0.1 — modular reorganization of the original single-file toolkit

</details>

## License

Copyright (C) 2026 Kélit Raynaud.

This add-on is free software, licensed under the **GNU General Public License
v3.0 or later** — see [LICENSE](LICENSE).

`dependencies/remote_execution.py` is Copyright Epic Games, Inc., taken from
the MIT-licensed [BlenderTools](https://github.com/poly-hammer/BlenderTools)
project and redistributed under its own terms — see
[dependencies/LICENSE-BlenderTools.txt](dependencies/LICENSE-BlenderTools.txt).
The coordinate conversions follow the same convention as BlenderTools.

Artwork, scenes and assets produced *with* this add-on are yours and are not
covered by this licence.
