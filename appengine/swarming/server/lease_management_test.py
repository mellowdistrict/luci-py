#!/usr/bin/python
# Copyright 2015 The LUCI Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Unit tests for lease_management.py."""

import datetime
import json
import unittest

import test_env
test_env.setup_test_env()

from google.appengine.ext import ndb
from protorpc.remote import protojson
import webtest

from components import machine_provider
from components import utils
from test_support import test_case

import bot_management
import lease_management


def rpc_to_json(rpc_message):
  """Converts the given RPC message to a POSTable JSON dict.

  Args:
    rpc_message: A protorpc.message.Message instance.

  Returns:
    A string representing a JSON dict.
  """
  return json.loads(protojson.encode_message(rpc_message))


class CleanUpBotsTest(test_case.TestCase):
  """Tests for lease_management.clean_up_bots."""

  def test_no_machine_types(self):
    lease_management.clean_up_bots()

  def test_none_pending_deletion(self):
    machine_type = lease_management.MachineType(
        id='none-pending-deletion',
        request_id_base='none-pending-deletion',
        target_size=2,
    )
    machine_type.put()

    lease_management.clean_up_bots()
    updated_machine_type = machine_type.key.get()
    self.failIf(updated_machine_type.pending_deletion)

  def test_pending_deletion(self):
    bot_ids = []
    for i in xrange(200):
      bot_ids.append('fake-bot-%d' % i)
      bot_management.BotInfo(key=bot_management.get_info_key(bot_ids[-1])).put()
    machine_type = lease_management.MachineType(
        id='few-pending-deletion',
        pending_deletion=bot_ids,
        request_id_base='few-pending-deletion',
        target_size=2,
    )
    machine_type.put()

    lease_management.clean_up_bots()
    updated_machine_type = machine_type.key.get()
    self.failIf(updated_machine_type.pending_deletion)
    for bot_id in bot_ids:
      self.failIf(bot_management.get_info_key(bot_id).get())


class ClearBotsPendingDeletionTest(test_case.TestCase):
  """Tests for lease_management._clear_bots_pending_deletion."""

  def test_machine_type_not_found(self):
    machine_type = lease_management.MachineType(
        id='not-found',
        pending_deletion=['fake-bot-1', 'fake-bot-2'],
    )

    lease_management._clear_bots_pending_deletion(
        machine_type.key, ['fake-bot-1', 'fake-bot-2'])
    self.assertEqual(len(machine_type.pending_deletion), 2)

  def test_machine_type_none_pending_deletion(self):
    machine_type = lease_management.MachineType(
        id='none-pending-deletion',
        request_id_base='none-pending-deletion',
        target_size=2,
    )
    machine_type.put()

    lease_management._clear_bots_pending_deletion(machine_type.key, [])
    updated_machine_type = machine_type.key.get()
    self.failIf(updated_machine_type.pending_deletion)

  def test_machine_type_pending_deletion(self):
    machine_type = lease_management.MachineType(
        id='pending-deletion',
        pending_deletion=['fake-bot-1', 'fake-bot-2'],
        request_id_base='pending-deletion',
        target_size=2,
    )
    machine_type.put()

    lease_management._clear_bots_pending_deletion(
        machine_type.key, ['fake-bot-1'])
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(updated_machine_type.pending_deletion), 1)
    self.assertEqual(updated_machine_type.pending_deletion[0], 'fake-bot-2')


