# -*- mode: Python; coding: utf-8 -*-

# Endless image builder library - flatpak utilities
#
# Copyright (C) 2017  Endless Mobile, Inc.
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

import base64
import codecs
from collections import deque, namedtuple, OrderedDict
from configparser import ConfigParser
import eib
import fnmatch
from gi import require_version
require_version('Flatpak', '1.0')
require_version('OSTree', '1.0')
from gi.repository import Flatpak, GLib, OSTree
import logging
import os
from urllib.parse import urlparse
from urllib.request import urlopen


logger = logging.getLogger(__name__)


class FlatpakError(eib.ImageBuildError):
    """Errors from flatpak installation processes"""
    pass


class FlatpakFullRef(namedtuple('FlatpakFullRef', (
        'ref', 'remote', 'kind', 'name', 'arch', 'branch', 'commit',
        'installed_size', 'download_size', 'runtime', 'sdk',
        'related'))):
    """Complete representation of Flatpak ref

    This is basically a superset of Flatpak.RemoteRef with a couple
    extra attributes. It collects all the needed information for a ref
    to be installed in one place. Additionally, by using namedtuple, it
    can be hashed for dictionaries and sets.
    """
    __slots__ = ()


class FlatpakInstallRef(object):
    """Flatpak ref for installation

    A wrapper around FlatpakFullRef containing a mutable subpaths
    attribute. This is needed because the subpaths get accessed from the
    dependent flatpak's RelatedRefs.
    """
    def __init__(self, full_ref):
        self.full_ref = full_ref

        # Provided later when resolving install set
        self.subpaths = None


