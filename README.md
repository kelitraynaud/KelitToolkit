# Kelit Toolkit

Blender add-on to prepare scenes and send them to Unreal Engine: one-click
USD send with materials, animation and cameras, plus the cleanup, naming,
instancing, collision and optimization tools that make a scene ready for it.

Developed by [Kélit Raynaud](https://github.com/kelitraynaud). Vibecoded.
Bug reports, feature requests and pull requests are welcome, see
[CONTRIBUTING.md](CONTRIBUTING.md).

> **Status: pre-release (0.9.x).** Shared for testing; the public 1.0.0 will
> follow once it has settled.

## Requirements

- Blender 5.0 or later (tested on 5.2).
- Unreal Engine 5.5 or later (tested on 5.8), running on the same computer,
  with the **Python Editor Script Plugin** (remote execution on) and the
  **USD Importer** plugin. The **Connection Doctor** can enable both for you.

## Install

**Drag and drop.** Drag this link from your browser into the Blender window:

[Drag into Blender to install Kelit Toolkit](https://kelitraynaud.github.io/KelitToolkit/kelittoolkit.zip?repository=https%3A%2F%2Fkelitraynaud.github.io%2FKelitToolkit%2Findex.json&blender_version_min=5.0.0)

The first time, Blender asks to add the Kelit Toolkit repository: click
**Add Repository**, then drag the link once more to install. Updates then
come through Blender itself.

**From a file.** Download `kelittoolkit-<version>.zip` from the
[releases](https://github.com/kelitraynaud/KelitToolkit/releases), then in
Blender: *Edit > Preferences > Get Extensions*, drop-down menu (top right) >
*Install from Disk*. Dragging the zip into the Blender window also works.

The panel is in the 3D View sidebar (**N** key) > **Kelit Toolkit**. The
tools are also in the *Object* menu.

Use either the extension or the legacy zip (`KelitToolkit_<version>.zip`,
*Add-ons > Install from Disk*), not both: they register the same tools.

## Updates

If you installed with the drag-and-drop link, updates appear in *Get
Extensions*. Otherwise press **Enable Native Updates** (panel > Send to
Unreal > Advanced, or the add-on preferences): it adds the repository
`https://kelitraynaud.github.io/KelitToolkit/index.json`, saves your
preferences and checks for updates. Then remove the copy you installed from
a file, so only one version runs.

## Your first send

1. Open your Unreal project.
2. In Blender, select the objects to send (or nothing, and use a collection).
3. Click **Send to Unreal**.
4. Check the options in the dialog (below) and click OK.
5. The result is reported in Blender's status bar. If Unreal does not
   answer, the Connection Doctor opens and tells you what to fix.

What it creates in Unreal:

- **Assets** in the Content Browser, under `Content Folder/<blend name>/`.
- **Actors** in the open level, one per Blender object, at the same world
  position, with the parenting rebuilt.
- With **Scene Animation (Sequencer)**: a **LevelSequence**
  `LS_<blend name>` that animates the actors and carries the camera.
- **Materials**: one material instance per Blender material, on the Kelit
  master material.

Sending again replaces the actors sent from this .blend and leaves the rest
of the level alone. The sequence is rebuilt on every send: keep manual
sequencer work in a separate sequence. Keep one fixed **Content Folder** per
Unreal project.

## Send options

| Option | What it does |
|---|---|
| Source | Selection (with parents and children), or a collection: 'Export' if the file has one, otherwise the active collection. |
| Place Actors in Level | Spawns the actors. Untick to import assets only. |
| Replace Sent Objects | Deletes and recreates the actors sent earlier from this .blend. |
| Import Materials | Sends materials. Untick for geometry only. |
| Materials | **Kelit master material** (default) or Unreal's own USD materials. |
| Two-Sided | Single-sided (default), as in Blender, or two-sided everywhere. |
| Alpha | Cutout (default, translucent only for materials set to Blended in Blender), ignore alpha, or translucent. |
| Export Detected Rigs as Skeletal Meshes | Armatures and their meshes become skeletal meshes. |
| Skeletal Animation Clips | Which actions are exported as animation sequences. |
| Scene Animation (Sequencer) | Builds the LevelSequence from the object and camera animation. |
| Include Camera | Sends the scene camera as a CineCamera with a Camera Cuts track. |
| Camera as Spawnable | Keeps the camera inside the sequence instead of the level. |
| Preserve Hierarchy (Animated) | Keeps animated parenting; keys are written in parent space. |
| Keyframes | Authored keys (default, only what Unreal needs) or every frame. |
| Force Geometry Reimport | Reimports geometry even when it has not changed. |

Objects disabled for render (camera icon) are not sent. Objects only hidden
in the viewport are sent.

## Tools

Tools that change or delete data say so in their tooltip. All of them can be
undone with Ctrl+Z.

### Send to Unreal

- **Send to Unreal**: the send described above.
- **Update Materials Only**: rebuilds the material instances without
  sending the geometry again.
- **Choose Project**: picks which running Unreal project receives the
  commands, when several are open.
- **Export Files Instead (USD / FBX)**: writes files to disk instead of
  sending to a running Unreal.
- Advanced:
  - **Test Unreal Connection** and **Connection Doctor (Setup Help)**:
    check, and fix, the Unreal project setup (plugins, remote execution),
    with backups of the files it changes.
  - **Enable Native Updates**: see Updates.
  - **Clear All Synced Actors**: removes the actors sent from this .blend.
  - **Make Sequence Self-Contained**: turns the sequence's actors into
    spawnables, so the sequence plays in any level.
  - **Export USD Hierarchy Only** and **Batch FBX Export**: file exports.
  - **Fix Permanently (DefaultEngine.ini)**: makes Unreal 5.5+ use its
    legacy FBX importer for files you import by hand.

### Prepare Assets

Transforms

- **Apply Scale** and **Apply Rotation**: apply the transform to every
  object sharing the mesh. Mirrored and zero scales are refused.
- **Normalize Scene Scale**: rescales the whole scene (objects, cameras,
  lights, location keys) by one factor, framing kept.
- **Rescale Rig + Animation**: rescales a rigged character and its
  animations by one factor (0.01 for a file in centimetres read as metres),
  scale applied, import empties removed.
- **Fix Bone Lengths**: shortens bones far too long for their place in the
  rig, without changing their orientation or the animation.
- **Auto Align**: finds an object's natural axes from its faces and lines
  it up with the world (Rotate), bakes that rotation into the mesh (Bake), or
  keeps the object in place and only realigns its local axes (Keep). Useful
  for props that arrive tilted.
- **Bake Camera Animation**: bakes the camera's final motion and focus onto
  the camera itself, so its rig can be deleted.

Instances

- **Detect Duplicates > Instances**: objects with the same shape, UVs and
  materials share one mesh, even when their position or their rotation was
  baked into the mesh (Rotated Copies option).
- **Rejoin Material Splits**: objects an export cut per material
  (`Box074Material260`, `Box074Material310`) become one object again.
- **Split Repeated Parts**: elements repeated inside one mesh come out as
  instances, rotated copies included; pieces that touch stay together.
- **Replace with Active**, **Group Similar in Collection**, **Realize
  Modifiers to Instances**.

Origin

- **Bottom Center**, **Center**, **Top Center**, **Custom Position**: move
  the origin of the selected meshes; every object sharing the mesh keeps its
  place. **Bottom Center (Fix Array)** compensates Array offsets.

Mesh

- **Apply All Modifiers** and **Enhance Low Poly** (adds Bevel, Subdivision
  and Weighted Normal, nothing applied).
- **High to Low Poly (Bake)**: low-poly copy to a triangle budget, UV
  unwrap, Normal, AO and Base Color baked from the high poly.
- **Optimize AI Meshes**: decimates AI-generated props (Meshy, Tripo,
  Hunyuan...) to a face budget per square metre, once per shared mesh. It
  adds a reversible `KT_Optimize` modifier by default; **Apply** bakes it.
  The star button (**Mark as Hero**) sets a minimum ratio for objects seen
  up close; the X button removes the modifiers. Compare at the final camera
  distance before applying: decimation keeps the UVs but can change shading.
- **Create Collision Mesh** (UCX convex hull, box, sphere) and **Generate
  LODs**.
- **Add Socket**: adds an empty named `SOCKET_<name>` at the 3D cursor,
  parented to the active mesh. Every `SOCKET_` empty under a mesh becomes a
  socket of its Static Mesh in Unreal (attach points for props, lights or
  effects), on Send to Unreal and in FBX exports. Sockets created by hand in
  Unreal are left alone.

### Clean & Check

- **Auto Clean**: the main cleanup tools in the right order (merge duplicate
  materials, unused empties, material splits, repeated parts, duplicates to
  instances, rotation and scale, origins, names, validation). The dialog
  shows what each step will touch; one Ctrl+Z undoes all of it.
- Naming: **Normalize Names (Quick)** (Unreal prefixes and PascalCase),
  **Normalize (Advanced)**, **Find & Replace**, **Clean Suffixes**, **Mesh
  Name from Object**, **Object Name from Mesh**, **Material Name from
  Mesh**, **Add Prefix**.
- Materials: **Auto Setup PBR Textures**, **Create Material by Mesh**,
  **Convert to Simple PBR**, **Detect Unsupported Nodes**, **Bake
  Procedural to Texture**, **Validate Texture Paths**, **Rename Textures for
  Unreal**, **Extract Textures**, **Delete Unused Materials** (empty slots of
  the selection), **Purge Unused Materials** (whole file), **Merge Duplicate
  Materials** and **Merge Duplicate Images** (by content, whatever the
  names).
- Scene Cleanup: **Scene Weight Report** (what weighs at render time:
  triangles, shared meshes, textures, heavy subdivisions), **Delete Hidden
  Objects**, **Delete Unused Empties** (camera rigs and constraint targets
  kept), **Remove Empty Parents**, **Remove Empty Collections**, **Purge
  Zero-Face Meshes**, **Clean Vertex Groups**, **Remove Unused Mesh Data**.
- **Validate for Unreal**: lists what still needs attention.

### Search and reports

The search field at the top of the panel filters every tool by name or
description. It shows each tool's full name, which can differ slightly from
the button text.

Validate, Auto Clean, Scene Weight Report and Optimize AI Meshes open a
report window: click a line to select its object, **Copy to Clipboard** to
share it.

## FAQ

**"No running Unreal Editor found"**
Unreal is not running with your project open, the Python Editor Script
Plugin is off, or remote execution is not ticked (Project Settings > Plugins
> Python). The Connection Doctor fixes the last two: point it at your
project folder, click OK, restart Unreal. With several editors open, use
**Choose Project**.

**Actors arrive without a mesh**
The USD Importer plugin is off in your project (the Connection Doctor
enables it, restart Unreal), or the send was interrupted: send again.

**Assets appear in two places**
The Content Folder changed between two sends of the same .blend. Keep one
fixed folder per project.

**Some objects did not arrive**
Objects disabled for render are skipped on purpose. Cache-driven copies (a
keyframed hierarchy plus an Alembic copy of itself, common in C4D exports)
are skipped too: the keyframed copy arrives. Note that **Delete Hidden
Objects** deletes viewport-hidden objects that a send would include.

**My sequence lost the tracks I added by hand**
Every send rebuilds the LevelSequence. Keep manual work in a separate
sequence.

**Everything is tiny (or huge) in Unreal**
The file is at 0.01 scale (common with C4D or FBX round-trips). Run
**Normalize Scene Scale**, or **Rescale Rig + Animation** for a rigged
character.

**The model looks see-through or inside-out in Unreal**
Blender exports backface culling off as two-sided, and anything plugged into
Alpha as translucent. The **Two-Sided** and **Alpha** options fix both
(single-sided and cutout by default).

**The camera arrives blurred**
Tick *Depth of Field* on the Blender camera: the send mirrors it. Animated
focus is sent as its first-frame value for now (python-authored focus
tracks crash Unreal 5.8's Sequencer).

**An FBX I imported by hand looks wrong in Unreal 5.5+**
Use **Fix Permanently (DefaultEngine.ini)** (Advanced) and restart Unreal.

**How do I remove what the add-on created?**
**Clear All Synced Actors** removes this .blend's actors; the assets are in
`Content Folder/<blend name>/`.

## Technical notes (Unreal 5.8)

- Mesh prims (and their `displayName`) are renamed after the Blender
  object; a `_Mesh` suffix protects names ending in digits from Unreal's
  trailing-number stripping.
- Import uses `kinds_to_collapse=0` and `use_prim_kinds_for_collapsing=False`
  so nested meshes are never merged.
- Asset matching: mesh-data hint, then prim-name hint, then name variants,
  then the asset registry for freshly imported meshes.
- Unreal's AUTO key tangents were measured against the editor (flat at end
  keys and at interior extrema, otherwise central difference clamped to 1.5x
  the smaller adjacent secant), so the key reduction predicts what Unreal
  will interpolate.
- Level actors are never renamed through the API (a name clash is a fatal
  engine error); only sequence-namespace binding names are set.
- Large remote-execution responses are reassembled across TCP segments.
- The remote-execution endpoints can be changed in the add-on preferences.

## Changelog

**Next**: Auto Align (Rotate, Bake, Keep), rotated copies recognized by
Detect Duplicates and Split Repeated Parts, Static Mesh sockets from
`SOCKET_` empties (Send to Unreal and FBX), Add Socket.

**0.9.4**: review release, nothing new, everything safer.

- Merge Duplicate Materials compares every node setting (operation, colour,
  ramp, blend mode, UV map, node groups) and the full image path: materials
  that only looked alike are no longer merged. Merge Duplicate Images skips
  unsaved painting and generated images.
- Images with the same name in different forms (Wood, Wood.001, wood.png)
  get distinct Unreal textures instead of sharing one.
- Fix Bone Lengths leaves IK, socket, target and constrained bones alone and
  is off by default in Rescale Rig + Animation. Rescale Rig checks
  everything before changing anything and rescales meshes skinned through a
  modifier only.
- Split Repeated Parts keeps elements with different UVs apart and is much
  faster; Rejoin Material Splits skips groups with different modifiers.
- Re-sends reset every material instance parameter (no leftover tint or
  emission); alpha packed into Roughness or Metallic is extracted; textures
  are written as real PNG; objects sharing a mesh with different materials
  get per-actor materials in Unreal.
- The static import cache is cleared before each re-import. With several
  Unreal editors open, the send asks you to choose a project instead of
  guessing; the Connection Doctor opens again when no editor answers.
- Viewports always come back from Solid after a material or image deletion,
  even on error. Optimize AI Meshes and Scene Weight Report edge cases fixed.
- Drag-and-drop install link, rewritten README, shorter tooltips, Object
  menu renamed Kelit Toolkit.

**0.9.3**: Auto Clean, Optimize AI Meshes (with Mark as Hero), Scene Weight
Report, Merge Duplicate Materials and Images, Rejoin Material Splits, Split
Repeated Parts, Rescale Rig + Animation, Fix Bone Lengths, report window,
Choose Project. Materials built on the Kelit master material, Two-Sided and
Alpha options, static geometry import cache, collection fallback for the
send. Fixes for skeletal export, origin presets on parented objects, UCX,
the Connection Doctor and EEVEE crashes on deletion. Operator ids moved to
`kelit_toolkit.*`: settings saved by older versions are not carried over.

<details>
<summary>Earlier versions</summary>

- 0.9.2: safety release. The animated-focus track of 0.9.1 could crash
  Unreal's Sequencer; animated focus falls back to its first-frame value.
  One-click **Enable Native Updates**.
- 0.9.1: Connection Doctor, progress feedback, remembered send options,
  mirrored objects kept, skeletal root neutralized, vertical sensor fit,
  native update channel.
- 0.9.0: first shared pre-release: full audit and hardening, headless test
  suite, Send2UE integration removed.
- 0.8: animated hierarchies, editable keyframes, camera as spawnables,
  Attach Tracks.
- 0.7: unified file export, tool search, Normalize Scene Scale, panel in
  workflow sections.
- 0.6: standalone remote transport.
- 0.5: spawnable sequences, camera depth of field.
- 0.4: master material and instances.
- 0.3: native LevelSequence animation, CineCamera and Camera Cuts.
- 0.2: USD Scene Sync, per-object re-sync, batch FBX.
- 0.1: modular reorganization of the original single-file toolkit.

</details>

## License

Copyright (C) 2026 Kélit Raynaud.

This add-on is free software, licensed under the **GNU General Public
License v3.0 or later**, see [LICENSE](LICENSE).

`dependencies/remote_execution.py` is Copyright Epic Games, Inc., taken from
the MIT-licensed [BlenderTools](https://github.com/poly-hammer/BlenderTools)
project and redistributed under its own terms, see
[dependencies/LICENSE-BlenderTools.txt](dependencies/LICENSE-BlenderTools.txt).

Artwork, scenes and assets produced with this add-on are yours and are not
covered by this license.
