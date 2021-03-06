#!/usr/bin/env python
# coding=utf-8
# Copyright 2013 The LUCI Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import base64
import json
import logging
import os
import signal
import shutil
import sys
import tempfile
import time
import unittest

import test_env_bot_code
test_env_bot_code.setup_test_env()

# Creates a server mock for functions in net.py.
import net_utils

from api import os_utilities
from depot_tools import fix_encoding
from utils import file_path
from utils import large
from utils import logging_utils
from utils import subprocess42
from utils import tools
import fake_swarming
import task_runner

CLIENT_DIR = os.path.normpath(
    os.path.join(test_env_bot_code.BOT_DIR, '..', '..', '..', 'client'))

sys.path.insert(0, os.path.join(CLIENT_DIR, 'tests'))
import isolateserver_mock


def get_manifest(script=None, inputs_ref=None, **kwargs):
  out = {
    'bot_id': 'localhost',
    'command':
        [sys.executable, '-u', '-c', script] if not inputs_ref else None,
    'env': {},
    'extra_args': [],
    'grace_period': 30.,
    'hard_timeout': 10.,
    'inputs_ref': inputs_ref,
    'io_timeout': 10.,
    'task_id': 23,
  }
  out.update(kwargs)
  return out


class TestTaskRunnerBase(net_utils.TestCase):
  def setUp(self):
    super(TestTaskRunnerBase, self).setUp()
    self.root_dir = tempfile.mkdtemp(prefix='task_runner')
    logging.info('Temp: %s', self.root_dir)
    self.work_dir = os.path.join(self.root_dir, 'work')
    os.chdir(self.root_dir)
    os.mkdir(self.work_dir)
    # Create the logs directory so run_isolated.py can put its log there.
    os.mkdir(os.path.join(self.root_dir, 'logs'))

    self.mock(
        task_runner, 'get_run_isolated',
        lambda: [sys.executable, os.path.join(CLIENT_DIR, 'run_isolated.py')])

  def tearDown(self):
    os.chdir(test_env_bot_code.BOT_DIR)
    try:
      file_path.rmtree(self.root_dir)
    except OSError:
      print >> sys.stderr, 'Failed to delete %s' % self.root_dir
    finally:
      super(TestTaskRunnerBase, self).tearDown()

  @classmethod
  def get_task_details(cls, *args, **kwargs):
    return task_runner.TaskDetails(get_manifest(*args, **kwargs))

  def gen_requests(self, cost_usd=0., **kwargs):
    return [
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        self.get_check_first(cost_usd),
        {'ok': True},
      ),
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        self.get_check_final(**kwargs),
        {'ok': True},
      ),
    ]

  def requests(self, **kwargs):
    """Generates the expected HTTP requests for a task run."""
    self.expected_requests(self.gen_requests(**kwargs))

  def get_check_first(self, cost_usd):
    def check_first(kwargs):
      self.assertLessEqual(cost_usd, kwargs['data'].pop('cost_usd'))
      self.assertEqual(
        {
          'data': {
            'id': 'localhost',
            'task_id': 23,
          },
        },
        kwargs)
    return check_first


