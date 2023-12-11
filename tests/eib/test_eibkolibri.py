# Tests for eibkolibri module
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

import logging
import os
import pytest
from urllib.parse import urljoin, urlparse

import eib
import eibkolibri

logger = logging.getLogger(__name__)

SERVER_URL = 'https://kolibri.example.com'
CHANNEL_ID = 'b43aae9d37294548ae75674cd23ddf4a'
JOB_ID = '1d67e97a98be4eb597b5cdb93e998989'


class MockKolibriServer:
    def __init__(self, requests_mocker, kolibri_version='0.15.12'):
        self.mocker = requests_mocker
        self.version = kolibri_version
        self.setup_responses(self.version)

    def setup_responses(self, kolibri_version):
        self.set_version_response(kolibri_version)
        self.set_channel_response()
        self.set_task_responses(kolibri_version)
        self.set_job_status_response()

    def set_version_response(self, kolibri_version):
        self.version = kolibri_version
        self.mocker.get(
            urljoin(SERVER_URL, 'api/public/info/'),
            json={
                "application": "kolibri",
                "kolibri_version": kolibri_version,
                "instance_id": "c9c0307035a3450fb315ed1ebb2fc215",
                "device_name": "test",
                "operating_system": "Linux"
            },
        )

    def set_channel_response(self, channel_id=CHANNEL_ID, exists=False):
        if exists:
            code = 200
            data = {
                "author": "",
                "description": "Test channel",
                "tagline": None,
                "id": channel_id,
                "last_updated": "2023-12-08T10:45:20.401110-07:00",
                "name": "Test",
                "root": channel_id,
                "thumbnail": None,
                "version": 2,
                "public": True,
                "num_coach_contents": None,
                "available": True,
                "lang_code": "mul",
                "lang_name": "Multiple languages",
            }
        else:
            code = 404
            data = [
                {
                    "id": "NOT_FOUND",
                    "metadata": {
                        "view": "Channel Metadata Instance",
                    },
                },
            ]

        self.mocker.get(
            urljoin(SERVER_URL, f'api/content/channel/{channel_id}/'),
            status_code=code,
            json=data,
        )

    def set_task_responses(
        self,
        kolibri_version,
        job_id=JOB_ID,
        channel_id=CHANNEL_ID,
    ):
        if kolibri_version.startswith('0.15.'):
            return self._set_task_responses_0_15(
                kolibri_version,
                job_id,
                channel_id,
            )
        elif kolibri_version.startswith('0.16.'):
            return self._set_task_responses_0_16(
                kolibri_version,
                job_id,
                channel_id,
            )

        logger.warning(
            f'Cannot create task responses for Kolibri {kolibri_version}'
        )

    def _set_task_responses_0_15(
        self,
        kolibri_version,
        job_id,
        channel_id,
    ):
        for task_name in (
            'channeldiffstats',
            'startchannelupdate',
            'startremotechannelimport',
            'startremotecontentimport',
        ):
            job_type = task_name.replace('start', '', 1).upper()
            data = {
                "status": "QUEUED",
                "exception": "None",
                "traceback": "None",
                "percentage": 0,
                "id": job_id,
                "cancellable": True,
                "clearable": False,
                "baseurl": "https://studio.learningequality.org",
                "type": job_type,
                "started_by": "156680771d8d5a1c9628393c5ca73c8e",
                "channel_id": channel_id,
            }

            self.mocker.post(
                urljoin(SERVER_URL, f'api/tasks/tasks/{task_name}/'),
                json=data,
            )

    def _set_task_responses_0_16(
        self,
        kolibri_version,
        job_id,
        channel_id,
    ):
        # All tasks use the same URL, so the response needs to be dynamic
        # based on the type in the request data.
        def task_data_callback(request, context):
            # In the real Request, json is a property, but it's a funcion in
            # requests_mock's fake Request. Handle both.
            if callable(request.json):
                req_data = request.json()
            else:
                req_data = request.json

            job_type = req_data.get('type', '')
            data = {
                "status": "QUEUED",
                "type": job_type,
                "exception": None,
                "traceback": "",
                "percentage": 0,
                "id": job_id,
                "cancellable": True,
                "clearable": False,
                "facility_id": None,
                "args": [channel_id],
                "kwargs": {
                    "baseurl": "https://studio.learningequality.org/",
                    "peer_id": None,
                },
                "extra_metadata": {
                    "channel_name": "unknown",
                    "channel_id": channel_id,
                    "peer_id": None,
                    "started_by": "156680771d8d5a1c9628393c5ca73c8e",
                    "started_by_username": "admin"
                },
                "scheduled_datetime": "2023-12-08T18:40:57.472971+00:00",
                "repeat": 0,
                "repeat_interval": 0,
                "retry_interval": None,
            }

            if job_type == 'kolibri.core.content.tasks.remoteimport':
                data['kwargs'].update({
                    "node_ids": None,
                    "exclude_node_ids": None,
                    "update": False,
                    "renderable_only": False,
                    "fail_on_error": True,
                    "all_thumbnails": False,
                })

            return data

        self.mocker.post(
            urljoin(SERVER_URL, 'api/tasks/tasks/'),
            json=task_data_callback,
        )

    def set_job_status_response(self, job_id=JOB_ID, channel_id=CHANNEL_ID):
        # The responses are a bit different between 0.15 and 0.16, but the code
        # only looks at that status and percentage field, which are the same.
        data = {
            "status": "COMPLETED",
            "exception": "None",
            "traceback": "None",
            "percentage": 0,
            "id": job_id,
            "cancellable": True,
            "clearable": True,
            "baseurl": "https://studio.learningequality.org",
            # We're reusing the same job ID for all tasks, so we don't know the
            # type. Fortunately, the eibkolibri code doesn't look at it.
            "type": "SOMETASK",
            "started_by": "156680771d8d5a1c9628393c5ca73c8e",
            "channel_id": channel_id,
        }
        self.mocker.get(
            urljoin(SERVER_URL, f'api/tasks/tasks/{job_id}/'),
            json=data,
        )

    def update_tasks_run(self):
        if self.version.startswith('0.15.'):
            def is_update_task_request(request):
                if request.method != 'POST':
                    return False
                return request.url.endswith('/channeldiffstats/')
        elif self.version.startswith('0.16.'):
            def is_update_task_request(request):
                if request.method != 'POST':
                    return False
                if callable(request.json):
                    req_data = request.json()
                else:
                    req_data = request.json
                return req_data.get('type') == (
                    'kolibri.core.content.tasks.remotechanneldiffstats'
                )
        else:
            logger.warning(
                f'Cannot parse task responses for Kolibri {self.version}'
            )
            return False

        return any([
            is_update_task_request(req) for req in self.mocker.request_history
        ])