class FlatpakRemote(object):
    """Configuration for Flatpak remote

    Contains the settings and data for an OSTree remote used for
    Flatpak. The refs dictionary contains a FlatpakRef for each Flatpak
    app or runtime ref found. It's populated by the enumerate() method.
    """

    # Map of flatpak kind to ref prefix
    FLATPAK_KIND_MAP = {
        Flatpak.RefKind.APP: 'app',
        Flatpak.RefKind.RUNTIME: 'runtime'
    }

    # Group used in flatpakrepo files
    FLATPAK_REPO_GROUP = 'Flatpak Repo'

    # Convenience for decoding from utf-8 from open file
    UTF8_READER = codecs.getreader('utf-8')

    def __init__(self, manager, name, url=None, deploy_url=None,
                 repo_file=None, apps=None, runtimes=None,
                 nosplit_apps=None, nosplit_runtimes=None, exclude=None,
                 title=None, default_branch=None, **extra_options):
        # Copy some manager attributes
        self.installation = manager.installation
        self.arch = manager.arch
        self.use_production = manager.use_production

        self.name = name
        self.url = url
        self.deploy_url = deploy_url
        self.repo_file = repo_file
        self.apps = apps.split() if apps else []
        self.runtimes = runtimes.split() if runtimes else []
        self.nosplit_apps = nosplit_apps.split() if nosplit_apps else []
        self.nosplit_runtimes = \
            nosplit_runtimes.split() if nosplit_runtimes else []
        self.exclude = set(exclude.split()) if exclude else set()
        self.title = title
        self.default_branch = default_branch

        # Only supported from repo_file
        self.gpg_key = None

        # Fetch repo_file if specified
        if self.repo_file:
            repo_config = self._get_repo_config()

            # Get URL, title and default branch if not specified in
            # configuration
            if not self.url:
                self.url = repo_config.get(self.FLATPAK_REPO_GROUP,
                                           'Url', fallback=None)
            if not self.title:
                self.title = repo_config.get(self.FLATPAK_REPO_GROUP,
                                             'Title', fallback=None)
            if not self.default_branch:
                self.default_branch = repo_config.get(
                    self.FLATPAK_REPO_GROUP, 'DefaultBranch',
                    fallback=None)

            # Get the base64 encoded GPG key
            self.gpg_key = repo_config.get(self.FLATPAK_REPO_GROUP,
                                           'GPGKey', fallback=None)

        # Make sure URL configured
        if not self.url:
            raise FlatpakError('No URL defined for remote', self.name)

        # Adjust URL for production usage
        if self.deploy_url and self.use_production:
            logger.info('Using production URL %s for %s',
                        self.deploy_url, self.name)
            self.url = self.deploy_url

        # Calculated values
        self.refs = {}

    def _get_repo_config(self):
        """Download and parse the repo config file"""
        config = ConfigParser()

        parts = urlparse(self.repo_file)
        if not parts.scheme or parts.scheme == 'file':
            logger.info('Loading repo file %s', self.repo_file)
            if not config.read(parts.path, encoding='utf-8'):
                raise FlatpakError('Could not read repo file',
                                   self.repo_file)
        else:
            logger.info('Downloading repo file %s', self.repo_file)
            with urlopen(self.repo_file) as resp:
                # ConfigParser likes text, so decode from utf-8
                config.read_file(self.UTF8_READER(resp),
                                 source=self.repo_file)

        return config

    def add(self):
        """Add this remote to the installation"""
        # Construct the remote
        logger.info('Adding flatpak remote %s', self.name)
        remote = Flatpak.Remote.new(self.name)
        remote.set_url(self.url)
        if self.title:
            remote.set_title(self.title)
        if self.default_branch:
            remote.set_default_branch(self.default_branch)

        # Import the GPG key if specified
        if self.gpg_key:
            # Strip any whitespace and decode from base64
            gpg_key_decoded = base64.b64decode(self.gpg_key.strip(),
                                               validate=True)

            # Convert to GBytes
            gpg_key_bytes = GLib.Bytes.new(gpg_key_decoded)
            remote.set_gpg_key(gpg_key_bytes)

        # Commit the changes
        self.installation.modify_remote(remote)

        # Fetch the deployed remote's metadata
        logger.info('Updating metadata for remote %s', self.name)
        eib.retry(self.installation.update_remote_sync, self.name)

        # Reset any configuration defined metadata
        self.reset_metadata()

        # If there's no default branch in the configuration, see if it
        # was set from the remote metadata
        if not self.default_branch:
            remote = self.installation.get_remote_by_name(self.name)
            self.default_branch = remote.get_default_branch()
            if self.default_branch:
                logger.info('Using %s as default branch for remote %s',
                            self.default_branch, self.name)

    def deploy(self):
        """Prepare remote for deployment

        Adjust the remote if the deploy URL differs from the pull URL.
        """
        if self.deploy_url is None or self.deploy_url == self.url:
            return

        logger.info('Setting %s URL to %s for deployment', self.name,
                    self.deploy_url)
        remote = self.installation.get_remote_by_name(self.name)
        remote.set_url(self.deploy_url)
        self.installation.modify_remote(remote)

        # Fetch the deployed remote's appstream and metadata
        logger.info('Updating appstream data for remote %s',
                    remote.name)
        eib.retry(self.installation.update_appstream_sync, remote.name,
                  self.arch)
        logger.info('Updating metadata for remote %s', remote.name)
        eib.retry(self.installation.update_remote_sync, remote.name)

        # Reset any configuration defined metadata
        self.reset_metadata()

    def reset_metadata(self):
        """Reset remote's metadata per configuration

        Set the title and default branch remote options back to the
        value used in the builder configuration. This is needed if the
        setting was changed by a call to update the remote's metadata
        from the server.
        """
        if not self.title and not self.default_branch:
            return

        remote = self.installation.get_remote_by_name(self.name)
        if self.title:
            remote.set_title(self.title)
        if self.default_branch:
            remote.set_default_branch(self.default_branch)
        self.installation.modify_remote(remote)

    def enumerate(self):
        """Populate refs from remote data

        Fetch the remote's data and create a FlatpakRef for each Flatpak
        app or runtime found.
        """
        logger.info('Fetching refs for %s', self.name)
        all_remote_refs = eib.retry(
            self.installation.list_remote_refs_sync, self.name)
        for remote_ref in all_remote_refs:
            # Get the full ostree ref
            ref = remote_ref.format_ref()
            logger.debug('Found %s ref %s', self.name, ref)

            # Get the installed and download size
            _, download_size, installed_size = eib.retry(
                self.installation.fetch_remote_size_sync, self.name,
                remote_ref)

            # Try to get the runtime and sdk from the flatpak metadata.
            # We could use GKeyFile as the INI parser, but ConfigParser
            # is more pleasant from python.
            metadata_bytes = eib.retry(
                self.installation.fetch_remote_metadata_sync, self.name,
                remote_ref)
            metadata = ConfigParser()
            metadata.read_string(metadata_bytes.get_data().decode('utf-8'))
            runtime = metadata.get('Application', 'runtime',
                                   fallback=None)
            sdk = metadata.get('Application', 'sdk', fallback=None)

            # Get all the related refs
            logger.debug('Getting related refs for %s ref %s',
                         self.name, ref)
            related = eib.retry(
                self.installation.list_remote_related_refs_sync,
                self.name, ref)

            # Create FlatpakFullRef
            self.refs[ref] = FlatpakFullRef(ref=ref,
                                            remote=self,
                                            kind=remote_ref.get_kind(),
                                            name=remote_ref.get_name(),
                                            arch=remote_ref.get_arch(),
                                            branch=remote_ref.get_branch(),
                                            commit=remote_ref.get_commit(),
                                            installed_size=installed_size,
                                            download_size=download_size,
                                            runtime=runtime,
                                            sdk=sdk,
                                            related=related)

    def check_excluded(self, name):
        logger.debug('Checking ID %s in %s against exclude list', name, self.name)
        if name in self.exclude:
            logger.debug('ID %s matched excludes: %s', name, self.exclude)
            return True

        return False

    def match(self, ref, kind):
        """Find matches for a flatpak ref"""
        if not ref:
            raise FlatpakError('Cannot match empty ref')
        if kind not in self.FLATPAK_KIND_MAP:
            raise FlatpakError('Unrecognized refkind', kind, 'for ref',
                               ref)

        kind_str = self.FLATPAK_KIND_MAP[kind]
        logger.info('Looking for %s ref "%s" match in %s', kind_str,
                    ref, self.name)

        # Parse out a partial flatpak ref
        parts = ref.split('/')
        n_parts = len(parts)
        name = parts[0]
        arch = None
        branch = None
        if n_parts > 3:
            raise FlatpakError('More than 2 /s in ref', ref)
        elif n_parts == 3:
            _, arch, branch = parts
        elif n_parts == 2:
            _, arch = parts

        # Fallback to arch and branch defaults
        if not arch:
            arch = self.arch
        if not branch:
            branch = self.default_branch

        match = None
        if branch is None:
            # No specific branch and no default branch. Use the "latest"
            # branch by sort order.
            match_ref = '/'.join((kind_str, name, arch, '*'))
            logger.debug('Matching ref pattern "%s" in remote %s',
                         match_ref, self.name)
            matches = sorted(fnmatch.filter(self.refs.keys(), match_ref))
            if len(matches) > 0:
                match = self.refs[matches[-1]]
        else:
            match_ref = '/'.join((kind_str, name, arch, branch))
            logger.debug('Matching ref %s in remote %s', match_ref,
                         self.name)
            match = self.refs.get(match_ref)

        if match:
            logger.debug('Found ref %s match %s in remote %s', ref, match,
                         self.name)
        else:
            logger.debug('Found no ref %s match in remote %s', ref,
                         self.name)
        return match