class TestTaskRunner(TestTaskRunnerBase):
  def setUp(self):
    super(TestTaskRunner, self).setUp()
    self.mock(time, 'time', lambda: 1000000000.)

  def get_check_final(self, exit_code=0, output='hi\n', outputs_ref=None):
    def check_final(kwargs):
      # It makes the diffing easier.
      if 'output' in kwargs['data']:
        kwargs['data']['output'] = base64.b64decode(kwargs['data']['output'])
      expected = {
        'data': {
          'cost_usd': 10.,
          'duration': 0.,
          'exit_code': exit_code,
          'hard_timeout': False,
          'id': 'localhost',
          'io_timeout': False,
          'output': output,
          'output_chunk_start': 0,
          'task_id': 23,
        },
      }
      if outputs_ref:
        expected['data']['outputs_ref'] = outputs_ref
      self.assertEqual(expected, kwargs)
    return check_final

  def _run_command(self, task_details):
    start = time.time()
    self.mock(time, 'time', lambda: start + 10)
    server = 'https://localhost:1'
    return task_runner.run_command(
        server, task_details, self.work_dir, 3600., start, 1)

  def test_load_and_run_raw(self):
    server = 'https://localhost:1'

    def run_command(
        swarming_server, task_details, work_dir, cost_usd_hour, start,
        min_free_space):
      self.assertEqual(server, swarming_server)
      # Necessary for OSX.
      self.assertEqual(
          os.path.realpath(self.work_dir), os.path.realpath(work_dir))
      self.assertTrue(isinstance(task_details, task_runner.TaskDetails))
      self.assertEqual(3600., cost_usd_hour)
      self.assertEqual(time.time(), start)
      self.assertEqual(1, min_free_space)
      return {
        u'exit_code': 1,
        u'hard_timeout': False,
        u'io_timeout': False,
        u'must_signal_internal_failure': None,
        u'version': task_runner.OUT_VERSION,
      }
    self.mock(task_runner, 'run_command', run_command)

    manifest = os.path.join(self.root_dir, 'manifest')
    with open(manifest, 'wb') as f:
      data = {
        'bot_id': 'localhost',
        'command': ['a'],
        'env': {'d': 'e'},
        'extra_args': [],
        'grace_period': 30.,
        'hard_timeout': 10,
        'inputs_ref': None,
        'io_timeout': 11,
        'task_id': 23,
      }
      json.dump(data, f)

    out_file = os.path.join(self.root_dir, 'work', 'task_runner_out.json')
    task_runner.load_and_run(manifest, server, 3600., time.time(), out_file, 1)
    expected = {
      u'exit_code': 1,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    with open(out_file, 'rb') as f:
      self.assertEqual(expected, json.load(f))

  def test_load_and_run_isolated(self):
    self.expected_requests([])
    server = 'https://localhost:1'

    def run_command(
        swarming_server, task_details, work_dir, cost_usd_hour, start,
        min_free_space):
      self.assertEqual(server, swarming_server)
      # Necessary for OSX.
      self.assertEqual(
          os.path.realpath(self.work_dir), os.path.realpath(work_dir))
      self.assertTrue(isinstance(task_details, task_runner.TaskDetails))
      self.assertEqual(3600., cost_usd_hour)
      self.assertEqual(time.time(), start)
      self.assertEqual(1, min_free_space)
      return {
        u'exit_code': 0,
        u'hard_timeout': False,
        u'io_timeout': False,
        u'must_signal_internal_failure': None,
        u'version': task_runner.OUT_VERSION,
      }
    self.mock(task_runner, 'run_command', run_command)

    manifest = os.path.join(self.root_dir, 'manifest')
    with open(manifest, 'wb') as f:
      data = {
        'bot_id': 'localhost',
        'command': None,
        'env': {'d': 'e'},
        'extra_args': ['foo', 'bar'],
        'grace_period': 30.,
        'hard_timeout': 10,
        'io_timeout': 11,
        'inputs_ref': {
          'isolated': '123',
          'isolatedserver': 'http://localhost:1',
          'namespace': 'default-gzip',
        },
        'task_id': 23,
      }
      json.dump(data, f)

    out_file = os.path.join(self.root_dir, 'work', 'task_runner_out.json')
    task_runner.load_and_run(manifest, server, 3600., time.time(), out_file, 1)
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    with open(out_file, 'rb') as f:
      self.assertEqual(expected, json.load(f))

  def test_run_command_raw(self):
    # This runs the command for real.
    self.requests(cost_usd=1, exit_code=0)
    task_details = self.get_task_details('print(\'hi\')')
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_run_command_isolated(self):
    # This runs the command for real.
    self.requests(
        cost_usd=1, exit_code=0,
        outputs_ref={
          u'isolated': u'123',
          u'isolatedserver': u'http://localhost:1',
          u'namespace': u'default-gzip',
        })
    task_details = self.get_task_details(inputs_ref={
      'isolated': '123',
      'isolatedserver': 'localhost:1',
      'namespace': 'default-gzip',
    }, extra_args=['foo', 'bar'])
    # Mock running run_isolated with a script.
    SCRIPT_ISOLATED = (
      'import json, sys;\n'
      'if len(sys.argv) != 2:\n'
      '  raise Exception(sys.argv);\n'
      'with open(sys.argv[1], \'wb\') as f:\n'
      '  json.dump({\n'
      '    \'exit_code\': 0,\n'
      '    \'had_hard_timeout\': False,\n'
      '    \'internal_failure\': None,\n'
      '    \'outputs_ref\': {\n'
      '      \'isolated\': \'123\',\n'
      '      \'isolatedserver\': \'http://localhost:1\',\n'
      '       \'namespace\': \'default-gzip\',\n'
      '    },\n'
      '  }, f)\n'
      'sys.stdout.write(\'hi\\n\')')
    self.mock(
        task_runner, 'get_isolated_cmd',
        lambda _work_dir, _details, isolated_result, min_free_space:
          [sys.executable, '-u', '-c', SCRIPT_ISOLATED, isolated_result])
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_run_command_fail(self):
    # This runs the command for real.
    self.requests(cost_usd=10., exit_code=1)
    task_details = self.get_task_details(
        'import sys; print(\'hi\'); sys.exit(1)')
    expected = {
      u'exit_code': 1,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_run_command_os_error(self):
    # This runs the command for real.
    # OS specific error, fix expectation for other OSes.
    output = (
      'Command "executable_that_shouldnt_be_on_your_system '
      'thus_raising_OSError" failed to start.\n'
      'Error: [Error 2] The system cannot find the file specified'
      ) if sys.platform == 'win32' else (
      'Command "executable_that_shouldnt_be_on_your_system '
      'thus_raising_OSError" failed to start.\n'
      'Error: [Errno 2] No such file or directory')
    self.requests(cost_usd=10., exit_code=1, output=output)
    task_details = task_runner.TaskDetails(
        {
          'bot_id': 'localhost',
          'command': [
            'executable_that_shouldnt_be_on_your_system',
            'thus_raising_OSError',
          ],
          'env': {},
          'extra_args': [],
          'grace_period': 30.,
          'hard_timeout': 6,
          'inputs_ref': None,
          'io_timeout': 6,
          'task_id': 23,
        })
    expected = {
      u'exit_code': -1,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_run_command_large(self):
    # Method should have "self" as first argument - pylint: disable=E0213
    class Popen(object):
      """Mocks the process so we can control how data is returned."""
      def __init__(self2, cmd, cwd, env, stdout, stderr, stdin, detached):
        self.assertEqual(task_details.command, cmd)
        self.assertEqual(self.work_dir, cwd)
        expected_env = os.environ.copy()
        expected_env['foo'] = 'bar'
        self.assertEqual(expected_env, env)
        self.assertEqual(subprocess42.PIPE, stdout)
        self.assertEqual(subprocess42.STDOUT, stderr)
        self.assertEqual(subprocess42.PIPE, stdin)
        self.assertEqual(True, detached)
        self2._out = [
          'hi!\n',
          'hi!\n',
          'hi!\n' * 100000,
          'hi!\n',
        ]

      def yield_any(self2, maxsize, timeout):
        self.assertLess(0, maxsize)
        self.assertLess(0, timeout)
        for i in self2._out:
          yield 'stdout', i

      @staticmethod
      def wait():
        return 0

      @staticmethod
      def kill():
        self.fail()

    self.mock(subprocess42, 'Popen', Popen)

    def check_final(kwargs):
      self.assertEqual(
          {
            'data': {
              # That's because the cost includes the duration starting at start,
              # not when the process was started.
              'cost_usd': 10.,
              'duration': 0.,
              'exit_code': 0,
              'hard_timeout': False,
              'id': 'localhost',
              'io_timeout': False,
              'output': base64.b64encode('hi!\n'),
              'output_chunk_start': 100002*4,
              'task_id': 23,
            },
          },
          kwargs)

    requests = [
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        {
          'data': {
            'cost_usd': 10.,
            'id': 'localhost',
            'task_id': 23,
          },
        },
        {'ok': True},
      ),
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        {
          'data': {
            'cost_usd': 10.,
            'id': 'localhost',
            'output': base64.b64encode('hi!\n' * 100002),
            'output_chunk_start': 0,
            'task_id': 23,
          },
        },
        {'ok': True},
      ),
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        check_final,
        {'ok': True},
      ),
    ]
    self.expected_requests(requests)
    task_details = task_runner.TaskDetails(
        {
          'bot_id': 'localhost',
          'command': ['large', 'executable'],
          'env': {'foo': 'bar'},
          'extra_args': [],
          'grace_period': 30.,
          'hard_timeout': 60,
          'inputs_ref': None,
          'io_timeout': 60,
          'task_id': 23,
        })
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_main(self):
    def load_and_run(
        manifest, swarming_server, cost_usd_hour, start, json_file,
        min_free_space):
      self.assertEqual('foo', manifest)
      self.assertEqual('http://localhost', swarming_server)
      self.assertEqual(3600., cost_usd_hour)
      self.assertEqual(time.time(), start)
      self.assertEqual('task_summary.json', json_file)
      self.assertEqual(1, min_free_space)

    self.mock(task_runner, 'load_and_run', load_and_run)
    cmd = [
      '--swarming-server', 'http://localhost',
      '--in-file', 'foo',
      '--out-file', 'task_summary.json',
      '--cost-usd-hour', '3600',
      '--start', str(time.time()),
      '--min-free-space', '1',
    ]
    self.assertEqual(0, task_runner.main(cmd))

  def test_main_reboot(self):
    def load_and_run(
        manifest, swarming_server, cost_usd_hour, start, json_file,
        min_free_space):
      self.assertEqual('foo', manifest)
      self.assertEqual('http://localhost', swarming_server)
      self.assertEqual(3600., cost_usd_hour)
      self.assertEqual(time.time(), start)
      self.assertEqual('task_summary.json', json_file)
      self.assertEqual(1, min_free_space)

    self.mock(task_runner, 'load_and_run', load_and_run)
    cmd = [
      '--swarming-server', 'http://localhost',
      '--in-file', 'foo',
      '--out-file', 'task_summary.json',
      '--cost-usd-hour', '3600',
      '--start', str(time.time()),
      '--min-free-space', '1',
    ]
    self.assertEqual(0, task_runner.main(cmd))


