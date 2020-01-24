# -*- coding: utf-8 -*-
"""Deployer deploys software.

This module contains Deployer-classes that deploy software and a
deployer_factory function used by Builder to choose between deployment
strategies.
"""
import logging
import os
import yaml
from jsonschema import validate
from buildrules.common.rule import SubprocessRule, LoggingRule, PythonRule
from swiftclient.service import SwiftError, SwiftService, SwiftUploadObject

DEPLOYMENTCONFIG_SCHEMA = {
    "$schema" : "http://json-schema.org/draft-07/schema#",
    "type" : "array",
    "default" : [],
    "items" : {
        "type" : "object",
        "properties" : {
            "method": {"type" : "string"}
        },
        "required": ["method"]
    }
}

class Deployer:

    DEPLOYER_SCHEMA = {
        "$schema" : "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties" : {
            "method": {"type" : "string"},
            "target_host": {"type" : "string"}
        },
        "required": ["method", "target_host"]
    }
    """This class will deploy installed software.

    Args:
        deployer_config (dict): Configuration that contains releavant fields
        defined in DEPLOYMENTCONFIG_SCHEMAS.
    """

    def __init__(self, deployer_config):
        self._logger = logging.getLogger(self.__class__.__name__)
        validate(deployer_config, self.DEPLOYER_SCHEMA)
        self._deployer_config = deployer_config

class RsyncDeployer(Deployer):

    DEPLOYER_SCHEMA = {
        "$schema" : "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties" : {
            "method": {"type" : "string"},
            "target_host": {"type" : "string"},
            "source": {"type" : "string"},
            "dest": {"type" : "string"},
            "working_directory": {"type" : "string"},
            "chmod_options": {"type" : "string"},
            "rsync_flags": {"type" : "string"},
            "ssh_command": {"type" : "string"},
            "delete": {"type" : "boolean"}
        },
        "required": ["method", "target_host", "source", "dest"]
    }

    DEFAULT_CONFIGS = {
        "rsync_flags": "-surlptDxv",
        "chmod_options": None,
        "ssh_command": "ssh",
        "delete": False,
        "working_directory": None
    }

    def _get_rsync_deployment_command(self, dry_run=False):
        rsync_deployer_config = self.DEFAULT_CONFIGS.copy()
        rsync_deployer_config.update(**self._deployer_config)

        cmd = ['rsync']
        cmd.append(rsync_deployer_config['rsync_flags'])
        if rsync_deployer_config['chmod_options']:
            cmd.append('--chmod={0}'.format(rsync_deployer_config['chmod_options']))
        cmd.extend(['-e',rsync_deployer_config['ssh_command']])
        if rsync_deployer_config['delete']:
            cmd.append('--delete')

        rsync_cwd = rsync_deployer_config['working_directory']
        if rsync_cwd:
            src = os.path.relpath(rsync_deployer_config['source'], rsync_cwd)
        else:
            src = rsync_deployer_config['source']

        target = '{0}:"{1}"'.format(rsync_deployer_config['target_host'],rsync_deployer_config['dest'])
        src = '"{0}/"'.format(src)

        return SubprocessRule(cmd + [src, target], shell=True, cwd=rsync_cwd)

    def get_rules(self):
        rules = []
        rules.append(LoggingRule('Deploying software with rsync deployer:'))
        rules.append(self._get_rsync_deployment_command())
        return rules


class SwiftDeployer(Deployer):

    DEPLOYER_SCHEMA = {
        "$schema" : "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties" : {
            "method": {"type" : "string"},
            "dest_container": {"type" : "string"},
            "source": {"type" : "string"},
            "source_replacement": {"type" : "string"},
            "os_secrets_file": {"type": "string"},
        },
        "required": ["method", "dest_container", "source", "os_secrets_file"]
    }

    OS_SECRETS_SCHEMA = {
        "$schema" : "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties" : {
            "os_username": {"type" : "string"},
            "os_password": {"type": "string"},
            "os_project_name": {"type" : "string"},
            "os_auth_url": {"type" : "string"},
        },
        "required": ["os_username", "os_password", "os_project_name", "os_auth_url"]
    }

    def __init__(self, deployer_config):

        super().__init__(deployer_config)

        with open(self._deployer_config['os_secrets_file'], 'r') as yaml_f:
            os_secrets = yaml.load(yaml_f.read(), Loader=yaml.Loader)
        validate(os_secrets, self.OS_SECRETS_SCHEMA)
        self._os_secrets = os_secrets

    def _swift_deploy(self):

        swift_options = {
            'auth_version': '3'
        }
        swift_options.update(self._os_secrets)

        with SwiftService(options=swift_options) as swift:

            container = self._deployer_config['dest_container']
            self._logger.info('Verifying access to destination container: %s.', container)
            container_stat = swift.stat(container)

            source_dir = self._deployer_config['source']
            objects = []
            for root_dir, subdirectories, file_list in os.walk(source_dir):
                if not (subdirectories + file_list):
                    objects.append({
                        'path': None,
                        'name': root_dir,
                        'options': {'dir_marker': True},
                    })
                else:
                    for filename in file_list:
                        file_path = os.path.join(root_dir, filename)
                        objects.append({
                            'path': file_path,
                            'name': file_path,
                            'options': None,
                        })

            source_replacement = self._deployer_config.get('source_replacement', None)
            if source_replacement:
                for obj in objects:
                    obj['name'] = obj['name'].replace(source_dir, source_replacement, 1)

            swift_objects = [ SwiftUploadObject(obj['path'], obj['name'], options=obj['options'])
                for obj in objects
            ]

            self._logger.info(
                'Uploading following objects into container %s:\n%s',
                container, '\n'.join([obj['name'] for obj in objects]))
            upload_results = list(swift.upload(container, swift_objects))
            for upload_result in upload_results:
                if upload_result['action'] == 'create_container':
                    self._logger.info(
                        'Creating container: %s', container)
                elif upload_result['action'] == 'create_dir_marker':
                    self._logger.info(
                        'Creating directory marker: %s', upload_result['object'])
                elif upload_result['action'] == 'upload_object':
                    self._logger.info(
                        'Uploading object: %s', upload_result['object'])

                if not upload_result['success']:
                    raise Exception('Failed to upload file! Full error: %s' % upload_result)

    def get_rules(self):
        rules = []
        rules.append(LoggingRule('Deploying software with swift deployer:'))
        rules.append(PythonRule(self._swift_deploy))
        return rules

def deployer_factory(confreader):
    """This function creates instances of subclasses of Deployer based on
    deployment_config. The configurations passed to the deployers class are validated
    again against the specific schema of each class.
    """

    confreader.validate('deployment_config', DEPLOYMENTCONFIG_SCHEMA)

    deployer_classes = {
        'rsync': RsyncDeployer,
        'swift': SwiftDeployer
    }

    deployers = []
    for deployer_config in confreader['deployment_config']:
        deployers.append(deployer_classes[deployer_config['method']](deployer_config))

    return deployers
