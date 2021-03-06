# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Datastore model for GCE Backend."""

from google.appengine.ext import ndb
from google.appengine.ext.ndb import msgprop

from components.machine_provider import rpc_messages

import utilities


class MetadataUpdate(ndb.Model):
  """A pending metadata update.

  Standalone instances should not be present in the datastore.
  """
  # Checksum for this metadata.
  checksum = ndb.ComputedProperty(
      lambda self: utilities.compute_checksum(self.metadata))
  # Metadata to modify. Keys present will overwrite existing metadata.
  # Use null values to delete keys.
  metadata = ndb.JsonProperty()
  # URL for the pending operation to apply this metadata update.
  url = ndb.StringProperty(indexed=False)


class ServiceAccount(ndb.Model):
  """A service account.

  Standalone instances should not be present in the datastore.
  """
  # Name of the service account.
  name = ndb.StringProperty(indexed=False)
  # List of authorized OAuth2 scopes for this service account.
  scopes = ndb.StringProperty(indexed=False, repeated=True)


class Instance(ndb.Model):
  """A GCE instance.

  Key:
    id: Name of the instance.
    kind: InstanceGroupManager.
  """
  # Active metadata operation.
  active_metadata_update = ndb.LocalStructuredProperty(MetadataUpdate)
  # Whether or not this instance is cataloged in the Machine Provider.
  cataloged = ndb.BooleanProperty(indexed=True)
  # Whether or not this instance is pending deletion.
  pending_deletion = ndb.BooleanProperty(indexed=True)
  # Pending metadata operations.
  pending_metadata_updates = ndb.LocalStructuredProperty(
      MetadataUpdate, repeated=True)
  # URL of the instance.
  url = ndb.StringProperty(indexed=False)


class InstanceGroupManager(ndb.Model):
  """An instance group manager in the config.

  Key:
    id: zone of the
      proto.config_pb2.InstanceGroupManagerConfig.InstanceGroupManager this
      entity represents.
    kind: InstanceTemplateRevision.
  """
  # Current number of instances managed by the instance group manager created
  # from this entity.
  current_size = ndb.ComputedProperty(lambda self: len(self.instances))
  # ndb.Keys for the active Instances.
  instances = ndb.KeyProperty(kind=Instance, repeated=True)
  # Maximum number of instances the instance group manager created from this
  # entity can maintain. Must be at least equal to minimum_size. Leave
  # unspecified for unlimited.
  maximum_size = ndb.IntegerProperty(indexed=False)
  # Minimum number of instances the instance group manager created from this
  # entity should maintain. Must be positive. Also defines the initial size
  # when first creating the instance group manager.
  minimum_size = ndb.IntegerProperty(indexed=False)
  # URL of the instance group manager created from this entity.
  url = ndb.StringProperty(indexed=False)


class InstanceTemplateRevision(ndb.Model):
  """A specific revision of an instance template in the config.

  Key:
    id: Checksum of the instance template config.
    kind: InstanceTemplate.
  """
  # List of ndb.Keys for the InstanceGroupManagers.
  active = ndb.KeyProperty(kind=InstanceGroupManager, repeated=True)
  # rpc_messages.Dimensions describing instances created from this template.
  dimensions = msgprop.MessageProperty(rpc_messages.Dimensions)
  # Disk size in GiB for instances created from this template.
  disk_size_gb = ndb.IntegerProperty(indexed=False)
  # List of ndb.Keys for drained InstanceGroupManagers.
  drained = ndb.KeyProperty(kind=InstanceGroupManager, repeated=True)
  # Name of the image for instances created from this template.
  image_name = ndb.StringProperty(indexed=False)
  # GCE machine type for instances created from this template.
  machine_type = ndb.StringProperty(indexed=False)
  # Initial metadata to apply when creating instances from this template.
  metadata = ndb.JsonProperty()
  # Project to create the instance template in.
  project = ndb.StringProperty(indexed=False)
  # List of service accounts available to instances created from this template.
  service_accounts = ndb.LocalStructuredProperty(ServiceAccount, repeated=True)
  # Initial list of tags to apply when creating instances from this template.
  tags = ndb.StringProperty(indexed=False, repeated=True)
  # URL of the instance template created from this entity.
  url = ndb.StringProperty(indexed=False)


class InstanceTemplate(ndb.Model):
  """An instance template in the config.

  Key:
    id: base_name of the
      proto.config_pb2.InstanceTemplateConfig.InstanceTemplate this entity
      represents.
    kind: InstanceTemplate (root entity).
  """
  # ndb.Key for the active InstanceTemplateRevision.
  active = ndb.KeyProperty(kind=InstanceTemplateRevision)
  # List of ndb.Keys for drained InstanceTemplateRevisions.
  drained = ndb.KeyProperty(kind=InstanceTemplateRevision, repeated=True)
