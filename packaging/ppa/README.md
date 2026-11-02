# Ubuntu PPA packaging

Source packaging for `ppa:yannmasoch/nautilus-my-computer`, targeting only currently-supported
series: resolute (26.04 LTS, GNOME 50) and stonking (26.10). Non-LTS Ubuntu
releases only get ~9 months of support, so older series (noble, oracular, plucky, questing)
either ship GNOME older than this extension targets or are already EOL - Launchpad rejects
uploads to EOL series outright ("obsolete and will not accept new uploads"). Revisit this list
whenever a new series ships.

## One-time setup (maintainer machine)

```shell
sudo apt install build-essential devscripts dpkg-dev debhelper gnupg
```

A GPG key must be configured and registered with your Launchpad account (`debuild` signs the
source package with it; unsigned uploads are rejected by Launchpad).

## Build and upload

```shell
packaging/ppa/build-and-upload.sh 0.11.2            # build only, output in ./ppa-build
packaging/ppa/build-and-upload.sh 0.11.2 --upload    # build and dput to the PPA
```

This builds one source package per target series from a tarball of the current checked-out
commit, each with a series-suffixed version (`0.13.3-1~ubuntu26.04.1`, `0.13.3-1~ubuntu26.10.1`).
Launchpad rejects uploading a version that already exists in another series of the same PPA,
so a single shared version only ever gets published to the first series (dput still succeeds;
the rejection arrives by email). The `.orig.tar.gz` is created once and shared, since
Launchpad's pool is shared across every series in a PPA and rejects re-uploading the same
filename with different contents. Tag and push the release commit first, since the build
tarball must match what actually gets built.

## Layout

- `debian/control` - package metadata and dependencies (`python3-nautilus`, `gir1.2-adw-1`, ...)
- `debian/rules` - delegates to the same `make build` / `make install` used by the AUR, Fedora,
  and openSUSE packages
- `debian/changelog` - base entry; `build-and-upload.sh` rewrites its version and distribution
  per series at build time (see above)
- `debian/copyright` - DEP-5 format, MIT license
- `debian/watch` - `uscan` rule tracking GitHub release tags

## Local build sanity check (no Launchpad needed)

```shell
cp -r packaging/ppa/debian ./debian
dpkg-buildpackage -us -uc -b
rm -rf ./debian ../nautilus-my-computer_*.deb ../nautilus-my-computer_*.buildinfo ../nautilus-my-computer_*.changes
```

Installs identically to the other distros (`nautilus-my-computer.py` and `nautilus_my_computer/`
under `/usr/share/nautilus-python/extensions/`, schema under `/usr/share/glib-2.0/schemas/`).