class TestTaskRunnerNoTimeMock(TestTaskRunnerBase):
  # Do not mock time.time() for these tests otherwise it becomes a tricky
  # implementation detail check.
  # These test cases run the command for real.

  # TODO(maruel): Calculate this value automatically through iteration?
  SHORT_TIME_OUT = 0.3

  # Here's a simple script that handles signals properly. Sadly SIGBREAK is not
  # defined on posix.
  SCRIPT_SIGNAL = (
    'import signal, sys, time;\n'
    'l = [];\n'
    'def handler(signum, _):\n'
    '  l.append(signum);\n'
    '  print(\'got signal %%d\' %% signum);\n'
    '  sys.stdout.flush();\n'
    'signal.signal(%s, handler);\n'
    'print(\'hi\');\n'
    'sys.stdout.flush();\n'
    'while not l:\n'
    '  try:\n'
    '    time.sleep(0.01);\n'
    '  except IOError:\n'
    '    pass;\n'
    'print(\'bye\')') % (
        'signal.SIGBREAK' if sys.platform == 'win32' else 'signal.SIGTERM')

  SCRIPT_SIGNAL_HANG = (
    'import signal, sys, time;\n'
    'l = [];\n'
    'def handler(signum, _):\n'
    '  l.append(signum);\n'
    '  print(\'got signal %%d\' %% signum);\n'
    '  sys.stdout.flush();\n'
    'signal.signal(%s, handler);\n'
    'print(\'hi\');\n'
    'sys.stdout.flush();\n'
    'while not l:\n'
    '  try:\n'
    '    time.sleep(0.01);\n'
    '  except IOError:\n'
    '    pass;\n'
    'print(\'bye\');\n'
    'time.sleep(100)') % (
        'signal.SIGBREAK' if sys.platform == 'win32' else 'signal.SIGTERM')

  SCRIPT_HANG = 'import time; print(\'hi\'); time.sleep(100)'

  def get_check_final(
      self, hard_timeout=False, io_timeout=False, exit_code=None,
      output='hi\n'):
    def check_final(kwargs):
      if hard_timeout or io_timeout:
        self.assertLess(self.SHORT_TIME_OUT, kwargs['data'].pop('cost_usd'))
        self.assertLess(self.SHORT_TIME_OUT, kwargs['data'].pop('duration'))
      else:
        self.assertLess(0., kwargs['data'].pop('cost_usd'))
        self.assertLess(0., kwargs['data'].pop('duration'))
      # It makes the diffing easier.
      kwargs['data']['output'] = base64.b64decode(kwargs['data']['output'])
      self.assertEqual(
          {
            'data': {
              'exit_code': exit_code,
              'hard_timeout': hard_timeout,
              'id': 'localhost',
              'io_timeout': io_timeout,
              'output': output,
              'output_chunk_start': 0,
              'task_id': 23,
            },
          },
          kwargs)
    return check_final

  def _load_and_run(self, manifest):
    # Dot not mock time since this test class is testing timeouts.
    server = 'https://localhost:1'
    in_file = os.path.join(self.work_dir, 'task_runner_in.json')
    with open(in_file, 'wb') as f:
      json.dump(manifest, f)
    out_file = os.path.join(self.work_dir, 'task_runner_out.json')
    task_runner.load_and_run(in_file, server, 3600., time.time(), out_file, 1)
    with open(out_file, 'rb') as f:
      return json.load(f)

  def _run_command(self, task_details):
    # Dot not mock time since this test class is testing timeouts.
    server = 'https://localhost:1'
    return task_runner.run_command(
        server, task_details, self.work_dir, 3600., time.time(), 1)

  def test_hard(self):
    # Actually 0xc000013a
    sig = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM
    self.requests(hard_timeout=True, exit_code=sig)
    task_details = self.get_task_details(
        self.SCRIPT_HANG, hard_timeout=self.SHORT_TIME_OUT)
    expected = {
      u'exit_code': sig,
      u'hard_timeout': True,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_io(self):
    # Actually 0xc000013a
    sig = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM
    self.requests(io_timeout=True, exit_code=sig)
    task_details = self.get_task_details(
        self.SCRIPT_HANG, io_timeout=self.SHORT_TIME_OUT)
    expected = {
      u'exit_code': sig,
      u'hard_timeout': False,
      u'io_timeout': True,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_hard_signal(self):
    self.requests(
        hard_timeout=True,
        exit_code=0,
        output='hi\ngot signal %d\nbye\n' % task_runner.SIG_BREAK_OR_TERM)
    task_details = self.get_task_details(
        self.SCRIPT_SIGNAL, hard_timeout=self.SHORT_TIME_OUT)
    # Returns 0 because the process cleaned up itself.
    expected = {
      u'exit_code': 0,
      u'hard_timeout': True,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_io_signal(self):
    self.requests(
        io_timeout=True, exit_code=0,
        output='hi\ngot signal %d\nbye\n' % task_runner.SIG_BREAK_OR_TERM)
    task_details = self.get_task_details(
        self.SCRIPT_SIGNAL, io_timeout=self.SHORT_TIME_OUT)
    # Returns 0 because the process cleaned up itself.
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': True,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_hard_no_grace(self):
    # Actually 0xc000013a
    sig = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM
    self.requests(hard_timeout=True, exit_code=sig)
    task_details = self.get_task_details(
        self.SCRIPT_HANG, hard_timeout=self.SHORT_TIME_OUT,
        grace_period=self.SHORT_TIME_OUT)
    expected = {
      u'exit_code': sig,
      u'hard_timeout': True,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_io_no_grace(self):
    # Actually 0xc000013a
    sig = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM
    self.requests(io_timeout=True, exit_code=sig)
    task_details = self.get_task_details(
        self.SCRIPT_HANG, io_timeout=self.SHORT_TIME_OUT,
        grace_period=self.SHORT_TIME_OUT)
    expected = {
      u'exit_code': sig,
      u'hard_timeout': False,
      u'io_timeout': True,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_hard_signal_no_grace(self):
    exit_code = 1 if sys.platform == 'win32' else -signal.SIGKILL
    self.requests(
        hard_timeout=True, exit_code=exit_code,
        output='hi\ngot signal %d\nbye\n' % task_runner.SIG_BREAK_OR_TERM)
    task_details = self.get_task_details(
        self.SCRIPT_SIGNAL_HANG, hard_timeout=self.SHORT_TIME_OUT,
        grace_period=self.SHORT_TIME_OUT)
    # Returns 0 because the process cleaned up itself.
    expected = {
      u'exit_code': exit_code,
      u'hard_timeout': True,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_io_signal_no_grace(self):
    exit_code = 1 if sys.platform == 'win32' else -signal.SIGKILL
    self.requests(
        io_timeout=True, exit_code=exit_code,
        output='hi\ngot signal %d\nbye\n' % task_runner.SIG_BREAK_OR_TERM)
    task_details = self.get_task_details(
        self.SCRIPT_SIGNAL_HANG, io_timeout=self.SHORT_TIME_OUT,
        grace_period=self.SHORT_TIME_OUT)
    # Returns 0 because the process cleaned up itself.
    expected = {
      u'exit_code': exit_code,
      u'hard_timeout': False,
      u'io_timeout': True,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual(expected, self._run_command(task_details))

  def test_isolated_grand_children(self):
    """Runs a normal test involving 3 level deep subprocesses."""
    # Uses load_and_run()
    files = {
      'parent.py': (
        'import subprocess, sys\n'
        'sys.exit(subprocess.call([sys.executable,\'-u\',\'children.py\']))\n'),
      'children.py': (
        'import subprocess, sys\n'
        'sys.exit(subprocess.call('
            '[sys.executable, \'-u\', \'grand_children.py\']))\n'),
      'grand_children.py': 'print \'hi\'',
    }

    def check_final(kwargs):
      # Warning: this modifies input arguments.
      self.assertLess(0, kwargs['data'].pop('cost_usd'))
      self.assertLess(0, kwargs['data'].pop('bot_overhead'))
      self.assertLess(0, kwargs['data'].pop('duration'))
      self.assertLess(
          0., kwargs['data']['isolated_stats']['download'].pop('duration'))
      # duration==0 can happen on Windows when the clock is in the default
      # resolution, 15.6ms.
      self.assertLessEqual(
          0., kwargs['data']['isolated_stats']['upload'].pop('duration'))
      # Makes the diffing easier.
      kwargs['data']['output'] = base64.b64decode(kwargs['data']['output'])
      for k in ('download', 'upload'):
        for j in ('items_cold', 'items_hot'):
          kwargs['data']['isolated_stats'][k][j] = large.unpack(
              base64.b64decode(kwargs['data']['isolated_stats'][k][j]))
      self.assertEqual(
          {
            'data': {
              'exit_code': 0,
              'hard_timeout': False,
              'id': u'localhost',
              'io_timeout': False,
              'isolated_stats': {
                u'download': {
                  u'initial_number_items': 0,
                  u'initial_size': 0,
                  u'items_cold': [10, 86, 94, 276],
                  u'items_hot': [],
                },
                u'upload': {
                  u'items_cold': [],
                  u'items_hot': [],
                },
              },
              'output': 'hi\n',
              'output_chunk_start': 0,
              'task_id': 23,
            },
          },
          kwargs)
    requests = [
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        self.get_check_first(0.),
        {'ok': True},
      ),
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        check_final,
        {'ok': True},
      ),
    ]
    self.expected_requests(requests)

    server = isolateserver_mock.MockIsolateServer()
    try:
      isolated = json.dumps({
        'command': ['python', 'parent.py'],
        'files': {
          name: {
            'h': server.add_content_compressed('default-gzip', content),
            's': len(content),
          } for name, content in files.iteritems()
        },
      })
      isolated_digest = server.add_content_compressed('default-gzip', isolated)
      manifest = get_manifest(
          inputs_ref={
            'isolated': isolated_digest,
            'namespace': 'default-gzip',
            'isolatedserver': server.url,
          })
      expected = {
        u'exit_code': 0,
        u'hard_timeout': False,
        u'io_timeout': False,
        u'must_signal_internal_failure': None,
        u'version': task_runner.OUT_VERSION,
      }
      self.assertEqual(expected, self._load_and_run(manifest))
    finally:
      server.close()

  def test_isolated_io_signal_no_grace_grand_children(self):
    """Handles grand-children process hanging and signal management.

    In this case, the I/O timeout is implemented by task_runner. An hard timeout
    would be implemented by run_isolated (depending on overhead).
    """
    # Uses load_and_run()
    # https://msdn.microsoft.com/library/cc704588.aspx
    # STATUS_CONTROL_C_EXIT=0xC000013A. Python sees it as -1073741510.
    exit_code = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM

    files = {
      'parent.py': (
        'import subprocess, sys\n'
        'print(\'parent\')\n'
        'p = subprocess.Popen([sys.executable, \'-u\', \'children.py\'])\n'
        'print(p.pid)\n'
        'p.wait()\n'
        'sys.exit(p.returncode)\n'),
      'children.py': (
        'import subprocess, sys\n'
        'print(\'children\')\n'
        'p = subprocess.Popen([sys.executable,\'-u\',\'grand_children.py\'])\n'
        'print(p.pid)\n'
        'p.wait()\n'
        'sys.exit(p.returncode)\n'),
      'grand_children.py': self.SCRIPT_SIGNAL_HANG,
    }
    # We need to catch the pid of the grand children to be able to kill it, so
    # create our own check_final() instead of using self._gen_requests().
    to_kill = []
    def check_final(kwargs):
      self.assertLess(self.SHORT_TIME_OUT, kwargs['data'].pop('cost_usd'))
      self.assertLess(self.SHORT_TIME_OUT, kwargs['data'].pop('duration'))
      self.assertLess(0., kwargs['data'].pop('bot_overhead'))
      self.assertLess(
          0., kwargs['data']['isolated_stats']['download'].pop('duration'))
      self.assertLess(
          0., kwargs['data']['isolated_stats']['upload'].pop('duration'))
      # Makes the diffing easier.
      for k in ('download', 'upload'):
        for j in ('items_cold', 'items_hot'):
          kwargs['data']['isolated_stats'][k][j] = large.unpack(
              base64.b64decode(kwargs['data']['isolated_stats'][k][j]))
      # The command print the pid of this child and grand-child processes, each
      # on its line.
      output = base64.b64decode(kwargs['data'].pop('output', ''))
      for line in output.splitlines():
        try:
          to_kill.append(int(line))
        except ValueError:
          pass
      self.assertEqual(
          {
            'data': {
              'exit_code': exit_code,
              'hard_timeout': False,
              'id': u'localhost',
              'io_timeout': True,
              'isolated_stats': {
                u'download': {
                  u'initial_number_items': 0,
                  u'initial_size': 0,
                  u'items_cold': [144, 150, 285, 307],
                  u'items_hot': [],
                },
                u'upload': {
                  u'items_cold': [],
                  u'items_hot': [],
                },
              },
              'output_chunk_start': 0,
              'task_id': 23,
            },
          },
          kwargs)
    requests = [
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        self.get_check_first(0.),
        {'ok': True},
      ),
      (
        'https://localhost:1/swarming/api/v1/bot/task_update/23',
        check_final,
        {'ok': True},
      ),
    ]
    self.expected_requests(requests)

    server = isolateserver_mock.MockIsolateServer()
    try:
      # TODO(maruel): -u is needed if you don't want python buffering to
      # interfere.
      isolated = json.dumps({
        'command': ['python', '-u', 'parent.py'],
        'files': {
          name: {
            'h': server.add_content_compressed('default-gzip', content),
            's': len(content),
          } for name, content in files.iteritems()
        },
      })
      isolated_digest = server.add_content_compressed('default-gzip', isolated)
      try:
        manifest = get_manifest(
            inputs_ref={
              'isolated': isolated_digest,
              'namespace': 'default-gzip',
              'isolatedserver': server.url,
            },
            # TODO(maruel): A bit cheezy, we'd want the I/O timeout to be just
            # enough to have the time for the PID to be printed but not more.
            io_timeout=1,
            grace_period=self.SHORT_TIME_OUT)
        expected = {
          u'exit_code': exit_code,
          u'hard_timeout': False,
          u'io_timeout': True,
          u'must_signal_internal_failure': None,
          u'version': task_runner.OUT_VERSION,
        }
        self.assertEqual(expected, self._load_and_run(manifest))
        self.assertEqual(2, len(to_kill))
      finally:
        for k in to_kill:
          try:
            if sys.platform == 'win32':
              os.kill(k, signal.SIGTERM)
            else:
              os.kill(k, signal.SIGKILL)
          except OSError:
            pass
    finally:
      server.close()


class TaskRunnerSmoke(unittest.TestCase):
  # Runs a real process and a real Swarming fake server.
  def setUp(self):
    super(TaskRunnerSmoke, self).setUp()
    self.root_dir = tempfile.mkdtemp(prefix='task_runner')
    logging.info('Temp: %s', self.root_dir)
    self._server = fake_swarming.Server(self)

  def tearDown(self):
    try:
      self._server.shutdown()
    finally:
      try:
        file_path.rmtree(self.root_dir)
      except OSError:
        print >> sys.stderr, 'Failed to delete %s' % self.root_dir
      finally:
        super(TaskRunnerSmoke, self).tearDown()

  def test_signal(self):
    # Tests when task_runner gets a SIGTERM.

    # https://msdn.microsoft.com/library/cc704588.aspx
    # STATUS_ENTRYPOINT_NOT_FOUND=0xc0000139. Python sees it as -1073741510.
    exit_code = -1073741510 if sys.platform == 'win32' else -signal.SIGTERM

    os.mkdir(os.path.join(self.root_dir, 'work'))
    signal_file = os.path.join(self.root_dir, 'work', 'signal')
    open(signal_file, 'wb').close()
    manifest = get_manifest(
        script='import os,time;os.remove(%r);time.sleep(60)' % signal_file,
        hard_timeout=60., io_timeout=60.)
    task_in_file = os.path.join(self.root_dir, 'task_runner_in.json')
    task_result_file = os.path.join(self.root_dir, 'task_runner_out.json')
    with open(task_in_file, 'wb') as f:
      json.dump(manifest, f)
    bot = os.path.join(self.root_dir, 'swarming_bot.1.zip')
    code, _ = fake_swarming.gen_zip(self._server.url)
    with open(bot, 'wb') as f:
      f.write(code)
    cmd = [
      sys.executable, bot, 'task_runner',
      '--swarming-server', self._server.url,
      '--in-file', task_in_file,
      '--out-file', task_result_file,
      '--cost-usd-hour', '1',
      # Include the time taken to poll the task in the cost.
      '--start', str(time.time()),
    ]
    logging.info('%s', cmd)
    proc = subprocess42.Popen(cmd, cwd=self.root_dir, detached=True)
    # Wait for the child process to be alive.
    while os.path.isfile(signal_file):
      time.sleep(0.01)
    # Send SIGTERM to task_runner itself. Ensure the right thing happen.
    # Note that on Windows, this is actually sending a SIGBREAK since there's no
    # such thing as SIGTERM.
    proc.send_signal(signal.SIGTERM)
    proc.wait()
    task_runner_log = os.path.join(self.root_dir, 'logs', 'task_runner.log')
    with open(task_runner_log, 'rb') as f:
      logging.info('task_runner.log:\n---\n%s---', f.read())
    expected = {
      u'exit_code': 0,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure': None,
      u'version': task_runner.OUT_VERSION,
    }
    self.assertEqual([], self._server.get_events())
    tasks = self._server.get_tasks()
    for task in tasks.itervalues():
      for event in task:
        event.pop('cost_usd')
        event.pop('duration', None)
        event.pop('bot_overhead', None)
    expected = {
      '23': [
        {
          u'id': u'localhost',
          u'task_id': 23,
        },
        {
          u'exit_code': exit_code,
          u'hard_timeout': False,
          u'id': u'localhost',
          u'io_timeout': False,
          u'task_id': 23,
        },
      ],
    }
    self.assertEqual(expected, tasks)
    expected = {
      'swarming_bot.1.zip',
      '4e019f31778ba7191f965469dc673280386bbd60-cacert.pem',
      'work',
      'logs',
      # TODO(maruel): Move inside work.
      'task_runner_in.json',
      'task_runner_out.json',
    }
    self.assertEqual(expected, set(os.listdir(self.root_dir)))
    expected = {
      u'exit_code': exit_code,
      u'hard_timeout': False,
      u'io_timeout': False,
      u'must_signal_internal_failure':
          u'task_runner received signal %d' % task_runner.SIG_BREAK_OR_TERM,
      u'version': 3,
    }
    with open(task_result_file, 'rb') as f:
      self.assertEqual(expected, json.load(f))
    self.assertEqual(0, proc.returncode)


if __name__ == '__main__':
  fix_encoding.fix_encoding()
  if '-v' in sys.argv:
    unittest.TestCase.maxDiff = None
  logging_utils.prepare_logging(None)
  logging_utils.set_console_level(
      logging.DEBUG if '-v' in sys.argv else logging.CRITICAL+1)
  # Fix litteral text expectation.
  os.environ['LANG'] = 'en_US.UTF-8'
  os.environ['LANGUAGE'] = 'en_US.UTF-8'
  unittest.main()
