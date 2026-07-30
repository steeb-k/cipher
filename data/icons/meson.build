datadir = join_paths('share', 'icons')
icondir = join_paths('hicolor', 'scalable', 'apps')
# The installed name must match application_id, since that is what
# set_default_icon_name() and the desktop file's Icon key both use. Upstream
# could omit the rename because the source filenames already matched its
# application ID; ours do not.
if get_option('profile') == 'development'
  install_data(
    join_paths(icondir, 'org.gnome.World.Secrets.Devel.svg'),
    install_dir: join_paths(datadir, icondir),
    rename: '@0@.svg'.format(application_id),
  )
else
  install_data(
    join_paths(icondir, 'org.gnome.World.Secrets.svg'),
    install_dir: join_paths(datadir, icondir),
    rename: '@0@.svg'.format(application_id),
  )
endif

symbolicdir = join_paths('hicolor', 'symbolic', 'apps')
install_data(
  join_paths(symbolicdir, 'org.gnome.World.Secrets-symbolic.svg'),
  install_dir: join_paths(datadir, symbolicdir),
  rename: '@0@-symbolic.svg'.format(application_id),
)
