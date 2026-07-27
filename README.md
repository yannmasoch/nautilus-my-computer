<div align="center">

# My Computer for Nautilus

My Computer for Nautilus, what GNOME Files should have always been

<br>

<img width="1366" alt="My Computer for Nautilus" src="assets/images/screenshot.png" />

<br>

**My Computer** is a custom view for Nautilus (GNOME Files), showing all your drives, volumes, and network mounts with usage levels in one clean panel.

*"GNOME dropped the Other Locations view and left nothing in its place. I built what should have always been there, and the GNOME community made it even better with ❤️"*

<br>

<a href="https://itsfoss.com/nautilus-my-computer/" target="_blank" rel="noopener noreferrer"><img src="https://itsfoss.com/assets/images/badges/itsfoss-linux-app-of-the-week-badge-dark.png" alt="It's FOSS Linux App of the Week" width="40%"></a>

</div>

## Installation

<div align="center">

**Shipped by default in stillOS**

<a href="https://stillhq.io" target="_blank" rel="noopener noreferrer"><img src="assets/logos/stillos-wordmark.svg" alt="stillOS" height="48"></a>

*"Maintenance-Free Linux"*

<br>

**Packaged for**

<a href="https://launchpad.net/~yannmasoch/+archive/ubuntu/nautilus-my-computer" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/Ubuntu%20PPA-E95420?style=flat&logo=ubuntu&logoColor=white" alt="Ubuntu PPA" height="22"></a>
<a href="https://copr.fedorainfracloud.org/coprs/yannmasoch/nautilus-my-computer/" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/Fedora%20COPR-3C6EB4?style=flat&logo=fedora&logoColor=white" alt="Fedora COPR" height="22"></a>
<a href="https://build.opensuse.org/package/show/home:yannmasoch/nautilus-my-computer" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/openSUSE%20OBS-73BA25?style=flat&logo=opensuse&logoColor=white" alt="openSUSE OBS" height="22"></a>
<a href="https://aur.archlinux.org/packages/nautilus-my-computer" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/Arch%20AUR-1793D1?style=flat&logo=archlinux&logoColor=white" alt="Arch AUR" height="22"></a>
<a href="https://github.com/yannmasoch/nautilus-my-computer/tree/main/packaging/nix" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/Nix%2FNixOS-5277C3?style=flat&logo=nixos&logoColor=white" alt="Nix / NixOS" height="22"></a>

</div>

Expand the section for your distribution for instructions. No native package? Use the universal script. Nothing is written outside your home directory.

<details>
<summary><img src="https://img.shields.io/badge/-E95420?style=flat&logo=ubuntu&logoColor=white" alt="Ubuntu" height="18" style="vertical-align: middle;"> <b>Ubuntu</b> (PPA)</summary>

```bash
sudo add-apt-repository ppa:yannmasoch/nautilus-my-computer
sudo apt update
sudo apt install nautilus-my-computer
```

Currently supported series: 26.04 LTS (resolute) and 26.10 (stonking). Non-LTS Ubuntu releases
only get about 9 months of support, so this list moves forward as older series reach end of life.

</details>

<details>
<summary><img src="https://img.shields.io/badge/-3C6EB4?style=flat&logo=fedora&logoColor=white" alt="Fedora" height="18" style="vertical-align: middle;"> <b>Fedora</b> (COPR)</summary>

```bash
sudo dnf copr enable yannmasoch/nautilus-my-computer
sudo dnf install nautilus-my-computer
```

</details>

<details>
<summary><img src="https://img.shields.io/badge/-73BA25?style=flat&logo=opensuse&logoColor=white" alt="openSUSE" height="18" style="vertical-align: middle;"> <b>openSUSE</b> (OBS)</summary>

**Tumbleweed**

```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:yannmasoch/openSUSE_Tumbleweed/home:yannmasoch.repo
sudo zypper refresh
sudo zypper install nautilus-my-computer
```

**16.0**

```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:yannmasoch/16.0/home:yannmasoch.repo
sudo zypper refresh
sudo zypper install nautilus-my-computer
```

</details>

<details>
<summary><img src="https://img.shields.io/badge/-1793D1?style=flat&logo=archlinux&logoColor=white" alt="Arch" height="18" style="vertical-align: middle;"> <b>Arch Linux</b> (AUR)</summary>

```bash
yay -S nautilus-my-computer
```

</details>

<details>
<summary><img src="https://img.shields.io/badge/-5277C3?style=flat&logo=nixos&logoColor=white" alt="NixOS" height="18" style="vertical-align: middle;"> <b>Nix / NixOS</b></summary>

```bash
nix profile install "github:yannmasoch/nautilus-my-computer?dir=packaging/nix"
```

</details>