class FlatpakManager(object):
    """Manager for flatpak installations

    Parse the defined flatpak settings from the image builder
    configuration in order to determine the runtimes, remotes and apps
    to install. The flatpak image builder settings come from the
    flatpak-remote-<name> sections. See config/defaults.ini for more
    details on those sections.
    """
    REMOTE_PREFIX = 'flatpak-remote-'

    def __init__(self, installation, config=None):
        self.installation = installation

        self.config = config
        if self.config is None:
            self.config = eib.get_config()

        self.install_refs = None

        # Get architecture from generic flatpak section, falling back to
        # default arch for host
        self.arch = self.config.get('flatpak', 'arch',
                                    fallback=Flatpak.get_default_arch())
        logger.info('Using flatpak arch %s', self.arch)

        # Get locales configuration from generic flatpak section
        self.locales = self.config.get('flatpak', 'locales',
                                       fallback=()).split()
        if self.locales:
            logger.info('Using flatpak locales %s',
                        ' '.join(self.locales))

        # See if production flatpaks should be used
        self.use_production = self.config.getboolean(
            'build', 'use_production_apps', fallback=False)

        self.remotes = OrderedDict()
        for sect in self.config.sections():
            head, sep, name = sect.partition(self.REMOTE_PREFIX)
            if sep == '' or head != '':
                # Not a section beginning with the prefix
                continue
            if name == '':
                # No name after the prefix
                raise FlatpakError(
                    'No remote name suffix in config section', sect)

            remote_options = dict(self.config.items_no_default(sect))
            logger.debug('Remote %s options: %s', name, remote_options)
            self.remotes[name] = FlatpakRemote(self, name,
                                               **remote_options)

    def get_repo(self):
        """Open the installation's OSTree repository

        The repo is opened on the fly so that it doesn't get out of sync
        with the installation's internal repo.
        """
        repo_file = self.installation.get_path().get_child('repo')
        repo = OSTree.Repo.new(repo_file)
        repo.open()
        return repo

    def add_remotes(self):
        """Add all configured remotes to repository"""
        for remote in self.remotes.values():
            remote.add()

    def deploy_remotes(self):
        """Configure all remotes for image deployment

        Puts in place deployment URL and sync appstream and metadata
        from final URL.
        """
        for remote in self.remotes.values():
            remote.deploy()

    def _set_languages(self):
        """Set the core.xa.languages repo option

        Define the flatpak languages to use for installs via the
        core.xa.languages repo config option.
        """
        if len(self.locales) == 0:
            return
        repo = self.get_repo()
        repo_config = repo.copy_config()
        value = ';'.join(self.locales)
        logger.info('Setting repo option core.xa.languages to %s', value)
        repo_config.set_value('core', 'xa.languages', value)
        repo.write_config(repo_config)
        self.installation.drop_caches()

    def _remove_languages(self):
        """Remove the core.xa.languages repo option

        Remove the flatpak languages repo config option.
        """
        repo = self.get_repo()
        repo_config = repo.copy_config()
        try:
            repo_config.remove_key('core', 'xa.languages')
        except GLib.Error as err:
            # Ignore errors for missing group or key
            if err.matches(GLib.KeyFile.error_quark(),
                           GLib.KeyFileError.GROUP_NOT_FOUND):
                pass
            elif err.matches(GLib.KeyFile.error_quark(),
                             GLib.KeyFileError.KEY_NOT_FOUND):
                pass
            else:
                raise
        repo.write_config(repo_config)
        self.installation.drop_caches()

    def enumerate_remotes(self):
        """Enumerate all configured remotes"""
        # Set xa.languages since subpaths get calculated when calling
        # installation.list_remote_related_refs_sync().
        self._set_languages()
        for remote in self.remotes.values():
            remote.enumerate()
        self._remove_languages()

    def _match_runtime(self, ref, runtime):
        """Find a ref's runtime

        Look for the ref's runtime in any remote, preferring the ref's
        own remote.
        """
        match = ref.remote.match(runtime,
                                 Flatpak.RefKind.RUNTIME)
        if not match:
            for name, remote in self.remotes.items():
                if name == ref.remote.name:
                    continue
                match = remote.match(runtime,
                                     Flatpak.RefKind.RUNTIME)
                if match:
                    break

        return match

    @staticmethod
    def _get_ekn_services_ref(full_ref):
        """Get required EknServices ref

        Endless knowledge apps need a matching EknServices app from the
        eos-sdk remote to support features in applications using that
        runtime. Currently required cases depending on the runtime used:

        com.endlessm.Platform//eos3.1 - EknServices//eos3
        com.endlessm.apps.Platform//1 - EknServices//eos3
        com.endlessm.apps.Platform//{2,3} - EknServices2//stable

        Note that any knowledge apps using
        com.endlessm.Platform//eos3.{0,2} would also require
        EknServices//eos3, but it's not believed there are any more
        knowledge apps using those runtimes.
        """
        # Only runtimes need to be checked
        if full_ref.kind != Flatpak.RefKind.RUNTIME:
            return None

        if ((full_ref.name == 'com.endlessm.Platform' and
             full_ref.branch == 'eos3.1') or
            (full_ref.name == 'com.endlessm.apps.Platform' and
             full_ref.branch == '1')):
            return ('app/com.endlessm.EknServices/{}/eos3'
                    .format(full_ref.arch))
        elif (full_ref.name == 'com.endlessm.apps.Platform' and
              full_ref.branch in ('2', '3')):
            return ('app/com.endlessm.EknServices2/{}/stable'
                    .format(full_ref.arch))
        else:
            return None

    def _add_ekn_services(self, full_ref):
        """Find a required EknServices ref and add to the install set

        If the ref needs an EknServices app, look for the appropriate one
        in the eos-sdk remote.
        """
        ekn_services_ref = self._get_ekn_services_ref(full_ref)
        if not ekn_services_ref:
            logger.debug('No EknServices required for %s', full_ref.ref)
            return
        if ekn_services_ref in self.install_refs:
            logger.debug('%s already in installed set', ekn_services_ref)
            return

        logger.debug('Searching for %s needed by %s', ekn_services_ref,
                     full_ref.ref)
        sdk_remote = self.remotes.get('eos-sdk')
        if not sdk_remote:
            raise FlatpakError('No eos-sdk remote for needed ref',
                               ekn_services_ref)
        ekn_services = sdk_remote.refs.get(ekn_services_ref)
        if not ekn_services:
            raise FlatpakError('Could not find', ekn_services_ref,
                               'for', full_ref.ref)
        logger.info('Adding %s for %s', ekn_services_ref, full_ref.ref)
        self.install_refs[ekn_services_ref] = FlatpakInstallRef(
            ekn_services)

    def _match_related(self, full_ref, related_ref):
        """Find a FlatpakFullRef's related ref

        Look for the related ref in any remote, preferring the ref's own
        remote.
        """
        match = full_ref.remote.refs.get(related_ref)
        if not match:
            for name, remote in self.remotes.items():
                if name == full_ref.remote.name:
                    continue
                match = remote.refs.get(related_ref)
                if match:
                    break

        return match

    def resolve_refs(self, split=False):
        """Resolve all refs needed for installation

        Add the apps and runtimes required for each remote and resolve
        all runtime and extension dependencies. If split is True, the
        nosplit apps and runtimes will not be included.
        """
        # Dict of FlatpakInstallRefs needed for installation keyed by
        # the ref string.
        self.install_refs = {}

        # Get required apps and runtimes
        for remote in self.remotes.values():
            if split:
                wanted_apps = set(remote.apps) - set(remote.nosplit_apps)
                wanted_runtimes = \
                    set(remote.runtimes) - set(remote.nosplit_runtimes)
            else:
                wanted_apps = remote.apps
                wanted_runtimes = remote.runtimes

            for app in wanted_apps:
                full_ref = remote.match(app, Flatpak.RefKind.APP)
                if full_ref is None:
                    raise FlatpakError('Could not find app', app, 'in',
                                       remote.name)
                if remote.check_excluded(full_ref.name):
                    raise FlatpakError('Explicitly added app', app, 'in',
                                       remote.name, 'is on excluded list.')
                logger.info('Adding app %s from %s', full_ref.ref,
                            remote.name)
                self.install_refs[full_ref.ref] = FlatpakInstallRef(
                    full_ref)

            for runtime in wanted_runtimes:
                full_ref = remote.match(runtime, Flatpak.RefKind.RUNTIME)
                if full_ref is None:
                    raise FlatpakError('Could not find runtime',
                                       runtime, 'in', remote.name)
                if remote.check_excluded(full_ref.name):
                    raise FlatpakError('Explicitly added runtime', runtime, 'in',
                                       remote.name, 'is on excluded list.')
                logger.info('Adding runtime %s from %s', full_ref.ref,
                            remote.name)
                self.install_refs[full_ref.ref] = FlatpakInstallRef(
                    full_ref)

        # Add runtime and related dependencies. Keep checking until
        # all required refs and dependencies have been resolved.
        checked_refs = set()
        while len(checked_refs) < len(self.install_refs):
            for install_ref in list(self.install_refs.values()):
                full_ref = install_ref.full_ref
                if full_ref.ref in checked_refs:
                    continue

                if full_ref.runtime and \
                   'runtime/' + full_ref.runtime not in self.install_refs:
                    runtime = self._match_runtime(full_ref,
                                                  full_ref.runtime)
                    if not runtime:
                        raise FlatpakError('Could not find runtime',
                                           full_ref.runtime, 'for ref',
                                           full_ref.ref)
                    if runtime.remote.check_excluded(runtime.name):
                        raise FlatpakError('Runtime', runtime.ref, 'for ref',
                                           full_ref.ref, 'from',
                                           runtime.remote.name,
                                           'is on excluded list.')
                    logger.info('Adding %s runtime %s from %s',
                                full_ref.ref, runtime.ref,
                                runtime.remote.name)
                    self.install_refs[runtime.ref] = FlatpakInstallRef(
                        runtime)

                # Hack - add EknServices from eos-sdk if needed
                self._add_ekn_services(full_ref)

                for related in full_ref.related:
                    if not related.should_download():
                        logger.debug('Skipping %s related ref %s',
                                     full_ref.name, related.get_name())
                        continue

                    related_ref = related.format_ref()
                    install_match = self.install_refs.get(related_ref)
                    if not install_match:
                        match = self._match_related(full_ref,
                                                    related_ref)
                        if not match:
                            logger.info(
                                'Could not find related ref %s for %s',
                                related_ref, full_ref.ref)
                            continue
                        if match.remote.check_excluded(match.name):
                            logger.info('Excluding', full_ref.ref, 'related ref',
                                        related_ref, 'from', match.remote.name)
                            continue
                        logger.info('Adding %s related ref %s from %s',
                                    full_ref.ref, related_ref,
                                    match.remote.name)
                        install_match = FlatpakInstallRef(match)
                        self.install_refs[related_ref] = install_match

                    # Make sure subpaths are set
                    if install_match.subpaths is None:
                        subpaths = related.get_subpaths()
                        logger.info('Setting %s subpaths to %s',
                                    related_ref, ' '.join(subpaths))
                        install_match.subpaths = subpaths

                checked_refs.add(full_ref.ref)

    @staticmethod
    def _subpaths_to_subdirs(subpaths):
        """Convert flatpak subpaths to ostree subdirs"""
        # Always add /metadata
        subdirs = ['/metadata']

        # Configured subpaths are subdirectories of /files. Subpaths
        # should begin with a leading /, but be safe
        for sub in subpaths:
            path = os.path.normpath('/'.join(('/files', sub)))
            subdirs.append(path)

        return subdirs

    def _do_pull(self, repo, remote, options):
        progress = OSTree.AsyncProgress.new()
        progress.connect(
            'changed',
            OSTree.Repo.pull_default_console_progress_changed,
            None)
        try:
            repo.pull_with_options(remote, options, progress)
        finally:
            progress.finish()

    def pull(self, commit_only=False, cache_repo_path=None):
        """Pull all refs to install

        Use OSTree to pull all the needed refs to a repository. If
        commit_only is True, the commit checksums are pulled without
        creating repository refs. If cache_repo_path points to an ostree
        repo, it will be used as a local object cache.
        """
        if self.install_refs is None:
            raise FlatpakError('Must run resolve_refs before pull')

        # Open the OSTree repo directly
        repo = self.get_repo()

        # Organize the refs by remote and by full and or subpaths
        remote_refs = {}
        for install_ref in self.install_refs.values():
            remote = install_ref.full_ref.remote.name
            if remote not in remote_refs:
                remote_refs[remote] = {
                    'full': [],
                    'subpath': []
                }
            if install_ref.subpaths:
                remote_refs[remote]['subpath'].append(install_ref)
            else:
                remote_refs[remote]['full'].append(install_ref)

        localcache_repos = (cache_repo_path,) if cache_repo_path else ()

        repo.prepare_transaction()
        try:
            common_pull_options = {
                'depth': GLib.Variant('i', 0),
                'localcache-repos': GLib.Variant('as', localcache_repos),
                'inherit-transaction': GLib.Variant('b', True),
            }

            for remote, refs in remote_refs.items():
                # Pull full refs, specifying checksum for commit only
                full_refs = []
                for install_ref in refs['full']:
                    if commit_only:
                        full_refs.append(install_ref.full_ref.commit)
                    else:
                        full_refs.append(install_ref.full_ref.ref)

                logger.info('Pulling %s refs %s', remote,
                            ' '.join(full_refs))
                options = common_pull_options.copy()
                options['refs'] = GLib.Variant('as', full_refs)
                options_var = GLib.Variant('a{sv}', options)
                eib.retry(self._do_pull, repo, remote, options_var)

                for install_ref in refs['subpath']:
                    if commit_only:
                        ref_to_pull = install_ref.full_ref.commit
                    else:
                        ref_to_pull = install_ref.full_ref.ref
                    subdirs = self._subpaths_to_subdirs(
                        install_ref.subpaths)
                    logger.info('Pulling %s ref %s subdirs %s',
                                remote, ref_to_pull, ' '.join(subdirs))
                    options = common_pull_options.copy()
                    options.update({
                        'refs': GLib.Variant('as', (ref_to_pull,)),
                        'subdirs': GLib.Variant('as', subdirs),
                        # Ensure no deltas are used for subdir pull
                        'disable-static-deltas': GLib.Variant('b', True)
                    })
                    options_var = GLib.Variant('a{sv}', options)
                    eib.retry(self._do_pull, repo, remote, options_var)

            repo.commit_transaction()
        except:
            logger.info('Pull failed, aborting transaction')
            repo.abort_transaction()
            raise

        self.installation.drop_caches()

    def install(self):
        """Install Flatpak refs

        Find and order all Flatpak refs needed and install them with the
        installation.
        """
        if self.install_refs is None:
            raise FlatpakError('Must run resolve_refs before pull')

        # Try to order refs so dependencies are installed first. This
        # simply installs refs with no runtime dependencies first and
        # assumes flatpak won't error for any issues with extensions
        # being installed before the ref they extend.
        refs_to_install = deque()
        for install_ref in self.install_refs.values():
            if install_ref.full_ref.runtime:
                refs_to_install.append(install_ref)
            else:
                refs_to_install.appendleft(install_ref)

        for install_ref in refs_to_install:
            full_ref = install_ref.full_ref
            logger.info('Installing %s from %s', full_ref.ref,
                        full_ref.remote.name)
            eib.retry(self.installation.install_full,
                      flags=Flatpak.InstallFlags.NONE,
                      remote_name=full_ref.remote.name,
                      kind=full_ref.kind,
                      name=full_ref.name,
                      arch=full_ref.arch,
                      branch=full_ref.branch,
                      subpaths=install_ref.subpaths)
