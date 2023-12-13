# Endless image builder library - Kolibri utilities
#
# Copyright © 2023 Endless OS Foundation LLC
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

import eib
import enum
import logging
from netrc import netrc
import os
import requests
from time import sleep
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


class RemoteKolibri:
    """Kolibri remote instance"""

    class Series(enum.Enum):
        """Supported Kolibri server series"""
        KOLIBRI_0_15 = enum.auto()
        KOLIBRI_0_16 = enum.auto()

    def __init__(self, base_url, username, password):
        self.base_url = base_url

        # Start a requests session with the credentials.
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({
            'Content-Type': 'application/json',
        })

        self.series = self._get_server_series()

    def import_channel(self, channel_id):
        """Import channel and content on remote Kolibri server"""
        if self.series == self.Series.KOLIBRI_0_15:
            return self._import_channel_0_15(channel_id)
        elif self.series == self.Series.KOLIBRI_0_16:
            return self._import_channel_0_16(channel_id)

        raise AssertionError('Unsupported server series')

    def _import_channel_0_15(self, channel_id):
        """Import channel and content on remote Kolibri 0.15 server"""
        # Import channel metadata.
        url = urljoin(
            self.base_url,
            'api/tasks/tasks/startremotechannelimport/',
        )
        data = {'channel_id': channel_id}
        logger.info(f'Importing channel {channel_id} metadata')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error('Failed to import channel: %s', resp.json())
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

        # Import channel content.
        url = urljoin(
            self.base_url,
            'api/tasks/tasks/startremotecontentimport/',
        )
        data = {
            'channel_id': channel_id,
            # Fetch all nodes so that the channel is fully mirrored.
            'renderable_only': False,
            'fail_on_error': True,
        }
        logger.info(f'Importing channel {channel_id} content')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error('Failed to import content: %s', resp.json())
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

    def _import_channel_0_16(self, channel_id, update=False):
        """Import channel and content on remote Kolibri 0.16 server"""
        url = urljoin(self.base_url, 'api/tasks/tasks/')
        data = {
            'type': 'kolibri.core.content.tasks.remoteimport',
            'channel_id': channel_id,
            'channel_name': 'unknown',
            'update': update,
            # Fetch all nodes so that the channel is fully mirrored.
            'renderable_only': False,
            'fail_on_error': True,
        }
        logger.info(f'Importing channel {channel_id}')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error('Failed to import channel: %s', resp.json())
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

    def update_channel(self, channel_id):
        """Update channel and content on remote Kolibri server"""
        if self.series == self.Series.KOLIBRI_0_15:
            return self._update_channel_0_15(channel_id)
        elif self.series == self.Series.KOLIBRI_0_16:
            return self._update_channel_0_16(channel_id)

        raise AssertionError('Unsupported server series')

    def _update_channel_0_15(self, channel_id):
        """Update channel and content on remote Kolibri 0.15 server"""
        # Generate channel diff stats.
        url = urljoin(self.base_url, 'api/tasks/tasks/channeldiffstats/')
        data = {'channel_id': channel_id, 'method': 'network'}
        logger.info(f'Generating channel {channel_id} diff')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error(
                    'Failed to generate channel diff: %s',
                    resp.json(),
                )
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

        # Update channel metadata and content.
        url = urljoin(self.base_url, 'api/tasks/tasks/startchannelupdate/')
        data = {
            'channel_id': channel_id,
            'sourcetype': 'remote',
            # Fetch all nodes so that the channel is fully mirrored.
            'renderable_only': False,
            'fail_on_error': True,
        }
        logger.info(f'Updating channel {channel_id} content')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error('Failed to update channel: %s', resp.json())
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

    def _update_channel_0_16(self, channel_id):
        """Update channel and content on remote Kolibri 0.15 server"""
        # Generate channel diff stats.
        url = urljoin(self.base_url, 'api/tasks/tasks/')
        data = {
            'type': 'kolibri.core.content.tasks.remotechanneldiffstats',
            'channel_id': channel_id,
            'channel_name': 'unknown',
        }
        logger.info(f'Generating channel {channel_id} diff')
        with self.session.post(url, json=data) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.error('Failed to generate channel diff: %s', resp.json())
                raise
            job = resp.json()
        self._wait_for_job(job['id'])

        # Update channel metadata and content.
        self._import_channel_0_16(channel_id, update=True)

    def seed_channel(self, channel_id):
        """Import or update channel and content on remote Kolibri server

        If the channel exists, it will be updated since Kolibri won't
        import new content nodes otherwise. An import is always run to
        ensure any nodes missed because of a previous failure are
        imported.
        """
        if self._channel_exists(channel_id):
            self.update_channel(channel_id)
        self.import_channel(channel_id)

    def _get_server_series(self):
        """Determine the server Kolibri series"""
        url = urljoin(self.base_url, 'api/public/info/')
        with self.session.get(url) as resp:
            resp.raise_for_status()
            info = resp.json()

        kolibri_version = info.get('kolibri_version', '')
        logger.debug(f'Server Kolibri version: "{kolibri_version}"')
        if kolibri_version.startswith('0.15.'):
            return self.Series.KOLIBRI_0_15
        elif kolibri_version.startswith('0.16.'):
            return self.Series.KOLIBRI_0_16

        raise Exception(f'Unsupported remote Kolibri version "{kolibri_version}"')

    def _get_job_status(self, job_id):
        """Get remote Kolibri job status"""
        url = urljoin(self.base_url, f'api/tasks/tasks/{job_id}/')
        with self.session.get(url) as resp:
            resp.raise_for_status()
            return resp.json()

    def _wait_for_job(self, job_id):
        """Wait for remote Kolibri job to complete"""
        logger.debug(f'Waiting for job {job_id} to complete')
        last_marker = None
        while True:
            data = self._get_job_status(job_id)

            # See the kolibri.core.tasks.job.State class for potential states
            # https://github.com/learningequality/kolibri/blob/develop/kolibri/core/tasks/job.py#L17
            status = data['status']
            if status == 'FAILED':
                logger.error(
                    f'Job {job_id} failed: '
                    f'{data["exception"]}\n{data["traceback"]}'
                )
                raise Exception(f'Job {job_id} failed')
            elif status == 'CANCELED':
                raise Exception(f'Job {job_id} cancelled')
            elif status == 'COMPLETED':
                if last_marker is None or last_marker < 100:
                    logger.info('Progress: 100%')
                break

            pct = int(data['percentage'] * 100)
            marker = pct - pct % 5
            if last_marker is None or marker > last_marker:
                logger.info(f'Progress: {pct}%')
                last_marker = marker

            # Wait a bit before checking the status again.
            sleep(0.5)

    def _channel_exists(self, channel_id):
        """Check if channel exists on remote Kolibri server"""
        url = urljoin(self.base_url, f'api/content/channel/{channel_id}/')
        logger.debug(f'Checking if channel {channel_id} exists')
        with self.session.get(url) as resp:
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                if resp.status_code == 404:
                    return False
                logger.error(
                    'Failed to check channel existence: %s',
                    resp.json(),
                )
                raise
            else:
                return True


def seed_remote_channels(channel_ids):
    """Import channels and content on remote Kolibri server

    Seeding is skipped if a custom content server is not used or there
    are no credentials for the server. Returns True when channels were
    seeded and False otherwise.
    """
    config = eib.get_config()

    base_url = config.get('kolibri', 'central_content_base_url', fallback=None)
    if not base_url:
        logger.info('Not using custom Kolibri content server')
        return False

    netrc_path = os.path.join(eib.SYSCONFDIR, 'netrc')
    if not os.path.exists(netrc_path):
        logger.info(f'No credentials in {netrc_path}')
        return False

    netrc_creds = netrc(netrc_path)
    host = urlparse(base_url).netloc
    creds = netrc_creds.authenticators(host)
    if not creds:
        logger.info(f'No credentials for {host} in {netrc_path}')
        return False
    username, _, password = creds

    remote = RemoteKolibri(base_url, username, password)
    for channel in channel_ids:
        logger.info(f'Seeding channel {channel} on {host}')
        remote.seed_channel(channel)

    return True
