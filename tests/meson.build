envs = [
  'G_TEST_SRCDIR=' + meson.current_source_dir(),
  'G_TEST_BUILDDIR=' + meson.current_build_dir(),
]

test('Test Element', python_bin, args: files('test_element.py'), env: envs)

test(
  'Test Password Generator',
  python_bin,
  args: files('test_password_generator.py'),
  env: envs,
)

# Standalone rather than pytest-based, so they run wherever PyNaCl is available.
test(
  'Test Browser Protocol',
  python_bin,
  args: files('browser_protocol_check.py'),
  env: envs,
)

test(
  'Test Browser Backend',
  python_bin,
  args: files('browser_backend_check.py'),
  env: envs,
)

test(
  'Test Custom Icons',
  python_bin,
  args: files('custom_icons_check.py'),
  env: envs,
)

test(
  'Test Favicon',
  python_bin,
  args: files('favicon_check.py'),
  env: envs,
)
