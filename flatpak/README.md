# Update python3-zxcvbn-rs-py-sources.json

Clone the project [zxcvbn-rs-py](https://github.com/fief-dev/zxcvbn-rs-py), checkout to the respective tag:

    git clone https://github.com/fief-dev/zxcvbn-rs-py
    git -C zxcvbn-rs-py checkout TAG
    flatpak-cargo-generator zxcvbn-rs-py/Cargo.lock \
    -o flatpak/python3-zxcvbn-rs-py-sources.json

# Update python3-validators.json

    flatpak-pip-generator validators -o flatpak/python3-validators

# Update python3-pykeepass.json

    flatpak-pip-generator --build-isolation pykeepass -o flatpak/python3-pykeepass

Then add `"pykeepass-build-sources.json"` as a source and `--ignore-installed` to the `pip3` command.

    jq --indent 4  '.sources += ["pykeepass-build-sources.json"]' flatpak/python3-pykeepass.json > flatpak/python3-pykeepass.json.tmp && mv --force flatpak/python3-pykeepass.json{.tmp,}

# Update python3-pykcs11-sources.json

    flatpak-pip-generator pykcs11 -o flatpak/python3-pykcs11-sources

# Update python3-pyhibp.json

    flatpak-pip-generator pyhibp -o flatpak/python3-pyhibp

# Update python3-pynacl.json

PyNaCl ships prebuilt manylinux wheels that bundle libsodium, so unlike the
other compiled dependencies it needs no build step -- but the wheels are
per-architecture, so the module lists one behind `only-arches` for each
architecture we build, the same way maturin.json does. `flatpak-pip-generator`
only emits the host architecture's wheel, so the second entry has to be added
by hand. Take the `cp38-abi3` manylinux2014 wheels, which are ABI-stable across
Python versions and target a glibc old enough for any runtime we use:

    curl -s https://pypi.org/pypi/pynacl/json | \
    jq -r '.releases[.info.version][]
           | select(.filename | test("cp38-abi3-manylinux2014"))
           | "\(.filename)\n  \(.url)\n  \(.digests.sha256)"'

`--no-deps` in the build command is deliberate: PyNaCl depends on cffi, which
python3-pykeepass.json already builds for argon2.

