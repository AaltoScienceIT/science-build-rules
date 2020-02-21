# -*- coding: utf-8 -*-
"""SingularityBuilder is a builder that builds using singularity.
"""
import sys
import re
import os
from collections import defaultdict
import shutil
import logging
from glob import glob
import yaml
import copy
import requests
import sh

from buildrules.common.builder import Builder
from buildrules.common.rule import PythonRule, SubprocessRule, LoggingRule
from buildrules.common.confreader import ConfReader
from buildrules.common.utils import (load_yaml, write_yaml, makedirs, copy_file,
        write_template, calculate_dict_checksum)

class SingularityBuilder(Builder):
    """SingularityBuilder extends on Builder and creates buildrules for Singularity build.
    """

    BUILDER_NAME = 'Singularity'
    CONF_FILES = ['config.yaml', 'build_config.yaml']
    SCHEMAS = [
        {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'Singularity configuration file schema',
            'type': 'object',
            'additionalProperties': False,
            'patternProperties': {
                'config': {
                    'type': 'object',
                    'default': {},
                    'additionalProperties': False,
                    'properties': {
                        'debug': {'type': 'boolean'},
                        'sudo': {'type': 'boolean'},
                        'fakeroot': {'type': 'boolean'},
                        'remove_after_update': {'type': 'boolean'},
                        'install_path': {'type': 'string'},
                        'wrapper_path': {'type': 'string'},
                        'build_stage': {'type': 'string'},
                        'module_path': {'type': 'string'},
                        'source_cache': {'type': 'string'},
                        'tmpdir': {'type': 'string'},
                        'auths_file': {'type': 'string'},
                    },
                },
            },
        }, {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'Package configuration file schema',
            'type': 'object',
            'additionalProperties': False,
            'patternProperties': {
                'command_collections': {
                    'type': 'object',
                    'patternProperties': {
                        '.*' : {
                            'type': 'object',
                            'additionalProperties': False,
                            'patternProperties': {
                                ('(environment|files|help|labels|'
                                 'post|runscript|setup|'
                                 'startscript|test)_commands'): {
                                     'type': 'array',
                                     'default': [],
                                     'items': {'type': 'string'}
                                 },
                            },
                        },
                    },
                },
                'flag_collections': {
                    'type': 'object',
                    'patternProperties': {
                        '.*' : {
                            'type': 'array',
                            'default': [],
                            'items': {'type': 'string'}
                        },
                    },
                },
                'definitions': {
                    'type': 'array',
                    'default': [],
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'docker_user': {'type': 'string'},
                            'docker_image': {'type': 'string'},
                            'debug': {'type': 'boolean'},
                            'sudo': {'type': 'boolean'},
                            'fakeroot': {'type': 'boolean'},
                            'tags': {
                                'type': 'array',
                                'items': {'type': 'string'},
                            },
                            'module_versions': {
                                'type': 'array',
                                'items': {'type': 'string'},
                            },
                            'flag_collections': {
                                'type': 'array',
                                'items': {'type': 'string'}
                            },
                            'command_collections': {
                                'type': 'array',
                                'items': {'type': 'string'}
                            }
                        },
                        'required': ['name', 'tags'],
                    },
                },
            },
        }]


    def __init__(self, conf_folder):
        self._singularity_path = os.path.join(os.getcwd(), 'singularity')
        super().__init__(conf_folder)
        self._source_cache = self._get_path('source_cache')
        self._tmpdir = self._get_path('tmpdir')
        self._build_stage = self._get_path('build_stage')
        self._install_path = self._get_path('install_path')
        self._module_path = self._get_path('module_path')
        self._wrapper_path = self._get_path('wrapper_path')
        self._installed_file = os.path.join(self._install_path, 'installed_images.yaml')
        self._command_collections = self._confreader['build_config'].get(
            'command_collections', {})
        self._flag_collections = self._confreader['build_config'].get(
            'flag_collections', {})
        self._auths = self._get_auths()

    def _get_path(self, path_name):
        path_config = {
            'install_path': '$singularity/opt/singularity/software',
            'module_path': '$singularity/opt/singularity/modules',
            'source_cache': '$singularity/var/singularity/cache',
            'tmpdir': '$singularity/var/singularity/tmpdir',
            'build_stage': '$singularity/var/singularity/stage',
            'wrapper_path': '$singularity/opt/singularity/bin',
        }
        path_config.update(self._confreader['config']['config'])
        return re.sub('\$singularity', self._singularity_path, path_config[path_name])

    def _get_auths(self):

        auths_file = os.path.expanduser(
            self._confreader['config']['config'].get(
                'auths_file',
                os.path.join('~', 'singularity_auths.yaml')))

        auth_schema = {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'Singularity auth file schema',
            'type': 'object',
            'additionalProperties': False,
            'patternProperties': {
                'auths': {
                    'type': 'object',
                    'default': {},
                    'patternProperties': {
                        '.*' : {
                            'type': 'object',
                            'additionalProperties': False,
                            'properties': {
                                'username': {'type': 'string'},
                                'password': {'type': 'string'},
                            },
                        },
                    },
                },
            },
        }
        auths = {}
        if os.path.isfile(auths_file):
            auths.update(ConfReader([auths_file],[auth_schema])['singularity_auths']['auths'])

        return auths

    def _get_directory_creation_rules(self):
        rules = []

        rules.extend([
            LoggingRule('Creating tmpdir directory: %s' % self._tmpdir),
            PythonRule(makedirs, [self._tmpdir, 0o755]),
            LoggingRule('Creating cache directory: %s' % self._source_cache),
            PythonRule(makedirs, [self._source_cache, 0o755]),
            LoggingRule('Creating build stage directory: %s' % self._build_stage),
            PythonRule(makedirs, [self._build_stage, 0o755]),
            LoggingRule('Creating installation directory: %s' % self._install_path),
            PythonRule(makedirs, [self._install_path, 0o755]),
            LoggingRule('Creating module directory: %s' % self._module_path),
            PythonRule(makedirs, [self._module_path, 0o755]),
        ])

        return rules

    def _get_installed_images(self):
        """ This function returns a dictionary that contains information on
        already installed images.

        Returns:
            dict: Dictionary of previously installed images.
        """

        installed_dict = {
            'images': {}
        }
        if os.path.isfile(self._installed_file):
            installed_dict = load_yaml(self._installed_file)
        return installed_dict

    def _update_installed_images(self, image_name, installation_config):
        """ This function updates the file that contains information on the
        previously installed environments.

        Args:
            image_name (str): Name of the image.
            image_config (dict): Anaconda environment config.
        """
        installed_dict = self._get_installed_images()
        installed_dict['images'][image_name] = installation_config
        with open(self._installed_file, 'w') as installed_file:
            installed_file.write(
                yaml.dump(
                    installed_dict,
                    default_flow_style=False,
                    Dumper=yaml.SafeDumper
                ))


    def _get_image_config(self, tag, definition_dict):
        default_config = {
            'registry': 'docker.io',
            'docker_user': 'library',
            'docker_image': definition_dict['name'],
            'tag': tag,
        }

        config = copy.deepcopy(default_config)
        config.update(definition_dict)

        # Setting definition name
        config['module_name'] = '{name!s}/{tag!s}'.format(**config)

        # Combining commands from all of the different command collections
        commands = defaultdict(list)
        for command_collection in config.pop('command_collections', []):
            collection = self._command_collections[command_collection]
            for key, item in collection.items():
                keyname = re.sub('_commands', '', key)
                commands[keyname] = commands[keyname] + item
        config['commands'] = dict(commands)

        # Combining flags from all of the different flag collections
        flags = []
        for flag_collection in config.pop('flag_collections', []):
            flags = flags + self._flag_collections[flag_collection]

        config['checksum'] = calculate_dict_checksum(config)
        config['checksum_small'] = config['checksum'][:8]
        config['nameformat'] = '{name!s}-{tag!s}-{checksum_small!s}'.format(**config)
        config['docker_url'] = '{docker_user!s}/{docker_image!s}:{tag!s}'.format(**config)

        config['flags'] = ' '.join(flags)

        return config

    def _write_definition_file(self, definition_file, registry=None, docker_url=None, commands=None):

        template = """
            Bootstrap: docker
            From: {{ docker_url }}
            Registry: {{ registry }}

            {% for command_collection, commands in commands.items() -%}
            %{{ command_collection }}
            {% for command in commands -%}
                {{ command }}
            {% endfor -%}
            {% endfor -%}
        """

        definition_config = {
            'registry': registry,
            'docker_url': docker_url,
            'commands': commands,
        }

        write_template(definition_file, definition_config, template=template)


    def _get_image_install_rules(self):

        rules = []

        default_env = {
            'SINGULARITY_CACHEDIR': self._source_cache,
            'SINGULARITY_TMPDIR': self._tmpdir
        }

        uid = os.getuid()

        # Obtain already installed images
        installed_images = self._get_installed_images()['images']

        remove_after_update = self._confreader['config']['config'].get(
            'remove_after_update',
            False)

        for definition in self._confreader['build_config']['definitions']:
            for tag in definition.pop('tags'):
                image_config = self._get_image_config(tag, definition)

                nameformat = image_config.pop('nameformat')
                commands = image_config.pop('commands')
                module_name = image_config.pop('module_name')

                build_path = os.path.join(
                    self._build_stage,
                    image_config['name'],
                    tag)

                build_definition_path = os.path.join(
                    build_path,
                    'definitions')

                build_image_path = os.path.join(
                    build_path,
                    'images')

                stage_definition = os.path.join(
                    build_definition_path,
                    '{0}.def'.format(nameformat))

                stage_image = os.path.join(
                    build_image_path,
                    '{0}.simg'.format(nameformat))

                install_path = os.path.join(
                    self._install_path,
                    image_config['name'],
                    tag)

                install_definition_path = os.path.join(
                    install_path,
                    'definitions')

                install_image_path = os.path.join(
                    install_path,
                    'images')

                install_definition = os.path.join(
                    install_definition_path,
                    os.path.basename(stage_definition))

                install_image = os.path.join(
                    install_image_path,
                    os.path.basename(stage_image))

                module_path = os.path.join(
                    self._module_path,
                    image_config['name'])

                image_config['definition_file'] = install_definition
                image_config['image_file'] = install_image
                image_config['module_path'] = module_path


                buildenv = copy.deepcopy(default_env)
                auths = self._auths.get(image_config['registry'], None)
                if auths:
                    buildenv.update({
                        'SINGULARITY_DOCKER_USERNAME': auths['username'],
                        'SINGULARITY_DOCKER_PASSWORD': auths['password']
                    })

                skip_install = False
                update_install = False

                # Check if same kind of an image is already installed
                installed_checksum = installed_images.get(
                    module_name, {}).get('checksum', '')

                if not installed_checksum:
                    install_msg = ("Image {0} is "
                                   "not installed. Starting installation.")
                elif installed_checksum != image_config['checksum']:
                    previous_image_path = installed_images[module_name]['image_file']
                    install_msg = ("Image {0} installed "
                                   "but marked for update.")
                    update_install = True
                else:
                    install_msg = ("Image {0} is already installed. "
                                   "Skipping installation.")
                    skip_install = True

                rules.append(LoggingRule(install_msg.format(module_name)))

                if not skip_install:

                     rules.extend([
                         PythonRule(makedirs, [build_definition_path]),
                         PythonRule(makedirs, [build_image_path]),
                         PythonRule(makedirs, [install_definition_path]),
                         PythonRule(makedirs, [install_image_path]),
                         PythonRule(makedirs, [module_path]),
                     ])

                     rules.extend([
                         LoggingRule(
                             'Writing definition file for %s' % module_name),
                         PythonRule(
                             self._write_definition_file,
                             args=[stage_definition],
                             kwargs={
                                 'registry': image_config['registry'],
                                 'docker_url': image_config['docker_url'],
                                 'commands': commands
                         }),
                     ])

                     singularity_build_cmd = ['singularity', 'build']
                     chown_cmd = ['chown', '{0}:{0}'.format(uid)]

                     debug = (image_config.get('debug', False) or
                             self._confreader['config']['config'].get('debug', False))
                     sudo = (image_config.get('sudo', False) or
                             self._confreader['config']['config'].get('sudo', False))
                     fakeroot = (image_config.get('fakeroot', False) or
                                 self._confreader['config']['config'].get(
                                     'fakeroot', False))
                     if debug:
                         singularity_build_cmd.insert(1, '-d')
                     if sudo:
                         singularity_build_cmd.insert(0, 'sudo')
                         chown_cmd.insert(0, 'sudo')
                     if fakeroot:
                         singularity_build_cmd.append('--fakeroot')
                     rules.extend([
                         LoggingRule(
                             'Building image for %s' % module_name),
                         SubprocessRule(
                             singularity_build_cmd + [stage_image, stage_definition],
                             env=buildenv,
                             shell=True)
                     ])
                     if sudo:
                         rules.append(
                             SubprocessRule(
                                 chown_cmd + [stage_image],
                                 shell=True))
                     rules.extend([
                         LoggingRule(
                             'Copying staged image to installation directory'),
                         PythonRule(
                             copy_file, [stage_image, install_image]),
                     ])

                     rules.extend([
                         LoggingRule(
                             'Copying definition file to installation directory'),
                         PythonRule(
                             copy_file, [stage_definition, install_definition]),
                     ])

                     rules.extend([
                         LoggingRule(
                             'Updating installed images'),
                         PythonRule(
                             self._update_installed_images,
                             [module_name, image_config])
                     ])
                rules.extend([
                    LoggingRule('Writing modulefile for %s' % module_name),
                    PythonRule(
                        self._write_modulefile,
                        [image_config['name'], image_config['tag'],
                         image_config['flags'], install_image, module_path]),
                ])

                if update_install and remove_after_update:
                    rules.extend([
                        LoggingRule(('Removing old environment from '
                                     '{0}').format(previous_image_path)),
                        PythonRule(os.remove, [previous_image_path])])

        return rules

    def _write_modulefile(self, name, tag, flags, image_file, module_path):
        """ This function writes a modulefile that points to Singularity
        image installed in image_file and whose name is name/version
        into a directory given by module_path.

        Args:
            name (str): Name of the Singularity module.
            tag (str): tag of the Singlarity module.
            flags (str): Flags of the Singularity module.
            image_file (str): Installation path of the environment.
            module_path (str): Directory for the modulefile.
        """

        moduleconfig = {
            'wrapper_path': self._wrapper_path,
            'name' : name,
            'tag' : tag,
            'image_file': image_file,
            'flags': flags,
        }


        template = """
            -- -*- lua -*-
            --
            -- Module file created by Singularity builder
            --

            whatis([[Name : {{ name }}]])
            whatis([[Version : {{ tag }}]])
            help([[This is an automatically created Singularity image.]])

            prepend_path("PATH", "{{ wrapper_path }}")

            setenv("SING_IMAGE", "{{ image_file }}")
            setenv("SING_FLAGS", " {{ flags }} ")
        """
        makedirs(module_path, 0o755)

        modulefile = os.path.join(module_path, '%s.lua' % tag)

        if os.path.exists(modulefile):
            raise RuleError('Modulefile %s already exists' % modulefile)

        write_template(modulefile, moduleconfig, template=template, chmod=0o644)

    def _clean_modules(self):
        """ This function creates build rules that clean up modulefiles.

        Returns:
            list: List of build rules.
        """

        if os.path.isdir(self._module_path):
            modulefiles = glob(
                os.path.join(self._module_path, '*', '*.lua')
            )
            for modulefile in modulefiles:
                os.remove(modulefile)

    def _get_modulefile_clean_rules(self):
        """ This function creates build rules that clean up modulefiles.

        Returns:
            list: List of build rules.
        """

        rules = []

        # Clean up modulefiles
        rules.extend([
            LoggingRule("Cleaning previous modulefiles."),
            PythonRule(self._clean_modules),
        ])

        return rules

    def _get_rules(self):
        """_get_rules provides build rules for the builder.

        Singularity build consists of the following steps:

        """

        rules = (
            self._get_directory_creation_rules() +
            self._get_modulefile_clean_rules() +
            self._get_image_install_rules()
        )
        return rules

if __name__ == "__main__":

    CONF_FOLDER = sys.argv[1]

    SINGULARITY_BUILDER = SingularityBuilder(CONF_FOLDER)
    SINGULARITY_BUILDER.describe()
