datadir = join_paths('share', 'icons')

# The application icon is a raster image, so it is installed at each size an
# icon theme looks for rather than once as a scalable SVG. The upstream
# scalable/ SVGs are deliberately no longer installed: an SVG satisfies theme
# lookup at every size, so leaving it in place would shadow these entirely and
# the old artwork would keep appearing.
#
# The installed name must match application_id, since that is what
# set_default_icon_name() and the desktop file's Icon key both use. Regenerate
# with tools/generate-app-icon.sh after replacing data/icons/appIcon.png.
icon_sizes = ['16', '24', '32', '48', '64', '128', '256', '512']
foreach size : icon_sizes
  sizedir = join_paths('hicolor', size + 'x' + size, 'apps')
  install_data(
    join_paths(sizedir, 'cipher.png'),
    install_dir: join_paths(datadir, sizedir),
    rename: '@0@.png'.format(application_id),
  )
endforeach

# The tray icon, installed scalable under its own name. Installing it under a
# distinct name is what keeps the caveat above from applying: an SVG named
# after the application ID would satisfy theme lookup at every size and shadow
# the raster icon, but this one is only ever asked for by name from tray.py.
trayicondir = join_paths('hicolor', 'scalable', 'apps')
install_data(
  join_paths(trayicondir, 'cipher-tray.svg'),
  install_dir: join_paths(datadir, trayicondir),
  rename: '@0@-tray.svg'.format(application_id),
)

# The same mark in each selectable colour, which is what tray.py actually asks
# for; the unsuffixed one above stays installed as the fallback for a setting
# that names a colour this build does not have. Regenerate with
# tools/generate-tray-icons.py, which re-paints the file above rather than
# redrawing it.
tray_icon_colours = [
  'pink',
  'blue',
  'green',
  'yellow',
  'orange',
  'red',
  'purple',
  'brown',
  'monochrome',
]
foreach colour : tray_icon_colours
  install_data(
    join_paths(trayicondir, 'cipher-tray-' + colour + '.svg'),
    install_dir: join_paths(datadir, trayicondir),
    rename: '@0@-tray-@1@.svg'.format(application_id, colour),
  )
endforeach

# Still an SVG, and necessarily so: a symbolic icon is recoloured by GTK, which
# a raster cannot support. Drawn to match the keyhole in appIcon.png rather than
# traced from it. Used for the empty-selection page, see unlocked_database.py.
symbolicdir = join_paths('hicolor', 'symbolic', 'apps')
install_data(
  join_paths(symbolicdir, 'cipher-symbolic.svg'),
  install_dir: join_paths(datadir, symbolicdir),
  rename: '@0@-symbolic.svg'.format(application_id),
)
