# Changelog

All notable changes to this project are documented here.

---

## v0.12.4
Filter My Computer cards by typing, fix a Preferred Folders drag glitch, and correct virtual folder icons.

### Added
- My Computer cards can now be filtered by typing while the panel is in
  focus, matching search-as-you-type in a native Nautilus folder.
  Navigation keys (/, ~, Ctrl+L) still work as before.

### Changed
- Custom folder icons set via Properties now update the moment you switch
  back to the window, instead of on a background timer.

### Fixed
- Preferred Folders no longer loses its drop-position highlight partway
  through a drag when reordering.
- The Recent folder now picks up a custom icon set via Properties > Icon,
  just like a real folder. Starred and Network fall back to their correct
  default icons instead of the wrong ones.
- The Recent folder now shows its correct clock icon instead of an
  hourglass, and unpinning then re-pinning it no longer corrupts its saved
  location.
- Fixed two bugs upstream in [ChromaLeon](https://github.com/Fabito02/ChromaLeon) so folder color
  tinting also applies correctly to the icons used by Recent, Starred, and Network.

Thanks to @MoshiurRahmanAdib for requesting address-bar card filtering in #47
and for the detailed diagnosis behind the Recent icon and re-pinning fixes in
#82; to @parthib-gaugewalker for the icon references and investigation that
shaped the virtual-folder icon fix in #83; and to @ParadaCarleton for the
initial virtual-folder icon report in #79.

---

## v0.12.3
Add captions and zoom-aware sizing to Preferred Folders, tabbed settings, and
fix several sizing and translation bugs.

### Added
- Preferred Folders can now show a caption under each card with the folder's
  item count and last-modified date, matching Nautilus's own captions. Turn
  it on or off from Settings.
- Preferred Folders and disk cards now resize together with Nautilus's own
  zoom level, and Ctrl+scroll zooms the Computer view the same way it does
  in a normal folder.
- The install script now supports a `--help` flag listing all available
  options.

### Changed
- The Settings dialog is now split into tabs by category instead of one
  long scrolling page, making it easier to find what you're looking for.

### Fixed
- Preferred Folders and disk cards now align to the same grid Nautilus
  itself uses, fixing folder sizes and padding that didn't quite match the
  native view.
- Preferred Folders now update immediately when you rename, delete, or move
  a folder, instead of requiring a restart.
- Custom folder icons now show up correctly in the My Computer panel
  instead of falling back to the generic folder icon.
- Home folders (Documents, Downloads, Music, etc.) now always show their
  real name, whether that's the system language default or one you've
  renamed yourself.
- German (de_DE) translations now load correctly.

Credit to @MoshiurRahmanAdib for contributing the install script's `--help` flag.
Credit to @caioolivv for reporting the folder icon, folder naming, and sizing
bugs, and to @unaibenidorm for reporting the grid sizing issue, with @aushamim
confirming it independently. Credit to @YoMama78 for reporting the German
translation bug, confirmed by @mikpinky and @Naezr, who also confirmed the
folder icon and naming bugs.

---

## v0.12.2
Fix a broken icon in the Grid/List/Column switcher on some systems.

### Fixed
- The Column View icon in the Grid/List/Column switcher could show up as a
  broken/missing icon, depending on your icon theme. It now falls back to a
  different icon automatically when the preferred one isn't available.

---

## v0.12.1
Replace the two-state view-mode button with a segmented Grid/List/Column
switcher.

### Changed
- The view-mode button is now a segmented Grid/List/Column switcher, so all
  three views are one click away instead of cycling through them. Sorting
  and other view settings moved into their own "View Options" button next
  to it.

---

## v0.12.0
Add an early, opt-in Column View (beta) as a third way to browse alongside
the normal file view and the Computer panel.

### Added
- Column View (beta): browse folders as Miller-style columns, with its own
  right-click menu and preview support. Still experimental, marked with a
  beta badge, and off by default.

---

## v0.11.9
Fix My Computer failing to load entirely on GNOME 47 and 48, plus two address
bar icon glitches.

### Fixed
- On GNOME Files 47 and 48, the extension could fail to load at all, so the
  Computer view never appeared, with no visible error unless Nautilus was
  started from a terminal (issue #61).
- Opening the Computer view in a tab, then navigating that same tab to a
  regular folder, could leave the Computer icon permanently stuck next to
  the tab's label until Nautilus was restarted (issue #29).
- On some systems (confirmed on Fedora 41), the address bar showed no icon
  at all for Computer, with a stray "/" in front of the name instead.

Credit to @pelach for reporting the crash with the exact logs needed to
track it down.

---

## v0.11.8
Fix the install script failing with a GitHub rate-limit error for some users.

### Fixed
- The install script could fail with a "429 Too Many Requests" error from
  GitHub, most often on shared or VPN networks where many people share the
  same public IP address. The script now downloads the extension as a single
  package instead of many separate files, which avoids the limit for
  virtually everyone.
- Running the script as `bash install.sh` from a manually cloned copy of the
  repository was incorrectly treated as an online install (and could hit the
  same rate limit) instead of installing directly from the local files.

Credit to the Reddit user who reported the install failure with logs that
pinpointed the exact GitHub error.

---

## v0.11.7
Fix a drive being listed twice in the Computer view for some network/autofs setups.

### Fixed
- Drives mounted through autofs (common in some fstab-based NAS/network share
  setups) could show up as two separate cards - one for the autofs trigger
  mount and one for the real filesystem mounted on top of it. The scanner now
  only accepts known real-storage, network, or optical filesystem types,
  imports GIO's own shadowed-mount detection, and collapses any leftover
  duplicate at the same mount point down to the one that's actually visible.

Credit to @root9191 for the diagnostic screenshots on issue #57 that showed
one card was `autofs` and the other `cifs`, which pinpointed the cause.

---

## v0.11.6
Fix a bug where the Computer view could get permanently stuck blank.

### Fixed
- Clicking "Computer" inside a file picker (e.g. a browser's "upload file"
  dialog) could leave the view blank for the rest of that window's life.
- Switching between tabs could occasionally leave the Computer view showing
  stale content instead of updating to match the tab you switched to.
- File pickers no longer jump to the Computer view on their own when the
  "Show Computer view on open" preference is enabled - that's for regular
  Nautilus windows only.

Credit to the reporter of issue #55 for finding and clearly describing the
original bug.

---

## v0.11.5
Add GNOME/Nautilus version-detection helpers and fix two GNOME 47 compatibility issues.

### Added
- `_nautilus_version()` in `common.py`: reads Nautilus's own compiled-in AppStream metadata via
  `Gio.resources_lookup_data("/org/gnome/nautilus/appdata")` (the same GResource its About dialog
  uses) to get the running app version in-process, with no subprocess or filesystem guessing.
- `_resolve_gtype()` in `common.py`: tries a list of GObject type names in order and returns the
  first one registered, centralizing the pattern needed whenever Nautilus renames an internal
  GObject type across releases.

### Fixed
- `_build_place_sidebar_row` now resolves either `NautilusSidebarRow` (48+) or
  `NautilusGtkSidebarRow` (47) via `_resolve_gtype`, instead of hardcoding the post-47 name only.
- The Preferred Folders / disk-grid justified `FlowBox` now uses `halign=FILL` unconditionally.
  On GTK 4.16 (Nautilus 47), an expanding `FlowBox` with `halign=START` wasn't allocated its full
  available width, collapsing the justified layout to one column and overlapping the next
  section. Verified `FILL` causes no regression on GTK 4.22 (Nautilus 50) either.

Credit to @PizzaLovingNerd for identifying both issues and the original fix approach in PR #54.

---

## v0.11.4
Fix openSUSE OBS auto-rebuild actually doing nothing.

### Fixed
- `packaging/opensuse/workflows.yml`'s `rebuild_package` step only calls OBS's plain
  `Package.rebuild` API, which rebuilds whatever source is already committed to the package - it
  never re-runs the `_service`, so every `v0.11.2`/`v0.11.3` tag push silently "succeeded" while
  actually just rebuilding the same stale `v0.11.1` content over and over (no error, since the step
  did exactly what it's coded to do, just not what we needed). Switched to `trigger_services`,
  which re-runs the `_service` (re-fetching the spec and tarball from GitHub via `download_url`/
  `download_files`) and triggers a real rebuild once the content actually changes.

---

## v0.11.3
Fix the CI issues found on v0.11.2's first live tag-push run.

### Fixed
- `aur-publish.yml` pinned `KSXGitHub/github-actions-deploy-aur@v4`, a floating major-version tag
  that doesn't actually exist for that action (only full versions like `v4.1.3` are published) -
  pinned to `v4.1.3` instead.
- `ppa-publish.yml` didn't install `build-essential`, which `dpkg-checkbuilddeps` requires
  regardless of whether it's explicitly listed in `debian/control`'s `Build-Depends` - added it to
  both the workflow and `packaging/ubuntu/README.md`'s documented one-time setup.
- `copr-publish.yml` combined `curl -f` and `--fail-with-body`, which are mutually exclusive
  fail-reporting modes - dropped the redundant `-f`.
- Disabled `aur-publish.yml` (`gh workflow disable`) rather than leaving it failing on every tag
  push - the AUR account itself was never created, so publishing can't succeed regardless of the
  action version fix. Re-enable once the account exists.

---

## v0.11.2
Tag-triggered package publishing across all four distro channels.

### Added
- `.github/workflows/aur-publish.yml` and `.github/workflows/ppa-publish.yml` now trigger on
  `push: tags: v*` instead of a manually-published GitHub release, since releases were never
  actually being published - pushing a version tag is the real release step for this project.
- `.github/workflows/copr-publish.yml`, triggering a Fedora COPR build directly via the COPR API
  (`/build/create/scm`) with the pushed tag as the `committish` override, since COPR's own
  SCM auto-rebuild only watches a fixed branch (`main`) and has no native tag-tracking mode.
- `.github/workflows/release.yml`, auto-creating a GitHub Release (with notes pulled from the
  matching section of this changelog) on every version tag push.
- openSUSE OBS was already tag-triggered (`packaging/opensuse/workflows.yml`, added in v0.11.1's
  follow-up work), no change needed there.

---

## v0.11.1
Ubuntu PPA packaging.

### Added
- Ubuntu `debian/` packaging (`packaging/ubuntu/`), targeting only currently-supported
  series: resolute (26.04 LTS, GNOME 50) and stonking (26.10, in development). Non-LTS
  Ubuntu releases only get ~9 months of support; noble, oracular, plucky, and questing
  either ship GNOME older than this extension targets or are already EOL. Built and
  installed end-to-end on a real Ubuntu 26.04 VM.
- `packaging/ubuntu/build-and-upload.sh` to build and `dput` signed source packages
  per series to `ppa:yannmasoch/nautilus-my-computer`. Builds the source package
  (`.dsc`/`.orig.tar.gz`/`.debian.tar.xz`) once and generates a separate signed `.changes`
  per series referencing the same files, since Launchpad's pool is shared across every
  series in a PPA and rejects re-uploading the same filename with different contents
  (which happens if `debian/changelog` is baked into a fresh source build per series).
  Version derived from `debian/changelog` via `dpkg-parsechangelog` rather than hardcoded.
- `ubuntu-validate` CI job building a real `.deb` via `dpkg-buildpackage` and checking
  installed paths with `lintian`
- README `## Installation` section now documents package-manager installs (Fedora COPR,
  openSUSE OBS, Ubuntu PPA), not just the universal `install.sh`. AUR is prepared
  (`packaging/aur/`) but not yet published - AUR is currently blocking new-account
  submissions after a recent spam wave.

### Fixed
- `EXT_VERSION` in `nautilus_my_computer/main.py` was still `0.10.1`, out of sync with
  `pyproject.toml`; the About page now reports the correct version

---

## v0.11.0
Package-manager distribution: Makefile-driven packaging for AUR, Fedora, and openSUSE.

### Added
- `Makefile` with standard `DESTDIR`/`PREFIX` install/uninstall targets, for use by
  distro packaging instead of the manual `~/.local` dev-loop steps
- AUR `PKGBUILD` (`packaging/aur/`), plus a GitHub Action that publishes it to the
  AUR git repo automatically on each GitHub release
- Fedora RPM `.spec` (`packaging/fedora/`), built, linted, and installed end-to-end
  on a real Fedora VM
- openSUSE RPM `.spec` (`packaging/opensuse/`), built, linted, and installed
  end-to-end on a real openSUSE Tumbleweed VM

### Changed
- Project description updated to "My Computer for Nautilus, what GNOME Files
  should have always been" across README, `pyproject.toml`, and the AUR package

---

## v0.10.1
Sidebar and Preferred Folders polish.

### Added
- Recent and Starred now appear by default in Preferred Folders, right
  after Home, with Network added at the end, each with a proper icon
- Switching your icon theme now updates disk and folder card icons
  immediately, no more restarting Nautilus to see the change

### Fixed
- The sidebar separator line between the Computer row and your other
  places no longer disappears when every native place is hidden
- The drag gutter on folder cards now matches Nautilus's native hover
  highlight instead of looking out of place while dragging

---

## v0.10.0
Preferred Folders can now be reordered by drag-and-drop, plus a big internal refactor.

### Added
- Drag-and-drop reordering for Preferred Folders cards, with a live preview
  of the landing slot as you drag; the new order is persisted to GSettings
  on drop
- Hidden-file detection for disk and folder cards (`.hidden-file` CSS class),
  matching native Nautilus behavior
- Live rename tracking for Preferred Folders via `Gio.FileMonitor` on parent
  directories, auto-correcting stale GSettings URIs when a folder is renamed
  on disk

### Changed
- Folder card grid layout is now compact (single-line label, 42px icon) and
  always shown as a grid, closer to native Nautilus folder cells
- Preferred Folders menu items renamed from "Add/Remove from Preferred" to
  "Pin to My Computer" / "Unpin from My Computer" across all surfaces (card,
  file view, pathbar menu)
- Removed the duplicate "Add/Remove from Bookmarks" item from the folder
  card right-click menu (kept only in the native file-view menu)

### Internal
- Split the monolithic entry file into `nautilus_my_computer/main.py` (app
  state and Nautilus integration), `common.py` (stateless helpers),
  `widgets.py` (`MyComputerDiskCard`/`MyComputerFolderCard`/
  `MyComputerCardSection`), and per-surface target modules (`bookmarks.py`,
  `preferred_folders.py`, `file_view_menu.py`); no behavior change
- Installer now handles the `nautilus_my_computer/` package directory as a
  distinct unit from the entry shim, and reads the local version from
  `nautilus_my_computer/__init__.py`

---

## v0.9.1
The "Open With…" row now does something.

### Added
- "Open With…" on local folder and disk cards now opens an app chooser
  matching native Nautilus's own picker (search, Recommended/Other Apps),
  attached to the Nautilus window, instead of always being greyed out

### Changed
- Preferred Folders card spacing increased from 16px to 24px between columns

### Fixed
- Turkish translation for "Open With…" was missing; added

### Docs
- README now documents the Preferred Folders group (added in v0.9.0), with a
  new screenshot and settings/feature list entries
  
---

## v0.9.0
One-click access to your everyday folders, right from the Computer view.

### Added
- A new "Preferred Folders" group at the top of the Computer panel, with
  cards for Home, Recent, Starred, Network, Documents, Downloads, Music,
  Videos, and Pictures, so you can jump straight to them without digging
  through the sidebar (issue #30)
- This group can be hidden if you'd rather not see it, and power users
  (or distributions shipping their own defaults) can customize which
  folders appear and in what order

---

## v0.8.4
Correct root disk detection on OSTree/bootc systems.

### Fixed
- On OSTree/bootc systems (e.g. stillOS), the disk view could show `/etc` or
  `/var` instead of a proper root disk, since `/proc/mounts` exposes those as
  separate implementation mounts. The root card now reports the real writable
  backing filesystem, and `/etc`, `/var`, and `/sysroot` are hidden from the
  disk list (credit @PizzaLovingNerd, stillOS, PR #44)

---

## v0.8.3
Raises the Python floor to match what the extension actually requires, and refreshes a doc screenshot.

### Changed
- Minimum Python bumped from 3.9 to 3.12. The extension already required
  libadwaita 1.5 (`Adw.PreferencesDialog`) and GNOME 46, so no distro that
  can run it ships Python older than 3.11; the 3.9 floor was no longer
  accurate
- Updated the custom bookmark icons screenshot in `assets/images/`

---

## v0.8.2
Translation prep for the bookmark icon picker.

### Internationalization
- Added translatable strings for the bookmark "Change Icon" menu item and
  picker dialog (Icon, Search icons…, Reset); French translated, other
  languages left for contributors to fill in

---

## v0.8.1
Lets distros and power users set a different default icon for the Computer entry.

### Added
- The Computer icon (in the sidebar and the address bar) can now be changed
  via GSettings, instead of always being the default `computer-symbolic`
  icon. Changes apply instantly, no restart needed. This is mainly useful
  for Linux distributions that want to ship their own icon by default.

---

## v0.8.0
Custom icons for bookmarks, with native right-click integration.

### Added
- Right-click "Change icon" on any sidebar bookmark, with a symbolic icon
  picker dialog; the chosen icon persists across Nautilus restarts and is
  pinned against Nautilus's async icon overwrites (issue #23)

---

## v0.7.13

### Fixed
- Opening a folder whose name contains "Computer" (e.g. an album folder named
  "OK Computer") no longer shows the My Computer view instead of the folder's
  contents; detection now checks the actual `computer:///` location instead of
  matching the window title text (credit @funinkina, issue #38)

---

## v0.7.12

### Fixed
- The right-click menu no longer offers Unmount or Format on system and home
  partitions, including the EFI partition (credit @mendres82, PR #34)
- Drives mounted manually or via fstab no longer show an Unmount option that
  didn't actually work

---

## v0.7.11

### Internationalization
- Hungarian translation (credit @pelach, PR #42)
- Turkish translation completed, filling in previously untranslated strings
  (credit @TaylanTatli, PR #43)

---

## v0.7.10

### Internationalization
- Russian translation (credit @fish-dd, PR #40)
- German translation fixes for consistency (credit @crian, PR #39)

---

## v0.7.9

### Fixed
- Disk cards required a double-click to open even when Nautilus's "Single click
  to open items" preference was set; cards now follow the `click-policy` GSettings
  key live, the same way they already follow grid/list view mode (credit
  @MoshiurRahmanAdib, issue #28)

---

## v0.7.8

### Internationalization
- German translation (credit @mendres82, PR #32; credit @crian, PR #25)
- Korean translation (credit @Saintliy, PR #20)
- Turkish translation (credit @TaylanTatli, PR #36)
- Sidebar - Places strings (Home, Recent, Starred, Trash) added across Arabic, Catalan,
  Spanish, French, Italian, Korean, Portuguese, and Turkish, bringing every language file
  to full parity with the current string set
- `"Network Volumes"` renamed to `"Network"` across all language files to match the
  current sidebar/group naming

### Fixed
- German `msgid "Network"` previously fell back to the untranslated source string after
  the rename; now reads "Netzwerk"

---

## v0.7.7
Installer fixes for package-manager detection, plus more detailed system info.

### Fixed
- Installer picked `apt` over `dnf` on Fedora systems that have `apt`/`dpkg`
  installed for unrelated `.deb` tooling, causing the install to fail
  outright (credit @sam-eon, issue #27)
- openSUSE package name for the Nautilus Python bindings was outdated for
  current Tumbleweed (credit @mendres82, PR #33); installer now resolves the
  correct name across Tumbleweed, Leap, and future Python version bumps
- Installer could abort entirely on Linux distributions not explicitly listed
  in its detection table, instead of falling back to binary-presence detection
- Installer could silently overwrite its own `--version` argument when reading
  `/etc/os-release` on distributions that define a `VERSION` key

### Changed
- Installer's System section now shows the detected distribution name and
  version numbers for the package manager, `nautilus-python`, and `gettext`
- Sidebar Computer group now shows all places by default (Home, Recent, Starred,
  Network, Trash) instead of hiding Recent, Starred, and Network; users can still
  toggle visibility via settings

---

## v0.7.6
Icon-accuracy fixes for disk cards.

### Fixed
- Disc-group cards (mounted ISO images) now always show the optical disc icon
  instead of a generic removable-media icon, since `udisks` tags loop-mounted
  ISOs the same way it tags plain removable drives
- A drive icon wrapped in an emblem (e.g. a readonly/encrypted badge) with no
  resolvable base icon name no longer renders blank; it now falls back to the
  group's default icon

---

## v0.7.5
Fixes a grid-view column collapse caused by long disk/device names.

### Fixed
- A long disk or device name (e.g. an MTP device path) could inflate every card's
  width and collapse the grid view down to fewer columns than normal

### Changed
- List view now uses its own row layout, separate from the grid card: icon, name,
  free/total text, and a wider usage bar on a single full-width line

---

## v0.7.4
Small polish release for the Computer sidebar button.

### Fixed
- Computer sidebar button now stays selected while the Computer panel is open

### Changed
- Cleaned up internal sidebar code names and small maintenance details

---

## v0.7.3
Stability release: reworks sidebar place handling to rely on native Nautilus rows
wherever possible, removing a class of duplicate-row and flicker bugs.

### Changed
- Sidebar places (Home, Recent, Starred, Network, Trash) are no longer rebuilt as
  custom rows. They stay fully native (icons, tooltips, context menus, drag-and-drop,
  trash-full icon all maintained by Nautilus); sidebar-show-* settings now toggle the
  visibility of the native row directly
- Only the Computer row is still custom-built, in its own section above the native
  sidebar list

### Fixed
- Duplicate Trash (and other native place) rows could appear after Nautilus reordered
  its sidebar list (device mount/unmount, bookmark changes), caused by a positional
  CSS hide rule that did not survive reorders
- Computer row briefly disappearing during bookmark drag-and-drop
- Computer row incorrectly accepting file drops

---

## v0.7.2
### Docs
- Updated README and new content and screenshots/visuals

---

## v0.7.1
### Added
- Drag-and-drop visual feedback on sidebar places: rows that aren't valid drop destinations grey out while a file drag is over the sidebar, matching native Nautilus behaviour
- Home accepts file drops (copy/move via Nautilus's own FileOperations2 D-Bus, with native progress and undo)

### Fixed
- Custom sidebar places now grey out in sync with Nautilus's native rows during a drag, instead of only one group reacting

---

## v0.7.0
### Added
- Custom Location group on the sidebar, replacing Nautilus's native LOCATIONS section with a LOCATIONS-driven architecture
- Context menus on all sidebar locations (Computer, Home, Recent, Starred, Network, Trash): Open, Open in New Tab, Open in New Window, plus location-specific actions (Properties, File History Settings, Trash Settings, Empty Trash)
- Sidebar visibility settings: per-location toggles to show or hide each entry in the location group (Home, Recent, Starred, Network, Trash) (credit @Aeternitae, issue #18)

### UX
- Settings: "Visibility" group renamed to "Panel visibility" for clarity
- Settings: new "Sidebar visibility" group added after Panel visibility

## v0.6.0
### UX
- make Computer button on sidebar compatible with native style and custom GTK4 CSS styles
- make Computer view panel compatible with native style and custom GTK4 CSS styles


## v0.5.4
### i18n
- i18n: finalize all UI strings in the Computer view and Settings panel
- i18n: update and complete translations for all supported languages

---

## v0.5.3
### i18n
- Disk size units now use `GLib.format_size()` for locale-aware output (e.g. "Ko / Mo / Go" in French, "octet" for bytes) - no custom unit translations needed

---

## v0.5.2
### UX
- Reduced vertical spacing between group labels and their disk cards in the Computer view

### Fixed
- Computer sidebar row icon and label now align with native Nautilus rows (Home, Recent, etc.)

### Maintenance
- Sidebar design values (icon gap, row inset padding) moved to the centralized CSS block

---

## v0.5.1
### UX
- Settings page labels and descriptions improved across all sections
- Visibility section now includes a description explaining Visible, Merged, and Hidden
- "Show system partitions" toggle moved to the bottom of the Visibility group
- "Disk Usage Color" renamed to "Usage Bar Color" with a short description
- Group names simplified to location-style: Removable, Disc, Network (was: Removable Devices, Disc Images, Network Volumes)
- "On this Computer" removed from visibility controls - it is always visible as the merge target

---

## v0.5.0
### Added
- New "System" group separating root, boot, EFI, and swap from regular drives
- Per-group visibility control: each group can be Visible, Merged into "On this computer", or Hidden
- "Show system partitions" toggle to include boot and EFI entries in the System group (default off)
- Sort-by-type ordering in the merged "On this computer" view: System first, then local drives, then removable, disc, network
- `DiskGroup` dataclass encapsulating group logic and state

### Changed
- Five groups total: System, On this computer, Removable Devices, Disc Images, Network Volumes
- Settings: replaced `hide-system-partitions` with `show-system-partitions` (inverted, same default behavior)

### Fixed
- USB drives running a Linux system (iso9660 filesystem) now correctly appear in Removable Devices instead of Disc Images
- Loop-mounted ISO images continue to appear in Disc Images regardless of mount path

---

## v0.4.6
### Fixed
- Installer `--branch` and `--version` are now independent axes, not mutually exclusive
- Bad branch falls back to `main`, bad version falls back to latest tag - no hard errors
- Version resolution now uses git tags instead of GitHub Releases, so tags always resolve
- Install type section always shows `Source`, `Branch`, and `Version` lines
- Local installs show current branch and version from the local file, not arg values

## v0.4.5
### Fixed
- Installer fully POSIX compliant - removed all `local` keyword usage
- Installer uses CLI flags (`--version=`, `--branch=`) instead of env vars, which do not survive `curl | sh` pipes
- `--version` and `--branch` validated as mutually exclusive
- `--branch` probed early, fails hard on unknown branch name
- `apt` package detection uses `dpkg-query` to avoid false positives on partially-removed packages
- GitHub API fallback to `main` now prints a visible warning instead of silently proceeding
- Installer skips `sudo` when already running as root
- `--version`/`--branch` flags produce a warning on local installs instead of silently doing nothing

### Maintenance
- Extracted `SCHEMA_ID`, `GETTEXT_DOMAIN`, `PYCACHE_GLOB` constants - all derived names have a single source of truth

## v0.4.4
### Fixed
- Installer (`install.sh`) now POSIX `sh` compliant, fixes `curl | sh` failing on systems where `/bin/sh` is `dash` (e.g. Debian, Ubuntu)

## v0.4.3
### UX
- Fixed a brief flicker of the file view when navigating to Computer

## v0.4.2
### UX
- Panel now opens in ~20-65ms instead of ~500-600ms

## v0.4.1
### Internationalization
- Updated Arabic translations (credit @e6ad2020)
- Updated French translations

## v0.4.0
### Added
- Italian, Spanish, Catalan and Portuguese translations (credit @unaibenidorm)
- Non-interactive installer with `curl | sh`, `VERSION` and `BRANCH` env vars (credit @sour-source)

### Fixed
- Disk cards not updating when drives are connected or disconnected
- Disk cards not updating during active file transfers
- Level bar gradient not rendering on Ubuntu 22.04 LTS and other GTK 4.6.x systems
- Crash on startup when Nautilus opens directly to a folder (credit @e6ad2020, PR #10)
- Navigation crash on pathbar (credit @unaibenidorm, @e6ad2020, issue #11)

## v0.3.1
### Fixed
- Crash on startup when `~/Templates` is non-empty (issue #4)
- Level bar gradient direction incorrect in RTL languages (credit @e6ad2020)

### Internationalization
- Arabic translation for Disc Images group (credit @e6ad2020)

## v0.3.0
### Added
- Native Computer button in the left sidebar, replacing the bookmark approach
- Right-click context menu on the Computer sidebar button (Open, Open in New Tab, Open in New Window, Settings)
- Computer sidebar button highlights when Computer view is active

### Removed
- Bookmark-based sidebar entry and all related code

## v0.2.1
### Fixed
- Installer now aborts cleanly when a release is missing (credit @sour-source)
- Missing icon for mounted ISO images (credit @sour-source)

## v0.2.0
### Added
- Internationalization support (i18n)
- Arabic translations (credit @e6ad2020)
- French translations

### Fixed
- Nautilus inherits terminal locale on restart instead of GNOME session locale