@pytest.mark.parametrize(
    ['version', 'series'],
    [
        ('0.15.12', eibkolibri.RemoteKolibri.Series.KOLIBRI_0_15),
        ('0.16.0b9', eibkolibri.RemoteKolibri.Series.KOLIBRI_0_16),
        ('0.16.5', eibkolibri.RemoteKolibri.Series.KOLIBRI_0_16),
        ('0.14.9', None),
        ('0.17.0a1', None),
        ('1', None),
        ('', None),
    ],
)
def test_version(version, series, requests_mock):
    """Test server version matching"""
    server = MockKolibriServer(requests_mock)

    server.set_version_response(version)
    if series is None:
        # Unsupported or invalid versions
        with pytest.raises(
            Exception,
            match=r'Unsupported remote Kolibri version',
        ):
            eibkolibri.RemoteKolibri(SERVER_URL, 'admin', 'admin')
    else:
        # Supported versions
        remote = eibkolibri.RemoteKolibri(SERVER_URL, 'admin', 'admin')
        assert remote.series == series


@pytest.mark.parametrize('version', ['0.15.12', '0.16.0'])
def test_import_channel(version, requests_mock):
    """Test importing channel"""
    server = MockKolibriServer(requests_mock, version)
    server.set_channel_response(exists=False)
    remote = eibkolibri.RemoteKolibri(SERVER_URL, 'admin', 'admin')
    remote.import_channel(CHANNEL_ID)
    assert not server.update_tasks_run()


@pytest.mark.parametrize('version', ['0.15.12', '0.16.0'])
def test_update_channel(version, requests_mock):
    """Test updating channel"""
    server = MockKolibriServer(requests_mock, version)
    server.set_channel_response(exists=True)
    remote = eibkolibri.RemoteKolibri(SERVER_URL, 'admin', 'admin')
    remote.update_channel(CHANNEL_ID)
    assert server.update_tasks_run()


@pytest.mark.parametrize('version', ['0.15.12', '0.16.0'])
def test_seed_channel(version, requests_mock):
    """Test seeding channel"""
    server = MockKolibriServer(requests_mock, version)

    # Import channel
    server.set_channel_response(exists=False)
    remote = eibkolibri.RemoteKolibri(SERVER_URL, 'admin', 'admin')
    remote.seed_channel(CHANNEL_ID)
    assert server.update_tasks_run() is False

    # Update channel
    requests_mock.reset_mock()
    server.set_channel_response(exists=True)
    remote.seed_channel(CHANNEL_ID)
    assert server.update_tasks_run() is True


@pytest.mark.parametrize('version', ['0.15.12', '0.16.0'])
def test_seed_remote_channels(
    version,
    config,
    tmp_builder_paths,
    monkeypatch,
    requests_mock,
):
    # Set the mock server URL in the configuration.
    config.add_section('kolibri')
    config.set('kolibri', 'central_content_base_url', SERVER_URL)
    monkeypatch.setattr(eib, 'get_config', lambda: config)

    # Write credentials to the netrc file.
    netrc_path = os.path.join(tmp_builder_paths['SYSCONFDIR'], 'netrc')
    server_host = urlparse(SERVER_URL).netloc
    with open(netrc_path, 'w') as f:
        f.write(f'machine {server_host} login admin password admin\n')

    # Import channel
    server = MockKolibriServer(requests_mock, version)
    server.set_channel_response(exists=False)
    assert eibkolibri.seed_remote_channels([CHANNEL_ID]) is True
    assert server.update_tasks_run() is False

    # Update channel
    requests_mock.reset_mock()
    server.set_channel_response(exists=True)
    assert eibkolibri.seed_remote_channels([CHANNEL_ID]) is True
    assert server.update_tasks_run() is True
