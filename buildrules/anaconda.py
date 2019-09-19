# -*- coding: utf-8 -*-
"""AnacondaBuilder is a builder that builds using anaconda.
"""
import sys
import re
import os
import shutil
import logging
from glob import glob
import yaml
import copy
import requests
import sh
import json

from buildrules.common.builder import Builder
from buildrules.common.rule import PythonRule, SubprocessRule, LoggingRule

class AnacondaBuilder(Builder):
    """AnacondaBuilder extends on Builder and creates buildrules for Anaconda build.
    """

    BUILDER_NAME = 'Spack'
    CONF_FILES = ['config.yaml', 'build_config.yaml']
    SCHEMAS = [
        {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'Anaconda configuration file schema',
            'type': 'object',
            'additionalProperties': False,
            'patternProperties': {
                'config': {
                    'type': 'object',
                    'default': {},
                    'properties': {
                        'install_tree': {'type': 'string'},
                        'build_stage': {
                            'oneOf': [
                                {'type': 'string'},
                                {'type': 'array',
                                 'items': {'type': 'string'}}],
                        },
                        'module_path': {'type': 'string'},
                        'source_cache': {'type': 'string'},
                    },
                },
            },
        }, {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'Package configuration file schema',
            'type': 'object',
            'additionalProperties': False,
            'patternProperties': {
                'installer_checksums': {
                    'type': 'object',
                    'default': {},
                },
                'environments': {
                    'type': 'array',
                    'default': [],
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'version': {'type': 'string'},
                            'miniconda': {'type': 'boolean'},
                            'installer_version': {'type': 'string'},
                            'python_version': {
                                'type': 'integer',
                                'minimum': 2,
                                'maximum': 3,
                            },
                            'pip_packages': {
                                'default' : [],
                                'type': 'array',
                                'items': {'type': 'string'},
                            },
                            'conda_packages': {
                                'default' : [],
                                'type': 'array',
                                'items': {'type': 'string'},
                            },
                        },
                        'required': ['name', 'version'],
                    },
                },
            },
        }]


    def __init__(self, conf_folder):
        self._conda_path = os.path.join(os.getcwd(), 'conda')
        super().__init__(conf_folder)
        self._source_cache = self._get_path('source_cache')
        self._build_stage = self._get_path('build_stage')
        self._install_path = self._get_path('install_path')
        self._module_path = self._get_path('module_path')
        self._installed_file = os.path.join(self._install_path, 'installed_environments.yml')

    def _get_path(self, path_name):
        path_config = {
            'install_path': '$conda/opt/conda/software',
            'module_path': '$conda/opt/conda/modules',
            'source_cache': '$conda/var/conda/cache',
            'build_stage': '$conda/var/conda/stage',
        }
        path_config.update(self._confreader['config']['config'])
        return re.sub('\$conda', self._conda_path, path_config[path_name])

    def _get_directory_creation_rules(self):
        rules = []

        rules.extend([
            LoggingRule('Creating cache directory: %s' % self._source_cache),
            PythonRule(self._makedirs, [self._source_cache, 0o755]),
            LoggingRule('Creating stage directory: %s' % self._build_stage),
            PythonRule(self._makedirs, [self._build_stage, 0o755]),
            LoggingRule('Creating installation directory: %s' % self._install_path),
            PythonRule(self._makedirs, [self._install_path, 0o755]),
            LoggingRule('Creating module directory: %s' % self._module_path),
            PythonRule(self._makedirs, [self._module_path, 0o755]),
        ])

        return rules

    def _create_environment_config(self, environment_dict):
        default_config = {
            'miniconda': True,
            'python_version': 3,
            'installer_version': 'latest',
            'pip_packages': [],
            'conda_packages': [],
        }

        config = copy.deepcopy(default_config)
        config.update(environment_dict)
        config['environment_name'] = '{name}/{version}'.format(**config)

        config['checksum'] = self._calculate_dict_checksum(config)
        config['checksum_small'] = config['checksum'][:8]

        return config

    def _get_installer_path(self, install_config):

        if install_config['miniconda']:
            installer_fmt = "Miniconda{python_version}-{installer_version}-Linux-x86_64.sh"
        else:
            installer_fmt = "Anaconda{python_version}-{installer_version}-Linux-x86_64.sh"

        installer = os.path.join(self._source_cache, installer_fmt.format(**install_config))

        return installer

    def _download_installer(self, install_config):

        cached_installer = self._get_installer_path(install_config)
        installer = os.path.basename(cached_installer)

        if install_config['miniconda']:
            installer_url = "https://repo.anaconda.com/miniconda/{0}".format(installer)
        else:
            installer_url = "https://repo.anaconda.com/archive/{0}".format(installer)

        if not os.path.isfile(cached_installer):
            self._logger.info((
                "Installer '%s' was not found in the cache directory. "
                "Downloading it."), installer)
            download_request = requests.get(installer_url)
            with open(cached_installer, 'wb') as installer_file:
                installer_file.write(download_request.content)

        checksum = self._confreader['build_config'].get('installer_checksums', {}).get(installer, '')
        if checksum:
            self._logger.info(
                "Calculating checksum for installer '%s'", installer)
            calculated_checksum = self._calculate_file_checksum(cached_installer)
            if calculated_checksum != checksum:
                self._logger.error(
                    ("The checksum for installer file '%s' "
                     "does not match the expected value:\n"
                     "Expected:   %s\n"
                     "Calculated: %s"),
                    installer,
                    checksum,
                    calculated_checksum)
                raise Exception('Invalid checksum for installer')

    def _get_stage_path(self, config):
        stage_name = '{name}-{version}-{checksum_small}'.format(**config)
        stage_path = os.path.join(
            self._build_stage,
            stage_name)
        return stage_path

    def _get_install_path(self, config):
        install_path = os.path.join(
            self._install_path,
            config['name'],
            config['version'],
            config['checksum_small'])
        return install_path

    def _get_module_path(self, config):
        module_path = os.path.join(
            self._module_path,
            config['name'])
        return module_path

    @classmethod
    def _clean_stage(self, stage_path):
        shutil.rmtree(stage_path)

    def _prepare_installation_paths(self, stage_path=None, install_path=None, module_path=None):

        if os.path.isdir(stage_path):
            self._logger.info((
                "Cleaning previous stage path: %s"), stage_path)
            self._clean_stage(stage_path)
        self._makedirs(stage_path, 0o755)

        install_root = os.path.dirname(install_path)
        if not os.path.isdir(install_root):
            self._makedirs(install_root, 0o755)

        if not os.path.isdir(module_path):
            self._makedirs(module_path, 0o755)

    def _get_installed_environments(self):
        installed_dict = {
            'environments': {}
        }
        if os.path.isfile(self._installed_file):
            with open(self._installed_file, 'r') as installed_file:
                installed_dict = yaml.load(installed_file, Loader=yaml.SafeLoader)
        return installed_dict

    def _update_installed_environments(self, environment_name, installation_config):
        installed_dict = self._get_installed_environments()
        installed_dict['environments'][environment_name] = installation_config
        with open(self._installed_file, 'w') as installed_file:
            installed_file.write(
                yaml.dump(
                    installed_dict,
                    default_flow_style=False,
                    Dumper=yaml.SafeDumper
                ))

    def _verify_condarc(self, conda_path=None, env=None):
        config_json = sh.conda('info','--json', _env=env).stdout.decode('utf-8')
        config = json.loads(config_json)
        conda_rc = os.path.join(conda_path, 'condarc')
        if config['config_files']:
            if len(config['config_files']) > 1:
                raise Exception(
                    ('Too many configuration files: '
                     '{0}').format(config['config_files']))
            elif config['config_files'][0] != conda_rc:
                raise Exception(
                    ('Configuration file is not from the '
                     'installation root: {0}'.format(config['config_files'])))

    def _get_environment_install_rules(self):
        rules = []

        installed_environments = self._get_installed_environments()['environments']

        env_path = list(filter(
            lambda x: re.search('^/usr',x),
            os.getenv('PATH').split(':')))

        for environment in self._confreader['build_config']['environments']:

            config = self._create_environment_config(environment)

            installer = self._get_installer_path(config)
            stage_path = self._get_stage_path(config)
            install_path = self._get_install_path(config)
            module_path = self._get_module_path(config)

            conda_env = {
                'PATH': ':'.join([os.path.join(stage_path, 'bin')] + env_path)
            }
            conda_install_cmd = ['conda', 'install', '--yes', '-n', 'base']

            if config['environment_name'] not in installed_environments:
                # This build installs a brand new environment
                rules.extend([
                    LoggingRule((
                        "Environment {{name}} not found.\n"
                        "Installing conda environment '{name}' with "
                        "module '{environment_name}'").format(**config)),
                    PythonRule(self._download_installer, [config]),
                    PythonRule(
                        self._prepare_installation_paths,
                        kwargs={
                            'stage_path': stage_path,
                            'install_path': install_path,
                            'module_path': module_path,
                        }
                    ),
                    SubprocessRule(
                        ['bash', installer, '-f', '-b', '-p', stage_path],
                        shell=True),
                ])
            rules.extend([
                LoggingRule('Verifying that only the environment condarc is utilized'),
                PythonRule(self._verify_condarc,
                    kwargs={
                        'conda_path': install_path,
                        'env': conda_env
                    }),
            ])
            if config.get('conda_packages',[]):
                rules.extend([
                    SubprocessRule(
                        conda_install_cmd + config['conda_packages'],
                        env=conda_env,
                        shell=True),
                ])
            rules.extend([
                PythonRule(
                    self._copy_dir,
                    [stage_path, install_path]
                ),
            ])
            rules.append(
                PythonRule(
                    self._update_installed_environments,
                    [config['environment_name'], config]))

        return rules

    def _get_rules(self):
        """_get_rules provides build rules for the builder.

        Anaconda build consists of the following steps:

        """

        rules = (
            self._get_directory_creation_rules() +
            self._get_environment_install_rules()
        )
        return rules

if __name__ == "__main__":

    CONF_FOLDER = sys.argv[1]

    ANACONDA_BUILDER = AnacondaBuilder(CONF_FOLDER)
    ANACONDA_BUILDER.describe()