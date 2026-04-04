# Copyright (c) 2014-2019, iocage
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""iocage build module - builds jail templates from RapSheet.json files."""
import hashlib
import json
import os
import subprocess as su
import uuid as uuid_mod

import iocage_lib.ioc_common as ioc_common
import iocage_lib.ioc_exec as ioc_exec
import iocage_lib.ioc_json as ioc_json
import iocage_lib.ioc_exceptions as ioc_exceptions

from iocage_lib.zfs import (
    ZFSException, create_snapshot, clone_snapshot, promote_dataset,
    rename_dataset, dataset_exists, set_dataset_property,
    destroy_zfs_resource, all_properties
)


# --- JSON Schema for RapSheet.json ---

RAPSHEET_SCHEMA = {
    "type": "object",
    "required": ["from", "steps"],
    "properties": {
        "from": {"type": "string", "minLength": 1},
        "metadata": {
            "type": "object",
            "properties": {
                "maintainer": {"type": "string"},
                "description": {"type": "string"},
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"}
                }
            },
            "additionalProperties": False
        },
        "properties": {
            "type": "object"
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 1
            }
        }
    },
    "additionalProperties": False
}

STEP_TYPES = {'run', 'env'}
RESERVED_STEP_TYPES = {'copy', 'workdir', 'expose', 'label'}


class RapSheet:
    """Parsed RapSheet data."""

    def __init__(self, from_source, metadata, properties, steps):
        self.from_source = from_source
        self.metadata = metadata or {}
        self.properties = properties or {}
        self.steps = steps


class RapSheetParser:
    """Parse and validate a RapSheet.json file."""

    def __init__(self, filepath):
        self.filepath = filepath

    def parse(self):
        """Parse and validate the RapSheet file, returning a RapSheet object."""
        if not os.path.isfile(self.filepath):
            raise ioc_exceptions.RapSheetParseError(
                f'RapSheet not found: {self.filepath}'
            )

        try:
            with open(self.filepath, 'r') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ioc_exceptions.RapSheetParseError(
                f'Invalid JSON in {self.filepath}: {e}'
            )

        self._validate_schema(data)
        self._validate_steps(data['steps'])

        return RapSheet(
            from_source=data['from'],
            metadata=data.get('metadata'),
            properties=data.get('properties'),
            steps=data['steps']
        )

    def _validate_schema(self, data):
        """Validate top-level structure."""
        try:
            import jsonschema
            jsonschema.validate(data, RAPSHEET_SCHEMA)
        except ImportError:
            # Fallback: manual validation if jsonschema not available
            if not isinstance(data, dict):
                raise ioc_exceptions.RapSheetParseError(
                    'RapSheet must be a JSON object'
                )
            if 'from' not in data:
                raise ioc_exceptions.RapSheetParseError(
                    'RapSheet must contain a "from" key'
                )
            if 'steps' not in data:
                raise ioc_exceptions.RapSheetParseError(
                    'RapSheet must contain a "steps" key'
                )
            if not isinstance(data['steps'], list) or len(data['steps']) < 1:
                raise ioc_exceptions.RapSheetParseError(
                    '"steps" must be a non-empty array'
                )
        except jsonschema.ValidationError as e:
            raise ioc_exceptions.RapSheetParseError(
                f'RapSheet validation error: {e.message}'
            )

    def _validate_steps(self, steps):
        """Validate each step has exactly one recognized key."""
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or len(step) != 1:
                raise ioc_exceptions.RapSheetParseError(
                    f'Step {i + 1}: each step must be an object with '
                    f'exactly one key'
                )
            key = next(iter(step))
            if key in RESERVED_STEP_TYPES:
                raise ioc_exceptions.RapSheetParseError(
                    f'Step {i + 1}: "{key}" is not yet implemented'
                )
            if key not in STEP_TYPES:
                raise ioc_exceptions.RapSheetParseError(
                    f'Step {i + 1}: unknown step type "{key}". '
                    f'Valid types: {", ".join(sorted(STEP_TYPES))}'
                )
            if key == 'run':
                if not isinstance(step['run'], str) or not step['run'].strip():
                    raise ioc_exceptions.RapSheetParseError(
                        f'Step {i + 1}: "run" value must be a non-empty string'
                    )
            elif key == 'env':
                if not isinstance(step['env'], dict) or not step['env']:
                    raise ioc_exceptions.RapSheetParseError(
                        f'Step {i + 1}: "env" value must be a non-empty object'
                    )
                for k, v in step['env'].items():
                    if not isinstance(v, str):
                        raise ioc_exceptions.RapSheetParseError(
                            f'Step {i + 1}: env value for "{k}" must be a '
                            f'string'
                        )


class LayerHasher:
    """Compute deterministic hash chain for build steps."""

    @staticmethod
    def canonical_json(obj):
        """Produce canonical JSON with sorted keys, no extra whitespace."""
        return json.dumps(obj, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def compute_hashes(from_source, steps):
        """
        Compute the layer hash chain.

        Returns a list of (index, step, layer_hash, parent_hash) tuples.
        """
        base_hash = hashlib.sha256(
            f'from:{from_source}'.encode()
        ).hexdigest()

        result = []
        parent_hash = base_hash

        for i, step in enumerate(steps):
            canonical = LayerHasher.canonical_json(step)
            layer_hash = hashlib.sha256(
                f'{parent_hash}|{canonical}'.encode()
            ).hexdigest()
            result.append({
                'index': i,
                'step': step,
                'layer_hash': layer_hash,
                'parent_hash': parent_hash,
                'hash12': layer_hash[:12],
            })
            parent_hash = layer_hash

        return result


class BuildCache:
    """Manage ZFS build cache datasets."""

    LAYER_HASH_PROP = 'org.freebsd.iocage:layer_hash'

    def __init__(self, pool, iocroot):
        self.pool = pool
        self.iocroot = iocroot
        self.cache_root = f'{pool}/iocage/builds/cache'

    def lookup(self, layer_hashes):
        """
        Find the deepest consecutive cache hit.

        Args:
            layer_hashes: list of LayerHasher results

        Returns:
            (cache_dataset, hit_index) or (None, -1) if no cache hit.
            hit_index is the index of the deepest matched layer.
        """
        if not layer_hashes:
            return None, -1

        # Build a set of all hashes we're looking for
        wanted = {lh['layer_hash']: lh['index'] for lh in layer_hashes}

        # Scan all snapshots under builds/cache/ for matching layer_hash props
        try:
            props = all_properties(
                paths=[self.cache_root],
                recursive=True,
                types=['snapshot']
            )
        except ZFSException:
            return None, -1

        # Map layer_hash -> snapshot name
        found = {}
        for snap_name, snap_props in props.items():
            lh = snap_props.get(self.LAYER_HASH_PROP)
            if lh and lh in wanted:
                found[lh] = snap_name

        # Find the deepest consecutive hit starting from index 0
        best_snap = None
        best_idx = -1

        for lh in layer_hashes:
            if lh['layer_hash'] in found:
                best_snap = found[lh['layer_hash']]
                best_idx = lh['index']
            else:
                break

        return best_snap, best_idx

    def clean_all(self):
        """Destroy all cached layers."""
        try:
            destroy_zfs_resource(self.cache_root, recursive=True)
        except ZFSException:
            pass

        from iocage_lib.zfs import create_dataset
        try:
            create_dataset({'name': self.cache_root})
        except ZFSException:
            pass


class IOCBuild:
    """Orchestrate the jail build process."""

    def __init__(self, rapsheet_path, name=None, tag=None, no_cache=False,
                 keep_build_jail=False, props=(), silent=False, callback=None):
        self.rapsheet_path = rapsheet_path
        self.name = name
        self.tag = tag
        self.no_cache = no_cache
        self.keep_build_jail = keep_build_jail
        self.props = props
        self.silent = silent
        self.callback = callback

        self.pool = ioc_json.IOCJson().json_get_value('pool')
        self.iocroot = ioc_json.IOCJson(self.pool).json_get_value('iocroot')
        self.cache = BuildCache(self.pool, self.iocroot)

        self.build_uuid = None
        self.build_path = None
        self.env_vars = {}

    def build(self):
        """
        Execute the build process.

        Returns the name/UUID of the resulting template.
        """
        # Step 1: Parse
        parser = RapSheetParser(self.rapsheet_path)
        rapsheet = parser.parse()

        self._log('INFO', f'Building from {rapsheet.from_source}')

        # Step 2: Compute hashes
        layer_hashes = LayerHasher.compute_hashes(
            rapsheet.from_source, rapsheet.steps
        )

        # Compute overall rapsheet hash
        rapsheet_hash = hashlib.sha256(
            LayerHasher.canonical_json({
                'from': rapsheet.from_source,
                'steps': rapsheet.steps
            }).encode()
        ).hexdigest()

        # Step 3: Cache lookup
        cache_snap = None
        cache_hit_idx = -1
        if not self.no_cache:
            cache_snap, cache_hit_idx = self.cache.lookup(layer_hashes)
            if cache_hit_idx >= 0:
                self._log('INFO',
                           f'Cache hit at step {cache_hit_idx + 1}/'
                           f'{len(rapsheet.steps)}')

        # Check if all layers are cached
        all_cached = cache_hit_idx == len(layer_hashes) - 1

        # Step 4: Create build jail
        self.build_uuid = f'ioc-build-{str(uuid_mod.uuid4())[:8]}'

        try:
            if all_cached:
                self._log('INFO', 'All steps cached, creating template')
            else:
                self._create_build_jail(
                    rapsheet.from_source, cache_snap, cache_hit_idx
                )

                # Replay env vars from cached steps
                for lh in layer_hashes[:cache_hit_idx + 1]:
                    step = lh['step']
                    step_type = next(iter(step))
                    if step_type == 'env':
                        self.env_vars.update(step['env'])

                # Step 5: Execute uncached steps
                self._execute_steps(
                    rapsheet.steps, layer_hashes, cache_hit_idx + 1
                )

                # Stop the build jail
                self._stop_build_jail()

            # Step 6: Cache new layers (rename build into cache)
            cache_id = self.build_uuid
            if not all_cached:
                build_ds = f'{self.pool}/iocage/jails/{self.build_uuid}'
                cache_ds = f'{self.pool}/iocage/builds/cache/{cache_id}'
                rename_dataset(build_ds, cache_ds)

            # Step 7: Create template
            template_name = self.name or self.build_uuid
            final_hash = layer_hashes[-1]['hash12']

            if all_cached:
                # Clone directly from the cache hit snapshot
                source_snap = cache_snap
            else:
                source_snap = (
                    f'{self.pool}/iocage/builds/cache/{cache_id}'
                    f'/root@{final_hash}'
                )

            self._create_template(
                template_name, source_snap, rapsheet, rapsheet_hash
            )

            # Step 8: Tag if requested
            if self.tag:
                self._apply_tag(template_name, self.tag)

            self._log('INFO',
                       f'Template created: {template_name}'
                       f'{" (" + self.tag + ")" if self.tag else ""}')

            return template_name

        except (Exception, KeyboardInterrupt) as e:
            self._cleanup_on_failure()
            if isinstance(e, KeyboardInterrupt):
                raise ioc_exceptions.BuildFailed(
                    'Build interrupted by user'
                )
            raise

    def _create_build_jail(self, from_source, cache_snap, cache_hit_idx):
        """Create the temporary build jail."""
        build_ds = f'{self.pool}/iocage/jails/{self.build_uuid}'
        build_root = f'{build_ds}/root'
        self.build_path = f'{self.iocroot}/jails/{self.build_uuid}'

        if cache_snap and cache_hit_idx >= 0:
            # Clone from cached snapshot
            self._log('INFO', f'Cloning from cache layer {cache_hit_idx + 1}')
            from iocage_lib.zfs import create_dataset
            create_dataset({
                'name': build_ds,
                'create_ancestors': True
            })
            clone_snapshot(cache_snap, build_root)
        else:
            # Create from release/template using existing machinery
            self._create_from_source(from_source)

        # Write minimal config.json for the build jail
        self._write_build_config(from_source)

        # Start the build jail
        self._start_build_jail()

    def _create_from_source(self, from_source):
        """Create a build jail from a release or template."""
        # Check if it's a template
        template_path = f'{self.iocroot}/templates/{from_source}'
        release_path = f'{self.iocroot}/releases/{from_source}'

        if os.path.isdir(template_path):
            # Clone from template
            source = (
                f'{self.pool}/iocage/templates/{from_source}'
                f'/root@{self.build_uuid}'
            )
            su.check_call(['zfs', 'snapshot', source], stderr=su.PIPE)

            build_ds = f'{self.pool}/iocage/jails/{self.build_uuid}'
            su.Popen(
                ['zfs', 'clone', '-p', source, f'{build_ds}/root'],
                stdout=su.PIPE
            ).communicate()
        elif os.path.isdir(release_path):
            # Clone from release
            source = (
                f'{self.pool}/iocage/releases/{from_source}'
                f'/root@{self.build_uuid}'
            )
            su.check_call(['zfs', 'snapshot', source], stderr=su.PIPE)

            build_ds = f'{self.pool}/iocage/jails/{self.build_uuid}'
            from iocage_lib.zfs import create_dataset
            create_dataset({
                'name': build_ds,
                'create_ancestors': True
            })
            su.Popen(
                ['zfs', 'clone', source, f'{build_ds}/root'],
                stdout=su.PIPE
            ).communicate()
        else:
            # Try to fetch the release
            self._log('INFO', f'Fetching {from_source}...')
            import iocage_lib.ioc_fetch as ioc_fetch
            ioc_fetch.IOCFetch(
                from_source, silent=self.silent
            ).fetch_release()

            if not os.path.isdir(
                f'{self.iocroot}/releases/{from_source}'
            ):
                raise ioc_exceptions.BuildFailed(
                    f'FROM source not found: {from_source}'
                )

            # Retry after fetch
            self._create_from_source(from_source)
            return

        self.build_path = f'{self.iocroot}/jails/{self.build_uuid}'

    def _write_build_config(self, from_source):
        """Write a config.json for the build jail."""
        default_props = ioc_json.IOCJson.retrieve_default_props()

        # Apply build-specific defaults
        config = dict(default_props)
        config['host_hostuuid'] = self.build_uuid
        config['host_hostname'] = self.build_uuid
        config['release'] = from_source
        config['type'] = 'jail'
        config['ip4'] = 'inherit'

        # Apply user-provided props
        for prop in self.props:
            if '=' in prop:
                key, value = prop.split('=', 1)
                config[key] = value

        config_path = os.path.join(self.build_path, 'config.json')
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, 'w') as f:
            json.dump(config, f, sort_keys=True, indent=4)

        # Create empty fstab
        fstab_path = os.path.join(self.build_path, 'fstab')
        if not os.path.exists(fstab_path):
            with open(fstab_path, 'w') as f:
                pass

    def _start_build_jail(self):
        """Start the build jail."""
        import iocage_lib.ioc_start as ioc_start
        ioc_start.IOCStart(
            self.build_uuid, self.build_path,
            silent=self.silent, callback=self.callback
        )

    def _stop_build_jail(self):
        """Stop the build jail."""
        import iocage_lib.ioc_stop as ioc_stop
        try:
            ioc_stop.IOCStop(
                self.build_uuid, self.build_path,
                silent=True
            )
        except (RuntimeError, SystemExit, FileNotFoundError):
            pass

    def _execute_steps(self, steps, layer_hashes, start_from):
        """Execute build steps starting from the given index."""
        total = len(steps)

        for i in range(start_from, total):
            step = steps[i]
            lh = layer_hashes[i]
            step_type = next(iter(step))

            if step_type == 'env':
                self._execute_env(step['env'], i, total)
            elif step_type == 'run':
                self._execute_run(step['run'], lh, i, total)

        # If the last step was an env (no snapshot taken), take a final one
        if steps and next(iter(steps[-1])) == 'env':
            final_lh = layer_hashes[-1]
            build_root = (
                f'{self.pool}/iocage/jails/{self.build_uuid}/root'
            )
            snap_name = f'{build_root}@{final_lh["hash12"]}'
            create_snapshot(snap_name)
            set_dataset_property(
                snap_name,
                BuildCache.LAYER_HASH_PROP,
                final_lh['layer_hash']
            )

    def _execute_env(self, env_dict, step_idx, total):
        """Process an env step."""
        env_summary = ' '.join(f'{k}={v}' for k, v in env_dict.items())
        self._log('INFO', f'Step {step_idx + 1}/{total}: env {env_summary}')

        self.env_vars.update(env_dict)

        # Write to login.conf inside the jail
        self._update_login_conf()

    def _execute_run(self, command, layer_hash, step_idx, total):
        """Execute a run step inside the build jail."""
        self._log('INFO', f'Step {step_idx + 1}/{total}: run {command}')

        # Build su_env with accumulated env vars
        su_env = {
            'PATH': '/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:'
                    '/usr/local/bin:/root/bin',
            'PWD': '/',
            'HOME': '/root',
            'TERM': 'xterm-256color',
        }
        su_env.update(self.env_vars)

        # Execute the command
        cmd = ('/bin/sh', '-c', command)

        try:
            with ioc_exec.IOCExec(
                cmd,
                self.build_path,
                uuid=self.build_uuid,
                host_user='root',
                su_env=su_env,
                callback=self.callback
            ) as _exec:
                output = ioc_common.consume_and_log(
                    _exec,
                    log=not self.silent,
                    callback=self.callback
                )
        except ioc_exceptions.CommandFailed as e:
            msgs = [
                _msg.decode().rstrip() if isinstance(_msg, bytes) else _msg
                for _msg in e.message
            ]
            error_msg = '\n'.join(msgs) if msgs else f'Command failed: {command}'

            if self.keep_build_jail:
                self._log('ERROR',
                           f'Build failed at step {step_idx + 1}: '
                           f'run {command}')
                self._log('ERROR',
                           f'Build jail retained: {self.build_uuid}')
                self._log('ERROR',
                           f'Debug with: iocage console {self.build_uuid}')

            raise ioc_exceptions.BuildFailed(
                f'Build failed at step {step_idx + 1}: {command}\n'
                f'{error_msg}'
            )

        # Create snapshot for this layer
        build_root = f'{self.pool}/iocage/jails/{self.build_uuid}/root'
        snap_name = f'{build_root}@{layer_hash["hash12"]}'

        create_snapshot(snap_name)
        set_dataset_property(
            snap_name,
            BuildCache.LAYER_HASH_PROP,
            layer_hash['layer_hash']
        )

    def _update_login_conf(self):
        """Update /etc/login.conf inside the jail with env vars."""
        if not self.env_vars:
            return

        login_conf_path = os.path.join(self.build_path, 'root', 'etc',
                                        'login.conf')

        if not os.path.isfile(login_conf_path):
            return

        # Build setenv string
        setenv_parts = []
        for k, v in sorted(self.env_vars.items()):
            if ',' in v:
                setenv_parts.append(f'{k}={v}')
            else:
                setenv_parts.append(f'{k}={v}')
        setenv_str = ','.join(setenv_parts)

        # Read current login.conf
        with open(login_conf_path, 'r') as f:
            content = f.read()

        # Check if there's already a setenv line in the default class
        # and replace or add it
        lines = content.split('\n')
        new_lines = []
        in_default_class = False
        setenv_added = False
        # Remove any existing setenv we previously added
        skip_next_continuation = False

        for line in lines:
            if skip_next_continuation:
                skip_next_continuation = line.rstrip().endswith('\\')
                continue

            stripped = line.strip()

            if stripped.startswith('default:') or stripped == 'default\\':
                in_default_class = True

            if (in_default_class and
                    ':setenv=' in stripped and
                    not setenv_added):
                # Replace existing setenv
                new_lines.append(f'\t:setenv={setenv_str}:\\')
                setenv_added = True
                # Skip continuation lines of old setenv
                skip_next_continuation = stripped.endswith('\\')
                continue

            # Look for a place to insert setenv before the class ends
            if (in_default_class and not setenv_added and
                    stripped == ':' or
                    (stripped and not stripped.startswith(':') and
                     not stripped.startswith('\\') and
                     not stripped.endswith('\\') and
                     stripped != 'default\\' and
                     not stripped.startswith('default:'))):
                # End of default class, insert setenv before this line
                if not setenv_added and in_default_class:
                    new_lines.append(f'\t:setenv={setenv_str}:\\')
                    setenv_added = True
                    in_default_class = False

            new_lines.append(line)

        if not setenv_added and in_default_class:
            # Append at end of file within default class
            new_lines.append(f'\t:setenv={setenv_str}:\\')

        with open(login_conf_path, 'w') as f:
            f.write('\n'.join(new_lines))

        # Rebuild the login database
        try:
            with ioc_exec.IOCExec(
                ('/usr/bin/cap_mkdb', '/etc/login.conf'),
                self.build_path,
                uuid=self.build_uuid,
                host_user='root',
                callback=self.callback
            ) as _exec:
                ioc_common.consume_and_log(_exec, log=False)
        except (ioc_exceptions.CommandFailed, Exception):
            # Non-fatal: cap_mkdb failure shouldn't break the build
            self._log('WARNING',
                       'Failed to rebuild login.conf database')

    def _create_template(self, name, source_snap, rapsheet, rapsheet_hash):
        """Create the final template from the build."""
        import datetime

        template_ds = f'{self.pool}/iocage/templates/{name}'
        template_root = f'{template_ds}/root'
        template_path = f'{self.iocroot}/templates/{name}'

        if dataset_exists(template_ds):
            raise ioc_exceptions.BuildFailed(
                f'Template already exists: {name}'
            )

        # Create parent dataset and clone root
        from iocage_lib.zfs import create_dataset
        create_dataset({
            'name': template_ds,
            'create_ancestors': True
        })

        clone_snapshot(source_snap, template_root)

        # Promote to make template independent of cache
        promote_dataset(template_root)

        # Write template config.json
        default_props = ioc_json.IOCJson.retrieve_default_props()
        config = dict(default_props)

        config['host_hostuuid'] = name
        config['host_hostname'] = name
        config['release'] = rapsheet.from_source
        config['type'] = 'template'
        config['template'] = 1
        config['rapsheet_hash'] = rapsheet_hash
        config['build_date'] = datetime.datetime.utcnow().strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        config['build_from'] = rapsheet.from_source

        # Apply metadata
        if rapsheet.metadata:
            if 'maintainer' in rapsheet.metadata:
                config['maintainer'] = rapsheet.metadata['maintainer']
            if 'description' in rapsheet.metadata:
                config['description'] = rapsheet.metadata['description']
            if 'labels' in rapsheet.metadata:
                config['labels'] = json.dumps(rapsheet.metadata['labels'])

        # Apply env vars
        if self.env_vars:
            config['build_env'] = json.dumps(self.env_vars)

        # Apply properties from RapSheet
        for key, value in rapsheet.properties.items():
            config[key] = value

        # Write config
        config_path = os.path.join(template_path, 'config.json')
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(config, f, sort_keys=True, indent=4)

        # Create empty fstab
        fstab_path = os.path.join(template_path, 'fstab')
        if not os.path.exists(fstab_path):
            with open(fstab_path, 'w') as f:
                pass

    def _apply_tag(self, template_name, tag):
        """Apply a tag to the template."""
        template_path = f'{self.iocroot}/templates/{template_name}'
        config_path = os.path.join(template_path, 'config.json')

        with open(config_path, 'r') as f:
            config = json.load(f)

        existing_tags = config.get('tags', 'none')
        if existing_tags == 'none' or not existing_tags:
            config['tags'] = tag
        else:
            tags = [t.strip() for t in existing_tags.split(',')]
            if tag not in tags:
                tags.append(tag)
            config['tags'] = ','.join(tags)

        with open(config_path, 'w') as f:
            json.dump(config, f, sort_keys=True, indent=4)

        # Also set as ZFS user property
        template_ds = f'{self.pool}/iocage/templates/{template_name}'
        try:
            set_dataset_property(
                template_ds,
                'org.freebsd.iocage:tags',
                config['tags']
            )
        except ZFSException:
            pass

    def _cleanup_on_failure(self):
        """Clean up on build failure."""
        if self.keep_build_jail:
            return

        if self.build_uuid:
            build_ds = f'{self.pool}/iocage/jails/{self.build_uuid}'
            build_path = f'{self.iocroot}/jails/{self.build_uuid}'

            # Stop the jail first
            try:
                import iocage_lib.ioc_stop as ioc_stop
                ioc_stop.IOCStop(self.build_uuid, build_path, silent=True)
            except (RuntimeError, SystemExit, FileNotFoundError):
                pass

            # Destroy the dataset
            try:
                destroy_zfs_resource(build_ds, recursive=True)
            except ZFSException:
                pass

    def _log(self, level, message):
        """Log a message."""
        ioc_common.logit(
            {'level': level, 'message': message},
            _callback=self.callback,
            silent=self.silent
        )