class GenerateLeaseRequestsTest(test_case.TestCase):
  """Tests for lease_management.generate_lease_requests."""

  def test_machine_type_not_found(self):
    machine_type = lease_management.MachineType(
        id='not-found',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id',
                request_hash='fake-hash',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='not-found',
        target_size=2,
    )

    requests = lease_management.generate_lease_requests(
        machine_type.key, 'https://example.com')
    self.failIf(requests)

  def test_machine_type_not_enabled(self):
    machine_type = lease_management.MachineType(
        id='not-enabled',
        enabled=False,
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id-1',
                request_hash='fake-hash',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_count=1,
        request_id_base='not-found',
        target_size=2,
    )
    machine_type.put()

    requests = lease_management.generate_lease_requests(
        machine_type.key, 'https://example.com')
    self.assertEqual(len(requests), 1)
    self.assertEqual(requests[0].request_id, 'fake-id-1')

  def test_need_one(self):
    machine_type = lease_management.MachineType(
      id='need-one',
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='need-one',
      target_size=1,
    )
    machine_type.put()

    requests = lease_management.generate_lease_requests(
        machine_type.key, 'https://example.com')
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(requests), 1)
    self.assertEqual(len(updated_machine_type.leases), 1)
    self.assertEqual(
        updated_machine_type.leases[0].client_request_id, 'need-one-1')
    self.assertEqual(updated_machine_type.request_count, 1)


class CleanUpExpiredLeasesTest(test_case.TestCase):
  """Tests for lease_management._clean_up_expired_leases."""

  def test_machine_type_no_leases(self):
    machine_type = lease_management.MachineType(
        id='no-leases',
        leases=[],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='no-leases',
        target_size=2,
    )

    expired  = lease_management._clean_up_expired_leases(machine_type)
    self.failIf(expired)
    self.failIf(machine_type.leases)
    self.failIf(machine_type.pending_deletion)

  def test_machine_type_no_expired_leases(self):
    machine_type = lease_management.MachineType(
        id='no-leases',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id-1',
                request_hash='fake-hash-1',
            ),
            lease_management.MachineLease(
                client_request_id='fake-id-2',
                hostname='fake-host',
                lease_expiration_ts=(
                    utils.utcnow() + datetime.timedelta(seconds=60)),
                request_hash='fake-hash-2',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='no-leases',
        target_size=2,
    )

    expired  = lease_management._clean_up_expired_leases(machine_type)
    self.failIf(expired)
    self.assertEqual(len(machine_type.leases), 2)
    self.failIf(machine_type.pending_deletion)

  def test_machine_type_one_expired_lease(self):
    machine_type = lease_management.MachineType(
        id='no-leases',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id-1',
                request_hash='fake-hash-1',
            ),
            lease_management.MachineLease(
                client_request_id='fake-id-2',
                hostname='fake-host-2',
                lease_expiration_ts=utils.EPOCH,
                request_hash='fake-hash-2',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        pending_deletion=[
          'fake-host-3',
        ],
        request_id_base='no-leases',
        target_size=2,
    )

    expired  = lease_management._clean_up_expired_leases(machine_type)
    self.assertEqual(len(expired), 1)
    self.assertEqual(expired[0], 'fake-host-2')
    self.assertEqual(len(machine_type.leases), 1)
    self.assertEqual(machine_type.leases[0].client_request_id, 'fake-id-1')
    self.assertEqual(machine_type.leases[0].request_hash, 'fake-hash-1')
    self.assertEqual(len(machine_type.pending_deletion), 2)
    hostnames = sorted(machine_type.pending_deletion)
    self.assertEqual(hostnames[0], 'fake-host-2')
    self.assertEqual(hostnames[1], 'fake-host-3')


class GenerateLeaseRequestStatusUpdatesTest(test_case.TestCase):
  """Tests for lease_management._generate_lease_request_status_updates."""

  def test_machine_type_no_leases(self):
    machine_type = lease_management.MachineType(
        id='no-leases',
        leases=[],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='no-leases',
        target_size=2,
    )

    requests = lease_management._generate_lease_request_status_updates(
        machine_type, 'https://example.com')
    self.failIf(requests)

  def test_machine_type_none_pending(self):
    machine_type = lease_management.MachineType(
        id='none-pending',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id',
                hostname='fake-host',
                request_hash='fake-hash',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='none-pending',
        target_size=2,
    )

    requests = lease_management._generate_lease_request_status_updates(
        machine_type, 'https://example.com')
    self.failIf(requests)

  def test_machine_type_one_pending(self):
    machine_type = lease_management.MachineType(
        id='one-pending',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id-1',
                hostname='fake-host-1',
                request_hash='fake-hash-1',
            ),
            lease_management.MachineLease(
                client_request_id='fake-id-2',
                hostname='fake-host-2',
                request_hash='fake-hash-2',
            ),
            lease_management.MachineLease(
                client_request_id='fake-id-3',
                request_hash='fake-hash-3',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='one-pending',
        target_size=2,
    )

    requests = lease_management._generate_lease_request_status_updates(
        machine_type, 'https://example.com')
    self.assertEqual(len(requests), 1)
    self.assertEqual(requests[0].request_id, 'fake-id-3')


