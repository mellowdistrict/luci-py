#!/usr/bin/env python
# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import datetime
import os
import sys
import unittest

import test_env
test_env.setup_test_env()

from google.appengine.ext import deferred
from google.appengine.ext import ndb

import webtest

import gae_ts_mon
from test_support import test_case

import ts_mon_metrics
from server import bot_management
from server import task_request
from server import task_result


def _gen_task_result_summary(now, key_id, properties=None, **kwargs):
  """Creates a TaskRequest."""
  props = {
    'command': [u'command1'],
    'dimensions': {u'pool': u'default'},
    'env': {},
    'execution_timeout_secs': 24*60*60,
    'io_timeout_secs': None,
  }
  props.update(properties or {})
  args = {
    'created_ts': now,
    'modified_ts': now,
    'name': 'Request name',
    'tags': [u'tag:1'],
    'user': 'Jesus',
    'key': ndb.Key('TaskResultSummary', key_id),
  }
  args.update(kwargs)
  return task_result.TaskResultSummary(**args)


def _gen_bot_info(key_id, last_seen_ts, **kwargs):
  args = {
    'key': ndb.Key('BotRoot', key_id, 'BotInfo', 'info'),
    'last_seen_ts': last_seen_ts,
    'dimensions': {
        'os': ['Linux', 'Ubuntu'],
        'bot_id': [key_id],
    },
  }
  args.update(**kwargs)
  args['dimensions_flat'] = bot_management._dimensions_to_flat(
      args.pop('dimensions'))
  return bot_management.BotInfo(**args)


class TestMetrics(test_case.TestCase):

  def setUp(self):
    super(TestMetrics, self).setUp()
    gae_ts_mon.reset_for_unittest()
    gae_ts_mon.initialize()
    self.now = datetime.datetime(2016, 4, 7)
    self.mock_now(self.now)

  def test_pool_from_dimensions(self):
    dimensions = {
        'os': ['Linux', 'Ubuntu'],
        'cpu': ['x86-64'],
        'id': ['bot_name'],  # This should be excluded from the pool name.
    }
    expected = 'cpu:x86-64|os:Linux|os:Ubuntu'
    self.assertEqual(expected, ts_mon_metrics.pool_from_dimensions(dimensions))

  def test_update_jobs_completed_metrics(self):
    tags = [
        'project:test_project',
        'subproject:test_subproject',
        'master:test_master',
        'buildername:test_builder',
        'name:some_tests',
    ]
    bot_id = 'test_bot'
    fields = {
        'project_id': 'test_project',
        'subproject_id': 'test_subproject',
        'executor_id': bot_id,
        'spec_name': 'test_master:test_builder:some_tests',
    }
    summary = _gen_task_result_summary(self.now, 1, tags=tags)
    summary.exit_code = 0 # sets failure = False.
    summary.internal_failure = False
    summary.duration = 42

    fields['result'] = 'success'
    self.assertIsNone(ts_mon_metrics.jobs_completed.get(fields=fields))
    ts_mon_metrics.update_jobs_completed_metrics(summary, bot_id)
    self.assertEqual(1, ts_mon_metrics.jobs_completed.get(fields=fields))

    summary.exit_code = 1 # sets failure = True.
    fields['result'] = 'failure'
    self.assertIsNone(ts_mon_metrics.jobs_completed.get(fields=fields))
    ts_mon_metrics.update_jobs_completed_metrics(summary, bot_id)
    self.assertEqual(1, ts_mon_metrics.jobs_completed.get(fields=fields))

    summary.internal_failure = True
    fields['result'] = 'infra-failure'
    self.assertIsNone(ts_mon_metrics.jobs_completed.get(fields=fields))
    ts_mon_metrics.update_jobs_completed_metrics(summary, bot_id)
    self.assertEqual(1, ts_mon_metrics.jobs_completed.get(fields=fields))

  def test_initialize(self):
    # Smoke test for syntax errors.
    ts_mon_metrics.initialize()

  def test_set_global_metrics(self):
    tags = [
        'project:test_project',
        'subproject:test_subproject',
        'master:test_master',
        'buildername:test_builder',
        'name:some_tests',
    ]
    summary_running = _gen_task_result_summary(self.now, 1, tags=tags)
    summary_running.state = task_result.State.RUNNING
    summary_running.modified_ts = self.now
    summary_running.bot_id = 'test_bot1'
    summary_running.put()

    summary_pending = _gen_task_result_summary(self.now, 2, tags=tags)
    summary_pending.state = task_result.State.PENDING
    summary_pending.modified_ts = self.now
    summary_pending.bot_id = 'test_bot2'
    summary_pending.put()

    _gen_bot_info('bot_ready', self.now).put()
    _gen_bot_info('bot_running', self.now, task_id='deadbeef').put()
    _gen_bot_info('bot_quarantined', self.now, quarantined=True).put()
    _gen_bot_info('bot_dead', self.now - datetime.timedelta(days=365)).put()
    bots_expected = {
        'bot_ready': 'ready',
        'bot_running': 'running',
        'bot_quarantined': 'quarantined',
        'bot_dead': 'dead',
    }

    ts_mon_metrics._set_global_metrics(self.now)

    jobs_fields = {
        'project_id': 'test_project',
        'subproject_id': 'test_subproject',
        'executor_id': 'test_bot1',
        'spec_name': 'test_master:test_builder:some_tests',
    }
    self.assertEqual('running', ts_mon_metrics.jobs_status.get(
        fields=jobs_fields, target_fields=ts_mon_metrics.TARGET_FIELDS))
    jobs_fields.update({'executor_id': 'test_bot2'})
    self.assertEqual('pending', ts_mon_metrics.jobs_status.get(
        fields=jobs_fields, target_fields=ts_mon_metrics.TARGET_FIELDS))

    for bot_id, status in bots_expected.iteritems():
      self.assertEqual(status,
                       ts_mon_metrics.executors_status.get(
                           fields={'executor_id': bot_id},
                           target_fields=ts_mon_metrics.TARGET_FIELDS))

      self.assertEqual('bot_id:%s|os:Linux|os:Ubuntu' % bot_id,
                       ts_mon_metrics.executors_pool.get(
                           fields={'executor_id': bot_id},
                           target_fields=ts_mon_metrics.TARGET_FIELDS))


if __name__ == '__main__':
  if '-v' in sys.argv:
    unittest.TestCase.maxDiff = None
  unittest.main()
