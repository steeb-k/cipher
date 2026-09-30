# Cipher
<img src="data/icons/hicolor/scalable/apps/cipher.svg" width="128" height="128" />
<p>Manage your passwords</p>

Cipher is a password manager for the KeePass v.4 format. It integrates with
the GNOME desktop, provides an easy and uncluttered interface for the
management of password databases, and connects to Firefox and Chromium
through its own browser extension, [Cipher Bridge](https://github.com/steeb-k/cipher-browser).

Cipher is based on [GNOME Secrets](https://gitlab.gnome.org/World/secrets);
see [Origins](#origins) below.

<img src="screenshots/screenshot-1.png" width="800px" />

## Features
* ⭐ Create or import KeePass safes
* 🌐 Fill and save logins in Firefox and Chromium through Cipher Bridge
* 🖼 Website icons for entries, fetched from the site itself and stored in the safe
* 🎨 A tray icon that shows the lock state, in a colour of your choosing
* ✨ Assign a color and additional attributes to entries
* 📎 Add attachments to your encrypted database
* 🎲 Generate cryptographically strong passwords
* 🛠 Change the password or keyfile of your database
* 🔎 Quickly search your favorite entries
* 🕐 Automatic database lock during inactivity
* 📲 Adaptive interface
* ⏱ Support for two-factor authentication

### Supported Encryption Algorithms
* AES 256-bit
* Twofish 256-bit
* ChaCha20 256-bit

### Supported Derivation Algorithms
* Argon2 KDBX4
* Argon2id KDBX4
* AES-KDF KDBX 3.1

## Installing
Cipher is packaged as a Flatpak. To build and install it from this checkout
you need [flatpak-builder](https://flathub.org/apps/org.flatpak.Builder) and
the GNOME 50 SDK:

```
flatpak install --user flathub org.gnome.Sdk//50 org.gnome.Platform//50
flatpak run org.flatpak.Builder --user --install --force-clean \
    --state-dir="$HOME/.cache/cipher-flatpak/state" \
    "$HOME/.cache/cipher-flatpak/build" flatpak/io.github.steeb_k.Cipher.json
```

To install into `~/.local` without Flatpak instead, `tools/install-local.sh`
builds with Meson and installs there; `tools/dev-run.sh` does the same into a
throwaway prefix and launches the result.

## Browser integration
Cipher speaks the KeePassXC browser protocol over a Unix socket, so the
[Cipher Bridge](https://github.com/steeb-k/cipher-browser) extension talks to
it the way KeePassXC-Browser talks to KeePassXC. Integration is off by
default:

```
gsettings set io.github.steeb_k.Cipher browser-integration true
```

The extension's repository explains how to install it and how to register
the native messaging host the browser launches to reach Cipher.

## Building locally
Cipher uses the Meson build system:
```
meson setup _build
ninja -C _build
ninja -C _build install
```

## Contributing
Bug reports and feature requests go to the
[issue tracker](https://github.com/steeb-k/cipher/issues). Pull requests are
welcome. Translations are maintained in `po/`; a pull request with an updated
`.po` file is the way to contribute one.

## Origins
Cipher is a fork of [GNOME Secrets](https://gitlab.gnome.org/World/secrets)
by Falk Alexander Seidl and its contributors, and keeps its history. The
browser extension, Cipher Bridge, is a fork of
[KeePassXC-Browser](https://github.com/keepassxreboot/keepassxc-browser) by
the KeePassXC Team. Both are licensed under the GPL-3.0; see
[LICENSE](LICENSE).