class GenerateLeaseRequestsForNewMachinesTest(test_case.TestCase):
  """Tests for lease_management._generate_lease_requests_for_new_machines."""

  def test_machine_type_at_capacity(self):
    machine_type = lease_management.MachineType(
        id='at-capacity',
        leases=[
            lease_management.MachineLease(
                client_request_id='fake-id-1',
                request_hash='fake-hash-1',
            ),
            lease_management.MachineLease(
                client_request_id='fake-id-2',
                request_hash='fake-hash-2',
            ),
        ],
        mp_dimensions=machine_provider.Dimensions(
            os_family=machine_provider.OSFamily.LINUX,
        ),
        request_id_base='at-capacity',
        target_size=2,
    )

    requests = lease_management._generate_lease_requests_for_new_machines(
        machine_type, 'https://example.com')
    self.failIf(requests)

  def test_need_one(self):
    machine_type = lease_management.MachineType(
      id='need-one',
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='need-one',
      target_size=1,
    )

    requests = lease_management._generate_lease_requests_for_new_machines(
        machine_type, 'https://example.com')
    self.assertEqual(len(requests), 1)

  def test_ensure_correct_request_ids(self):
    machine_type = lease_management.MachineType(
      id='ensure-correct-request-ids',
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_count=2,
      request_id_base='ensure-correct-request-ids',
      target_size=2,
    )

    requests = lease_management._generate_lease_requests_for_new_machines(
        machine_type, 'https://example.com')
    self.assertEqual(len(requests), 2)
    request_ids = sorted(request.request_id for request in requests)
    self.assertEqual(request_ids[0], 'ensure-correct-request-ids-3')
    self.assertEqual(request_ids[1], 'ensure-correct-request-ids-4')


