# Nix derivation for My Computer for Nautilus.
#
# Reuses the repository Makefile, the shared packaging entry point already used
# by the Fedora spec and the Arch PKGBUILD: `make build` compiles the gettext
# catalogs and byte-checks the sources, `make install` lays the plugin, schema
# and locales under $out/share. With PREFIX=$out and DESTDIR unset, the Makefile
# also runs glib-compile-schemas itself, so no extra postInstall is needed.
#
# Written as a callPackage-style function (src supplied by the caller) so the
# same file can be reused verbatim for a future nixpkgs submission.
{
  lib,
  stdenv,
  src,
  gettext,
  glib,
  python3,
  nautilus-python,
  libadwaita,
}:

stdenv.mkDerivation {
  pname = "nautilus-my-computer";
  # Keep in sync with pyproject.toml, the Fedora spec and the Arch PKGBUILD.
  version = "0.12.4";

  inherit src;

  nativeBuildInputs = [
    gettext # msgfmt, for the .po -> .mo catalogs (make build)
    glib # glib-compile-schemas (make install)
    python3 # py_compile syntax check (make check)
  ];

  buildInputs = [
    nautilus-python # runtime: the plugin host
    libadwaita # runtime: Adw widgets used by the panel
  ];

  makeFlags = [ "PREFIX=${placeholder "out"}" ];

  # glib's setup hook relocates the compiled schema to
  # share/gsettings-schemas/<name>/glib-2.0/schemas during fixup. That is correct
  # on NixOS (aggregated system-wide) but sits off the default GSettings search
  # path everywhere else, so a plain `nix profile install` on a non-NixOS distro
  # would not find it. Move it back to the standard share/glib-2.0/schemas, which
  # is on XDG_DATA_DIRS for ~/.nix-profile, so the extension works cross-distro.
  postFixup = ''
    if [ -d "$out/share/gsettings-schemas" ]; then
      mkdir -p "$out/share/glib-2.0"
      mv "$out"/share/gsettings-schemas/*/glib-2.0/schemas "$out/share/glib-2.0/schemas"
      rm -rf "$out/share/gsettings-schemas"
    fi
  '';

  meta = {
    description = "My Computer for Nautilus, what GNOME Files should have always been";
    homepage = "https://github.com/yannmasoch/nautilus-my-computer";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
