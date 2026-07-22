Name:           nautilus-my-computer
Version:        0.12.4
Release:        1%{?dist}
Summary:        My Computer for Nautilus, what GNOME Files should have always been

License:        MIT
URL:            https://github.com/yannmasoch/nautilus-my-computer
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  gettext
BuildRequires:  make
BuildRequires:  python3

Requires:       nautilus-python
Requires:       libadwaita

%description
My Computer is a custom view for GNOME Files (Nautilus), showing all your
drives, volumes, and network mounts with usage levels in one clean panel:
System, Devices and Drives, Removable Devices, and Network Volumes, with
usage bars, context menus, live refresh, and mount/unmount/eject actions.

%prep
%autosetup

%build
make build

%install
%make_install
%find_lang %{name}

%files -f %{name}.lang
%license LICENSE
%{_datadir}/nautilus-python/extensions/nautilus-my-computer.py
%{_datadir}/nautilus-python/extensions/nautilus_my_computer/
%{_datadir}/glib-2.0/schemas/io.github.yannmasoch.nautilus-my-computer.gschema.xml

%changelog
%autochangelog
