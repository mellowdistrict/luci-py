#!/usr/bin/env python
# Copyright 2014 The LUCI Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import base64
import logging
import sys
import unittest

from test_support import test_env
test_env.setup_test_env()

import mock

from google.appengine.ext import ndb

from components import auth
from components import config
from components.config import api
from components.config import remote
from components.config import test_config_pb2
from components.config.proto import project_config_pb2
from test_support import test_case


class ApiTestCase(test_case.TestCase):
  def setUp(self):
    super(ApiTestCase, self).setUp()
    self.provider = mock.Mock()
    provider_future = ndb.Future()
    provider_future.set_result(self.provider)
    self.mock(config.api, '_get_config_provider_async', lambda: provider_future)
    self.provider.get_async.return_value = ndb.Future()
    self.provider.get_async.return_value.set_result(
        ('deadbeef', 'param: "value"'))

  def test_get(self):
    revision, cfg = config.get(
        'services/foo', 'bar.cfg', test_config_pb2.Config)
    self.assertEqual(revision, 'deadbeef')
    self.assertEqual(cfg.param, 'value')

  def test_get_self_config(self):
    revision, cfg = config.get_self_config('bar.cfg', test_config_pb2.Config)
    self.assertEqual(revision, 'deadbeef')
    self.assertEqual(cfg.param, 'value')

  def test_get_project_config(self):
    revision, cfg = config.get_project_config(
        'foo', 'bar.cfg', test_config_pb2.Config)
    self.assertEqual(revision, 'deadbeef')
    self.assertEqual(cfg.param, 'value')

  def test_get_ref_config(self):
    revision, cfg = config.get_ref_config('foo', 'refs/x', 'bar.cfg')
    self.assertEqual(revision, 'deadbeef')
    self.assertEqual(cfg, 'param: "value"')

  def test_cannot_load_config(self):
    self.provider.get_async.side_effect = ValueError
    with self.assertRaises(config.CannotLoadConfigError):
      config.get('services/foo', 'bar.cfg')

  def test_get_projects(self):
    self.provider.get_projects_async.return_value = ndb.Future()
    self.provider.get_projects_async.return_value.set_result([
      {
       'id': 'chromium',
       'repo_type': 'GITILES',
       'repo_url': 'https://chromium.googlesource.com/chromium/src',
       'name': 'Chromium browser'
      },
      {
       'id': 'infra',
       'repo_type': 'GITILES',
       'repo_url': 'https://chromium.googlesource.com/infra/infra',
      },
    ])
    projects = config.get_projects()
    self.assertEqual(projects, [
      config.Project(
          id='chromium',
          repo_type='GITILES',
          repo_url='https://chromium.googlesource.com/chromium/src',
          name='Chromium browser'),
      config.Project(
          id='infra',
          repo_type='GITILES',
          repo_url='https://chromium.googlesource.com/infra/infra',
          name=''),
    ])

  def test_get_project_configs(self):
    self.provider.get_project_configs_async.return_value = ndb.Future()
    self.provider.get_project_configs_async.return_value.set_result({
      'projects/chromium': ('deadbeef', 'param: "value"'),
      'projects/v8': ('aaaabbbb', 'param: "value2"'),
      'projects/skia': ('deadbeef', 'invalid config'),
    })

    expected = {
      'chromium': ('deadbeef', test_config_pb2.Config(param='value')),
      'v8': ('aaaabbbb', test_config_pb2.Config(param='value2')),
    }
    actual = config.get_project_configs('bar.cfg', test_config_pb2.Config)
    self.assertEqual(expected, actual)

  def test_get_ref_configs(self):
    self.provider.get_ref_configs_async.return_value = ndb.Future()
    self.provider.get_ref_configs_async.return_value.set_result({
      'projects/chromium/refs/heads/master': ('dead', 'param: "master"'),
      'projects/chromium/refs/non-branch': ('beef', 'param: "ref"'),
      'projects/v8/refs/heads/master': ('aaaa', 'param: "value2"'),
      'projects/skia/refs/heads/master': ('deadbeef', 'invalid config'),
    })

    expected = {
      'chromium': {
        'refs/heads/master': ('dead', test_config_pb2.Config(param='master')),
        'refs/non-branch': ('beef', test_config_pb2.Config(param='ref')),
      },
      'v8': {
        'refs/heads/master': ('aaaa', test_config_pb2.Config(param='value2')),
      },
    }
    actual = config.get_ref_configs('bar.cfg', test_config_pb2.Config)
    self.assertEqual(expected, actual)

  def set_up_identity(self):
    self.mock(auth, 'is_group_member', mock.Mock())
    auth.is_group_member.side_effect = lambda group, *_: group == 'all'
    self.mock(auth, 'get_current_identity', mock.Mock())
    auth.get_current_identity.return_value = auth.Anonymous

  def test_has_project_access_public_group(self):
    self.set_up_identity()

    self.mock(api, 'get_project_config_async', mock.Mock())
    api.get_project_config_async.return_value = ndb.Future()
    api.get_project_config_async.return_value.set_result(
        project_config_pb2.ProjectCfg(
            access=['group:all'],
        )
    )

    self.assertTrue(config.has_project_access('chromium'))

    api.get_project_config_async.assert_called_once('chromium')

  def test_has_project_access_anon_identity(self):
    self.set_up_identity()

    self.mock(api, 'get_project_config_async', mock.Mock())
    api.get_project_config_async.return_value = ndb.Future()
    api.get_project_config_async.return_value.set_result(
        project_config_pb2.ProjectCfg(
            access=['anonymous:anonymous'],
        )
    )

    self.assertTrue(config.has_project_access('chromium'))

    api.get_project_config_async.assert_called_once('chromium')

  def test_has_project_access_no_access(self):
    self.set_up_identity()

    self.mock(api, 'get_project_config_async', mock.Mock())
    api.get_project_config_async.return_value = ndb.Future()
    api.get_project_config_async.return_value.set_result(
        project_config_pb2.ProjectCfg(),  # access not configured => internal
    )

    self.assertFalse(config.has_project_access('chromium'))



if __name__ == '__main__':
  if '-v' in sys.argv:
    unittest.TestCase.maxDiff = None
  else:
    logging.basicConfig(level=logging.CRITICAL)
  unittest.main()
