# Copyright 2016 Internap.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import pytz

from datetime import datetime
from datetime import timedelta
from dateutil import parser as date_parser
from pkg_resources import get_distribution

from almanach.common.DateFormatException import DateFormatException
from almanach.core.model import Instance, Volume, VolumeType
from almanach import config


class Controller(object):
    def __init__(self, database_adapter):
        self.database_adapter = database_adapter
        self.metadata_whitelist = config.device_metadata_whitelist()

        self.volume_existence_threshold = timedelta(0, config.volume_existence_threshold())

    def get_application_info(self):
        return {
            "info": {"version": get_distribution("almanach").version},
            "database": {"all_entities": self.database_adapter.count_entities(),
                         "active_entities": self.database_adapter.count_active_entities()}
        }

    def _fresher_entity_exists(self, entity_id, date):
        try:
            entity = self.database_adapter.get_active_entity(entity_id)
            if entity and entity.last_event > date:
                return True
        except KeyError:
            pass
        except NotImplementedError:
            pass
        return False

    def create_instance(self, instance_id, tenant_id, create_date, flavor, os_type, distro, version, name, metadata):
        create_date = self._validate_and_parse_date(create_date)
        logging.info("instance %s created in project %s (flavor %s; distro %s %s %s) on %s" % (
            instance_id, tenant_id, flavor, os_type, distro, version, create_date))
        if self._fresher_entity_exists(instance_id, create_date):
            logging.warning("instance %s already exists with a more recent entry", instance_id)
            return

        filtered_metadata = self._filter_metadata_with_whitelist(metadata)

        entity = Instance(instance_id, tenant_id, create_date, None, flavor, {"os_type": os_type, "distro": distro,
                                                                              "version": version},
                          create_date, name, filtered_metadata)
        self.database_adapter.insert_entity(entity)

    def delete_instance(self, instance_id, delete_date):
        delete_date = self._validate_and_parse_date(delete_date)
        logging.info("instance %s deleted on %s" % (instance_id, delete_date))
        self.database_adapter.close_active_entity(instance_id, delete_date)

    def resize_instance(self, instance_id, flavor, resize_date):
        resize_date = self._validate_and_parse_date(resize_date)
        logging.info("instance %s resized to flavor %s on %s" % (instance_id, flavor, resize_date))
        try:
            instance = self.database_adapter.get_active_entity(instance_id)
            if flavor != instance.flavor:
                self.database_adapter.close_active_entity(instance_id, resize_date)
                instance.flavor = flavor
                instance.start = resize_date
                instance.end = None
                instance.last_event = resize_date
                self.database_adapter.insert_entity(instance)
        except KeyError as e:
            logging.error("Trying to resize an instance with id '%s' not in the database yet." % instance_id)
            raise e

    def rebuild_instance(self, instance_id, distro, version, os_type, rebuild_date):
        rebuild_date = self._validate_and_parse_date(rebuild_date)
        instance = self.database_adapter.get_active_entity(instance_id)
        logging.info("instance %s rebuilded in project %s to os %s %s %s on %s" % (instance_id, instance.project_id,
                                                                                   os_type, distro, version,
                                                                                   rebuild_date))
        if instance.os.distro != distro or instance.os.version != version:
            self.database_adapter.close_active_entity(instance_id, rebuild_date)

            instance.os.distro = distro
            instance.os.version = version
            instance.os.os_type = os_type
            instance.start = rebuild_date
            instance.end = None
            instance.last_event = rebuild_date
            self.database_adapter.insert_entity(instance)

    def update_instance_create_date(self, instance_id, create_date):
        logging.info("instance %s create date updated for %s" % (instance_id, create_date))
        try:
            instance = self.database_adapter.get_active_entity(instance_id)
            instance.start = datetime.strptime(create_date[0:19], "%Y-%m-%d %H:%M:%S")
            self.database_adapter.update_active_entity(instance)
            return True
        except KeyError as e:
            logging.error("Trying to update an instance with id '%s' not in the database yet." % instance_id)
            raise e

    def create_volume(self, volume_id, project_id, start, volume_type, size, volume_name, attached_to=None):
        start = self._validate_and_parse_date(start)
        logging.info("volume %s created in project %s to size %s on %s" % (volume_id, project_id, size, start))
        if self._fresher_entity_exists(volume_id, start):
            return

        volume_type_name = self._get_volume_type_name(volume_type)

        entity = Volume(volume_id, project_id, start, None, volume_type_name, size, start, volume_name, attached_to)
        self.database_adapter.insert_entity(entity)

    def _get_volume_type_name(self, volume_type_id):
        if volume_type_id is None:
            return None

        volume_type = self.database_adapter.get_volume_type(volume_type_id)
        return volume_type.volume_type_name

    def attach_volume(self, volume_id, date, attachments):
        date = self._validate_and_parse_date(date)
        logging.info("volume %s attached to %s on %s" % (volume_id, attachments, date))
        try:
            self._volume_attach_instance(volume_id, date, attachments)
        except KeyError as e:
            logging.error("Trying to attach a volume with id '%s' not in the database yet." % volume_id)
            raise e

    def detach_volume(self, volume_id, date, attachments):
        date = self._validate_and_parse_date(date)
        logging.info("volume %s detached on %s" % (volume_id, date))
        try:
            self._volume_detach_instance(volume_id, date, attachments)
        except KeyError as e:
            logging.error("Trying to detach a volume with id '%s' not in the database yet." % volume_id)
            raise e

    def _volume_attach_instance(self, volume_id, date, attachments):
        volume = self.database_adapter.get_active_entity(volume_id)
        date = self._localize_date(date)
        volume.last_event = date
        existing_attachments = volume.attached_to
        volume.attached_to = attachments

        if existing_attachments or self._is_within_threshold(date, volume):
            self.database_adapter.update_active_entity(volume)
        else:
            self._close_volume(volume_id, volume, date)

    def _volume_detach_instance(self, volume_id, date, attachments):
        volume = self.database_adapter.get_active_entity(volume_id)
        date = self._localize_date(date)
        volume.last_event = date
        volume.attached_to = attachments

        if attachments or self._is_within_threshold(date, volume):
            self.database_adapter.update_active_entity(volume)
        else:
            self._close_volume(volume_id, volume, date)

    def _is_within_threshold(self, date, volume):
        return date - volume.start < self.volume_existence_threshold

    def _close_volume(self, volume_id, volume, date):
        self.database_adapter.close_active_entity(volume_id, date)
        volume.start = date
        volume.end = None
        self.database_adapter.insert_entity(volume)

    def rename_volume(self, volume_id, volume_name):
        try:
            volume = self.database_adapter.get_active_entity(volume_id)
            if volume and volume.name != volume_name:
                logging.info("volume %s renamed from %s to %s" % (volume_id, volume.name, volume_name))
                volume.name = volume_name
                self.database_adapter.update_active_entity(volume)
        except KeyError:
            logging.error("Trying to update a volume with id '%s' not in the database yet." % volume_id)

    def resize_volume(self, volume_id, size, update_date):
        update_date = self._validate_and_parse_date(update_date)
        try:
            volume = self.database_adapter.get_active_entity(volume_id)
            logging.info("volume %s updated in project %s to size %s on %s" % (volume_id, volume.project_id, size,
                                                                               update_date))
            self.database_adapter.close_active_entity(volume_id, update_date)

            volume.size = size
            volume.start = update_date
            volume.end = None
            volume.last_event = update_date
            self.database_adapter.insert_entity(volume)
        except KeyError as e:
            logging.error("Trying to update a volume with id '%s' not in the database yet." % volume_id)
            raise e

    def delete_volume(self, volume_id, delete_date):
        delete_date = self._localize_date(self._validate_and_parse_date(delete_date))
        logging.info("volume %s deleted on %s" % (volume_id, delete_date))
        try:
            if self.database_adapter.count_entity_entries(volume_id) > 1:
                volume = self.database_adapter.get_active_entity(volume_id)
                if delete_date - volume.start < self.volume_existence_threshold:
                    self.database_adapter.delete_active_entity(volume_id)
                    return
            self.database_adapter.close_active_entity(volume_id, delete_date)
        except KeyError as e:
            logging.error("Trying to delete a volume with id '%s' not in the database yet." % volume_id)
            raise e

    def create_volume_type(self, volume_type_id, volume_type_name):
        logging.info("volume type %s with name %s created" % (volume_type_id, volume_type_name))
        volume_type = VolumeType(volume_type_id, volume_type_name)
        self.database_adapter.insert_volume_type(volume_type)

    def list_instances(self, project_id, start, end):
        return self.database_adapter.list_entities(project_id, start, end, Instance.TYPE)

    def list_volumes(self, project_id, start, end):
        return self.database_adapter.list_entities(project_id, start, end, Volume.TYPE)

    def list_entities(self, project_id, start, end):
        return self.database_adapter.list_entities(project_id, start, end)

    def get_volume_type(self, type_id):
        return self.database_adapter.get_volume_type(type_id)

    def delete_volume_type(self, type_id):
        self.database_adapter.delete_volume_type(type_id)

    def list_volume_types(self):
        return self.database_adapter.list_volume_types()

    def _filter_metadata_with_whitelist(self, metadata):
        return {key: value for key, value in metadata.items() if key in self.metadata_whitelist}

    def _validate_and_parse_date(self, date):
        try:
            date = date_parser.parse(date)
            return self._localize_date(date)
        except TypeError:
            raise DateFormatException()

    @staticmethod
    def _localize_date(date):
        try:
            return pytz.utc.localize(date)
        except ValueError:
            return date
