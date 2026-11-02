# Security Policy

## Supported versions

Only the latest release of **My Computer for Nautilus** receives security fixes. Please update
before reporting, in case the problem is already fixed.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub:

1. Go to the [Security tab](https://github.com/yannmasoch/nautilus-my-computer/security).
2. Click **Report a vulnerability**.
3. Describe the problem, the version you tested, and how to reproduce it.

Only you and the maintainers can see the report. We will work with you on a fix and credit you in
the release notes, unless you prefer to stay anonymous.

## Scope

This extension runs inside the Nautilus process, so a flaw can affect your files and session.
Examples worth reporting:

- Running unintended commands or code, for example through crafted file, mount or network names.
- Exposing or altering files outside what the user chose.
- Weaknesses in the install script or the published packages.

Bugs that only crash or glitch the interface are not security issues, please use the
[bug report form](https://github.com/yannmasoch/nautilus-my-computer/issues/new?template=bug_report.yml).
Vulnerabilities in Nautilus, GTK or GNOME themselves should go to those projects.
