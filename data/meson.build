message('Compiling resources')

metainfo_conf = configuration_data()
metainfo_conf.set('APPID', application_id)
metainfo_conf.set('GETTEXT_PACKAGE', meson.project_name())
metainfo_conf.set('NAME', project_name)
metainfo = i18n.merge_file(
  input: configure_file(
    input: files('org.gnome.World.Secrets.metainfo.xml.in.in'),
    output: 'org.gnome.World.Secrets.metainfo.xml.in',
    configuration: metainfo_conf,
  ),
  output: application_id + '.metainfo.xml',
  install: true,
  install_dir: join_paths(join_paths('share'), 'metainfo'),
  po_dir: join_paths(meson.project_source_root(), 'po'),
)

# Validating the metainfo file
appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test(
    'Validate metainfo file',
    appstreamcli,
    args: ['validate', '--no-net', '--explain', metainfo],
    workdir: meson.current_build_dir(),
  )
endif

resources_conf = configuration_data()
resources_conf.set('APPID', application_id)
gnome.compile_resources(
  'resources',
  configure_file(
    input: files('resources.gresource.xml.in'),
    output: 'resources.gresource.xml',
    configuration: resources_conf,
  ),
  gresource_bundle: true,
  source_dir: meson.current_build_dir(),
  install_dir: DATA_DIR,
  install: true,
  dependencies: metainfo,
)

# Installing the schema file
gschema_conf = configuration_data()
gschema_conf.set('APP_ID', application_id)
gschema_conf.set('GETTEXT_PACKAGE', gettext_package)
# Schemas and enums share one system-wide namespace: every installed
# .gschema.xml is compiled into a single cache. An untemplated enum id or
# schema path therefore collides with any other build of this application,
# and glib-compile-schemas fails with "already specified", breaking the cache
# for every application on the system rather than just ours.
gschema_conf.set('APP_PATH', '/' + application_id.replace('.', '/') + '/')
configure_file(
  # Source filename is literal, as it is for the metainfo and desktop files
  # above; only the installed name follows application_id. Deriving the input
  # from project_id would make renaming the application require renaming these
  # files too, for no benefit.
  input: 'org.gnome.World.Secrets.gschema.xml.in',
  output: '@0@.gschema.xml'.format(application_id),
  configuration: gschema_conf,
  install: true,
  install_dir: 'share/glib-2.0/schemas',
)

# Install mimetype file
install_data(
  'org.gnome.World.Secrets.xml',
  install_dir: 'share/mime/packages',
  rename: '@0@.xml'.format(application_id),
)

desktop_conf = configuration_data()
desktop_conf.set('ICON', application_id)
desktop = i18n.merge_file(
  input: configure_file(
    input: files('org.gnome.World.Secrets.desktop.in.in'),
    output: 'org.gnome.World.Secrets.desktop.in',
    configuration: desktop_conf,
  ),
  output: '@0@.desktop'.format(application_id),
  po_dir: join_paths(meson.project_source_root(), 'po'),
  type: 'desktop',
  install: true,
  install_dir: join_paths(
    get_option('prefix'),
    get_option('datadir'),
    'applications',
  ),
)

# Validating the desktop file
desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test(
    'Validate desktop file',
    desktop_file_validate,
    args: join_paths(meson.current_build_dir(), application_id + '.desktop'),
  )
endif

# Validating schemas
compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test(
    'Validate schema file',
    compile_schemas,
    args: ['--strict', '--dry-run', meson.current_source_dir()],
  )
endif

subdir('icons')