class UpdateLeasesTest(test_case.TestCase):
  """Tests for lease_management.update_leases."""

  def test_leases_fulfilled(self):
    machine_type = lease_management.MachineType(
      id='fulfilled',
      leases=[
          lease_management.MachineLease(client_request_id='fake-id-1'),
          lease_management.MachineLease(client_request_id='fake-id-2'),
      ],
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='fulfilled',
      target_size=2,
    )
    machine_type.put()

    responses = rpc_to_json(machine_provider.BatchedLeaseResponse(responses=[
        machine_provider.LeaseResponse(
            client_request_id='fake-id-1',
            hostname='fake-host-1',
            lease_expiration_ts=1,
            request_hash='fake-hash-1',
            state=machine_provider.LeaseRequestState.FULFILLED,
        ),
        machine_provider.LeaseResponse(
            client_request_id='fake-id-2',
            hostname='fake-host-2',
            lease_expiration_ts=2,
            request_hash='fake-hash-2',
            state=machine_provider.LeaseRequestState.FULFILLED,
        ),
    ]))

    lease_management.update_leases(machine_type.key, responses)
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(updated_machine_type.leases), 2)
    self.failUnless(updated_machine_type.leases[0].hostname)
    self.failUnless(updated_machine_type.leases[1].hostname)
    self.failUnless(updated_machine_type.leases[0].lease_expiration_ts)
    self.failUnless(updated_machine_type.leases[1].lease_expiration_ts)
    request_hashes = sorted(
        lease.request_hash for lease in updated_machine_type.leases)
    self.assertEqual(request_hashes[0], 'fake-hash-1')
    self.assertEqual(request_hashes[1], 'fake-hash-2')

  def test_lease_denied(self):
    machine_type = lease_management.MachineType(
      id='denied',
      leases=[
          lease_management.MachineLease(client_request_id='fake-id-1'),
          lease_management.MachineLease(client_request_id='fake-id-2'),
      ],
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='denied',
      target_size=2,
    )
    machine_type.put()

    responses = rpc_to_json(machine_provider.BatchedLeaseResponse(responses=[
        machine_provider.LeaseResponse(
            client_request_id='fake-id-1',
            request_hash='fake-hash-1',
            state=machine_provider.LeaseRequestState.DENIED,
        ),
    ]))

    lease_management.update_leases(machine_type.key, responses)
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(updated_machine_type.leases), 1)
    self.assertEqual(
        updated_machine_type.leases[0].client_request_id, 'fake-id-2')
    self.failIf(updated_machine_type.leases[0].hostname)
    self.failIf(updated_machine_type.leases[0].lease_expiration_ts)
    self.failIf(updated_machine_type.leases[0].request_hash)

  def test_lease_untriaged(self):
    machine_type = lease_management.MachineType(
      id='untriaged',
      leases=[
          lease_management.MachineLease(client_request_id='fake-id-1'),
      ],
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='untriaged',
      target_size=2,
    )
    machine_type.put()

    responses = rpc_to_json(machine_provider.BatchedLeaseResponse(responses=[
        machine_provider.LeaseResponse(
            client_request_id='fake-id-1',
            request_hash='fake-hash-1',
            state=machine_provider.LeaseRequestState.UNTRIAGED,
        ),
    ]))

    lease_management.update_leases(machine_type.key, responses)
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(updated_machine_type.leases), 1)
    self.assertEqual(
        updated_machine_type.leases[0].client_request_id, 'fake-id-1')
    self.failIf(updated_machine_type.leases[0].hostname)
    self.failIf(updated_machine_type.leases[0].lease_expiration_ts)
    self.assertEqual(updated_machine_type.leases[0].request_hash, 'fake-hash-1')

  def test_lease_expired(self):
    machine_type = lease_management.MachineType(
      id='untriaged',
      leases=[
          lease_management.MachineLease(client_request_id='fake-id-1'),
      ],
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='untriaged',
      target_size=2,
    )
    machine_type.put()

    responses = rpc_to_json(machine_provider.BatchedLeaseResponse(responses=[
        machine_provider.LeaseResponse(
            client_request_id='fake-id-1',
            request_hash='fake-hash-1',
            state=machine_provider.LeaseRequestState.FULFILLED,
        ),
    ]))

    lease_management.update_leases(machine_type.key, responses)
    updated_machine_type = machine_type.key.get()
    self.failIf(updated_machine_type.leases)

  def test_lease_errors(self):
    machine_type = lease_management.MachineType(
      id='errors',
      leases=[
          lease_management.MachineLease(client_request_id='fake-id-1'),
          lease_management.MachineLease(client_request_id='fake-id-2'),
          lease_management.MachineLease(client_request_id='fake-id-3'),
      ],
      mp_dimensions=machine_provider.Dimensions(
          os_family=machine_provider.OSFamily.LINUX,
      ),
      request_id_base='errors',
      target_size=2,
    )
    machine_type.put()

    responses = rpc_to_json(machine_provider.BatchedLeaseResponse(responses=[
        machine_provider.LeaseResponse(
            client_request_id='fake-id-1',
            error=machine_provider.LeaseRequestError.DEADLINE_EXCEEDED,
        ),
        machine_provider.LeaseResponse(
            client_request_id='fake-id-2',
            error=machine_provider.LeaseRequestError.REQUEST_ID_REUSE,
        ),
        machine_provider.LeaseResponse(
            client_request_id='fake-id-3',
            error=machine_provider.LeaseRequestError.TRANSIENT_ERROR,
        ),
    ]))

    lease_management.update_leases(machine_type.key, responses)
    updated_machine_type = machine_type.key.get()
    self.assertEqual(len(updated_machine_type.leases), 2)
    request_ids = sorted(
        request.client_request_id for request in updated_machine_type.leases)
    self.assertEqual(request_ids[0], 'fake-id-1')
    self.assertEqual(request_ids[1], 'fake-id-3')


if __name__ == '__main__':
  unittest.main()
