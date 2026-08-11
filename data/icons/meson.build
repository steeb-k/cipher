datadir = join_paths('share', 'icons')

# The application icon, installed once as a scalable SVG: an SVG satisfies
# theme lookup at every size, so there is nothing to gain from rasterising it
# into a size ladder as well. There deliberately is no raster copy anywhere
# under hicolor -- a leftover PNG at some size would win the lookup there and
# the old artwork would keep appearing at exactly that size.
#
# The installed name must match application_id, since that is what
# set_default_icon_name() and the desktop file's Icon key both use.
appicondir = join_paths('hicolor', 'scalable', 'apps')
install_data(
  join_paths(appicondir, 'cipher.svg'),
  install_dir: join_paths(datadir, appicondir),
  rename: '@0@.svg'.format(application_id),
)

# The tray icon, installed scalable under its own name so it does not collide
# with the application icon above; it is only ever asked for by name from
# tray.py. Regenerate with tools/generate-tray-icons.py, which re-paints the
# symbolic icon rather than redrawing it.
trayicondir = join_paths('hicolor', 'scalable', 'apps')
install_data(
  join_paths(trayicondir, 'cipher-tray.svg'),
  install_dir: join_paths(datadir, trayicondir),
  rename: '@0@-tray.svg'.format(application_id),
)

# The same mark in each selectable colour, which is what tray.py actually asks
# for; the unsuffixed one above stays installed as the fallback for a setting
# that names a colour this build does not have.
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
  # Not selectable: the state a locked safe takes, whatever colour is chosen.
  'locked',
]
foreach colour : tray_icon_colours
  install_data(
    join_paths(trayicondir, 'cipher-tray-' + colour + '.svg'),
    install_dir: join_paths(datadir, trayicondir),
    rename: '@0@-tray-@1@.svg'.format(application_id, colour),
  )
endforeach

# The monochrome form of the same mark, drawn as flat fills so GTK can recolour
# it. Used for the empty-selection page, see unlocked_database.py, and it is
# also what the tray icons above are painted from.
symbolicdir = join_paths('hicolor', 'symbolic', 'apps')
install_data(
  join_paths(symbolicdir, 'cipher-symbolic.svg'),
  install_dir: join_paths(datadir, symbolicdir),
  rename: '@0@-symbolic.svg'.format(application_id),
)
