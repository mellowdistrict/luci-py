#!/usr/bin/env python
# coding=utf-8
# Copyright 2013 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import StringIO
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

# Import everything that does not require sys.path hack first.
import local_test_runner
import logging_utils

from common import test_request_message

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, ROOT_DIR)

import test_env

test_env.setup_test_env()

from depot_tools import auto_stub
from mox import mox

# pylint: disable=W0212


DATA_FILE_REGEX = r'\S*/%s/%s'
DATA_FOLDER_REGEX = r'\S*/%s'


class TestLocalTestRunner(auto_stub.TestCase):
  """Test class for the LocalTestRunner class."""

  def setUp(self):
    super(TestLocalTestRunner, self).setUp()
    self.result_url = 'http://::2:2/result2'
    self.ping_url = 'http://::2:2/ping'
    self.ping_delay = 10
    self.test_run_name = 'TestRunName'
    self.config_name = 'ConfigName'
    self._mox = mox.Mox()
    self._mox.StubOutWithMock(local_test_runner, 'url_helper')
    self._mox.StubOutWithMock(local_test_runner, 'zipfile')

    (handle, self.data_file_name) = tempfile.mkstemp(
        prefix='local_test_runner_test')
    os.close(handle)
    self.files_to_remove = [self.data_file_name]

    self.result_string = 'This is a result string'
    self.result_codes = [1, 42, 3, 0]

    self.test_name1 = 'test1'
    self.test_name2 = 'test2'
    self.mock_proc = None
    self.mock_zipfile = None
    self.mock(logging_utils, 'set_console_level', lambda _: None)

  def tearDown(self):
    self._mox.UnsetStubs()
    for file_to_remove in self.files_to_remove:
      os.remove(file_to_remove)
    super(TestLocalTestRunner, self).tearDown()

  def CreateValidFile(self, test_objects_data=None, test_run_data=None,
                      test_run_env=None):
    """Creates a text file that the local_test_runner can load.

    Args:
      test_objects_data: An array of 6-tuples with (test_name, action,
          decorate_output, env_vars, hard_time_out, io_time_out) values them.
          A TestObject will be created for each of them.
      test_run_data: The data list to be passed to the TestRun object.
      test_run_env: The dictionary to be used for the test run's env_vars.
    """
    test_objects_data = test_objects_data or [('a', ['a'], None, 0, 0)]
    test_run_data = test_run_data or []

    dimensions = dict(os='a', browser='a', cpu='a')
    test_config = test_request_message.TestConfiguration(
        config_name=self.config_name, dimensions=dimensions)
    test_objects = []
    for test_object_data in test_objects_data:
      assert len(test_object_data) == 5, test_object_data
      test_objects.append(test_request_message.TestObject(
          test_name=test_object_data[0], action=test_object_data[1],
          decorate_output=test_object_data[2],
          hard_time_out=test_object_data[3], io_time_out=test_object_data[4]))

    test_run = test_request_message.TestRun(
        env_vars=test_run_env,
        data=test_run_data,
        configuration=test_config,
        result_url=self.result_url,
        ping_url=self.ping_url,
        ping_delay=self.ping_delay,
        tests=test_objects)

    data = test_request_message.Stringize(test_run, json_readable=True)
    with open(self.data_file_name, 'wb') as f:
      f.write(data.encode('utf-8'))

  def testInvalidTestRunFiles(self):
    def TestInvalidContent(file_content):
      with open(self.data_file_name, 'wb') as f:
        f.write(file_content)
      with self.assertRaises(local_test_runner.Error):
        local_test_runner.LocalTestRunner(self.data_file_name)
    TestInvalidContent('')
    TestInvalidContent('{}')
    TestInvalidContent('{\'a\':}')
    TestInvalidContent('{\'data\': []}')
    TestInvalidContent('{\'data\': [\'a\']}')
    TestInvalidContent('{\'data\': [\'a\'],'
                       '\'result_url\':\'http://a.com\'}')
    TestInvalidContent('{\'data\': [\'a\'],'
                       '\'result_url\':\'http://a.com\', \'tests\': [\'a\']}')

  def testValidTestRunFiles(self):
    self.CreateValidFile()
    # This will raise an exception, which will make the test fail,
    # if there is a problem.
    local_test_runner.LocalTestRunner(self.data_file_name)

  def PrepareRunCommandCall(self, cmd):
    self.mock_proc = self._mox.CreateMock(subprocess.Popen)
    self._mox.StubOutWithMock(local_test_runner.subprocess, 'Popen')
    local_test_runner.subprocess.Popen(
        cmd,
        env=None,
        bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        universal_newlines=True,
        cwd=os.path.join(BASE_DIR, 'work')).AndReturn(self.mock_proc)

    (output_pipe, input_pipe) = os.pipe()
    self.mock_proc.stdin_handle = os.fdopen(input_pipe, 'wb')

    stdout_handle = os.fdopen(output_pipe)
    self.mock_proc.stdout = stdout_handle

  def testRunCommand(self):
    cmd = ['a']
    exit_code = 42
    self.PrepareRunCommandCall(cmd)

    self.mock_proc.stdin_handle.write(self.result_string)
    self.mock_proc.poll().AndReturn(None)
    self.mock_proc.poll().WithSideEffects(
        self.mock_proc.stdin_handle.close()).AndReturn(exit_code)
    self._mox.ReplayAll()

    self.CreateValidFile()
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    (exit_code_ret, result_string_ret) = runner._RunCommand(cmd, 0, 0, None)
    self.assertEqual(exit_code_ret, exit_code)
    self.assertEqual(result_string_ret, self.result_string)

    self._mox.VerifyAll()

  def testRunCommandHardTimeout(self):
    cmd = ['a']
    self.PrepareRunCommandCall(cmd)

    self.mock_proc.pid = 42
    self.mock_proc.poll().AndReturn(None)

    self._mox.StubOutWithMock(local_test_runner, '_TimedOut')
    # Ensure the hard limit is hit.
    local_test_runner._TimedOut(1, mox.IgnoreArg()).AndReturn(
        True)
    # Ensure the IO time limit isn't hit.
    local_test_runner._TimedOut(0, mox.IgnoreArg()).AndReturn(
        True)

    self._mox.ReplayAll()

    self.CreateValidFile()
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    (exit_code, result_string) = runner._RunCommand(cmd, 1, 0, None)
    self.assertNotEqual(exit_code, 0)
    self.assertTrue(result_string)
    self.assertIn(str(self.mock_proc.pid), result_string)

    self._mox.VerifyAll()

  def testRunCommandIOTimeout(self):
    cmd = ['a']
    self.PrepareRunCommandCall(cmd)

    self.mock_proc.pid = 42
    self.mock_proc.poll().AndReturn(None)

    self._mox.StubOutWithMock(local_test_runner, '_TimedOut')
    # The hard limit isn't hit
    local_test_runner._TimedOut(1, mox.IgnoreArg()).AndReturn(False)
    # Ensure the IO time limit is hit.
    local_test_runner._TimedOut(0, mox.IgnoreArg()).AndReturn(True)

    self._mox.ReplayAll()

    self.CreateValidFile()
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    (exit_code, result_string) = runner._RunCommand(cmd, 1, 0, None)
    self.assertNotEqual(exit_code, 0)
    self.assertTrue(result_string)
    self.assertIn(str(self.mock_proc.pid), result_string)

    self._mox.VerifyAll()

  def testRunCommandAndPing(self):
    cmd = ['a']
    exit_code = 42
    self.PrepareRunCommandCall(cmd)

    # Ensure that the first the server is pinged after both poll because
    # the require ping delay will have elapsed.
    self.mock_proc.stdin_handle.write(self.result_string)
    self.mock_proc.poll().AndReturn(None)
    local_test_runner.url_helper.UrlOpen(self.ping_url).AndReturn('')
    self.mock_proc.poll().WithSideEffects(
        self.mock_proc.stdin_handle.close()).AndReturn(exit_code)
    local_test_runner.url_helper.UrlOpen(self.ping_url).AndReturn('')
    self._mox.ReplayAll()

    # Set the ping delay to 0 to ensure we get a ping for this runner.
    self.ping_delay = 0

    self.CreateValidFile()
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    (exit_code_ret, result_string_ret) = runner._RunCommand(cmd, 0, 0, None)
    self.assertEqual(exit_code_ret, exit_code)
    self.assertEqual(result_string_ret, self.result_string)

    self._mox.VerifyAll()

  def PrepareDownloadCall(self):
    local_file2 = 'bar'
    data_url_with_local_name = ('http://b.com/download_key',
                                local_file2)
    self.CreateValidFile(test_run_data=[data_url_with_local_name])

    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    data_dir = os.path.basename(runner.data_dir)
    local_test_runner.url_helper.DownloadFile(
        mox.Regex(DATA_FILE_REGEX % (data_dir, local_file2)),
        data_url_with_local_name[0]).AndReturn(True)

    self.mock_zipfile = self._mox.CreateMock(zipfile.ZipFile)
    local_test_runner.zipfile.ZipFile(
        mox.Regex(DATA_FILE_REGEX %
                  (data_dir, local_file2))).AndReturn(self.mock_zipfile)
    self.mock_zipfile.extractall(mox.Regex(DATA_FOLDER_REGEX % data_dir))
    self.mock_zipfile.close()
    return runner

  def testDownloadAndExplode(self):
    runner = self.PrepareDownloadCall()
    self._mox.ReplayAll()
    self.assertTrue(runner.DownloadAndExplodeData())

    self._mox.VerifyAll()

  def PrepareRunTestsCall(self, log=None, decorate_output=None, results=None,
                          test_names=None, test_run_env=None):
    if not decorate_output:
      decorate_output = [False, False]
    if not results:
      results = [(0, 'success'), (0, 'success')]
    action1 = ['foo']
    action2 = ['bar', 'foo', 'bar']
    if not test_names:
      test_names = [self.test_name1, self.test_name2]
    self.assertEqual(len(results), 2)
    test_objects_data = [
        (test_names[0], action1, decorate_output[0], 60, 0),
        (test_names[1], action2, decorate_output[1], 60, 9)
    ]
    self.assertEqual(len(decorate_output), len(results))
    self.CreateValidFile(test_objects_data=test_objects_data,
                         test_run_env=test_run_env)

    runner = local_test_runner.LocalTestRunner(self.data_file_name, log=log)
    self._mox.StubOutWithMock(runner, '_RunCommand')
    env_items = os.environ.items()
    if test_run_env:
      env_items += test_run_env.items()
    env = dict(env_items)

    runner._RunCommand(action1, 60, 0, env=env).AndReturn(results[0])
    runner._RunCommand(action2, 60, 9, env=env).AndReturn(results[1])
    return runner

  def testRunTests(self):
    runner = self.PrepareRunTestsCall(decorate_output=[True, True])
    self._mox.ReplayAll()
    result_codes, result_string = runner.RunTests()

    self.assertEqual([0, 0], result_codes)
    expected = (
        u'[==========] Running: foo\n'
        'success\n'
        '(Step: 0 ms)\n'
        '[==========] Running: bar foo bar\n'
        'success\n'
        '(Step: 0 ms)\n'
        '(Total: 0 ms)')
    self.assertEqual(expected, result_string)

  def _RunTestsWithEnvVars(self, platform):
    test_run_env = {'var3': 'value3', 'var4': 'value4'}
    self._mox.StubOutWithMock(local_test_runner.sys, 'platform')
    local_test_runner.sys.platform = platform
    runner = self.PrepareRunTestsCall(
        decorate_output=[True, True], test_run_env=test_run_env)
    self._mox.ReplayAll()
    result_codes, result_string = runner.RunTests()

    self.assertEqual([0, 0], result_codes)
    expected = (
        u'[==========] Running: foo\n'
        'success\n'
        '(Step: 0 ms)\n'
        '[==========] Running: bar foo bar\n'
        'success\n'
        '(Step: 0 ms)\n'
        '(Total: 0 ms)')
    self.assertEqual(expected, result_string)

  def testRunTestsWithEnvVarsOnWindows(self):
    self._RunTestsWithEnvVars('win32')

  def testRunTestsWithEnvVarsOnLinux(self):
    self._RunTestsWithEnvVars('linux2')

  def testRunTestsWithEnvVarsOnMac(self):
    self._RunTestsWithEnvVars('darwin')

  def testRunFailedTests(self):
    ok_str = 'OK'
    not_ok_str = 'NOTOK'
    runner = self.PrepareRunTestsCall(results=[(1, not_ok_str), (0, ok_str)])
    self._mox.ReplayAll()
    result_codes, result_string = runner.RunTests()

    self.assertEqual([1, 0], result_codes)
    expected = (
        u'NOTOK\n'
        'OK')
    self.assertEqual(expected, result_string)

  def testRunDecoratedTests(self):
    ok_str = 'OK'
    not_ok_str = 'NOTOK'
    runner = self.PrepareRunTestsCall(
        results=[(1, not_ok_str), (0, ok_str)], decorate_output=[False, True])
    self._mox.ReplayAll()
    result_codes, result_string = runner.RunTests()

    self.assertEqual([1, 0], result_codes)
    expected = (
        u'[==========] Running: foo\n'
        'NOTOK\n'
        '(Step: 0 ms)\n'
        '[==========] Running: bar foo bar\n'
        'OK\n'
        '(Step: 0 ms)\n'
        '(Total: 0 ms)')
    self.assertEqual(expected, result_string)

  def testRunTestsWithNonAsciiOutput(self):
    # This result string must be a byte string, not unicode, because that is
    # how the subprocess returns its output. If this string is changed to
    # unicode then the test will pass, but real world tests can still crash.
    unicode_string_valid_utf8 = b'Output with \xe2\x98\x83 â'
    unicode_string_invalid_utf8 = b'Output with invalid \xa0\xa1'

    test_names = [u'Testâ-\xe2\x98\x83',
                  u'Test1â-\xe2\x98\x83']
    self.test_run_name = u'Unicode Test Runâ-\xe2\x98\x83'
    encoding = 'utf-8'

    with logging_utils.CaptureLogs('local_test_runner_test.log') as log:
      runner = self.PrepareRunTestsCall(
          log,
          decorate_output=[True, False],
          results=[
            (0, unicode_string_valid_utf8),
            (0, unicode_string_invalid_utf8),
          ],
          test_names=test_names)
      self._mox.ReplayAll()
      result_codes, result_string = runner.RunTests()
    self.assertEqual([0, 0], result_codes)
    self.assertEqual(unicode, type(result_string))

    # The valid utf-8 output should still be present and unchanged, but the
    # invalid string should have the invalid characters replaced.
    self.assertIn(unicode_string_valid_utf8.decode(encoding), result_string)

    string_invalid_output = (
        '! Output contains characters not valid in %s encoding !\n%s' %
        (encoding, unicode_string_invalid_utf8.decode(encoding, 'replace')))
    self.assertIn(string_invalid_output, result_string)

    self._mox.VerifyAll()

  def testPublishResults(self):
    self.CreateValidFile()
    local_test_runner.url_helper.UrlOpen(
        self.result_url,
        data={
          'o': self.result_string,
          'x': ', '.join(str(i) for i in self.result_codes),
        },
        max_tries=15).AndReturn('')
    local_test_runner.url_helper.UrlOpen(
        '%s?1=2' % self.result_url,
        data={
          'o': self.result_string,
          'x': ', '.join(str(i) for i in self.result_codes),
        },
        max_tries=15).AndReturn('')
    self._mox.ReplayAll()

    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    self.assertTrue(
        runner.PublishResults(self.result_codes, self.result_string))

    # Also test with other CGI param in the URL.
    self.result_url = '%s?1=2' % self.result_url
    self.CreateValidFile()  # Recreate the request file with new value.
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    self.assertTrue(
        runner.PublishResults(self.result_codes, self.result_string))

    self._mox.VerifyAll()

  def testPublishResultsUnableToReachResultUrl(self):
    self.CreateValidFile()
    local_test_runner.url_helper.UrlOpen(
        self.result_url,
        data={
          'o': self.result_string,
          'x': ', '.join(str(i) for i in self.result_codes),
        },
        max_tries=15).AndReturn(None)
    self._mox.ReplayAll()

    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    self.assertFalse(
        runner.PublishResults(self.result_codes, self.result_string))

    self._mox.VerifyAll()

  def testPublishResultsHTTPS(self):
    self.CreateValidFile()
    self.result_url = 'https://localhost/result2'

    local_test_runner.url_helper.UrlOpen(
        self.result_url,
        data={
          'o': self.result_string,
          'x': ', '.join(str(i) for i in self.result_codes),
        },
        max_tries=15).AndReturn('')
    self._mox.ReplayAll()

    self.CreateValidFile()
    runner = local_test_runner.LocalTestRunner(self.data_file_name)
    self.assertTrue(
        runner.PublishResults(self.result_codes, self.result_string))

    self._mox.VerifyAll()

  def testPublishInternalErrors(self):
    logging.basicConfig(level=logging.FATAL)
    for i in logging.getLogger().handlers:
      self.mock(i, 'stream', StringIO.StringIO())

    self.CreateValidFile()
    exception_text = 'Bad MAD, no cookie!'

    def Validate(data):
      data = data.copy()
      out = data.pop('o')
      self.assertEqual({'x': ''}, data)
      self.assertIn(exception_text, out)
      return True

    local_test_runner.url_helper.UrlOpen(
        unicode(self.result_url),
        data=mox.Func(Validate),
        max_tries=15).AndReturn('')
    self._mox.ReplayAll()

    with logging_utils.CaptureLogs('local_test_runner_test.log') as log:
      runner = local_test_runner.LocalTestRunner(self.data_file_name, log=log)
      # This looks a bit strange, but logging.exception should only be
      # called from within an exception handler.
      # TODO(maruel): Make it not print to stderr.
      try:
        raise local_test_runner.Error(exception_text)
      except local_test_runner.Error as e:
        logging.exception(e)
      runner.PublishInternalErrors()

    self._mox.VerifyAll()

  def testMainBadFile(self):
    self.mock(logging.getLogger(), 'addHandler', lambda _: None)
    with open(self.data_file_name, 'wb') as f:
      f.write('a')

    # Test the case where the swarm file itself is corrupted.
    args = [
      '--request_file_name', self.data_file_name,
    ]
    result = local_test_runner.main(args)
    self.assertEqual(1, result)

  def testMainBadFileRestart(self):
    self.mock(logging.getLogger(), 'addHandler', lambda _: None)
    with open(self.data_file_name, 'wb') as f:
      f.write('a')

    # Test the case where the swarm file itself is corrupted.
    args = ['--request_file_name', self.data_file_name]
    result = local_test_runner.main(args)
    self.assertEqual(1, result)


if __name__ == '__main__':
  logging_utils.prepare_logging(None)
  logging_utils.set_console_level(
      logging.DEBUG if '-v' in sys.argv else logging.CRITICAL+1)
  unittest.main()
