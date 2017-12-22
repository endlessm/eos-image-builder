# -*- mode: Python; coding: utf-8 -*-

# Endless image builder: image sizing helper
#
# Copyright © 2017–2018 Endless Mobile, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# NOTE - This module is used from the host system to setup the build, so
# any resources used here (python modules, executed programs) must be
# available there. Use a separate module and make sure the components
# are in the buildroot if the utility is only inside the build.
import collections
import logging

from gi import require_version
require_version('Flatpak', '1.0')
from gi.repository import GLib, Flatpak  # noqa

log = logging.getLogger(__name__)

App = collections.namedtuple('App', (
    'remote', 'id', 'download_size', 'installed_size',
))


# https://phabricator.endlessm.com/T18626#436847
MUST_KEEP_APPS = {
    'net.gcompris.Gcompris',
    'net.sourceforge.Supertuxkart',
}
PREFER_REMOVE_NS = {
    'org.kde.',
}
PREFER_REMOVE_APPS = {
    'net.sourceforge.Warmux',
    'io.github.Supertux',
    'org.marsshooter.Marsshooter',
    'net.wz2100.Warzone2100',
    'org.tuxfamily.Xmoto',
}


def fetch_apps_for_remote(remote, branch, arch='x86_64'):
    log.info('Listing apps on remote %s', remote)
    installation = Flatpak.Installation.new_system()
    remote_refs = installation.list_remote_refs_sync(remote)
    apps = {}
    for ref in remote_refs:
        if not all((ref.get_kind() == Flatpak.RefKind.APP,
                    ref.get_arch() == arch,
                    ref.get_branch() == branch)):
            continue

        app_id = ref.get_name()
        sizes = installation.fetch_remote_size_sync(remote, ref, None)
        apps[app_id] = App(remote=remote,
                           id=app_id,
                           download_size=sizes.download_size,
                           installed_size=sizes.installed_size)

    return apps


def get_apps_for_remote(remote, branch, app_ids):
    apps = []

    app_ids = app_ids.split()
    if app_ids:
        remote_apps = fetch_apps_for_remote(remote, branch)
        for app_id in app_ids:
            apps.append(remote_apps[app_id])

    return apps


class AppListFormatter(object):
    def __init__(self, apps, personality, verdicts):
        locales = {
            personality,
            personality.split('_')[0],  # pt_BR -> pt
        }
        self.locale_apps = []
        self.generic_apps = []
        self.verdicts = verdicts

        remote_width = 0
        id_width = 0
        for app in apps:
            remote_width = max(remote_width, len(app.remote))
            id_width = max(id_width, len(app.id))

            if app.id.split('.')[-1] in locales:
                self.locale_apps.append(app)
            else:
                self.generic_apps.append(app)

        # Rather than using a construction like '{:>{}}'.format('abc', 8), make
        # a static format string we can re-use with different data. The heading
        # and table formatting is valid Phabricator markup
        widths = (remote_width, id_width, 8, 7)
        self.format_string = (
            '| {{:{}}} | {{:{}}} | {{:>{}}} | {{:{}}} |\n'.format(*widths))
        self.header = ''.join((
            self.format_string.format('Remote', 'App ID', 'Size', 'Remove?'),
            self.format_string.format(*('-' * width for width in widths)),
        ))

    def _write_table(self, stream, title, apps):
        stream.write('== {} ==\n\n'.format(title))
        stream.write(self.header)

        for app in apps:
            verdict = self.verdicts.get(app.id, '')
            row = (app.remote, app.id, GLib.format_size(app.download_size),
                   verdict)
            stream.write(self.format_string.format(*row))

        stream.write('\n')

    def write(self, stream):
        self._write_table(stream, 'Locale-specific apps', self.locale_apps)
        self._write_table(stream, 'Generic apps', self.generic_apps)


def show_apps(config, budget, stream):
    '''Lists apps that will be installed for this image, and their approximate
    compressed size. (We use the compressed download size according to `flatpak
    remote-ls -d` as an approximation for how the uncompressed app will affect
    the compressed size of the SquashFS.) We also suggest which apps to remove
    to bring the image within its size budget.'''
    # TODO: take into account the runtimes which will end up in the image,
    # whether because they are explicitly installed in the image config, or
    # as a dependency of the selected apps.
    apps = []

    c = config['flatpak']

    apps.extend(get_apps_for_remote(c['flathub_remote'],
                                    'stable',
                                    c['install_flathub']))
    apps.extend(get_apps_for_remote(c['apps_remote'],
                                    c['apps_branch'],
                                    c['install_eos']))

    # Sort in descending download size order, then by app ID
    apps.sort(key=lambda app: (- app.download_size, app.id))
    total = sum(app.download_size for app in apps)
    excess = total - budget

    excess_after_removals = excess
    verdicts = {app_id: 'No' for app_id in MUST_KEEP_APPS}

    # Any app which is larger than the budget must certainly be removed
    for app in apps:
        if app.download_size > budget:
            verdicts[app.id] = 'Yes'
            excess_after_removals -= app.download_size

    # Remove any app which we're happy to sacrifice
    for app in apps:
        if excess_after_removals <= 0:
            break

        if app.id in verdicts:
            continue

        if (
            app.id in PREFER_REMOVE_APPS or
            any(app.id.startswith(prefix) for prefix in PREFER_REMOVE_NS)
        ):
            verdicts[app.id] = 'Yes'
            excess_after_removals -= app.download_size

    # Propose removing the largest n apps until we're under budget.
    # TODO: prefer to remove generic apps?
    for app in apps:
        if excess_after_removals <= 0:
            break

        if app.id in verdicts:
            continue

        verdicts[app.id] = 'Maybe'
        excess_after_removals -= app.download_size

    stream.write('Estimated size: {}\n'.format(GLib.format_size(total)))
    if excess > 0:
        stream.write('Over budget: {}\n'.format(GLib.format_size(excess)))
        stream.write('Space after proposed removals: {}\n'.format(
            GLib.format_size(-excess_after_removals)))
    else:
        stream.write('Under budget: {}\n'.format(GLib.format_size(-excess)))
    stream.write('\n')

    personality = config['build']['personality']
    AppListFormatter(apps, personality, verdicts).write(stream)