<details>
<summary><img src="https://img.shields.io/badge/-555555?style=flat&logo=git&logoColor=white" alt="Git" height="18" style="vertical-align: middle;"> <b>Universal install script</b> (any distro)</summary>

- **Directly install from remote**

  ```bash
  curl -fsSL https://raw.githubusercontent.com/yannmasoch/nautilus-my-computer/main/install.sh | sh
  ```

  Uninstall:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/yannmasoch/nautilus-my-computer/main/install.sh | sh -s -- --uninstall
  ```

<br>

- **Or, install from local clone**

  Clone the repo and run the installer locally. This installs from the local files
  with no downloads, so it avoids GitHub rate-limits:

  ```bash
  git clone https://github.com/yannmasoch/nautilus-my-computer.git
  cd nautilus-my-computer
  ./install.sh
  ```

  Uninstall:

  ```bash
  ./install.sh --uninstall
  ```

  > [!NOTE]  
  >
  > Run it as `./install.sh` (not `bash install.sh`) so it installs from your local clone instead of downloading.

</details>

<br>

See tested distributions/Nautilus versions [here](#tested-on).

## Features

My Computer adds a native-feeling "Computer" panel to GNOME Files that shows drives, volumes, removable media, and network mounts grouped by type; including usage bars, mount/eject controls, and a "Preferred Folders" section where you can pin folders and other locations. It also adds customizable visibility and sidebar options, custom bookmark icon options, and a new Column View (Beta).

### Sidebar integration

Computer sits at the top of the GNOME Files sidebar; click to open the panel and right-click for more options.

![Computer button](assets/images/computer-button.png)

### Preferred Folders

A "Preferred Folders" group sits at the top of the Computer view, giving you one-click access to your everyday folders without digging through the sidebar.

This group can be hidden if you'd rather not see it. Pin or unpin any folder as you want. You can drag the Preferred Folders to reorder them.

![Preferred Folders](assets/images/preferred-folders.png)

Captions follow native Nautilus captions; show or hide them per your preference.

![Preferred Folders captions](assets/images/preferred-folders-captions.png)

### Groups visibility

My Computer organizes your storage into five groups:

- **System:** root, boot, EFI, and swap partitions
- **On this Computer:** your internal drives and partitions
- **Removable:** USB drives, phones, cameras, and removable media
- **Disc:** optical drives and mounted ISO images
- **Network:** network shares and remote filesystems

Each group (except **On this Computer**) has three visibility settings, configurable from the right-click menu on the Computer button:

- **Visible**: shown as its own labeled section
- **Hidden**: removed from the panel entirely
- **Merged**: folded into **On this Computer**, keeping everything in one flat list

Make the **My Computer** view your own, and organize it to show only what matters to you.

![Groups Visibility](assets/images/groups-visibility.png)

### Sidebar customization

The default GNOME Files sidebar shows every built-in location whether you use it or not. My Computer gives you the visibility control of each one, so you can keep only what you actually need.

**Computer** is always shown, it's the whole point of the extension. Everything else is up to you, turn off what you never use and your sidebar stays short and clean.

![Sidebar Visibility](assets/images/sidebar-visibility.png)

#### Custom bookmark icons

Right-click any bookmark in the sidebar and choose "Change icon" to pick a custom symbolic icon from a searchable picker.

![Custom bookmark icons](assets/images/custom-bookmark-icons.png)

## Settings

Right-click on _Computer_ in the sidebar to access the settings.

![Computer button](assets/images/computer-button.png)

My Computer Settings let you:

- Open GNOME Files directly on the Computer view at startup
- Show or hide system partitions (root, boot, EFI, swap)
- Control the visibility of each group: visible, hidden, or merged into On this Computer
- Choose which sidebar locations are shown: Home, Recent, Starred, Network, Trash
- Choose which folders appear in Preferred Folders, and hide the row entirely
- Customize the disk usage bar color to match your style

![Settings Page](assets/images/settings-page.png)

> Settings are stored via GSettings under `io.github.yannmasoch.nautilus-my-computer` and persist across sessions.

## Column View (Public Beta)

Browse folders as Miller-style columns, a long-requested Nautilus feature that never shipped natively. Pick a folder in one column and its contents open in the next, so you can navigate several levels deep while keeping the whole path in view.

Column View sits alongside the native list and grid views as a third option on GNOME Files' own view-mode toggle (or press <kbd>Ctrl</kbd>+<kbd>3</kbd>).

![Column View](assets/images/column-view-screenshot.png)

The old two-state view-mode button is now a segmented Grid/List/Column switcher, so all three views are one click away instead of being buried behind a cycle button. Its native sorting and view-settings menu still lives right next to it, in its own "View Options" button.

![Column View toggle button](assets/images/column-view-toggle-button.png)

This is an early public beta, marked with a **Beta** badge in the view. The core browsing experience is solid, but it's still fresh and needs real-world use to surface edge cases. Found something rough? [Check details for Column View on GitHub](https://github.com/yannmasoch/nautilus-my-computer/issues/67).

## Style

My Computer uses native GNOME visuals throughout. The panel follows the system light/dark preference and respects your icon and GTK themes. You can also set the disk usage bar to follow the system accent color, or choose a custom color or gradient.

![Light and dark mode](assets/images/light-and-dark-mode.png)

![Color mode preview](assets/images/color-mode.png)

## Feature highlights

- **All your storage in one place:** local drives, USB sticks, phones, network mounts, and removable media grouped by type.
- **Usage bars:** at-a-glance capacity for every mounted volume.
- **Mount & eject:** mount, unmount, and eject volumes directly from the panel without leaving Files.
- **Live refresh:** the panel updates automatically when drives are connected or disconnected.
- **Fully native:** follows your GNOME accent color, icon theme, and dark/light mode with no extra configuration.
- **Customizable bars:** choose between GNOME accent color, a custom color, or a custom gradient for the usage bars.
- **Start on My Computer:** choose to open GNOME Files directly on the My Computer panel every time.
- **Right-click context menu:** open, open in new tab, open in new window, mount, unmount, and eject volumes directly from a native-feel context menu.
- **Groups visibility:** choose which storage groups are visible, hidden, or merged into one flat list.
- **Sidebar visibility:** choose which sidebar locations are shown: Home, Recent, Starred, Network, and Trash.
- **Custom bookmark icons:** right-click any bookmark to pick a custom symbolic icon from a searchable picker, persisted across restarts.
- **Preferred Folders:** one-click cards for your everyday folders at the top of the Computer panel, customizable via Settings or the "..." menu's "Pin to My Computer" item, and reorderable by drag-and-drop.
- **Open With:** pick an app to open a folder or disk with, from a native-feel app chooser.
- **Live icon theme updates:** disk and folder card icons refresh immediately when you switch icon themes, no restart needed.
- **Column View (public beta):** browse folders as Miller-style columns, a third view mode alongside list and grid, with its own context menu and previews.

## Tested on

| Distro                | GNOME Files | Status | File Picker      |
| --------------------- | ----------- | ------ | ---------------- |
| Arch                  | 50.2.2      | ✅     | ✅               |
| Fedora 44 Workstation | 50.2.2      | ✅     | ✅               |
| Fedora 44 Workstation | 50.0        | ✅     | ✅               |
| openSUSE Tumbleweed   | 50.2.2      | ✅     | ✅               |
| Ubuntu 26.04 LTS      | 50.0        | ✅     | ✅               |
| Fedora 41 Workstation | 47.0        | ✅     | ❌ Not supported |
| Fedora 42 Workstation | 48.0        | ✅     | ❌ Not supported |
| Zorin OS 18           | 46.4        | ☑️    | ❓ Not tested    |

> [!NOTE]  
>
> GNOME Files versions below 50 may have limited functionality. Full support targets GNOME Files 50+.

## Languages

My Computer is fully localized. The UI language is picked up automatically from your GNOME locale settings.

Both left-to-right (LTR) and right-to-left (RTL) layouts are supported; the panel mirrors its layout direction automatically when an RTL language is active.

Translations are managed on [Weblate](https://hosted.weblate.org/projects/nautilus-my-computer/):

[![Translation status](https://hosted.weblate.org/widget/nautilus-my-computer/matrix-auto.svg)](https://hosted.weblate.org/engage/nautilus-my-computer/)

Want to add your language or improve an existing translation? Join the project on Weblate, no `.po` editing or pull request needed.

## About

GNOME Files has no public API for adding custom views anymore. **My Computer** injects itself directly into the Nautilus widget tree at runtime, something no other extension currently does.

This unconventional approach is what makes it feel truly native, but it also means the extension relies on internal Nautilus structures that are not guaranteed to stay stable across versions.

Some integration points can break when GNOME Files changes its internal layout, as seen on Zorin OS 18.

These weak points are known and documented. Each release consolidates them further toward stability.

The goal is for My Computer to feel indistinguishable from a built-in feature and to stay that way across GNOME updates.

## Contributors

My Computer is community-built. These people have shaped what it is today.

[![Contributors](https://contrib.rocks/image?repo=yannmasoch/nautilus-my-computer)](https://github.com/yannmasoch/nautilus-my-computer/graphs/contributors)

Thanks also go to everyone who opened issues, reported bugs, and shared feedback on GitHub and social media. This project owes just as much to them as it does to the code.

Want to contribute? Check out [CONTRIBUTING.md](CONTRIBUTING.md).

## For GNOME community

**My Computer** is built by the GNOME community with ❤️ and your feedback is important!

*“They did not know it was impossible, so they did it”* ― Jean Cocteau (1954)
