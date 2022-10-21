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
from collections import namedtuple, OrderedDict
from configparser import ConfigParser
from contextlib import contextmanager
import eib
from eibostree import fetch_remote_collection_id
import fnmatch
from gi import require_version
require_version('Flatpak', '1.0')
require_version('OSTree', '1.0')
from gi.repository import Flatpak, GLib, Gio, OSTree
import logging
import os
import sys
from urllib.parse import urlparse
from urllib.request import urlopen


logger = logging.getLogger(__name__)


class FlatpakError(eib.ImageBuildError):
    """Errors from flatpak installation processes"""
    pass


class FlatpakFullRef(namedtuple('FlatpakFullRef', (
        'remote', 'remote_ref', 'installed_size', 'download_size', 'metadata',
        'related'))):
    """Complete representation of Flatpak ref

    This is basically a superset of Flatpak.RemoteRef with a couple
    extra attributes. It collects all the needed information for a ref
    to be installed in one place. Additionally, by using namedtuple, it
    can be hashed for dictionaries and sets.
    """
    __slots__ = ()

    @property
    def ref(self):
        return self.remote_ref.format_ref()

    @property
    def kind(self):
        return self.remote_ref.get_kind()

    @property
    def name(self):
        return self.remote_ref.get_name()

    @property
    def arch(self):
        return self.remote_ref.get_arch()

    @property
    def branch(self):
        return self.remote_ref.get_branch()

    @property
    def commit(self):
        return self.remote_ref.get_commit()

    @property
    def runtime(self):
        """Get the ref for the flatpak's runtime dependency

        This matches the algorithm in FlatpakTransaction's add_deps().
        Only applications and extensions with extra data that require a
        runtime to extract the extra data have a hard runtime depdendency.

        https://github.com/flatpak/flatpak/blob/master/common/flatpak-transaction.c#L1985
        """
        if self.kind == Flatpak.RefKind.APP:
            section = 'Application'
        elif self.has_extra_data and \
             not self.metadata.getboolean('Extra Data', 'NoRuntime',
                                          fallback=False):
            section = 'ExtensionOf'
        else:
            return None

        runtime = self.metadata.get(section, 'runtime', fallback=None)
        if runtime is not None:
            runtime = 'runtime/' + runtime
            # Make sure the runtime specified in the metadata isn't this
            # flatpak itself.
            if runtime != self.ref:
                return runtime

    @property
    def has_extra_data(self):
        return 'Extra Data' in self.metadata


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
                 repo_file=None, apps=None, runtimes=None, exclude=None,
                 allow_extra_data=None, title=None, default_branch=None,
                 **extra_options):
        # Copy some manager attributes
        self.manager = manager
        self.installation = manager.installation
        self.arch = manager.arch
        self.enable_p2p_updates = manager.enable_p2p_updates

        self.name = name
        self.url = url
        self.deploy_url = deploy_url
        self.repo_file = repo_file
        self.apps = apps.split() if apps else []
        self.runtimes = runtimes.split() if runtimes else []
        self.exclude = set(exclude.split()) if exclude else set()
        self.allow_extra_data = set(allow_extra_data.split()) \
            if allow_extra_data else set()
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

        # If the deploy URL isn't set, use the pull URL
        if not self.deploy_url:
            self.deploy_url = self.url

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
        remote.set_gpg_verify(True)
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

        # Recent flatpak began automatically adding collection IDs in
        # certain scenarios, but once they're set they can't change.
        # Clear any collection ID that might be present since it may not
        # match the configured URL.
        remote.set_collection_id(None)

        # Commit the changes
        self.installation.modify_remote(remote)

        # In case the remote metadata update applies a collection ID,
        # delete any current ostree-metadata ref to ensure flatpak pulls
        # it again.
        logger.info('Deleting %s ref for %s', OSTree.REPO_METADATA_REF,
                    self.name)
        repo = self.manager.get_repo()
        repo.set_ref_immediate(self.name, OSTree.REPO_METADATA_REF, None)
        self.installation.drop_caches()

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

    def _set_collection_id(self, collection_id):
        """Set the remote collection-id config setting

        Define the collection-id in the remote configuration group to
        enable P2P updates in the booted system.
        """
        logger.info('Setting flatpak remote %s collection-id to "%s"',
                    self.name, collection_id)
        remote = self.installation.get_remote_by_name(self.name)
        remote.set_collection_id(collection_id)
        self.installation.modify_remote(remote)

    def deploy(self):
        """Prepare remote for deployment

        Sync the appstream and metadata from the remote. If the deploy
        URL differs from the pull URL, it's adjusted here, too.
        """
        if self.deploy_url != self.url:
            logger.info('Setting %s URL to %s for deployment', self.name,
                        self.deploy_url)
            remote = self.installation.get_remote_by_name(self.name)
            remote.set_url(self.deploy_url)
            self.installation.modify_remote(remote)

        # Set the flatpak remote collection ID if it's enabled and the
        # remote has a collection ID
        # Temporarily(?) disable P2P for eos-runtimes (which are legacy). See:
        # https://phabricator.endlessm.com/T22756#602959
        # https://github.com/flatpak/flatpak/issues/1832
        if self.enable_p2p_updates and self.name != 'eos-runtimes':
            repo = self.manager.get_repo()
            collection_id = fetch_remote_collection_id(repo, self.name)
            if collection_id is not None:
                self._set_collection_id(collection_id)

        # Fetch the deployed remote's appstream and metadata
        # NOTE: This is done after adding the collection ID so that the
        # ostree-metadata will be pulled as well, which enables using USB
        # updates without ever going online
        logger.info('Updating appstream data for remote %s',
                    self.name)
        eib.retry(self.installation.update_appstream_sync, self.name,
                  self.arch)
        logger.info('Updating metadata for remote %s', self.name)
        eib.retry(self.installation.update_remote_sync, self.name)

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
            #
            # We disable strict parsing so that it doesn't error on
            # duplicate sections or options, which flatpak-builder
            # apparently generates sometimes
            # (https://phabricator.endlessm.com/T22435).
            metadata_bytes = eib.retry(
                self.installation.fetch_remote_metadata_sync, self.name,
                remote_ref)
            metadata_str = metadata_bytes.get_data().decode('utf-8')
            metadata = ConfigParser(strict=False)
            try:
                metadata.read_string(metadata_str)
            except:
                print('Could not read {} {} metadata:\n{}'
                      .format(self.name, ref, metadata_str),
                      file=sys.stderr)
                raise

            # Get all the related refs
            logger.debug('Getting related refs for %s ref %s',
                         self.name, ref)
            related = eib.retry(
                self.installation.list_remote_related_refs_sync,
                self.name, ref)

            # Create FlatpakFullRef
            self.refs[ref] = FlatpakFullRef(remote=self,
                                            remote_ref=remote_ref,
                                            installed_size=installed_size,
                                            download_size=download_size,
                                            metadata=metadata,
                                            related=related)

    def check_excluded(self, name):
        logger.debug('Checking ID %s in %s against exclude list', name, self.name)
        if name in self.exclude:
            logger.debug('ID %s matched excludes: %s', name, self.exclude)
            return True

        return False

    def check_allow_extra_data(self, name):
        logger.debug('Checking ID %s in %s against allow_extra_data list',
                     name, self.name)
        if name in self.allow_extra_data:
            logger.debug('ID %s matched allow_extra_data: %s', name,
                         self.allow_extra_data)
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

    def __init__(self, installation, config=None, is_cache_repo=False):
        self.installation = installation
        self.installation_path = self.installation.get_path().get_path()

        self.config = config
        if self.config is None:
            self.config = eib.get_config()

        self.is_cache_repo = is_cache_repo

        self.install_refs = None

        # Get architecture from generic flatpak section, falling back to
        # default arch for host. The config default is an empty string,
        # so the direct fallback mechanism doesn't work.
        self.arch = self.config.get('flatpak', 'arch', fallback=None)
        if not self.arch:
            self.arch = Flatpak.get_default_arch()
        logger.info('Using flatpak arch %s', self.arch)

        # Get locales configuration from generic flatpak section
        self.locales = self.config.get('flatpak', 'locales',
                                       fallback='').split()
        if self.locales:
            logger.info('Using flatpak locales %s',
                        ' '.join(self.locales))

        # See if collection IDs should be set
        self.enable_p2p_updates = self.config.getboolean(
            'flatpak', 'enable_p2p_updates', fallback=False)

        # See if extra-languages should be set
        self.set_extra_languages = self.config.getboolean(
            'flatpak', 'set_extra_languages', fallback=False)

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

            # Skip if the remote is disabled
            remote_enabled = self.config.getboolean(
                sect, 'enable', fallback=True)
            if not remote_enabled:
                logger.info('Remote %s disabled, skipping', name)
                continue

            # Pass the remote options as keyword arguments to
            # FlatpakRemote after removing unrecognized options
            remote_options = dict(self.config.items(sect))
            remote_options.pop('enable', None)
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

    def _remove_languages(self):
        """Remove the core.xa.languages repo option"""
        repo = self.get_repo()
        repo_config = repo.copy_config()
        for option in ["xa.languages", "xa.extra-languages"]:
            logger.info("Removing repo option core.%s", option)
            try:
                repo_config.remove_key('core', option)
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

    def _set_extra_languages(self):
        """Set the core.xa.extra-languages repo option

        Define the flatpak languages to use for installs via the
        core.xa.extra-languages repo config option.
        """
        if len(self.locales) == 0:
            return
        repo = self.get_repo()
        repo_config = repo.copy_config()
        value = ';'.join(self.locales)
        logger.info('Setting repo option core.xa.extra-languages to %s', value)
        repo_config.set_value('core', 'xa.extra-languages', value)
        repo.write_config(repo_config)
        self.installation.drop_caches()

    def _set_masked(self):
        """Set the core.xa.masked repo option from excluded flatpaks

        This is used to filter out excluded flatpaks that would
        automatically be added as related refs.

        Unfortunately, this is a global option whereas the excluded
        flatpaks are specified in the configuration per remote. It may
        be better to use Flatpak.Remote.set_filter(), but the semantics
        might not be the same.
        """
        excluded = set()
        for remote in self.remotes.values():
            excluded |= remote.exclude
        if len(excluded) == 0:
            return

        repo = self.get_repo()
        repo_config = repo.copy_config()
        value = ';'.join(excluded)
        logger.info('Setting repo option core.xa.masked to %s', value)
        repo_config.set_value('core', 'xa.masked', value)
        repo.write_config(repo_config)
        self.installation.drop_caches()

    def _remove_masked(self):
        """Remove the core.xa.masked repo option"""
        repo = self.get_repo()
        repo_config = repo.copy_config()
        logger.info('Removing repo option core.xa.masked')
        try:
            repo_config.remove_key('core', 'xa.masked')
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

    @contextmanager
    def tmp_xa_config(self):
        """Temporary xa namespaced repo configuration"""
        try:
            # Configure the extra languages for pull or install.
            self._set_extra_languages()
            self._set_masked()
            yield
        finally:
            if self.is_cache_repo or not self.set_extra_languages:
                # Don't leave the languages hanging around for the next build
                # or set in the image, respectively
                self._remove_languages()
            self._remove_masked()

    def enumerate_remotes(self):
        """Enumerate all configured remotes"""
        # Set languages since subpaths get calculated when calling
        # installation.list_remote_related_refs_sync().
        with self.tmp_xa_config():
            for remote in self.remotes.values():
                remote.enumerate()

    def _log_installation_free_space(self):
        """Write a log entry with the available installation space

        Use os.statvfs to get the free and blocks at the installation's
        path and then print a log message with the information.
        """
        stats = os.statvfs(self.installation_path)
        free = stats.f_bsize * stats.f_bfree
        total = stats.f_bsize * stats.f_blocks
        percent = (100.0 * stats.f_bfree) / stats.f_blocks
        logger.info('%s free space: %s / %s (%.1f%%)',
                    self.installation_path, GLib.format_size(free),
                    GLib.format_size(total), percent)

    @staticmethod
    def _log_operations(operations):
        logger.debug('Resolved flatpak operations:')
        for op in operations:
            logger.debug(
                '%s %s:%s %s',
                op.get_operation_type().value_nick,
                op.get_remote(),
                op.get_ref(),
                op.get_commit(),
            )

    def _check_excluded_operations(self, operations):
        """Verify none of the refs in the opertions are excluded"""
        excluded = []
        extra_data = []
        eol = []
        eol_rebase = []

        for op in operations:
            ref = op.get_ref()
            remote = self.remotes[op.get_remote()]
            full_ref = remote.refs[ref]
            name = full_ref.name

            if remote.check_excluded(name):
                logger.error(
                    '%s in %s is on excluded list',
                    full_ref.ref,
                    full_ref.remote.name,
                )
                excluded.append(full_ref)

            if (
                full_ref.has_extra_data
                and not remote.check_allow_extra_data(name)
            ):
                logger.error(
                    '%s in %s contains potentially non-redistributable extra data',
                    full_ref.ref,
                    full_ref.remote.name,
                )
                extra_data.append(full_ref)

            if full_ref.remote_ref.get_eol():
                logger.warning(
                    '%s in %s is marked as EOL: %s',
                    full_ref.ref,
                    full_ref.remote.name,
                    full_ref.remote_ref.get_eol(),
                )
                eol.append(full_ref)

            if full_ref.remote_ref.get_eol_rebase():
                logger.error(
                    '%s in %s is marked as EOL, superseded by %s',
                    full_ref.ref,
                    full_ref.remote.name,
                    full_ref.remote_ref.get_eol_rebase(),
                )
                eol_rebase.append(full_ref)

        if excluded:
            raise FlatpakError(
                'Excluded refs in resolved flatpaks:',
                ', '.join(full_ref.ref for full_ref in excluded),
            )

        if extra_data:
            raise FlatpakError(
                'Extra data refs in resolved flatpaks:',
                ', '.join(full_ref.ref for full_ref in extra_data),
            )

        # TODO: optionally make plain EOL fatal? make this optionally
        # non-fatal?
        if eol_rebase:
            raise FlatpakError(
                'Refs marked eol-rebase in resolved flatpaks:',
                ', '.join(full_ref.ref for full_ref in eol_rebase),
            )

    def _add_installs(self, transaction):
        for remote in self.remotes.values():
            for app in remote.apps:
                ref = remote.match(app, Flatpak.RefKind.APP)
                logger.info('Adding app %s from %s', ref.ref, remote.name)
                transaction.add_install(remote.name, ref.ref, None)
            for runtime in remote.runtimes:
                ref = remote.match(runtime, Flatpak.RefKind.RUNTIME)
                logger.info('Adding runtime %s from %s', ref.ref, remote.name)
                transaction.add_install(remote.name, ref.ref, None)

    def _new_transaction(self):
        txn = Flatpak.Transaction.new_for_installation(self.installation)
        self._add_installs(txn)
        return txn

    def _on_txn_op_done(self, transaction, operation, commit, result, user_data):
        op_str = user_data
        if not op_str:
            op_str = operation.get_operation_type().value_nick
        logger.info(
            'Flatpak %s operation done: %s:%s %s',
            op_str,
            operation.get_remote(),
            operation.get_ref(),
            operation.get_commit(),
        )
        self._log_installation_free_space()

    def _on_pull_txn_ready(self, transaction, user_data):
        operations = transaction.get_operations()
        self._log_operations(operations)
        self._check_excluded_operations(operations)
        return True

    def pull(self):
        """Pull all refs to install

        Use a no-deploy transaction to pull the commits for the desired
        flatpaks. This is intended to be used in a cache repo.
        """
        with self.tmp_xa_config():
            txn = self._new_transaction()
            txn.set_no_deploy(True)
            txn.connect('ready', self._on_pull_txn_ready, None)
            txn.connect('operation-done', self._on_txn_op_done, 'pull')
            txn.run()

    def _on_inst_txn_ready(self, transaction, cache_repo_path):
        operations = transaction.get_operations()
        self._log_operations(operations)
        self._check_excluded_operations(operations)

        # If a cache repo was specified, seed the commits before
        # continuing with the install.
        if cache_repo_path:
            refs = {
                op.get_ref(): op.get_commit()
                for op in operations
            }
            self.seed(cache_repo_path, refs)

        return True

    def install(self, cache_repo_path=None):
        """Install Flatpaks

        Find and order all Flatpak refs needed and install them with the
        installation.
        """
        with self.tmp_xa_config():
            txn = self._new_transaction()
            txn.set_disable_static_deltas(True)
            txn.connect('ready', self._on_inst_txn_ready, cache_repo_path)
            txn.connect('operation-done', self._on_txn_op_done, 'install')
            txn.run()

    def _on_resolve_txn_ready(self, transaction):
        # Return False to abort the transaction. Everything will be
        # handled after the transaction ends.
        return False

    def resolve(self):
        """Resolve all refs needed for installation"""
        with self.tmp_xa_config():
            txn = self._new_transaction()
            txn.connect('ready', self._on_resolve_txn_ready)
            try:
                txn.run()
            except GLib.GError as err:
                # The transaction is expected to be aborted. Fail on
                # anything else.
                if not err.matches(
                    Flatpak.Error.quark(),
                    Flatpak.Error.ABORTED,
                ):
                    raise

            operations = txn.get_operations()
            self._log_operations(operations)
            self._check_excluded_operations(operations)

            full_refs = []
            for op in operations:
                remote = self.remotes[op.get_remote()]
                full_refs.append(remote.refs[op.get_ref()])
            return full_refs

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

    def seed(self, cache_repo_path, refs):
        """Pull commits from a cache repo to the installation repo

        This is used during install to get as many commit objects as
        possible into the installation repo ahead of time. It would be
        better if you could specify the OSTree pull localcache-repos
        option, but flatpak doesn't support that.
        """
        cache_repo_file = Gio.File.new_for_path(cache_repo_path)
        cache_repo = OSTree.Repo.new(cache_repo_file)
        cache_repo.open()
        revs_to_pull = []
        for ref, rev in refs.items():
            try:
                _, _, state = cache_repo.load_commit(rev)
            except GLib.GError as err:
                if err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                    logger.debug(
                        'Skipping %s rev %s not in %s',
                        ref, rev, cache_repo_path
                    )
                    continue
                raise

            # Pulling partial refs like locales would require working
            # out the subpaths. That doesn't seem worth the effort.
            if state != OSTree.RepoCommitState.NORMAL:
                logger.debug(
                    'Skipping %s rev %s in %s partial',
                    ref, rev, cache_repo_path
                )
                continue

            logger.debug('Seeding %s rev %s from %s', ref, rev, cache_repo_path)
            revs_to_pull.append(rev)

        # Figure out pull options
        logger.info('Seeding from %s: %s', cache_repo_path, revs_to_pull)
        remote = cache_repo_file.get_uri()
        options = GLib.Variant('a{sv}', {
            'refs': GLib.Variant('as', revs_to_pull),
            'depth': GLib.Variant('i', 0),
            'disable-static-deltas': GLib.Variant('b', True),
            'inherit-transaction': GLib.Variant('b', True),
        })

        repo = self.get_repo()
        repo.prepare_transaction()
        try:
            self._log_installation_free_space()
            eib.retry(self._do_pull, repo, remote, options, timeout=30)
            repo.commit_transaction()
        except:
            logger.error('Pull failed, aborting transaction')
            repo.abort_transaction()
            raise

        self.installation.drop_caches()
